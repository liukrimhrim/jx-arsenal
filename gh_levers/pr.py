#!/usr/bin/env python3
"""pr — PR lifecycle lever for agents. TSV by default, --json for the raw payload.

  pr threads <n> [--all] [--full]                  review threads (unresolved only unless --all)
  pr reply <n> <comment-id> --body-file F | --body S [--dry-run]
                                                   reply to a review comment, then re-read it to verify
  pr comment <n> --body-file F | --body S [--dry-run]
                                                   top-level PR comment, then re-read it to verify
  pr since <n> [--since ISO]                       commits + file diffstat pushed after the last review by someone else
  pr ci <n> [--wait] [--failed] [--interval S]     check rollup; --wait polls until nothing is pending;
                                                   --failed prints job ids of failed checks (feed to `ci logs`)
  pr doctor

Exit codes: 0 ok · 1 something failed (a failing check, a verify mismatch) · 2 usage/auth.
Posting (reply/comment) is an outward action: run with --dry-run first, post only with approval.
"""
import argparse, json, re, subprocess, sys, time
from datetime import datetime, timezone

def sh(args, stdin=None):
    p = subprocess.run(args, input=stdin, text=True, capture_output=True)
    if p.returncode:
        sys.stderr.write(p.stderr.strip() + "\n"); sys.exit(2 if "auth" in p.stderr or "login" in p.stderr else 1)
    return p.stdout

def gh(*a, stdin=None): return sh(["gh", *a], stdin)
def api(path, method="GET", body=None, repo=None):
    args = ["api", "-X", method, "-H", "Accept: application/vnd.github+json", path]
    if body is not None: args += ["--input", "-"]
    out = gh(*args, stdin=json.dumps(body) if body is not None else None)
    return json.loads(out) if out.strip() else {}

def repo_of(args):
    return args.repo or gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip()

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))

def emit(rows, header, args):
    if args.json: print(json.dumps(rows, indent=1))
    else: tsv(rows, header)

def read_body(args):
    if args.body_file: return open(args.body_file).read()
    if args.body is not None: return args.body
    sys.exit("need --body-file or --body")

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

# ---------------- since ----------------
def cmd_since(args):
    repo = repo_of(args)
    since = args.since
    if not since:
        me = gh("api", "user", "-q", ".login").strip()
        reviews = api(f"/repos/{repo}/pulls/{args.n}/reviews?per_page=100")
        others = [r["submitted_at"] for r in reviews if r.get("submitted_at") and r["user"]["login"] != me]
        if not others: sys.exit("no review by someone else; pass --since ISO")
        since = max(others)
    commits = api(f"/repos/{repo}/pulls/{args.n}/commits?per_page=250")
    new = [c for c in commits if c["commit"]["committer"]["date"] > since]
    rows = [{"sha": c["sha"][:10], "date": c["commit"]["committer"]["date"], "msg": c["commit"]["message"].splitlines()[0]} for c in new]
    sys.stderr.write(f"since {since}: {len(new)} commits\n")
    if not new: return
    base = new[0]["parents"][0]["sha"]; head = new[-1]["sha"]
    cmp_ = api(f"/repos/{repo}/compare/{base}...{head}")
    files = [{"status": f["status"], "+": f["additions"], "-": f["deletions"], "file": f["filename"]} for f in cmp_.get("files", [])]
    if args.json: print(json.dumps({"since": since, "commits": rows, "files": files}, indent=1)); return
    tsv(rows, ["sha", "date", "msg"]); print(); tsv(files, ["status", "+", "-", "file"])

# ---------------- ci ----------------
def checks(args):
    p = subprocess.run(["gh", "pr", "checks", str(args.n), "--json", "name,state,bucket,link,workflow"] + (["-R", args.repo] if args.repo else []),
                       text=True, capture_output=True)
    if p.returncode not in (0, 1, 8) or not p.stdout.strip():  # 8 = pending, 1 = failing
        sys.stderr.write(p.stderr); sys.exit(2)
    return json.loads(p.stdout)

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
    sys.stderr.write(f"{len(failed)} failed, {sum(c['bucket']=='pass' for c in rows)} passed, {sum(c['bucket']=='pending' for c in rows)} pending\n")
    sys.exit(1 if failed else 0)

def cmd_doctor(args):
    print(gh("auth", "status").strip().splitlines()[1].strip()); print("repo:", repo_of(args)); print("ok")

def main():
    ap = argparse.ArgumentParser(prog="pr", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", "-R", help="owner/name (default: current checkout)")
    ap.add_argument("--json", action="store_true", help="raw JSON instead of TSV")
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("threads"); p.add_argument("n", type=int); p.add_argument("--all", action="store_true", help="include resolved"); p.add_argument("--full", action="store_true", help="full bodies"); p.set_defaults(f=cmd_threads)
    for name, f in (("reply", cmd_reply), ("comment", cmd_comment)):
        p = sp.add_parser(name); p.add_argument("n", type=int)
        if name == "reply": p.add_argument("comment_id", type=int)
        p.add_argument("--body-file"); p.add_argument("--body"); p.add_argument("--dry-run", action="store_true"); p.set_defaults(f=f)
    p = sp.add_parser("since"); p.add_argument("n", type=int); p.add_argument("--since", help="ISO timestamp; default = last review by someone else"); p.set_defaults(f=cmd_since)
    p = sp.add_parser("ci"); p.add_argument("n", type=int); p.add_argument("--wait", action="store_true"); p.add_argument("--failed", action="store_true")
    p.add_argument("--interval", type=int, default=30); p.add_argument("--timeout", type=int, default=2400); p.set_defaults(f=cmd_ci)
    sp.add_parser("doctor").set_defaults(f=cmd_doctor)
    args = ap.parse_args(); args.f(args)

if __name__ == "__main__": main()
