#!/usr/bin/env python3
"""Compare every action the runner downloaded for this job with a fresh GitHub tarball of the same ref.

Subcommands:
  compare  Walk _actions the way curate-gh-actions' DiscoverActionCache does, download each root's
           ref from https://api.github.com/repos/<owner>/<repo>/tarball/<ref>, and compare the
           archive entry by entry against the runner's copy, without extracting it. Results go to
           $RUNNER_TEMP/cmp/results.json.
  swap     For every root the last compare reported DIFFERENT, extract the archive and replace the
           runner's copy with it, the way ReplaceActionContent does (a symlinked root is replaced by
           a directory).

The comparison runs in one direction only - archive to runner. Every regular file and symlink in the
archive must exist in the runner's copy with the same content; files only the runner has are listed
as extras but do not make a root DIFFERENT, since an action that already ran (a pre step, say) may
have written into its own directory.

A symlink in the archive is matched by whatever form the runner left at that path: a symlink with
the same target, a regular file holding the target's bytes (the runner's tar-then-copy on Linux and
macOS), or a regular file holding the link path as text (a zip extracted by .NET on Windows). Which
form matched is reported, since that is part of what the probe measures.
"""
import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse

WATERMARK = ".completed"
WORK = os.path.join(os.environ.get("RUNNER_TEMP", tempfile.gettempdir()), "cmp")
RESULTS = os.path.join(WORK, "results.json")
CHUNK = 64 << 10


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
    if os.name == "nt":
        # Windows' curl.exe (schannel) fails outright when the revocation server is unreachable,
        # which hosted Windows runners hit (CRYPT_E_REVOCATION_OFFLINE in run 36568845889).
        cmd.append("--ssl-revoke-best-effort")
    token = os.environ.get("GH_TOKEN")
    if token:
        cmd += ["-H", f"Authorization: Bearer {token}"]
    if api:
        cmd += ["-H", "Accept: application/vnd.github+json"]
    cmd += ["-o", out] if out else []
    cmd.append(url)
    proc = subprocess.run(cmd, capture_output=True, text=out is None)
    if proc.returncode != 0:
        # Never echo cmd: it carries the token.
        stderr = proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        raise RuntimeError(f"GET {url} failed (curl exit {proc.returncode}): {stderr.strip()}")
    return proc.stdout


def quoted_ref(ref):
    return "/".join(urllib.parse.quote(p, safe="") for p in ref.split("/"))


def current_commit(owner, repo, ref):
    try:
        return json.loads(curl(f"https://api.github.com/repos/{owner}/{repo}/commits/{quoted_ref(ref)}", api=True))["sha"]
    except Exception as err:  # informational only
        return f"<unresolved: {err}>"


def strip_top(name):
    """Drop the archive's single top-level directory; "" for that directory itself or the pax header."""
    parts = name.removeprefix("./").split("/", 1)
    return parts[1].rstrip("/") if len(parts) == 2 else ""


def same_stream(a, b):
    while True:
        ca, cb = a.read(CHUNK), b.read(CHUNK)
        if ca != cb:
            return False
        if not ca:
            return True


def same_file_bytes(path, expected):
    """Whether the regular file at path holds exactly the bytes readable from expected."""
    with open(path, "rb") as f:
        return same_stream(expected, f)


def compare_archive(archive, runner_root):
    """Compare archive -> runner without extracting; return the per-root findings."""
    archive_paths = set()
    missing, differ, mode_diff = [], [], []
    link_forms = {}
    pax_comment = ""
    top_dir = ""
    with tarfile.open(archive, "r:gz") as tf:
        pax_comment = tf.pax_headers.get("comment", "")
        for member in tf:
            if not top_dir and member.isdir():
                top_dir = member.name.split("/", 1)[0]
            rel = strip_top(member.name)
            if not rel or member.isdir():
                continue
            if not (member.isfile() or member.issym()):
                continue
            archive_paths.add(rel)
            path = os.path.join(runner_root, *rel.split("/"))
            if not os.path.lexists(path):
                missing.append(rel)
                continue
            if member.isfile():
                if os.path.islink(path) or os.path.getsize(path) != member.size:
                    differ.append(rel)
                    continue
                with tf.extractfile(member) as data:
                    if not same_file_bytes(path, data):
                        differ.append(rel)
                        continue
                if os.name == "posix" and bool(member.mode & 0o111) != bool(os.stat(path).st_mode & 0o111):
                    mode_diff.append(rel)
                continue
            # A symlink entry: accept any form the runner is known to leave behind.
            if os.path.islink(path):
                form = "symlink" if os.readlink(path) == member.linkname else None
            else:
                target = os.path.normpath(os.path.join(os.path.dirname(path), member.linkname))
                form = None
                if os.path.isfile(target):
                    with open(target, "rb") as t:
                        if same_file_bytes(path, t):
                            form = "file-with-target-bytes"
                if form is None:
                    with open(path, "rb") as f:
                        if f.read() == member.linkname.encode():
                            form = "file-with-link-text"
            if form is None:
                differ.append(rel)
            else:
                link_forms[rel] = form

    runner_paths = set()
    for dirpath, dirnames, filenames in os.walk(runner_root):
        for name in filenames + [d for d in dirnames if os.path.islink(os.path.join(dirpath, d))]:
            runner_paths.add(os.path.relpath(os.path.join(dirpath, name), runner_root).replace(os.sep, "/"))
    return {
        "served_top_dir": top_dir,
        "pax_comment": pax_comment,
        "served_files": len(archive_paths),
        "runner_files": len(runner_paths),
        "missing_on_runner": sorted(missing),
        "content_differs": sorted(differ),
        "symlink_forms": link_forms,
        "exec_bit_differs": sorted(mode_diff),
        "extra_on_runner": sorted(runner_paths - archive_paths),
        "verdict": "DIFFERENT" if missing or differ else "IDENTICAL",
    }


def cmd_compare(args):
    root = actions_root()
    base = os.path.join(WORK, args.label)
    shutil.rmtree(base, ignore_errors=True)
    os.makedirs(base)
    print(f"platform: {platform.system()} {platform.machine()}  runner: {os.environ.get('RUNNER_NAME')} "
          f"({os.environ.get('RUNNER_ENVIRONMENT', '?')})")
    print(f"actions root: {root}")
    for var in ("ACTIONS_RUNNER_ACTION_ARCHIVE_CACHE", "ACTIONS_RUNNER_SYMLINK_CACHED_ACTIONS"):
        print(f"{var}={os.environ.get(var, '<unset>')}")
    results = []
    for i, (owner, repo, ref, path) in enumerate(discover(root)):
        entry = {"action": f"{owner}/{repo}@{ref}", "owner": owner, "repo": repo, "ref": ref, "runner_path": path,
                 "runner_is_symlink": os.path.islink(path),
                 "runner_symlink_target": os.readlink(path) if os.path.islink(path) else None,
                 "ref_commit_now": current_commit(owner, repo, ref)}
        archive = os.path.join(base, f"{i}.tar.gz")
        try:
            curl(f"https://api.github.com/repos/{owner}/{repo}/tarball/{quoted_ref(ref)}", out=archive)
            entry["archive"] = archive
            entry.update(compare_archive(archive, os.path.realpath(path)))
        except Exception as err:
            entry["verdict"] = "ERROR"
            entry["error"] = repr(err)
        results.append(entry)
        report(entry)
    with open(RESULTS, "w") as f:
        json.dump(results, f, indent=2)
    with open(os.path.join(WORK, f"results-{args.label}.json"), "w") as f:
        json.dump(results, f, indent=2)
    summarize(args.label, results)


def report(e):
    print(f"\n::group::{e['verdict']:<9} {e['action']}")
    for key in ("runner_path", "runner_is_symlink", "runner_symlink_target", "served_top_dir", "pax_comment",
                "ref_commit_now", "served_files", "runner_files", "error"):
        if e.get(key) not in (None, False, ""):
            print(f"  {key}: {e[key]}")
    if e.get("symlink_forms"):
        print(f"  symlink_forms ({len(e['symlink_forms'])}):")
        for rel, form in e["symlink_forms"].items():
            print(f"    {rel}: {form}")
    for key in ("missing_on_runner", "content_differs", "exec_bit_differs", "extra_on_runner"):
        if e.get(key):
            print(f"  {key} ({len(e[key])}):")
            for rel in e[key][:25]:
                print(f"    {rel}")
            if len(e[key]) > 25:
                print(f"    ... {len(e[key]) - 25} more")
    print("::endgroup::")
    print(f"CMP-VERDICT {e['verdict']} {e['action']} symlink={'yes' if e['runner_is_symlink'] else 'no'} "
          f"links={','.join(sorted(set(e.get('symlink_forms', {}).values()))) or '-'} "
          f"extras={len(e.get('extra_on_runner', []))}")


def summarize(label, results):
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    with open(summary, "a", encoding="utf-8") as f:
        f.write(f"### Content comparison: {label} ({platform.system()}, {os.environ.get('RUNNER_ENVIRONMENT', '?')})\n\n")
        f.write("| Action | Verdict | Served top dir | Runner symlink | Differs | Missing | Extra on runner | Symlink forms |\n")
        f.write("|---|---|---|---|---|---|---|---|\n")
        for e in results:
            f.write(f"| `{e['action']}` | **{e['verdict']}** | `{e.get('served_top_dir', '')}` | "
                    f"{'yes' if e['runner_is_symlink'] else 'no'} | "
                    f"{', '.join(e.get('content_differs', [])[:5])} | {', '.join(e.get('missing_on_runner', [])[:5])} | "
                    f"{', '.join(e.get('extra_on_runner', [])[:5])} | "
                    f"{', '.join(sorted(set(e.get('symlink_forms', {}).values())))} |\n")
        f.write("\n")


def extract_stripped(archive, dest):
    """Extract archive into dest without its top-level directory; a symlink that cannot be created
    (Windows without the privilege) is written as a copy of its target, which tarfile does itself."""
    with tarfile.open(archive, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            rel = strip_top(m.name)
            if not rel:
                continue
            m.name = rel
            members.append(m)
        kwargs = {"filter": "fully_trusted"} if hasattr(tarfile, "fully_trusted_filter") else {}
        tf.extractall(dest, members=members, **kwargs)


def cmd_swap(_args):
    with open(RESULTS) as f:
        results = json.load(f)
    for e in results:
        if e["verdict"] != "DIFFERENT":
            continue
        target = e["runner_path"]
        served = os.path.join(os.path.dirname(e["archive"]), "served-" + os.path.basename(e["archive"]))
        extract_stripped(e["archive"], served)
        previous = target + ".cmp-previous"
        # Like ReplaceActionContent: rename aside, move the served tree in; a symlink itself is replaced.
        os.rename(target, previous)
        shutil.move(served, target)
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
