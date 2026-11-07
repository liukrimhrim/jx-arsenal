#!/usr/bin/env python3
"""jx ci — GitHub Actions lever for agents. TSV by default, --json for the raw payload.

  jx ci runs <workflow> [--branch B] [--limit N] [--status S]   recent runs of a workflow (file name or display name)
  jx ci jobs <run-id|-> [--attempt N]                           jobs of a run; --attempt reads an earlier attempt
  jx ci logs <job-id|-> [--grep PAT] [-C N] [--tail N]          one job's log, timestamps stripped
  jx ci failed <run-id|-> [--attempt N] [--grep PAT] [-C N]     every failed job of a run: header + matching log lines
  jx ci dispatch <workflow> [--ref R] [-f k=v ...] [--wait] [--dry-run]   workflow_dispatch, then resolve the new run id
  jx ci wait <run-id|-> [--interval S] [--timeout S]            block until the run completes; exit 1 unless success

An id of `-` reads ids from TSV on stdin: the column named like the argument (run_id / job_id), else the
first column. So `jx pr ci 123 --failed | jx ci failed -` needs no awk and no model in between.
  jx ci doctor

Why `--attempt`: `gh run view --log-failed` always returns the LATEST attempt; a rerun hides the
original failure. Why the default grep: a job can fail with no "FAILED" line — match error-ish text.
"""
import argparse, json, re, subprocess, sys, time, signal
signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # `| head` must not traceback
from datetime import datetime, timezone

ERRISH = r"(?i)##\[error\]|\berror\b|traceback|\bfailed\b|exit code [1-9]|exited with|assert"

CMD = ""  # current subcommand, for error hints
NOT_FOUND = {
    "runs": "workflow not found. `gh workflow list` shows file names and display names; either works here.",
    "dispatch": "workflow not found, or it has no workflow_dispatch trigger. `gh workflow list`, then check the workflow file's `on:` block.",
    "jobs": "run id not found. `jx ci runs <workflow> [--branch B]` lists run ids; `jx pr ci <n> --failed` gives run_id per failed check.",
    "failed": "run id not found. `jx ci runs <workflow> [--branch B]` lists run ids; `jx pr ci <n> --failed` gives run_id per failed check.",
    "wait": "run id not found. `jx ci runs <workflow> [--branch B]` lists run ids.",
    "logs": "job id not found, or its log expired (90 days). `jx ci jobs <run_id>` lists job ids; `jx pr ci <n> --failed` gives job_id per failed check.",
}

def sh(args, stdin=None, ok=(0,)):
    try: p = subprocess.run(args, input=stdin, text=True, capture_output=True)
    except FileNotFoundError: sys.exit("`gh` not found on PATH; install the GitHub CLI, then `gh auth login`")
    if p.returncode not in ok:
        err = p.stderr.strip() or p.stdout.strip()
        if re.search(r"auth|login|token|401|403", err, re.I): hint, code = "→ `gh auth status`; if logged out, `gh auth login`", 2
        elif re.search(r"404|not found|could not resolve", err, re.I): hint, code = "→ " + NOT_FOUND.get(CMD, "check the id"), 1
        else: hint, code = "", 1
        sys.stderr.write(f"{err}\n{hint}\n" if hint else err + "\n"); sys.exit(code)
    return p.stdout

def gh(*a, **k): return sh(["gh", *a], **k)
def repo_of(args): return args.repo or gh("repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner").strip()
def api(path, repo): return json.loads(gh("api", "-H", "Accept: application/vnd.github+json", f"/repos/{repo}{path}"))
def R(args): return ["-R", args.repo] if args.repo else []

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, args):
    if args.json: print(json.dumps(rows, indent=1))
    else: tsv(rows, header)

def ids(arg, col):
    """One id, or every id from a TSV on stdin when arg is `-` (column `col`, else the first column)."""
    if arg != "-": return [int(arg)]
    lines = [l.rstrip("\n") for l in sys.stdin if l.strip()]
    if not lines: sys.exit(f"no ids on stdin; expected a TSV with a {col} column (e.g. from `jx pr ci <n> --failed`)")
    head = lines[0].split("\t"); i = head.index(col) if col in head else 0
    out = []
    for l in lines[1:] if not lines[0].split("\t")[i].isdigit() else lines:
        v = l.split("\t")[i].strip()
        if v.isdigit() and int(v) not in out: out.append(int(v))
    if not out: sys.exit(f"no numeric {col} values on stdin")
    return out

def dur(a, b):
    if not a or not b: return ""
    f = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))
    return f"{int((f(b) - f(a)).total_seconds())}s"

# ---------------- runs ----------------
def cmd_runs(args):
    a = ["run", "list", "--workflow", args.workflow, "--limit", str(args.limit), "--json", "databaseId,status,conclusion,event,headBranch,headSha,createdAt,url,displayTitle,attempt"] + R(args)
    if args.branch: a += ["--branch", args.branch]
    if args.status: a += ["--status", args.status]
    rows = [{"id": r["databaseId"], "status": r["status"], "conclusion": r["conclusion"], "attempt": r["attempt"], "event": r["event"], "branch": r["headBranch"],
             "sha": r["headSha"][:10], "created": r["createdAt"], "title": r["displayTitle"][:60], "url": r["url"]} for r in json.loads(gh(*a))]
    emit(rows, ["id", "status", "conclusion", "attempt", "event", "branch", "sha", "created", "title", "url"], args)

# ---------------- jobs ----------------
def jobs_of(run_id, attempt, repo):
    base = f"/actions/runs/{run_id}" + (f"/attempts/{attempt}" if attempt else "") + "/jobs?per_page=100&page="
    out, page = [], 1
    while True:
        d = api(base + str(page), repo); out += d["jobs"]
        if len(d["jobs"]) < 100: return out
        page += 1

def job_rows(js):
    return [{"job_id": j["id"], "status": j["status"], "conclusion": j["conclusion"], "name": j["name"], "started": j["started_at"],
             "duration": dur(j["started_at"], j["completed_at"]), "attempt": j["run_attempt"], "url": j["html_url"]} for j in js]

def cmd_jobs(args):
    repo = repo_of(args); rows = []
    for rid in ids(args.run_id, "run_id"): rows += [{"run_id": rid, **r} for r in job_rows(jobs_of(rid, args.attempt, repo))]
    rows.sort(key=lambda r: ({"failure": 0, "cancelled": 1, "timed_out": 1}.get(r["conclusion"] or "", 2), r["name"]))
    emit(rows, ["job_id", "run_id", "status", "conclusion", "name", "started", "duration", "attempt", "url"], args)

# ---------------- logs ----------------
def job_log(job_id, repo):
    raw = gh("api", f"/repos/{repo}/actions/jobs/{job_id}/logs")
    raw = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)  # ANSI colour codes from tool output
    return [re.sub(r"^\S+T\S+Z ", "", ln) for ln in raw.splitlines()]

def grep(lines, pat, ctx):
    rx = re.compile(pat); hits, out, last = [i for i, l in enumerate(lines) if rx.search(l)], [], -1
    for i in hits:
        lo, hi = max(0, i - ctx), min(len(lines), i + ctx + 1)
        if lo > last + 1 and out: out.append("--")
        for k in range(max(lo, last + 1), hi): out.append(f"{k+1}: {lines[k]}")
        last = hi - 1
    return out, len(hits)

def cmd_logs(args):
    repo = repo_of(args); jobs = ids(args.job_id, "job_id")
    for jid in jobs:
        if len(jobs) > 1: print(f"=== job {jid}")
        logs_one(job_log(jid, repo), args)

def logs_one(lines, args):
    if args.grep:
        out, n = grep(lines, args.grep, args.C); print("\n".join(out)); sys.stderr.write(f"{n} hits in {len(lines)} lines\n")
    else:
        print("\n".join(lines[-args.tail:])); sys.stderr.write(f"last {min(args.tail, len(lines))} of {len(lines)} lines\n")

def cmd_failed(args):
    repo = repo_of(args); bad = []
    for rid in ids(args.run_id, "run_id"): bad += [j for j in jobs_of(rid, args.attempt, repo) if j["conclusion"] in ("failure", "timed_out", "cancelled")]
    if not bad: sys.stderr.write("no failed jobs\n"); return
    for j in bad:
        print(f"=== {j['name']}  job {j['id']}  {j['conclusion']}  attempt {j['run_attempt']}  {j['html_url']}")
        steps = [s["name"] for s in j.get("steps", []) if s["conclusion"] == "failure"]
        if steps: print(f"failed steps: {', '.join(steps)}")
        out, n = grep(job_log(j["id"], repo), args.grep, args.C)
        print("\n".join(out[-args.max_lines:]) if out else "(no matching lines; try --grep or `jx ci logs <job-id>`)"); print()
    sys.exit(1)

# ---------------- dispatch / wait ----------------
def cmd_wait(args):
    deadline = time.time() + args.timeout; rc = 0
    for rid in ids(args.run_id, "run_id"):
        while True:
            d = json.loads(gh("run", "view", str(rid), "--json", "status,conclusion,url", *R(args)))
            if d["status"] == "completed":
                print(json.dumps({"run_id": rid, **d})); rc |= d["conclusion"] != "success"; break
            if time.time() > deadline: print(json.dumps({"run_id": rid, **d})); sys.stderr.write("timeout\n"); sys.exit(1)
            sys.stderr.write(f"{rid} {d['status']}… sleep {args.interval}s\n"); time.sleep(args.interval)
    sys.exit(rc)

def cmd_dispatch(args):
    repo = repo_of(args)
    ref = args.ref or sh(["git", "branch", "--show-current"]).strip()
    t0 = datetime.now(timezone.utc).isoformat()
    a = ["workflow", "run", args.workflow, "--ref", ref] + R(args)
    for kv in args.inputs: a += ["-f", kv]
    if args.dry_run: print("DRY RUN: gh " + " ".join(a) + f"\n  → triggers a real run of {args.workflow} on {ref}; drop --dry-run to do it"); return
    gh(*a)
    for _ in range(12):
        time.sleep(5)
        runs = json.loads(gh("run", "list", "--workflow", args.workflow, "--branch", ref, "--event", "workflow_dispatch", "--limit", "5",
                             "--json", "databaseId,createdAt,url,status", *R(args)))
        new = [r for r in runs if r["createdAt"] >= t0[:19]]
        if new:
            r = max(new, key=lambda r: r["createdAt"]); print(json.dumps({"run_id": r["databaseId"], "url": r["url"], "ref": ref}))
            if args.wait: args.run_id = r["databaseId"]; cmd_wait(args)
            return
    sys.exit("dispatched but no new run appeared in 60s; check `jx ci runs`")

def cmd_doctor(args):
    print(gh("auth", "status").strip().splitlines()[1].strip()); print("repo:", repo_of(args)); print("ok")

def add_globals(p, suppress=False):
    d = {"default": argparse.SUPPRESS} if suppress else {}
    p.add_argument("--repo", "-R", help="owner/name; default is the current checkout's origin", **d)
    p.add_argument("--json", action="store_true", help="raw JSON instead of TSV", **d)

def main():
    global CMD
    ap = argparse.ArgumentParser(prog="jx ci", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); add_globals(ap)
    g = argparse.ArgumentParser(add_help=False); add_globals(g, suppress=True)  # same flags accepted after the subcommand
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sp.add_parser("runs", parents=[g], help="recent runs of a workflow, newest first")
    p.add_argument("workflow", help="workflow file name (ci.yml) or display name"); p.add_argument("--branch", help="only runs on this branch"); p.add_argument("--limit", type=int, default=10, help="max runs (10)"); p.add_argument("--status", help="queued | in_progress | completed | success | failure | cancelled"); p.set_defaults(f=cmd_runs)
    p = sp.add_parser("jobs", parents=[g], help="jobs of a run, failed first")
    p.add_argument("run_id", help="an id, or - to read a run_id column from TSV on stdin"); p.add_argument("--attempt", type=int, help="read this attempt instead of the latest; 1 = the original failure after a rerun"); p.set_defaults(f=cmd_jobs)
    p = sp.add_parser("logs", parents=[g], help="one job's log, timestamps and ANSI stripped")
    p.add_argument("job_id", help="an id, or - to read a job_id column from TSV on stdin"); p.add_argument("--grep", help="python regex; prints matching lines with context instead of the tail"); p.add_argument("-C", type=int, default=2, help="context lines around each --grep hit (2)"); p.add_argument("--tail", type=int, default=200, help="lines to print without --grep (200)"); p.set_defaults(f=cmd_logs)
    p = sp.add_parser("failed", parents=[g], help="every failed job of a run: failed steps plus error-ish log lines in context")
    p.add_argument("run_id", help="an id, or - to read a run_id column from TSV on stdin"); p.add_argument("--attempt", type=int, help="read this attempt; 1 = the original failure after a rerun"); p.add_argument("--grep", default=ERRISH, help="regex for log lines to show (default matches error/traceback/failed/exit code)"); p.add_argument("-C", type=int, default=2, help="context lines around each hit (2)"); p.add_argument("--max-lines", type=int, default=120, help="cap on printed lines per job (120)"); p.set_defaults(f=cmd_failed)
    p = sp.add_parser("dispatch", parents=[g], help="workflow_dispatch a workflow and resolve the new run id; --dry-run shows the command")
    p.add_argument("workflow", help="workflow file name"); p.add_argument("--ref", help="branch or tag; default is the current branch"); p.add_argument("-f", dest="inputs", action="append", default=[], metavar="k=v", help="workflow input, repeatable"); p.add_argument("--wait", action="store_true", help="block until the run completes")
    p.add_argument("--dry-run", action="store_true", help="print the gh command, trigger nothing"); p.add_argument("--interval", type=int, default=30, help="seconds between polls when waiting (30)"); p.add_argument("--timeout", type=int, default=3600, help="give up waiting after this many seconds (3600)"); p.set_defaults(f=cmd_dispatch)
    p = sp.add_parser("wait", parents=[g], help="block until a run completes; exit 0 only on success")
    p.add_argument("run_id", help="an id, or - to read a run_id column from TSV on stdin"); p.add_argument("--interval", type=int, default=30, help="seconds between polls (30)"); p.add_argument("--timeout", type=int, default=3600, help="give up after this many seconds (3600)"); p.set_defaults(f=cmd_wait)
    sp.add_parser("doctor", parents=[g], help="auth status and resolved repo").set_defaults(f=cmd_doctor)
    args = ap.parse_args(); CMD = args.cmd; args.f(args)

if __name__ == "__main__": main()
