// cmpbench times, for every action the runner downloaded for this job, how long it takes to
// download the same ref from GitHub's tarball endpoint and to decide whether the runner's copy
// holds the same content, by four strategies:
//
//	extract+sha256    extract the archive to a temp dir, then sha256 both sides of every file
//	extract+bytes     extract, then size check + chunked byte compare, one file at a time
//	extract+bytes-par extract, then the same compare across NumCPU workers
//	stream            read the .tar.gz entry by entry and compare each against the runner's file,
//	                  never writing the archive's content to disk
//
// Every strategy starts from the downloaded .tar.gz on disk and runs end to end, extraction
// included. With -cold, the page cache is dropped before each strategy (needs passwordless sudo).
package main

import (
	"archive/tar"
	"bufio"
	"bytes"
	"compress/gzip"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"sync"
	"time"
)

const watermark = ".completed"

type root struct{ owner, repo, ref, path string }

type timing struct {
	Action     string             `json:"action"`
	Files      int                `json:"files"`
	Bytes      int64              `json:"bytes"`
	ArchiveMB  float64            `json:"archive_mb"`
	Downloads  []time.Duration    `json:"downloads"`
	TarXzf     time.Duration      `json:"tar_xzf"`
	Strategies map[string]outcome `json:"strategies"`
	PaxComment string             `json:"pax_comment"`
}

type outcome struct {
	Took      time.Duration `json:"took"`
	Identical bool          `json:"identical"`
	Err       string        `json:"err,omitempty"`
}

var strategyOrder = []string{"extract+sha256", "extract+bytes", "extract+bytes-par", "stream"}

func main() {
	cold := flag.Bool("cold", false, "drop the page cache before every strategy")
	out := flag.String("out", "", "write results as JSON here")
	flag.Parse()

	actionsRoot := filepath.Join(os.Getenv("RUNNER_WORKSPACE"), "..", "_actions")
	roots, err := discover(actionsRoot)
	must(err)
	work, err := os.MkdirTemp("", "cmpbench-")
	must(err)
	defer os.RemoveAll(work)

	fmt.Printf("os=%s/%s runner=%s (%s) cpus=%d cold=%v roots=%d\n", runtime.GOOS, runtime.GOARCH,
		os.Getenv("RUNNER_NAME"), os.Getenv("RUNNER_ENVIRONMENT"), runtime.NumCPU(), *cold, len(roots))
	var results []timing
	for i, r := range roots {
		t := timing{Action: fmt.Sprintf("%s/%s@%s", r.owner, r.repo, r.ref), Strategies: map[string]outcome{}}
		archive := filepath.Join(work, fmt.Sprintf("%d.tar.gz", i))
		for range 2 {
			d, err := download(r, archive)
			must(err)
			t.Downloads = append(t.Downloads, d)
		}
		st, _ := os.Stat(archive)
		t.ArchiveMB = float64(st.Size()) / (1 << 20)
		t.Files, t.Bytes, t.PaxComment, err = archiveStats(archive)
		must(err)
		t.TarXzf = timeTarXzf(archive, filepath.Join(work, "tar"))

		runnerDir, err := filepath.EvalSymlinks(r.path)
		must(err)
		for _, name := range strategyOrder {
			dest := filepath.Join(work, "x-"+name)
			if *cold {
				dropCaches()
			}
			start := time.Now()
			identical, err := runStrategy(name, archive, dest, runnerDir)
			o := outcome{Took: time.Since(start), Identical: identical}
			if err != nil {
				o.Err = err.Error()
			}
			t.Strategies[name] = o
			must(os.RemoveAll(dest))
		}
		results = append(results, t)
		printRow(t)
	}
	if *out != "" {
		data, _ := json.MarshalIndent(results, "", "  ")
		must(os.WriteFile(*out, data, 0o644))
	}
	writeSummary(results, *cold)
}

func runStrategy(name, archive, dest, runnerDir string) (bool, error) {
	switch name {
	case "stream":
		return streamCompare(archive, runnerDir)
	}
	if err := extract(archive, dest); err != nil {
		return false, err
	}
	rels, err := listFiles(dest)
	if err != nil {
		return false, err
	}
	switch name {
	case "extract+sha256":
		return compareAll(dest, runnerDir, rels, 1, sameBySHA)
	case "extract+bytes":
		return compareAll(dest, runnerDir, rels, 1, sameByBytes)
	case "extract+bytes-par":
		return compareAll(dest, runnerDir, rels, runtime.NumCPU(), sameByBytes)
	}
	return false, fmt.Errorf("unknown strategy %q", name)
}

// discover mirrors curate-gh-actions' DiscoverActionCache: a root is a directory with a sibling
// <name>.completed marker, or a symlink; a ref's intermediate directories hold only directories.
func discover(actionsRoot string) ([]root, error) {
	var roots []root
	owners, err := os.ReadDir(actionsRoot)
	if err != nil {
		return nil, err
	}
	for _, o := range owners {
		if !o.IsDir() {
			continue
		}
		repos, err := os.ReadDir(filepath.Join(actionsRoot, o.Name()))
		if err != nil {
			return nil, err
		}
		for _, r := range repos {
			if r.IsDir() {
				walkRefs(filepath.Join(actionsRoot, o.Name(), r.Name()), "", o.Name(), r.Name(), &roots)
			}
		}
	}
	return roots, nil
}

func walkRefs(dir, ref, owner, repo string, roots *[]root) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	marked := map[string]bool{}
	for _, e := range entries {
		if !e.IsDir() && strings.HasSuffix(e.Name(), watermark) {
			marked[strings.TrimSuffix(e.Name(), watermark)] = true
		}
	}
	for _, e := range entries {
		if !e.IsDir() && e.Type()&fs.ModeSymlink == 0 {
			continue
		}
		child := e.Name()
		if ref != "" {
			child = ref + "/" + e.Name()
		}
		p := filepath.Join(dir, e.Name())
		if marked[e.Name()] || e.Type()&fs.ModeSymlink != 0 {
			*roots = append(*roots, root{owner, repo, child, p})
		} else {
			walkRefs(p, child, owner, repo, roots)
		}
	}
}

func download(r root, dest string) (time.Duration, error) {
	parts := strings.Split(r.ref, "/")
	for i, p := range parts {
		parts[i] = url.PathEscape(p)
	}
	u := fmt.Sprintf("https://api.github.com/repos/%s/%s/tarball/%s", r.owner, r.repo, strings.Join(parts, "/"))
	req, err := http.NewRequest(http.MethodGet, u, nil)
	if err != nil {
		return 0, err
	}
	if tok := os.Getenv("GH_TOKEN"); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	start := time.Now()
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return 0, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return 0, fmt.Errorf("GET %s: %s", u, resp.Status)
	}
	f, err := os.Create(dest)
	if err != nil {
		return 0, err
	}
	if _, err := io.Copy(f, resp.Body); err != nil {
		f.Close()
		return 0, err
	}
	return time.Since(start), f.Close()
}

func openTar(archive string) (*tar.Reader, func() error, error) {
	f, err := os.Open(archive)
	if err != nil {
		return nil, nil, err
	}
	gz, err := gzip.NewReader(bufio.NewReaderSize(f, 1<<20))
	if err != nil {
		f.Close()
		return nil, nil, err
	}
	return tar.NewReader(gz), f.Close, nil
}

// stripTop drops the archive's single top-level directory; "" means the entry is that directory
// or not under it (the pax global header).
func stripTop(name string) string {
	_, rest, ok := strings.Cut(strings.TrimPrefix(name, "./"), "/")
	if !ok {
		return ""
	}
	return strings.TrimSuffix(rest, "/")
}

func archiveStats(archive string) (files int, size int64, pax string, err error) {
	tr, closeFn, err := openTar(archive)
	if err != nil {
		return 0, 0, "", err
	}
	defer closeFn()
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return files, size, pax, nil
		}
		if err != nil {
			return 0, 0, "", err
		}
		if h.Typeflag == tar.TypeXGlobalHeader {
			pax = h.PAXRecords["comment"]
		}
		if h.Typeflag == tar.TypeReg || h.Typeflag == tar.TypeSymlink {
			files++
			size += h.Size
		}
	}
}

func extract(archive, dest string) error {
	tr, closeFn, err := openTar(archive)
	if err != nil {
		return err
	}
	defer closeFn()
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return nil
		}
		if err != nil {
			return err
		}
		rel := stripTop(h.Name)
		if rel == "" {
			continue
		}
		target := filepath.Join(dest, rel)
		switch h.Typeflag {
		case tar.TypeDir:
			if err := os.MkdirAll(target, 0o755); err != nil {
				return err
			}
		case tar.TypeReg:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			f, err := os.OpenFile(target, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, fs.FileMode(h.Mode)&0o777)
			if err != nil {
				return err
			}
			if _, err := io.Copy(f, tr); err != nil {
				f.Close()
				return err
			}
			if err := f.Close(); err != nil {
				return err
			}
		case tar.TypeSymlink:
			if err := os.MkdirAll(filepath.Dir(target), 0o755); err != nil {
				return err
			}
			if err := os.Symlink(h.Linkname, target); err != nil {
				// Windows without the symlink privilege: keep the link path as content, as a zip would.
				if err := os.WriteFile(target, []byte(h.Linkname), 0o644); err != nil {
					return err
				}
			}
		}
	}
}

func timeTarXzf(archive, dest string) time.Duration {
	must(os.MkdirAll(dest, 0o755))
	defer os.RemoveAll(dest)
	start := time.Now()
	must(exec.Command("tar", "-xzf", archive, "-C", dest).Run())
	return time.Since(start)
}

func listFiles(root string) ([]string, error) {
	var rels []string
	err := filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.Type().IsRegular() || d.Type()&fs.ModeSymlink != 0 {
			rel, _ := filepath.Rel(root, p)
			rels = append(rels, rel)
		}
		return nil
	})
	return rels, err
}

// compareAll checks every served file against the runner's copy; os.Stat/Open follow symlinks on
// both sides, since the runner materializes an archive's symlinks as regular files.
func compareAll(served, runner string, rels []string, workers int, same func(a, b string) (bool, error)) (bool, error) {
	jobs := make(chan string)
	var mu sync.Mutex
	identical := true
	var firstErr error
	var wg sync.WaitGroup
	for range workers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for rel := range jobs {
				ok, err := same(filepath.Join(served, rel), filepath.Join(runner, rel))
				mu.Lock()
				if err != nil && firstErr == nil {
					firstErr = err
				}
				identical = identical && ok
				mu.Unlock()
			}
		}()
	}
	for _, rel := range rels {
		jobs <- rel
	}
	close(jobs)
	wg.Wait()
	return identical, firstErr
}

func hashFile(p string) ([]byte, error) {
	f, err := os.Open(p)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return nil, err
	}
	return h.Sum(nil), nil
}

func sameBySHA(a, b string) (bool, error) {
	ha, err := hashFile(a)
	if err != nil {
		return false, err
	}
	hb, err := hashFile(b)
	if err != nil {
		return false, nil
	}
	return bytes.Equal(ha, hb), nil
}

func sameByBytes(a, b string) (bool, error) {
	sa, err := os.Stat(a)
	if err != nil {
		return false, err
	}
	fb, err := os.Open(b)
	if err != nil {
		return false, nil
	}
	defer fb.Close()
	sb, err := fb.Stat()
	if err != nil || sa.Size() != sb.Size() {
		return false, err
	}
	fa, err := os.Open(a)
	if err != nil {
		return false, err
	}
	defer fa.Close()
	return sameStream(fa, fb)
}

var bufPool = sync.Pool{New: func() any { b := make([]byte, 2*64<<10); return &b }}

func sameStream(a, b io.Reader) (bool, error) {
	bp := bufPool.Get().(*[]byte)
	defer bufPool.Put(bp)
	bufA, bufB := (*bp)[:64<<10], (*bp)[64<<10:]
	for {
		na, errA := io.ReadFull(a, bufA)
		nb, errB := io.ReadFull(b, bufB)
		if na != nb || !bytes.Equal(bufA[:na], bufB[:nb]) {
			return false, nil
		}
		doneA := errors.Is(errA, io.EOF) || errors.Is(errA, io.ErrUnexpectedEOF)
		doneB := errors.Is(errB, io.EOF) || errors.Is(errB, io.ErrUnexpectedEOF)
		if doneA || doneB {
			return doneA && doneB, nil
		}
		if errA != nil {
			return false, errA
		}
		if errB != nil {
			return false, errB
		}
	}
}

// streamCompare decides from the archive stream alone, without extracting. A symlink entry is
// checked by what the runner has at that path: a link with the same target, or a regular file
// holding the same bytes as the runner's copy of the link's target.
func streamCompare(archive, runner string) (bool, error) {
	tr, closeFn, err := openTar(archive)
	if err != nil {
		return false, err
	}
	defer closeFn()
	identical := true
	for {
		h, err := tr.Next()
		if errors.Is(err, io.EOF) {
			return identical, nil
		}
		if err != nil {
			return false, err
		}
		rel := stripTop(h.Name)
		if rel == "" {
			continue
		}
		p := filepath.Join(runner, rel)
		switch h.Typeflag {
		case tar.TypeReg:
			f, err := os.Open(p)
			if err != nil {
				identical = false
				continue
			}
			st, err := f.Stat()
			if err != nil || st.Size() != h.Size {
				f.Close()
				identical = false
				continue
			}
			ok, err := sameStream(tr, f)
			f.Close()
			if err != nil {
				return false, err
			}
			identical = identical && ok
		case tar.TypeSymlink:
			if st, err := os.Lstat(p); err == nil && st.Mode()&fs.ModeSymlink != 0 {
				target, _ := os.Readlink(p)
				identical = identical && target == h.Linkname
				continue
			}
			ok, err := sameByBytes(filepath.Join(filepath.Dir(p), h.Linkname), p)
			if err != nil || !ok {
				// A zip extracted by .NET on Windows leaves the link path as the file's content.
				content, readErr := os.ReadFile(p)
				ok = readErr == nil && string(content) == h.Linkname
			}
			identical = identical && ok
		}
	}
}

func dropCaches() {
	switch runtime.GOOS {
	case "linux":
		must(exec.Command("sudo", "sh", "-c", "sync; echo 3 > /proc/sys/vm/drop_caches").Run())
	case "darwin":
		must(exec.Command("sudo", "-n", "purge").Run())
	default:
		must(fmt.Errorf("-cold is not supported on %s", runtime.GOOS))
	}
}

func ms(d time.Duration) string {
	return fmt.Sprintf("%.1f", float64(d.Microseconds())/1000)
}

func printRow(t timing) {
	var parts []string
	for _, name := range strategyOrder {
		o := t.Strategies[name]
		parts = append(parts, fmt.Sprintf("%s=%sms(%v)", name, ms(o.Took), o.Identical))
	}
	fmt.Printf("%-50s files=%6d archive=%6.1fMB dl=%sms/%sms tar-xzf=%sms %s pax=%q\n",
		t.Action, t.Files, t.ArchiveMB, ms(t.Downloads[0]), ms(t.Downloads[1]), ms(t.TarXzf), strings.Join(parts, " "), t.PaxComment)
}

func writeSummary(results []timing, cold bool) {
	p := os.Getenv("GITHUB_STEP_SUMMARY")
	if p == "" {
		return
	}
	sort.Slice(results, func(i, j int) bool { return results[i].Files < results[j].Files })
	f, err := os.OpenFile(p, os.O_APPEND|os.O_WRONLY, 0o644)
	must(err)
	defer f.Close()
	fmt.Fprintf(f, "### cmpbench %s/%s %s (cold=%v, cpus=%d), times in ms\n\n", runtime.GOOS, runtime.GOARCH,
		os.Getenv("RUNNER_ENVIRONMENT"), cold, runtime.NumCPU())
	fmt.Fprintf(f, "| Action | Files | Archive MB | Download 1 | Download 2 | tar -xzf | %s | pax comment |\n", strings.Join(strategyOrder, " | "))
	fmt.Fprintf(f, "|---|---|---|---|---|---|%s---|\n", strings.Repeat("---|", len(strategyOrder)))
	for _, t := range results {
		var cells []string
		for _, name := range strategyOrder {
			o := t.Strategies[name]
			cell := ms(o.Took)
			if !o.Identical {
				cell += " (DIFF)"
			}
			cells = append(cells, cell)
		}
		fmt.Fprintf(f, "| `%s` | %d | %.1f | %s | %s | %s | %s | `%s` |\n", t.Action, t.Files, t.ArchiveMB,
			ms(t.Downloads[0]), ms(t.Downloads[1]), ms(t.TarXzf), strings.Join(cells, " | "), t.PaxComment)
	}
	fmt.Fprintln(f)
}

func must(err error) {
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
