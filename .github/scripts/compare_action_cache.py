#!/usr/bin/env python3
"""Compare every action the runner downloaded for this job with a fresh GitHub tarball of the same ref.

Subcommands:
  compare  Walk _actions the way curate-gh-actions' DiscoverActionCache does, download each root's
           ref from https://api.github.com/repos/<owner>/<repo>/tarball/<ref>, extract it the way the
           runner does (tar -xzf, then unwrap the single top-level directory) and compare the served
           tree with the runner's copy. Results go to $RUNNER_TEMP/cmp/results.json.
  swap     For every root the last compare reported DIFFERENT, replace the runner's copy with the
           served tree, the way ReplaceActionContent does (a symlinked root is replaced by a directory).

The comparison runs in one direction only - archive to runner. Every regular file and symlink in the
served tree must exist in the runner's copy with the same content; files only the runner has are
listed as extras but do not make a root DIFFERENT, since an action that already ran (a pre step, say)
may have written into its own directory. Exec-bit differences are listed but do not decide the verdict.
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.parse

WATERMARK = ".completed"
WORK = os.path.join(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()), "cmp")
RESULTS = os.path.join(WORK, "results.json")


def actions_root():
    return os.path.normpath(os.path.join(os.environ["RUNNER_WORKSPACE"], "..", "_actions"))


def discover(root):
    """Return (owner, repo, ref, path) for every action root under root."""
    found = []
    for owner in sorted(os.listdir(root)):
        owner_path = os.path.join(root, owner)
        if not os.path.isdir(owner_path):
            continue
        for repo in sorted(os.listdir(owner_path)):
            repo_path = os.path.join(owner_path, repo)
            if os.path.isdir(repo_path):
                walk_refs(repo_path, "", owner, repo, found)
    return found


def walk_refs(directory, ref, owner, repo, found):
    entries = sorted(os.listdir(directory))
    marked = {e[: -len(WATERMARK)] for e in entries
              if e.endswith(WATERMARK) and os.path.isfile(os.path.join(directory, e))}
    for name in entries:
        path = os.path.join(directory, name)
        if name.endswith(WATERMARK) and os.path.isfile(path):
            continue
        child_ref = f"{ref}/{name}" if ref else name
        if name in marked or os.path.islink(path):
            found.append((owner, repo, child_ref, path))
        elif os.path.isdir(path) and all(
            os.path.isdir(os.path.join(path, e)) or e.endswith(WATERMARK) for e in os.listdir(path)
        ):
            walk_refs(path, child_ref, owner, repo, found)


def curl(url, out=None, api=False):
    cmd = ["curl", "-sSfL", "--retry", "2"]
    token = os.environ.get("GH_TOKEN")
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if api:
        cmd += ["-H", "Accept: application/vnd.github+json"]
    cmd += ["-o", out] if out else []
    cmd.append(url)
    return subprocess.run(cmd, check=True, capture_output=True, text=out is None).stdout


def quoted_ref(ref):
    return "/".join(urllib.parse.quote(p, safe="") for p in ref.split("/"))


def download_served(owner, repo, ref, dest):
    """Download and extract the tarball of ref into dest; return the unwrapped content dir and its name."""
    os.makedirs(dest)
    archive = os.path.join(dest, "archive.tar.gz")
    curl(f"https://api.github.com/repos/{owner}/{repo}/tarball/{quoted_ref(ref)}", out=archive)
    staging = os.path.join(dest, "_staging")
    os.makedirs(staging)
    subprocess.run(["tar", "-xzf", archive], cwd=staging, check=True)
    top = [e for e in os.listdir(staging) if os.path.isdir(os.path.join(staging, e))]
    if len(top) != 1:
        raise RuntimeError(f"archive holds {len(top)} top-level directories, want 1")
    return os.path.join(staging, top[0]), top[0]


def current_commit(owner, repo, ref):
    try:
        return json.loads(curl(f"https://api.github.com/repos/{owner}/{repo}/commits/{quoted_ref(ref)}", api=True))["sha"]
    except Exception as err:  # informational only
        return f"<unresolved: {err}>"


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def tree_entries(root):
    """Map every regular file and symlink under root (not following links) to its relative path."""
    entries = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            full = os.path.join(dirpath, name)
            entries[os.path.relpath(full, root)] = full
    return entries


def content_key(path):
    """What 'same content' means for one entry: a link's target text, or a file's bytes."""
    if os.path.islink(path):
        target = os.readlink(path)
        resolved = os.path.realpath(path)
        return ("link", target, sha256(resolved) if os.path.isfile(resolved) else None)
    return ("file", None, sha256(path))


def same_content(served, runner):
    s, r = content_key(served), content_key(runner)
    if s[0] == r[0] == "link":
        return s[1] == r[1]
    # A link on one side and a file on the other: compare the bytes the link resolves to.
    return s[2] is not None and s[2] == r[2]


def compare_trees(served_root, runner_root):
    served, runner = tree_entries(served_root), tree_entries(runner_root)
    missing, differ, kind_changed, mode_diff = [], [], [], []
    for rel, served_path in sorted(served.items()):
        runner_path = runner.get(rel)
        if runner_path is None:
            missing.append(rel)
            continue
        if not same_content(served_path, runner_path):
            differ.append(rel)
            continue
        if os.path.islink(served_path) != os.path.islink(runner_path):
            kind_changed.append(rel)
        elif not os.path.islink(served_path):
            if (os.stat(served_path).st_mode & 0o111) != (os.stat(runner_path).st_mode & 0o111):
                mode_diff.append(rel)
    extra = sorted(set(runner) - set(served))
    return {
        "served_files": len(served),
        "runner_files": len(runner),
        "missing_on_runner": missing,
        "content_differs": differ,
        "link_vs_file_same_bytes": kind_changed,
        "exec_bit_differs": mode_diff,
        "extra_on_runner": extra,
        "verdict": "DIFFERENT" if missing or differ else "IDENTICAL",
    }


def cmd_compare(args):
    root = actions_root()
    served_base = os.path.join(WORK, args.label, "served")
    shutil.rmtree(os.path.join(WORK, args.label), ignore_errors=True)
    print(f"actions root: {root}")
    print(f"ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE={os.environ.get('ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE', '<unset>')}")
    results = []
    for i, (owner, repo, ref, path) in enumerate(discover(root)):
        entry = {"action": f"{owner}/{repo}@{ref}", "owner": owner, "repo": repo, "ref": ref, "runner_path": path,
                 "runner_is_symlink": os.path.islink(path),
                 "runner_symlink_target": os.readlink(path) if os.path.islink(path) else None,
                 "ref_commit_now": current_commit(owner, repo, ref)}
        try:
            served_root, top = download_served(owner, repo, ref, os.path.join(served_base, str(i)))
            entry["served_root"] = served_root
            entry["served_top_dir"] = top
            entry.update(compare_trees(served_root, os.path.realpath(path)))
        except Exception as err:
            entry["verdict"] = "ERROR"
            entry["error"] = str(err)
        results.append(entry)
        report(entry)
    os.makedirs(WORK, exist_ok=True)
    with open(RESULTS, "w") as f:
        json.dump(results, f, indent=2)
    with open(os.path.join(WORK, f"results-{args.label}.json"), "w") as f:
        json.dump(results, f, indent=2)
    summarize(args.label, results)


def report(e):
    print(f"\n::group::{e['verdict']:<9} {e['action']}")
    for key in ("runner_path", "runner_is_symlink", "runner_symlink_target", "served_top_dir", "ref_commit_now",
                "served_files", "runner_files", "error"):
        if e.get(key) not in (None, False):
            print(f"  {key}: {e[key]}")
    for key in ("missing_on_runner", "content_differs", "link_vs_file_same_bytes", "exec_bit_differs", "extra_on_runner"):
        if e.get(key):
            print(f"  {key} ({len(e[key])}):")
            for rel in e[key][:25]:
                print(f"    {rel}")
            if len(e[key]) > 25:
                print(f"    ... {len(e[key]) - 25} more")
    print("::endgroup::")
    print(f"CMP-VERDICT {e['verdict']} {e['action']}")


def summarize(label, results):
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a") as f:
        f.write(f"### Content comparison: {label}\n\n")
        f.write("| Action | Verdict | Served top dir | Runner symlink | Differs | Missing | Extra on runner | Link vs file | Exec bit |\n")
        f.write("|---|---|---|---|---|---|---|---|---|\n")
        for e in results:
            f.write(f"| `{e['action']}` | **{e['verdict']}** | `{e.get('served_top_dir', '')}` | "
                    f"{'yes' if e['runner_is_symlink'] else 'no'} | "
                    f"{', '.join(e.get('content_differs', [])[:5])} | {', '.join(e.get('missing_on_runner', [])[:5])} | "
                    f"{', '.join(e.get('extra_on_runner', [])[:5])} | {', '.join(e.get('link_vs_file_same_bytes', [])[:5])} | "
                    f"{', '.join(e.get('exec_bit_differs', [])[:5])} |\n")
        f.write("\n")


def cmd_swap(_args):
    with open(RESULTS) as f:
        results = json.load(f)
    for e in results:
        if e["verdict"] != "DIFFERENT":
            continue
        target = e["runner_path"]
        previous = target + ".cmp-previous"
        # Like ReplaceActionContent: rename aside, move the served tree in; a symlink itself is replaced.
        os.rename(target, previous)
        shutil.move(e["served_root"], target)
        print(f"SWAPPED {e['action']}: {target} now holds {e['served_top_dir']} (previous content at {previous})")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    compare = sub.add_parser("compare")
    compare.add_argument("--label", default="compare")
    compare.set_defaults(func=cmd_compare)
    sub.add_parser("swap").set_defaults(func=cmd_swap)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    sys.exit(main())
