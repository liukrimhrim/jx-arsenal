#!/usr/bin/env python3
"""jx pr — PR lifecycle lever for agents. TSV by default, --json for the raw payload.

  jx pr show <n> [n ...] [--body] [--files]           one row per PR: state, head sha, mergeable, size, check counts, unresolved threads
  jx pr comments <n> [--since ISO|7d|push|mine] [--full]   top-level comments and review verdicts, oldest first
  jx pr threads <n> [--all] [--full]                  review threads (unresolved only unless --all)
  jx pr reply <n> <comment-id> --body-file F [--dry-run]   reply to a review comment, then re-read it to verify
  jx pr comment <n> --body-file F [--dry-run]         top-level PR comment, then re-read it to verify
  jx pr edit <n> [--title T] [--body-file F] [--dry-run]   change title/body, then re-read to verify; --dry-run diffs the body
  jx pr commits <n> [--since ISO|7d]                   commits + file diffstat pushed after the last review by someone else
  jx pr triage <n> [--wait]                           check rollup, then `jx ci failed` for every failing run: one call, no glue
  jx pr ci <n> [--wait] [--failed]                    check rollup; --wait polls until settled; --failed emits run_id/job_id TSV for `jx ci failed -`
  jx pr doctor

Exit codes: 0 ok · 1 failed/cancelled · 2 usage/auth · 8 CI pending or no checks reported.
Posting (reply/comment/edit) is an outward action: run with --dry-run first, post only with approval.
"""
import argparse, difflib, json, re, subprocess, sys, time, signal
from collections import Counter
signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # `| head` must not traceback
from datetime import datetime, timedelta, timezone

CMD = ""  # current subcommand, for error hints
NOT_FOUND = {
    "triage": "PR not found in this repo. `gh pr list --state all --search <text>` finds the number; pass -R owner/name for another repo.",
    "threads": "PR not found in this repo. `gh pr list --state all --search <text>` finds the number; pass -R owner/name for another repo.",
    "commits": "PR not found in this repo. `gh pr list --state all --search <text>` finds the number; pass -R owner/name for another repo.",
    "ci": "PR not found in this repo. `gh pr list --state all --search <text>` finds the number; pass -R owner/name for another repo.",
    "comment": "PR not found in this repo. `gh pr list --state all --search <text>` finds the number; pass -R owner/name for another repo.",
    **dict.fromkeys(("show", "comments", "edit"), "PR not found in this repo. `gh pr list --state all --search <text>` finds the number; pass -R owner/name for another repo."),
    "reply": "PR or comment id not found. `jx pr threads <n>` prints the root comment_id of each thread; reply to that id, not the thread id.",
}

def sh(args, stdin=None):
    try: p = subprocess.run(args, input=stdin, text=True, errors="replace", capture_output=True)
    except FileNotFoundError: sys.exit("`gh` not found on PATH; install the GitHub CLI, then `gh auth login`")
    if p.returncode:
        err = p.stderr.strip() or p.stdout.strip()
        # not-found first, and auth only on gh's own phrasings: a PR numbered 403 or a repo named auth-service is not an auth failure
        if re.search(r"HTTP 404|not found|could not resolve", err, re.I): hint, code = "→ " + NOT_FOUND.get(CMD, "check the id"), 1
        elif re.search(r"HTTP 40[13]\b|gh auth login|authenticat|bad credentials", err, re.I): hint, code = "→ `gh auth status`; if logged out, `gh auth login`", 2
        else: hint, code = "", 1
        sys.stderr.write(f"{err}\n{hint}\n" if hint else err + "\n"); sys.exit(code)
    return p.stdout

def gh(*a, stdin=None): return sh(["gh", *a], stdin)
def api(path, method="GET", body=None, repo=None):
    args = ["api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
    if body is not None: args += ["--input", "-"]
    out = gh(*args, stdin=json.dumps(body) if body is not None else None)
    return json.loads(out) if out.strip() else {}

def pages(path):
    """Every page of a list endpoint, flattened."""
    return [x for page in json.loads(gh("api", "--paginate", "--slurp", "-H", "Accept: application/vnd.github+json", path)) for x in page]

def repo_of(args):
    return args.repo or gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip()

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))

def emit(rows, header, args):
    if args.json: print(json.dumps(rows, indent=1))
    else: tsv(rows, header)

def since(s):
    """30m, 6h, 7d, 2w: that long ago, in UTC. Anything else is parsed as an ISO time (a naive one is UTC)."""
    m = re.fullmatch(r"(\d+)([smhdw])", s or "")
    if m: return datetime.now(timezone.utc) - timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)]: int(m.group(1))})
    try: dt = datetime.fromisoformat((s or "").replace("Z", "+00:00")); return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError: sys.exit(f"bad time {s!r}; use 30m, 6h, 7d, 2w, or an ISO time like 2026-01-31T00:00:00Z")

def iso(dt): return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def read_body(args):
    if args.body_file:
        try: return open(args.body_file).read()
        except OSError as e: sys.exit(f"--body-file: {e}")
    if args.body is not None: return args.body
    sys.exit("need --body-file or --body")

# ---------------- show ----------------
SHOW = "number,state,isDraft,title,author,headRefName,headRefOid,baseRefName,mergeable,mergeStateStatus,mergeCommit,reviewDecision,additions,deletions,changedFiles,statusCheckRollup,updatedAt,url"
# ponytail: first 100 review threads only; `jx pr threads` pages through all of them
TQ = """query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){pullRequest(number:$n){
 comments{totalCount} reviewThreads(first:100){nodes{isResolved}} commits(last:1){totalCount nodes{commit{committedDate}}}}}}"""

def bucket(c):
    """One statusCheckRollup entry: a CheckRun has status + conclusion, a StatusContext has state."""
    if c.get("__typename") == "CheckRun" and c.get("status") != "COMPLETED": return "pending"
    s = (c.get("conclusion") or c.get("state") or "").upper()
    if s in ("SUCCESS", "NEUTRAL"): return "pass"
    if s == "SKIPPED": return "skip"
    if s in ("", "PENDING", "EXPECTED"): return "pending"
    return "fail"

def threads_and_head(repo, n):
    o, r = repo.split("/")
    return json.loads(gh("api", "graphql", "-F", f"o={o}", "-F", f"r={r}", "-F", f"n={n}", "-f", f"query={TQ}"))["data"]["repository"]["pullRequest"]

def login(u): return (u or {}).get("login", "ghost")  # a deleted account comes back as null

def cmd_show(args):
    repo = repo_of(args); rows = []
    for n in args.n:
        d = json.loads(gh("pr", "view", str(n), "-R", repo, "--json", SHOW + ",body" * args.body + ",files" * args.files))
        g = threads_and_head(repo, n)
        b = Counter(map(bucket, d["statusCheckRollup"])); last = g["commits"]["nodes"]
        row = {"n": n, "state": "DRAFT" if d["isDraft"] and d["state"] == "OPEN" else d["state"], "title": d["title"], "author": login(d["author"]),
               "head": d["headRefName"], "head_sha": d["headRefOid"][:10], "base": d["baseRefName"], "mergeable": d["mergeable"], "merge_state": d["mergeStateStatus"],
               "merge_commit": (d["mergeCommit"] or {}).get("oid", "")[:10], "review": d["reviewDecision"], "+": d["additions"], "-": d["deletions"], "files": d["changedFiles"],
               "commits": g["commits"]["totalCount"], "checks": " ".join(f"{k}={b[k]}" for k in ("fail", "pending", "pass", "skip") if b[k]) or "none",
               "unresolved": sum(not t["isResolved"] for t in g["reviewThreads"]["nodes"]), "comments": g["comments"]["totalCount"],
               "last_commit": last[0]["commit"]["committedDate"] if last else "", "updated": d["updatedAt"], "url": d["url"]}
        if args.body: row["body"] = d["body"]
        if args.files: row["file_list"] = [{"+": f["additions"], "-": f["deletions"], "path": f["path"]} for f in d["files"]]
        rows.append(row)
    if args.json: print(json.dumps(rows, indent=1)); return
    tsv(rows, ["n", "state", "head", "head_sha", "base", "mergeable", "merge_state", "merge_commit", "review", "checks", "unresolved", "comments", "commits", "+", "-", "files", "last_commit", "author", "title", "url"])
    for row in rows:
        if args.files: print(f"\n=== #{row['n']} files"); tsv(row["file_list"], ["+", "-", "path"])
        if args.body: print(f"\n=== #{row['n']} {row['title']}\n{row['body']}")

# ---------------- comments ----------------
def cmd_comments(args):
    repo = repo_of(args)
    rows = [{"kind": "comment", "id": c["id"], "author": login(c["user"]), "created": c["created_at"], "state": "", "body": c["body"], "url": c["html_url"]}
            for c in pages(f"/repos/{repo}/issues/{args.n}/comments?per_page=100")]
    rows += [{"kind": "review", "id": v["id"], "author": login(v["user"]), "created": v["submitted_at"], "state": v["state"], "body": v["body"], "url": v["html_url"]}
             for v in pages(f"/repos/{repo}/pulls/{args.n}/reviews?per_page=100")
             if v.get("submitted_at") and (v["body"] or v["state"] != "COMMENTED")]  # a bodiless COMMENTED review only wraps inline comments: `jx pr threads`
    rows.sort(key=lambda x: x["created"])
    if args.since:
        if args.since == "push":
            cut = threads_and_head(repo, args.n)["commits"]["nodes"][0]["commit"]["committedDate"]
        elif args.since == "mine":
            me = gh("api", "user", "-q", ".login").strip(); mine = [x["created"] for x in rows if x["author"] == me]
            if not mine: sys.exit(f"no comment or review by {me} on #{args.n}; pass --since ISO or 7d")
            cut = mine[-1]
        else: cut = iso(since(args.since))
        rows = [x for x in rows if x["created"] > cut]; sys.stderr.write(f"after {cut}: ")
    if not args.full:
        for x in rows: x["body"] = (x["body"] or "")[:200]
    emit(rows, ["kind", "id", "author", "created", "state", "body", "url"], args)
    sys.stderr.write(f"{len(rows)} comments/reviews\n")

# ---------------- threads ----------------
Q = """query($o:String!,$r:String!,$n:Int!,$after:String){repository(owner:$o,name:$r){pullRequest(number:$n){
 reviewThreads(first:100,after:$after){pageInfo{hasNextPage endCursor}
 nodes{id isResolved isOutdated path line comments(first:50){nodes{databaseId author{login} createdAt body url}}}}}}}"""

def cmd_threads(args):
    o, r = repo_of(args).split("/")
    rows, after = [], None
    while True:
        d = json.loads(gh("api", "graphql", "-F", f"o={o}", "-F", f"r={r}", "-F", f"n={args.n}", "-F", f"after={after or ''}", "-f", f"query={Q}"))
        rt = d["data"]["repository"]["pullRequest"]["reviewThreads"]
        for t in rt["nodes"]:
            if t["isResolved"] and not args.all: continue
            for i, c in enumerate(t["comments"]["nodes"]):
                body = c["body"] if args.full else c["body"][:200]
                rows.append({"thread": t["id"], "resolved": t["isResolved"], "outdated": t["isOutdated"], "root": i == 0,
                             "comment_id": c["databaseId"], "path": t["path"], "line": t["line"], "author": c["author"]["login"] if c["author"] else "",
                             "created": c["createdAt"], "body": body, "url": c["url"]})
        if not rt["pageInfo"]["hasNextPage"]: break
        after = rt["pageInfo"]["endCursor"]
    emit(rows, ["thread", "resolved", "outdated", "root", "comment_id", "path", "line", "author", "created", "body", "url"], args)
    sys.stderr.write(f"{len({r['thread'] for r in rows})} threads, {len(rows)} comments\n")

# ---------------- reply / comment ----------------
def verify(got, body, args):
    ok = got.get("body", "").strip() == body.strip()
    print(json.dumps({"id": got.get("id"), "url": got.get("html_url"), "verified": ok}))
    if not ok: sys.exit(1)

def cmd_reply(args):
    repo, body = repo_of(args), read_body(args)
    if args.dry_run: print(f"DRY RUN: POST /repos/{repo}/pulls/{args.n}/comments/{args.comment_id}/replies\n---\n{body}"); return
    posted = api(f"/repos/{repo}/pulls/{args.n}/comments/{args.comment_id}/replies", "POST", {"body": body})
    verify(api(f"/repos/{repo}/pulls/comments/{posted['id']}"), body, args)

def cmd_comment(args):
    repo, body = repo_of(args), read_body(args)
    if args.dry_run: print(f"DRY RUN: POST /repos/{repo}/issues/{args.n}/comments\n---\n{body}"); return
    posted = api(f"/repos/{repo}/issues/{args.n}/comments", "POST", {"body": body})
    verify(api(f"/repos/{repo}/issues/comments/{posted['id']}"), body, args)

def cmd_edit(args):
    repo = repo_of(args); want = {}
    if args.title is not None: want["title"] = args.title
    if args.body_file or args.body is not None: want["body"] = read_body(args)
    if not want: sys.exit("nothing to change; pass --title and/or --body-file")
    cur = api(f"/repos/{repo}/pulls/{args.n}")
    if args.dry_run:
        print(f"DRY RUN: PATCH /repos/{repo}/pulls/{args.n}")
        if "title" in want: print(f"title: {cur['title']!r} -> {want['title']!r}")
        if "body" in want:
            # compared stripped, like the verify step: a trailing newline from `gh -q .body > F` is not a change
            diff = "\n".join(difflib.unified_diff((cur["body"] or "").strip().splitlines(), want["body"].strip().splitlines(), "current body", "new body", lineterm=""))
            print(diff or "body: unchanged")
        return
    api(f"/repos/{repo}/pulls/{args.n}", "PATCH", want)
    got = api(f"/repos/{repo}/pulls/{args.n}")
    ok = all((got.get(k) or "").strip() == v.strip() for k, v in want.items())
    print(json.dumps({"url": got.get("html_url"), "changed": sorted(want), "verified": ok}))
    if not ok: sys.exit(1)

# ---------------- since ----------------
def cmd_since(args):
    repo = repo_of(args)
    since_ = iso(since(args.since)) if args.since else None  # the ISO cut GitHub timestamps compare against as strings
    if not since_:
        me = gh("api", "user", "-q", ".login").strip()
        reviews = pages(f"/repos/{repo}/pulls/{args.n}/reviews?per_page=100")
        others = [r["submitted_at"] for r in reviews if r.get("submitted_at") and login(r["user"]) != me]
        if not others: sys.exit("no review by someone else; pass --since ISO")
        since_ = max(others)
    commits = pages(f"/repos/{repo}/pulls/{args.n}/commits?per_page=100")
    new = [c for c in commits if c["commit"]["committer"]["date"] > since_]
    rows = [{"sha": c["sha"][:10], "date": c["commit"]["committer"]["date"], "msg": c["commit"]["message"].splitlines()[0]} for c in new]
    sys.stderr.write(f"since {since_}: {len(new)} commits\n")
    if not new: return
    if not new[0]["parents"]: sys.exit("the first new commit is a root commit: nothing to diff against; `jx pr show N --files` lists the whole PR")
    base = new[0]["parents"][0]["sha"]; head = new[-1]["sha"]
    cmp_ = api(f"/repos/{repo}/compare/{base}...{head}")
    files = [{"status": f["status"], "+": f["additions"], "-": f["deletions"], "file": f["filename"]} for f in cmp_.get("files", [])]
    if args.json: print(json.dumps({"since": since_, "commits": rows, "files": files}, indent=1)); return
    tsv(rows, ["sha", "date", "msg"]); print(); tsv(files, ["status", "+", "-", "file"])

# ---------------- ci ----------------
def checks(args):
    try: p = subprocess.run(["gh", "pr", "checks", str(args.n), "--json", "name,state,bucket,link,workflow"] + (["-R", args.repo] if args.repo else []),
                            text=True, capture_output=True)
    except FileNotFoundError: sys.exit("`gh` not found on PATH; install the GitHub CLI, then `gh auth login`")
    if p.returncode == 1 and not p.stdout.strip() and p.stderr.strip().startswith("no checks reported on the "):
        return []
    if p.returncode not in (0, 1, 8) or not p.stdout.strip():  # 8 = pending, 1 = failing
        sys.stderr.write(p.stderr); sys.exit(2)
    return json.loads(p.stdout)

def check_exit(rows):
    if any(c['bucket'] in ('fail', 'cancel') for c in rows): return 1
    if not rows or any(c['bucket'] not in ('pass', 'skipping') for c in rows): return 8
    return 0

def cmd_ci(args):
    deadline = time.time() + args.timeout
    while True:
        rows = checks(args)
        pending = [c for c in rows if c["bucket"] == "pending"]
        if not (args.wait and pending): break
        if time.time() > deadline: sys.stderr.write("timeout\n"); break
        sys.stderr.write(f"{len(pending)} pending… sleep {args.interval}s\n"); time.sleep(args.interval)
    failed = [c for c in rows if c["bucket"] == "fail"]
    if args.failed:
        out = []
        for c in failed:
            m = re.search(r"/actions/runs/(\d+)/job/(\d+)", c["link"] or "")
            out.append({"job_id": m.group(2) if m else "", "run_id": m.group(1) if m else "", "name": c["name"], "link": c["link"]})
        emit(out, ["job_id", "run_id", "name", "link"], args)
    else:
        rows.sort(key=lambda c: ({"fail": 0, "pending": 1, "cancel": 2, "pass": 3, "skipping": 4}.get(c["bucket"], 5), c["name"]))
        emit(rows, ["bucket", "state", "name", "workflow", "link"], args)
    sys.stderr.write(f"{len(failed)} failed, {sum(c['bucket']=='pass' for c in rows)} passed, {sum(c['bucket']=='pending' for c in rows)} pending, {sum(c['bucket']=='cancel' for c in rows)} cancelled\n")
    sys.exit(check_exit(rows))

def cmd_triage(args):
    """pr ci --failed, then ci failed per run, in-process: the failing log lines arrive in one call."""
    import os
    deadline = time.time() + args.timeout
    while True:
        rows = checks(args); pending = [c for c in rows if c["bucket"] == "pending"]
        if not (args.wait and pending) or time.time() > deadline: break
        sys.stderr.write(f"{len(pending)} pending… sleep {args.interval}s\n"); time.sleep(args.interval)
    failed = [c for c in rows if c["bucket"] == "fail"]
    print(f"{len(failed)} failed, {sum(c['bucket']=='pass' for c in rows)} passed, {len(pending)} pending, {sum(c['bucket']=='cancel' for c in rows)} cancelled")
    if not failed: sys.exit(check_exit(rows))
    for c in failed: print(f"FAIL\t{c['name']}\t{c['link']}")
    runs = list(dict.fromkeys(m.group(1) for c in failed for m in [re.search(r"/actions/runs/(\d+)", c["link"] or "")] if m))
    ci = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ci.py")
    for r in runs:
        print(); sys.stdout.flush()
        subprocess.run([sys.executable, ci, "failed", r, "--max-lines", str(args.max_lines)] + (["-R", args.repo] if args.repo else []))
    sys.exit(1)

def cmd_doctor(args):
    print(gh("auth", "status").strip().splitlines()[1].strip()); print("repo:", repo_of(args)); print("ok")

def add_globals(p, suppress=False):
    d = {"default": argparse.SUPPRESS} if suppress else {}
    p.add_argument("--repo", "-R", help="owner/name; default is the current checkout's origin", **d)
    p.add_argument("--json", action="store_true", help="raw JSON instead of TSV", **d)

def main():
    global CMD
    ap = argparse.ArgumentParser(prog="jx pr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); add_globals(ap)
    g = argparse.ArgumentParser(add_help=False); add_globals(g, suppress=True)  # same flags accepted after the subcommand
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sp.add_parser("show", parents=[g], help="one row per PR: state, head sha, mergeability, size, check counts, unresolved threads, comment count")
    p.add_argument("n", type=int, nargs="+", help="PR number(s)"); p.add_argument("--body", action="store_true", help="print each PR body after the table")
    p.add_argument("--files", action="store_true", help="print each PR's changed files with +/- after the table"); p.set_defaults(f=cmd_show)
    p = sp.add_parser("comments", parents=[g], help="top-level comments and review verdicts (not inline threads), oldest first")
    p.add_argument("n", type=int, help="PR number"); p.add_argument("--since", help="ISO time, 30m/6h/7d/2w, `push` (after the head commit) or `mine` (after your last comment or review)")
    p.add_argument("--full", action="store_true", help="full bodies instead of the first 200 chars"); p.set_defaults(f=cmd_comments)
    p = sp.add_parser("threads", parents=[g], help="review threads with comment ids; unresolved only unless --all")
    p.add_argument("n", type=int, help="PR number"); p.add_argument("--all", action="store_true", help="include resolved threads"); p.add_argument("--full", action="store_true", help="full comment bodies instead of the first 200 chars"); p.set_defaults(f=cmd_threads)
    for name, f, h in (("reply", cmd_reply, "reply to a review comment, then re-read it to verify"), ("comment", cmd_comment, "post a top-level PR comment, then re-read it to verify")):
        p = sp.add_parser(name, parents=[g], help=h); p.add_argument("n", type=int, help="PR number")
        if name == "reply": p.add_argument("comment_id", type=int, help="root comment_id from `jx pr threads`")
        p.add_argument("--body-file", help="file holding the markdown body"); p.add_argument("--body", help="inline body"); p.add_argument("--dry-run", action="store_true", help="print endpoint and body, post nothing"); p.set_defaults(f=f)
    p = sp.add_parser("edit", parents=[g], help="change a PR's title and/or body, then re-read it to verify")
    p.add_argument("n", type=int, help="PR number"); p.add_argument("--title", help="new title"); p.add_argument("--body-file", help="file holding the new markdown body"); p.add_argument("--body", help="inline new body")
    p.add_argument("--dry-run", action="store_true", help="print the title change and a unified diff of the body; change nothing"); p.set_defaults(f=cmd_edit)
    p = sp.add_parser("commits", parents=[g], help="commits and per-file diffstat pushed after the last review by someone else")
    p.add_argument("n", type=int, help="PR number"); p.add_argument("--since", help="ISO timestamp or 30m/6h/7d/2w; default is the last review submitted by someone other than you"); p.set_defaults(f=cmd_since)
    p = sp.add_parser("ci", parents=[g], help="check rollup sorted fail > pending > pass; exit 1 if anything failed")
    p.add_argument("n", type=int, help="PR number"); p.add_argument("--wait", action="store_true", help="poll until nothing is pending"); p.add_argument("--failed", action="store_true", help="print job_id/run_id of failed checks for `jx ci failed` / `jx ci logs`")
    p.add_argument("--interval", type=int, default=30, help="seconds between polls (30)"); p.add_argument("--timeout", type=int, default=2400, help="give up after this many seconds (2400)"); p.set_defaults(f=cmd_ci)
    p = sp.add_parser("triage", parents=[g], help="check rollup plus `jx ci failed` for every failing run, in one call")
    p.add_argument("n", type=int, help="PR number"); p.add_argument("--wait", action="store_true", help="poll until nothing is pending first"); p.add_argument("--interval", type=int, default=30, help="seconds between polls (30)")
    p.add_argument("--timeout", type=int, default=2400, help="give up waiting after this many seconds (2400)"); p.add_argument("--max-lines", type=int, default=60, help="log lines per failed job (60)"); p.set_defaults(f=cmd_triage)
    sp.add_parser("doctor", parents=[g], help="auth status and resolved repo").set_defaults(f=cmd_doctor)
    args = ap.parse_args(); CMD = args.cmd; args.f(args)

if __name__ == "__main__": main()
