#!/usr/bin/env python3
"""jx temporal — Temporal lever for agents. Port-forwards the frontend for the call, runs the local `temporal` CLI with
JSON output, prints TSV. --json for the raw payload.

  jx temporal <env> count [--type T] [--status S] [--queue Q] [--since 6h] [--query Q] [--by-status]
                                              how many executions match; --by-status groups by ExecutionStatus
  jx temporal <env> list  [--type T] [--status S] [--queue Q] [--since 6h] [--query Q] [--limit 50]
                                              one row per execution: id, run, type, status, queue, start, close, duration
  jx temporal <env> describe <workflow-id> [--run R]  item/value: status, times, innermost failure + type, one pending_activity row per in-flight activity
  jx temporal <env> history  <workflow-id> [--run R] [--failures] [--grep REGEX] [--limit 200]
                                              one row per event: event_id, time, type, detail (activity, failure message, child id, marker)
  jx temporal <env> failed [--type T] [--queue Q] [--since 6h] [--limit 20] [--group]
                                              Failed/TimedOut/Terminated executions with the innermost failure; --group = failure, count, example_id
  jx temporal <env> queue <name> [--type workflow|activity|both]   pollers with last access age; exit 1 when nothing is listening
  jx temporal <env> schedules [ID]            all schedules: paused, type, next/last run, last result; one ID: spec plus last 5 actions
  jx temporal <env> doctor

<env> is the kubectl context unless ~/.config/jx-arsenal/temporal.json maps it:
  {"envs": {"prod": {"context": "prod-cluster", "k8s_namespace": "temporal", "service": "frontend-headless", "namespace": "default"}}}
Defaults: k8s namespace `temporal`, the first Service whose name ends in `frontend-headless`, Temporal namespace `default`.
Filters compose into a visibility query: WorkflowType, ExecutionStatus, TaskQueue, StartTime >= now-since, AND --query verbatim.
"""
import argparse, contextlib, io, json, os, re, signal, socket, subprocess, sys, time
from datetime import datetime, timedelta, timezone
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
CFG = os.path.expanduser("~/.config/jx-arsenal/temporal.json")

def env_of(name):
    try: envs = json.load(open(CFG))["envs"]
    except FileNotFoundError: envs = {}
    e = envs.get(name, {})
    return {"context": e.get("context", name), "k8s_namespace": e.get("k8s_namespace", "temporal"), "service": e.get("service", "frontend-headless"), "namespace": e.get("namespace", "default")}

def all_envs():
    if os.environ.get("T_ENVS"): return os.environ["T_ENVS"].split(",")
    try: return list(json.load(open(CFG))["envs"])
    except FileNotFoundError: sys.exit(f"`jx temporal all` needs T_ENVS=a,b,c or an envs map in {CFG}")

def kubectl(env, *args):
    p = subprocess.run(["kubectl", "--context", env["context"], "--request-timeout", "20s", "-n", env["k8s_namespace"], *args], text=True, capture_output=True, timeout=45)
    if p.returncode: sys.exit(p.stderr.strip() + f"\n→ `jx k {env['context']} doctor`; a cluster behind a VPN/tailnet needs it up first")
    return p.stdout

def free_port():
    with socket.socket() as s: s.bind(("127.0.0.1", 0)); return s.getsockname()[1]

@contextlib.contextmanager
def forward(env):
    """Port-forward the Temporal frontend Service for the duration of one call. The forward is per call so nothing
    leaks between invocations and two concurrent calls never share a port."""
    svcs = [s.split("/", 1)[1] for s in kubectl(env, "get", "svc", "-o", "name").split()]
    svc = next((s for s in svcs if s.endswith(env["service"])), None)
    if not svc: sys.exit(f"no Service ending in {env['service']!r} in namespace {env['k8s_namespace']} of {env['context']}; services: {', '.join(svcs) or 'none'}. Set k8s_namespace/service in {CFG}")
    port = free_port()
    p = subprocess.Popen(["kubectl", "--context", env["context"], "-n", env["k8s_namespace"], "port-forward", f"svc/{svc}", f"{port}:7233"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(40):
            if p.poll() is not None: sys.exit(f"port-forward exited: {p.stderr.read().strip()}\n→ `jx k {env['context']} pods -n {env['k8s_namespace']}` shows whether the frontend is up")
            with socket.socket() as s:
                s.settimeout(0.25)
                if s.connect_ex(("127.0.0.1", port)) == 0: break
            time.sleep(0.25)
        else: sys.exit("port-forward never accepted a connection in 10s")
        yield f"127.0.0.1:{port}"
    finally:
        p.terminate()
        try: p.wait(3)
        except subprocess.TimeoutExpired: p.kill()

def tcli(addr, env, *args):
    p = subprocess.run(["temporal", "--address", addr, "--namespace", env["namespace"], "-o", "json", *args], text=True, capture_output=True, timeout=120)
    if p.returncode:
        err = p.stderr.strip()
        hint = "→ visibility query syntax: WorkflowType=\"x\" AND ExecutionStatus=\"Failed\" AND StartTime >= \"2026-01-01T00:00:00Z\"; `jx temporal <env> list --limit 3` shows real values" if re.search(r"invalid query|parse|syntax", err, re.I) else \
               "→ workflow id not found in this namespace; `jx temporal <env> list --type T --since 1d` lists ids" if re.search(r"not found|NotFound", err) else ""
        sys.exit(err + (f"\n{hint}" if hint else ""))
    return json.loads(p.stdout) if p.stdout.strip() else {}

def since(s):
    m = re.fullmatch(r"(\d+)([mhd])", s or "")
    if not m: sys.exit(f"bad duration {s!r}; use 30m, 6h, 7d")
    return datetime.now(timezone.utc) - timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]: int(m.group(1))})

def build_query(a):
    parts = []
    if a.type: parts.append(f'WorkflowType="{a.type}"')
    if getattr(a, "status", None): parts.append(f'ExecutionStatus="{a.status}"')
    if a.queue: parts.append(f'TaskQueue="{a.queue}"')
    if a.since: parts.append(f'StartTime >= "{since(a.since).strftime("%Y-%m-%dT%H:%M:%SZ")}"')
    if a.query: parts.append(f"({a.query})")
    return " AND ".join(parts)

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, a, raw=None):
    if a.json: print(json.dumps(raw if raw is not None else rows, indent=1))
    else: tsv(rows, header)
def short_status(s): return (s or "").replace("WORKFLOW_EXECUTION_STATUS_", "").title().replace("_", "")
def ts(s): return s[:19] + "Z" if s else ""
def parse_ts(s): return datetime.fromisoformat(s[:26].rstrip("Z") + "+00:00")
def dur(a, b): return f"{int((parse_ts(b) - parse_ts(a)).total_seconds())}s" if a and b else ""
def ago(s): return int((datetime.now(timezone.utc) - parse_ts(s)).total_seconds()) if s else ""
def run_args(a): return ["--run-id", a.run] if a.run else []

def innermost(f):
    # Outer layers only say "Child Workflow execution failed" / "Activity task failed"; the leaf cause is the real reason.
    while f and f.get("cause"): f = f["cause"]
    return f or {}
def leaf_message(f):
    leaf = innermost(f)
    return leaf.get("message") or (leaf.get("timeoutFailureInfo") or {}).get("timeoutType", "").replace("TIMEOUT_TYPE_", "")
def attrs_of(ev): return next((v for k, v in ev.items() if k.endswith("EventAttributes")), {})

def close_failure(d):
    """(message, type) from a describe payload's closeEvent: innermost failure cause, a termination reason, or ''."""
    ev = d.get("closeEvent") or {}; at = attrs_of(ev)
    if at.get("failure"):
        leaf = innermost(at["failure"]); return leaf_message(at["failure"]), (leaf.get("applicationFailureInfo") or {}).get("type") or leaf.get("source", "")
    if at.get("reason"): return at["reason"], "Terminated"
    if "TIMED_OUT" in ev.get("eventType", ""): return "workflow execution timed out", "TimedOut"
    return "", ""

def cmd_count(a):
    env = env_of(a.env); q = build_query(a)
    with forward(env) as addr:
        if a.by_status:
            d = tcli(addr, env, "workflow", "count", "--query", (q + " " if q else "") + "GROUP BY ExecutionStatus")
            rows = [{"status": g["groupValues"][0], "count": int(g["count"])} for g in d.get("groups", [])]
            rows.sort(key=lambda r: -r["count"]); emit(rows, ["status", "count"], a, d); sys.stderr.write(f"total {d.get('count')}  query: {q or '(all)'}\n")
        else:
            d = tcli(addr, env, "workflow", "count", *(["--query", q] if q else []))
            print(f"count\n{d.get('count')}"); sys.stderr.write(f"query: {q or '(all)'}\n")

def rows_of(execs):
    return [{"workflow_id": e["execution"]["workflowId"], "run_id": e["execution"]["runId"][:8], "type": e["type"]["name"], "status": short_status(e.get("status")),
             "queue": e.get("taskQueue", ""), "start": ts(e.get("startTime")), "close": ts(e.get("closeTime")), "duration": dur(e.get("startTime"), e.get("closeTime"))} for e in execs]

def cmd_list(a):
    env = env_of(a.env); q = build_query(a)
    with forward(env) as addr:
        d = tcli(addr, env, "workflow", "list", "--limit", str(a.limit), *(["--query", q] if q else []))
    rows = rows_of(d or []); emit(rows, ["workflow_id", "run_id", "type", "status", "queue", "start", "close", "duration"], a, d)
    sys.stderr.write(f"{len(rows)} executions  query: {q or '(all)'}\n")

def cmd_describe(a):
    env = env_of(a.env)
    with forward(env) as addr: d = tcli(addr, env, "workflow", "describe", "--workflow-id", a.workflow_id, *run_args(a))
    i = d.get("workflowExecutionInfo", {}); x = i.get("execution", {}); msg, typ = close_failure(d)
    items = [("workflow_id", x.get("workflowId")), ("run_id", x.get("runId")), ("type", i.get("type", {}).get("name")), ("status", short_status(i.get("status"))),
             ("queue", i.get("taskQueue") or d.get("executionConfig", {}).get("taskQueue", {}).get("name", "")), ("start", ts(i.get("startTime"))), ("close", ts(i.get("closeTime"))),
             ("duration", dur(i.get("startTime"), i.get("closeTime"))), ("history_length", i.get("historyLength", "")), ("failure", msg), ("failure_type", typ)]
    for p in d.get("pendingActivities") or []:
        items.append(("pending_activity", f"{p.get('activityType', {}).get('name')} attempt={p.get('attempt', 1)} state={(p.get('state') or '').replace('PENDING_ACTIVITY_STATE_', '')} last_failure={leaf_message(p.get('lastFailure'))[:120]}"))
    emit([{"item": k, "value": v} for k, v in items], ["item", "value"], a, d)

def event_detail(e):
    t, at = e.get("eventType", ""), attrs_of(e)
    if "ACTIVITY_TASK_SCHEDULED" in t: return f"{at.get('activityType', {}).get('name')} id={at.get('activityId')}"
    if at.get("failure"): return leaf_message(at["failure"])
    if at.get("timeoutType"): return at["timeoutType"].replace("TIMEOUT_TYPE_", "")
    if "CHILD_WORKFLOW" in t: return f"{at.get('workflowExecution', {}).get('workflowId') or at.get('workflowId', '')} {at.get('workflowType', {}).get('name', '')}".strip()
    if "MARKER" in t: return at.get("markerName", "")
    return at.get("reason", "")

def cmd_history(a):
    env = env_of(a.env)
    with forward(env) as addr: d = tcli(addr, env, "workflow", "show", "--workflow-id", a.workflow_id, *run_args(a))
    evs = d.get("events") or []
    rows = [{"event_id": e.get("eventId"), "time": ts(e.get("eventTime")), "type": e.get("eventType", "").replace("EVENT_TYPE_", ""), "detail": event_detail(e)} for e in evs]
    if a.failures: rows = rows[:1] + [r for r in rows[1:] if re.search(r"FAILED|TIMED_OUT|TERMINATED|CANCELED", r["type"])]
    if a.grep: rx = re.compile(a.grep); rows = [r for r in rows if rx.search(f"{r['type']} {r['detail']}")]
    n = len(rows); rows = rows[-a.limit:]
    emit(rows, ["event_id", "time", "type", "detail"], a, d)
    sys.stderr.write(f"{len(rows)} shown of {n} matching ({len(evs)} events in history)  → `jx temporal {a.env} describe {a.workflow_id}` for the summary\n")

def cmd_failed(a):
    env = env_of(a.env); q = " AND ".join(filter(None, [build_query(a), 'ExecutionStatus IN ("Failed", "TimedOut", "Terminated")']))
    with forward(env) as addr:
        execs = tcli(addr, env, "workflow", "list", "--limit", str(a.limit), "--query", q) or []
        for e in execs: e["failure"] = close_failure(tcli(addr, env, "workflow", "describe", "--workflow-id", e["execution"]["workflowId"], "--run-id", e["execution"]["runId"]))[0][:120]
    rows = [{"workflow_id": e["execution"]["workflowId"], "type": e["type"]["name"], "close": ts(e.get("closeTime")), "status": short_status(e.get("status")), "failure": e["failure"]} for e in execs]
    if a.group:
        groups = {}
        for r in rows: groups.setdefault(r["failure"], {"failure": r["failure"], "count": 0, "example_id": r["workflow_id"]})["count"] += 1
        rows = sorted(groups.values(), key=lambda r: -r["count"]); emit(rows, ["failure", "count", "example_id"], a, execs)
    else: emit(rows, ["workflow_id", "type", "close", "status", "failure"], a, execs)
    sys.stderr.write(f"{len(execs)} executions described, capped at --limit {a.limit}  query: {q}  → `jx temporal {a.env} history <workflow_id> --failures` for one\n")

def cmd_queue(a):
    env = env_of(a.env); kinds = ["workflow", "activity"] if a.type == "both" else [a.type]; rows, raw = [], {}
    with forward(env) as addr:
        for k in kinds: raw[k] = tcli(addr, env, "task-queue", "describe", "--task-queue", a.name, "--task-queue-type", k)
    for k in kinds:
        for p in raw[k].get("pollers") or []:
            rows.append({"kind": k, "identity": p.get("identity"), "last_access": ts(p.get("lastAccessTime")), "seconds_ago": ago(p.get("lastAccessTime")), "rate": p.get("ratePerSecond")})
    if not rows: sys.exit(f"no pollers: nothing is listening on this queue\n→ `jx temporal {a.env} list --limit 5` shows queue names that have executions")
    rows.sort(key=lambda r: (r["kind"], r["seconds_ago"])); emit(rows, ["kind", "identity", "last_access", "seconds_ago", "rate"], a, raw)
    n = {k: sum(r["kind"] == k for r in rows) for k in kinds}; backlog = {k: (raw[k].get("stats") or [{}])[0].get("approximateBacklogCount", "?") for k in kinds}
    sys.stderr.write(", ".join(f"{n[k]} {k} pollers (backlog {backlog[k]})" for k in kinds) + f", oldest access {max(r['seconds_ago'] for r in rows)}s ago\n")

def sched_rows(scheds):
    rows = []
    for s in scheds:
        i = s.get("info", {}); last = (i.get("recentActions") or [{}])[-1]
        rows.append({"schedule_id": s.get("scheduleId"), "paused": bool(i.get("paused")), "workflow_type": i.get("workflowType", {}).get("name", ""), "next_run": ts((i.get("futureActionTimes") or [""])[0]),
                     "last_run": ts(last.get("actualTime")), "recent_result": short_status(last.get("startWorkflowStatus"))})
    return rows

def cmd_schedules(a):
    env = env_of(a.env)
    with forward(env) as addr: d = tcli(addr, env, "schedule", *(["describe", "--schedule-id", a.schedule_id] if a.schedule_id else ["list"]))
    if not a.schedule_id:
        rows = sched_rows(d or []); emit(rows, ["schedule_id", "paused", "workflow_type", "next_run", "last_run", "recent_result"], a, d)
        sys.stderr.write(f"{len(rows)} schedules  → `jx temporal {a.env} schedules <schedule_id>` for spec and recent actions\n"); return
    sc, i = d.get("schedule", {}), d.get("info", {}); sw = sc.get("action", {}).get("startWorkflow", {}); pol = sc.get("policies", {})
    items = [("schedule_id", a.schedule_id), ("paused", bool(sc.get("state", {}).get("paused"))), ("notes", sc.get("state", {}).get("notes", "")), ("workflow_type", sw.get("workflowType", {}).get("name", "")),
             ("workflow_id", sw.get("workflowId", "")), ("queue", sw.get("taskQueue", {}).get("name", "")), ("overlap_policy", pol.get("overlapPolicy", "").replace("SCHEDULE_OVERLAP_POLICY_", "")), ("catchup_window", pol.get("catchupWindow", "")),
             ("spec", json.dumps(sc.get("spec", {}), separators=(",", ":"))), ("created", ts(i.get("createTime"))), ("action_count", i.get("actionCount", "0")), ("overlap_skipped", i.get("overlapSkipped", "0")),
             ("running", " ".join(r.get("workflowId", "") for r in i.get("runningWorkflows") or [])), ("next_runs", " ".join(ts(t) for t in (i.get("futureActionTimes") or [])[:3]))]
    for r in (i.get("recentActions") or [])[-5:]:
        items.append(("action", f"{ts(r.get('scheduleTime'))} {r.get('startWorkflowResult', {}).get('workflowId', '')} {short_status(r.get('startWorkflowStatus'))}"))
    emit([{"item": k, "value": v} for k, v in items], ["item", "value"], a, d)

def cmd_doctor(a):
    env = env_of(a.env)
    with forward(env) as addr:
        d = tcli(addr, env, "operator", "cluster", "describe")
        n = tcli(addr, env, "operator", "namespace", "describe", "-n", env["namespace"])
    print(f"env={a.env} context={env['context']} k8s_namespace={env['k8s_namespace']} address={addr} server={d.get('serverVersion', '?')} namespace={env['namespace']} retention={n.get('config', {}).get('workflowExecutionRetentionTtl', '?')} map={'yes' if os.path.exists(CFG) else 'no'}")

def add_filters(p, status=True):
    p.add_argument("--type", help="WorkflowType")
    if status: p.add_argument("--status", help="ExecutionStatus: Running, Completed, Failed, Canceled, Terminated, ContinuedAsNew, TimedOut")
    p.add_argument("--queue", help="TaskQueue"); p.add_argument("--since", help="StartTime within the last 30m, 6h, 7d"); p.add_argument("--query", help="extra visibility query, ANDed verbatim")

def main():
    ap = argparse.ArgumentParser(prog="jx temporal", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("env", help="environment: kubectl context or a key in the map file, or `all`"); ap.add_argument("--json", action="store_true", help="raw CLI JSON instead of TSV")
    g = argparse.ArgumentParser(add_help=False); g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw CLI JSON instead of TSV")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sp.add_parser("count", parents=[g], help="count executions matching the filters"); add_filters(p); p.add_argument("--by-status", action="store_true", help="group counts by ExecutionStatus"); p.set_defaults(f=cmd_count)
    p = sp.add_parser("list", parents=[g], help="executions matching the filters, newest first"); add_filters(p); p.add_argument("--limit", type=int, default=50, help="max executions (50)"); p.set_defaults(f=cmd_list)
    p = sp.add_parser("describe", parents=[g], help="one execution: status, times, innermost failure, pending activities"); p.add_argument("workflow_id", help="workflow id; `jx temporal <env> list` shows ids"); p.add_argument("--run", help="run id (latest run)"); p.set_defaults(f=cmd_describe)
    p = sp.add_parser("history", parents=[g], help="event history of one execution, one row per event"); p.add_argument("workflow_id", help="workflow id"); p.add_argument("--run", help="run id (latest run)")
    p.add_argument("--failures", action="store_true", help="only *FAILED, *TIMED_OUT, *TERMINATED, *CANCELED events plus the first"); p.add_argument("--grep", help="python regex on type and detail"); p.add_argument("--limit", type=int, default=200, help="last N events after filtering (200)"); p.set_defaults(f=cmd_history)
    p = sp.add_parser("failed", parents=[g], help="Failed/TimedOut/Terminated executions with their innermost failure; --group counts by failure"); add_filters(p, status=False)
    p.add_argument("--limit", type=int, default=20, help="max executions described, one CLI call each (20)"); p.add_argument("--group", action="store_true", help="rows failure, count, example_id instead of one row per execution"); p.set_defaults(f=cmd_failed)
    p = sp.add_parser("queue", parents=[g], help="who is polling a task queue, with last access age; exit 1 when nobody is"); p.add_argument("name", help="task queue name; `jx temporal <env> list` shows queues")
    p.add_argument("--type", choices=["workflow", "activity", "both"], default="both", help="poller kind (both)"); p.set_defaults(f=cmd_queue)
    p = sp.add_parser("schedules", parents=[g], help="schedules with paused flag, next/last run and last result; one id gives spec and recent actions"); p.add_argument("schedule_id", nargs="?", help="one schedule for its spec and last 5 actions (all schedules)"); p.set_defaults(f=cmd_schedules)
    sp.add_parser("doctor", parents=[g], help="port-forward works, server version, namespace retention").set_defaults(f=cmd_doctor)
    a = ap.parse_args()
    if a.env != "all": a.f(a); return
    first = True
    for env in all_envs():
        a.env = env; buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try: a.f(a)
            except SystemExit as e: sys.stderr.write(f"[{env}] {e}\n"); continue
        lines = buf.getvalue().splitlines()
        if a.json or a.cmd == "doctor": print(buf.getvalue(), end=""); continue
        if first and lines: print("env\t" + lines[0]); first = False
        for ln in lines[1:]: print(f"{env}\t{ln}")

if __name__ == "__main__": main()
