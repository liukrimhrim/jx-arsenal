#!/usr/bin/env python3
"""jx git — git idioms agents re-derive every session. TSV by default, --json for raw where it applies.

  jx git cat <path> [path ...] [--ref origin/main | --pr N]   file contents at a ref, after a quiet fetch; no checkout
  jx git ls <path> [--ref origin/main | --pr N] [-r]          names under a path at a ref
  jx git since [BASE] [--log | --files | --stat]              fetch, merge-base, then commits / files / diffstat vs BASE (origin/main)
  jx git commit -m MSG | -F FILE | -F - [--expect-branch B] [--strict] [--push] [--no-sign] [--trailer LINE ...] -- <path ...>
                                                              stage only the named paths and commit; refuses main/master, a switched
                                                              branch, no paths, unknown or unchanged paths; warns about foreign changes
  jx git wt <name> [--base origin/main] [--dir DIR] [--copy PATH ...]   worktree on a fresh branch under $TMPDIR; --copy moves edits over
  jx git wt ls | jx git wt rm <dir>                           list worktrees / remove one (never the main worktree)
  jx git export <ref> <dir>                                   `git archive` into DIR plus DIR/.git-sha with the full sha
  jx git touching <path ...> [-R owner/name]                  open PRs whose files overlap these paths (gh; exit 2 when unavailable)
  jx git doctor                                               toplevel, branch, upstream, ahead/behind, uncommitted, shallow, origin, main age

Refs: `origin/<branch>` triggers `git fetch -q origin <branch>` first; `--pr N` fetches `pull/N/head` into `pr/N`.
On a shallow clone where origin/main shares no history with HEAD, `since` falls back to the local `main` and says so.
`commit` runs its guards before any write and verifies afterwards that the committed files are exactly the requested ones
and (with --push) that origin's tip equals HEAD; every refusal is one line naming the tell and the fix.
"""
import argparse, json, os, re, shutil, signal, subprocess, sys, tempfile
signal.signal(signal.SIGPIPE, signal.SIG_DFL)

def git(*args, ok=(0,), stdin=None):
    p = subprocess.run(["git", *args], text=True, capture_output=True, input=stdin)
    if p.returncode not in ok:
        err = p.stderr.strip() or p.stdout.strip()
        hint = ""
        if re.search(r"does not exist in|exists on disk, but not in|path .* does not exist", err): hint = "→ `jx git ls <dir> --ref <ref>` lists what exists at that ref; paths are repo-relative"
        elif re.search(r"unknown revision|bad revision|Not a valid object|invalid reference", err): hint = "→ `git branch -r | head` lists remote refs; for a PR use --pr N"
        elif re.search(r"Could not read from remote|Permission denied|Authentication failed", err): hint = "→ remote unreachable or unauthenticated; `gh auth status`, or check the remote URL with `git remote -v`"
        sys.exit(err + (f"\n{hint}" if hint else ""))
    return p.stdout

def resolve(ref, pr):
    """Make `ref` usable: fetch the branch behind origin/<b>, or fetch a PR head into pr/N. Returns the ref to use."""
    if pr:
        git("fetch", "-q", "origin", f"pull/{pr}/head:pr/{pr}"); return f"pr/{pr}"
    m = re.fullmatch(r"origin/(.+)", ref)
    if m: git("fetch", "-q", "origin", m.group(1))
    return ref

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, a, raw=None):
    if a.json: print(json.dumps(raw if raw is not None else rows, indent=1))
    else: tsv(rows, header)

def cmd_cat(a):
    ref = resolve(a.ref, a.pr)
    for i, p in enumerate(a.path):
        if len(a.path) > 1: print(f"{'' if i == 0 else chr(10)}=== {ref}:{p}")
        sys.stdout.write(git("show", f"{ref}:{p.strip('/')}"))

def cmd_ls(a):
    ref = resolve(a.ref, a.pr)
    out = git("ls-tree", "--name-only", *(["-r"] if a.r else []), ref, a.path.rstrip("/") + ("/" if a.path.strip("/") else ""))
    print(out, end="")

def ctime(ref):
    """Committer time of `ref`'s tip, or None when the ref does not exist."""
    out = git("log", "-1", "--format=%ct", ref, "--", ok=(0, 128)).strip()
    return int(out) if out.isdigit() else None

def main_age():
    """How far local main's tip trails origin/main's, as `N days`/`N hours`; None when either ref is absent or main is not behind."""
    local, remote = ctime("main"), ctime("origin/main")
    if local is None or remote is None or remote <= local: return None
    s = remote - local
    return f"{s // 86400} days" if s >= 86400 else f"{max(1, s // 3600)} hours"

def merge_base(base):
    mb = git("merge-base", base, "HEAD", ok=(0, 1, 128)).strip()
    if not mb and base == "origin/main":  # a shallow clone can hold origin/main without the history that joins it to HEAD
        mb = git("merge-base", "main", "HEAD", ok=(0, 1, 128)).strip()
        if mb:
            sys.stderr.write("since: origin/main shares no history with HEAD here; using local main\n"); base = "main"
            if age := main_age(): sys.stderr.write(f"since: local main last moved {age} before origin/main; numbers below are against that stale base\n")
    if not mb: sys.exit(f"no merge-base between {base} and HEAD; pass BASE that shares history, or `git fetch --unshallow`")
    return base, mb

def cmd_since(a):
    base, mb = merge_base(resolve(a.base, None))
    if a.log: print(git("log", "--oneline", "--no-merges", f"{mb}..HEAD"), end=""); return
    if a.files: print(git("diff", "--name-status", mb), end=""); return
    if a.stat: print(git("diff", "--stat", mb), end=""); return
    ahead = git("rev-list", "--count", f"{mb}..HEAD").strip(); behind = git("rev-list", "--count", f"HEAD..{base}").strip()
    files = [l for l in git("diff", "--name-status", mb).splitlines() if l.strip()]
    dirty = [l for l in git("status", "--porcelain").splitlines() if l.strip()]
    rows = [{"branch": git("rev-parse", "--abbrev-ref", "HEAD").strip(), "base": base, "merge_base": mb[:10], "ahead": ahead, "behind": behind,
             "files_changed": len(files), "uncommitted": len(dirty)}]
    emit(rows, list(rows[0]), a)
    sys.stderr.write("→ --log for commits, --files for paths, --stat for the diffstat\n")

def toplevel(): return git("rev-parse", "--show-toplevel").strip()
def branch(): return git("rev-parse", "--abbrev-ref", "HEAD").strip()

def changed_paths():
    """{repo-relative path: XY status} from `git status`; a rename is keyed by its new name. -z keeps paths unquoted."""
    out, ents = {}, git("status", "--porcelain", "-z", "-uall").split("\0")
    i = 0
    while i < len(ents):
        e = ents[i]; i += 1
        if not e: continue
        out[e[3:]] = e[:2]
        if e[0] in "RC": i += 1  # the old name follows as its own entry
    return out

def under(path, files):
    """Files equal to `path` or beneath it when `path` is a directory."""
    return [f for f in files if f == path or f.startswith(path.rstrip("/") + "/")]

def rel_paths(paths, top):
    """User paths (relative to the shell cwd) → repo-relative, refusing anything outside or absent from both worktree and index."""
    out = []
    for p in paths:
        r = os.path.normpath(os.path.relpath(os.path.abspath(p), top))
        if r.startswith("..") or not (os.path.lexists(p) or git("ls-files", "--error-unmatch", "--", r, ok=(0, 1, 128)).strip()):
            sys.exit(f"{p}: no such path (shell cwd may have drifted; paths are relative to {top})")
        out.append(r)
    return out

def read_message(a):
    if a.m is not None: msg = a.m
    elif a.F == "-": msg = sys.stdin.read()
    else:
        with open(a.F) as f: msg = f.read()
    if not msg.strip(): sys.exit("empty commit message")
    return msg.rstrip("\n") + "\n" + ("\n" + "\n".join(a.trailer) + "\n" if a.trailer else "")

def cmd_commit(a):
    b = branch()
    if b in ("main", "master"): sys.exit(f"refusing to commit on {b}; make a branch first (`jx git wt <name>`)")
    if a.expect_branch and b != a.expect_branch: sys.exit(f"HEAD is on {b}, expected {a.expect_branch}: the checkout switched under you; your edits may be on the wrong branch (`git status`)")
    if not a.paths: sys.exit("name the paths to commit; this command never stages everything")
    top = toplevel(); paths = rel_paths(a.paths, top); changed = changed_paths()
    os.chdir(top)  # from here on every pathspec is repo-relative
    for p, r in zip(a.paths, paths):
        if not under(r, changed): sys.exit(f"{p}: nothing to commit")
    foreign = sorted(f for f in changed if not any(under(r, [f]) for r in paths))
    if foreign:
        sys.stderr.write(f"warning: {len(foreign)} other changed paths not in this commit (another agent?): {' '.join(foreign[:10])}{' ...' if len(foreign) > 10 else ''}\n")
        if a.strict: sys.exit("--strict: refusing to commit beside foreign changes; commit or move them first (`jx git wt <name> --copy <path>`)")
    msg = read_message(a)
    git("add", "--", *paths)
    git(*(["-c", "commit.gpgsign=false"] if a.no_sign else []), "commit", "-q", "-F", "-", stdin=msg)
    sha = git("rev-parse", "HEAD").strip()
    files = [f for f in git("show", "--name-only", "--format=", "HEAD").split("\n") if f]
    stray = [f for f in files if not any(under(r, [f]) for r in paths)]
    if stray or any(not under(r, files) for r in paths): sys.exit(f"committed files differ from requested ({sha[:10]}): {' '.join(stray) or 'a requested path is missing'}; inspect with `git show --stat HEAD`")
    emit([{"sha": sha, "files": len(files)}], ["sha", "files"], a)
    print(git("show", "--stat", "--format=", "HEAD"), end="")
    if a.push:
        git("push", "-q", "-u", "origin", b)
        remote = git("ls-remote", "origin", f"refs/heads/{b}").split()
        if not remote or remote[0] != sha: sys.exit(f"push did not land: origin/{b} is {remote[0][:10] if remote else 'absent'}, HEAD is {sha[:10]}")
        print(f"pushed {sha} to origin/{b}")

def worktrees():
    """[{dir, branch, head}] from `worktree list --porcelain`; the first row is the main worktree."""
    rows = []
    for block in git("worktree", "list", "--porcelain").strip().split("\n\n"):
        kv = dict((l.split(" ", 1) + [""])[:2] for l in block.splitlines() if l)
        if "worktree" in kv: rows.append({"dir": kv["worktree"], "branch": kv.get("branch", "").replace("refs/heads/", "") or "(detached)", "head": kv.get("HEAD", "")})
    return rows

def cmd_wt(a):
    if a.name == "ls": emit(worktrees(), ["dir", "branch", "head"], a); return
    if a.name == "rm":
        if not a.rm_dir: sys.exit("pass the worktree directory: `jx git wt rm <dir>` (`jx git wt ls` lists them)")
        wts = worktrees(); target = os.path.realpath(a.rm_dir)
        if target == os.path.realpath(wts[0]["dir"]): sys.exit(f"{a.rm_dir} is the main worktree; only worktrees added with `jx git wt <name>` can be removed")
        if target not in {os.path.realpath(w["dir"]) for w in wts}: sys.exit(f"{a.rm_dir} is not a worktree of this repo (`jx git wt ls`)")
        git("worktree", "remove", "--force", a.rm_dir); git("worktree", "prune"); print(f"removed {a.rm_dir}"); return
    top = toplevel(); copies = rel_paths(a.copy, top)
    base = resolve(a.base, None); sha = git("rev-parse", base).strip()
    d = os.path.normpath(os.path.abspath(a.dir or os.path.join(tempfile.gettempdir(), "jx-wt-" + a.name.replace("/", "-"))))
    git("worktree", "add", "-q", "-B", a.name, d, base)
    for r in copies:
        src, dst = os.path.join(top, r), os.path.join(d, r)
        if os.path.isdir(src): shutil.copytree(src, dst, dirs_exist_ok=True)
        else: os.makedirs(os.path.dirname(dst), exist_ok=True); shutil.copy2(src, dst)
    emit([{"dir": d, "branch": a.name, "base_sha": sha}], ["dir", "branch", "base_sha"], a)
    sys.stderr.write(f"→ `cd {d}` then `jx git commit -m MSG -- <path ...>`" + (f"; {len(copies)} paths copied in, the originals are untouched" if copies else "") + "\n")

def cmd_export(a):
    ref = resolve(a.ref, None); sha = git("rev-parse", f"{ref}^{{commit}}").strip()
    os.makedirs(a.dir, exist_ok=True)
    tar = subprocess.run(["git", "archive", ref], capture_output=True)
    if tar.returncode: sys.exit(tar.stderr.decode().strip())
    x = subprocess.run(["tar", "-x", "-C", a.dir], input=tar.stdout, capture_output=True)
    if x.returncode: sys.exit(x.stderr.decode().strip())
    with open(os.path.join(a.dir, ".git-sha"), "w") as f: f.write(sha + "\n")
    emit([{"dir": os.path.abspath(a.dir), "sha": sha}], ["dir", "sha"], a)

def cmd_touching(a):
    if not shutil.which("gh"): sys.stderr.write("gh not found; install the GitHub CLI and `gh auth login`\n"); sys.exit(2)
    top = toplevel(); paths = [os.path.normpath(os.path.relpath(os.path.abspath(p), top)) for p in a.path]
    p = subprocess.run(["gh", "pr", "list", "--state", "open", "--limit", "200", "--json", "number,title,headRefName,files", *(["-R", a.R] if a.R else [])], text=True, capture_output=True)
    if p.returncode: sys.stderr.write(p.stderr.strip() + "\n→ `gh auth status`; pass -R owner/name when the cwd has no GitHub remote\n"); sys.exit(2)
    rows = []
    for pr in json.loads(p.stdout):
        files = [f["path"] for f in pr.get("files") or []]
        n = len({f for r in paths for f in under(r, files)})
        if n: rows.append({"number": pr["number"], "branch": pr["headRefName"], "title": pr["title"], "overlap": n})
    rows.sort(key=lambda r: (-r["overlap"], r["number"]))
    emit(rows, ["number", "branch", "title", "overlap"], a)
    if rows: sys.stderr.write("→ `jx git since origin/<branch> --files` shows what one of them changes; `jx pr threads N` for its review\n")

def cmd_doctor(a):
    b = branch()
    up = git("for-each-ref", "--format=%(upstream:short)", f"refs/heads/{b}").strip()  # the configured upstream, even when its ref was never fetched
    have = bool(up) and bool(git("rev-parse", "--verify", "-q", up, ok=(0, 1)).strip())
    lr = git("rev-list", "--left-right", "--count", f"{up}...HEAD").split() if have else ["?", "?"] if up else ["", ""]
    rows = [{"item": "toplevel", "value": toplevel()}, {"item": "branch", "value": b}, {"item": "upstream", "value": (up + ("" if have else " (not fetched)")) if up else "none"},
            {"item": "ahead", "value": lr[1]}, {"item": "behind", "value": lr[0]}, {"item": "uncommitted", "value": len(changed_paths())},
            {"item": "shallow", "value": git("rev-parse", "--is-shallow-repository").strip()},
            {"item": "origin", "value": git("remote", "get-url", "origin", ok=(0, 2, 128)).strip() or "none"},
            {"item": "local_main_behind_origin", "value": main_age() or ("n/a" if ctime("main") is None or ctime("origin/main") is None else "0")}]
    emit(rows, ["item", "value"], a)

def main():
    ap = argparse.ArgumentParser(prog="jx git", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="raw JSON where a command has a payload")
    g = argparse.ArgumentParser(add_help=False); g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw JSON where a command has a payload")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    def ref(p):
        p.add_argument("--ref", default="origin/main", help="ref to read from (origin/main); origin/<b> is fetched first"); p.add_argument("--pr", type=int, metavar="N", help="read the head of pull request N instead")
    p = sp.add_parser("cat", parents=[g], help="file contents at a ref, no checkout"); p.add_argument("path", nargs="+", help="repo-relative path(s)"); ref(p); p.set_defaults(f=cmd_cat)
    p = sp.add_parser("ls", parents=[g], help="names under a path at a ref"); p.add_argument("path", nargs="?", default="", help="repo-relative directory (root)"); ref(p); p.add_argument("-r", action="store_true", help="recursive"); p.set_defaults(f=cmd_ls)
    p = sp.add_parser("since", parents=[g], help="how this branch differs from BASE: ahead/behind, files, or the log"); p.add_argument("base", nargs="?", default="origin/main", help="base ref (origin/main)")
    x = p.add_mutually_exclusive_group(); x.add_argument("--log", action="store_true", help="commits since the merge-base"); x.add_argument("--files", action="store_true", help="changed paths with status"); x.add_argument("--stat", action="store_true", help="diffstat"); p.set_defaults(f=cmd_since)
    p = sp.add_parser("commit", parents=[g], help="stage only the named paths and commit; refuses main, a switched branch, unnamed or unchanged paths")
    x = p.add_mutually_exclusive_group(required=True); x.add_argument("-m", metavar="MSG", help="commit message"); x.add_argument("-F", metavar="FILE", help="message from FILE, or - for stdin")
    p.add_argument("paths", nargs="*", metavar="path", help="paths to stage, after `--`; a directory stages everything changed under it")
    p.add_argument("--expect-branch", metavar="B", help="refuse unless HEAD is on branch B (any branch but main/master)"); p.add_argument("--strict", action="store_true", help="refuse when other paths are changed too (warn)")
    p.add_argument("--push", action="store_true", help="push -u origin <branch> afterwards and verify the remote tip (off)"); p.add_argument("--no-sign", action="store_true", help="commit with commit.gpgsign=false (repo config)")
    p.add_argument("--trailer", action="append", default=[], metavar="LINE", help="trailer line appended after a blank line, repeatable (none)"); p.set_defaults(f=cmd_commit)
    p = sp.add_parser("wt", parents=[g], help="add a worktree on a fresh branch outside the checkout; `ls` and `rm <dir>` manage them")
    p.add_argument("name", help="branch to create (reset if it exists), or `ls` / `rm`"); p.add_argument("rm_dir", nargs="?", metavar="DIR", help="rm only: the worktree directory to remove")
    p.add_argument("--base", default="origin/main", help="start point, fetched first when origin/<b> (origin/main)"); p.add_argument("--dir", help="where to put the worktree ($TMPDIR/jx-wt-<name>)")
    p.add_argument("--copy", action="append", default=[], metavar="PATH", help="copy this path from the current tree into the worktree, repeatable; directories recurse (none)"); p.set_defaults(f=cmd_wt)
    p = sp.add_parser("export", parents=[g], help="extract a ref into a directory with a .git-sha file; no .git, no checkout")
    p.add_argument("ref", help="ref to export; origin/<b> is fetched first"); p.add_argument("dir", help="target directory (created)"); p.set_defaults(f=cmd_export)
    p = sp.add_parser("touching", parents=[g], help="open PRs whose files overlap these paths; exit 2 without gh")
    p.add_argument("path", nargs="+", help="paths; a directory matches every file under it"); p.add_argument("-R", metavar="owner/name", help="repository for gh (the cwd's GitHub remote)"); p.set_defaults(f=cmd_touching)
    p = sp.add_parser("doctor", parents=[g], help="toplevel, branch, upstream and ahead/behind, uncommitted count, shallow, origin url, local main age; no remote calls"); p.set_defaults(f=cmd_doctor)
    a = ap.parse_args(); a.f(a)

if __name__ == "__main__": main()
