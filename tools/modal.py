#!/usr/bin/env python3
"""jx modal — Modal lever for agents. TSV by default, --json for the raw CLI payload. Read-only: no deploy, stop or rollback.

  jx modal [-p PROFILE] [-e ENV] <command>
  jx modal apps [--state deployed|stopped|all]        apps with state, live task count, created (deployed)
  jx modal containers [APP] [--count] [--watch SECS] [--interval 5]
                                                       running containers; --count per app; --watch samples the count over time
  jx modal logs <app|ap-ID|ta-ID|-> [--since 10m] [--tail N] [--grep REGEX] [-C 2] [--search TEXT] [--source S] [--for SECS]
                                                       past log lines with UTC timestamps; --for follows live for SECS seconds
  jx modal history <app>                               deploy history: version, when, who, commit
  jx modal cost [--for RANGE | --since 7d] [--daily | --hourly] [--top 15] [--tag NAMES]
                                                       spend per app with share; --daily/--hourly keeps the time axis
  jx modal ls <envs|secrets|volumes>                   names with created / last used
  jx modal whoami                                      active profile, workspace, environments

Every timestamp is printed in UTC; the Modal CLI prints local time with an offset.
"""
import argparse, json, re, signal, subprocess, sys, time
from datetime import datetime, timedelta, timezone
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
ENV_AWARE = {("app", "list"), ("app", "logs"), ("app", "history"), ("container", "list"), ("secret", "list"), ("volume", "list")}
CMD = ""

def utc(s):
    """Any Modal CLI timestamp to ISO seconds Z. A value with no offset is already UTC (billing intervals)."""
    if not s: return ""
    try: dt = datetime.fromisoformat(str(s).strip().replace(" ", "T", 1))
    except ValueError: return str(s)
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def since(s):
    """30m, 6h, 7d, 2w: that long ago, in UTC. Anything else is parsed as an ISO time (a naive one is UTC)."""
    m = re.fullmatch(r"(\d+)([smhdw])", s or "")
    if m: return datetime.now(timezone.utc) - timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)]: int(m.group(1))})
    try: dt = datetime.fromisoformat((s or "").replace("Z", "+00:00")); return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError: sys.exit(f"bad time {s!r}; use 30m, 6h, 7d, 2w, or an ISO time like 2026-01-31T00:00:00Z")

def modal(a, *args, parse=True, timeout=120):
    cmd = ["modal", *args] + (["--json"] if parse else [])
    if a.env and tuple(args[:2]) in ENV_AWARE: cmd += ["--env", a.env]
    if a.profile: cmd += ["--profile", a.profile]
    try: p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    except FileNotFoundError: sys.exit("`modal` not found on PATH; install the Modal CLI, then `jx modal whoami`")
    except subprocess.TimeoutExpired as e: return (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or "")
    if p.returncode:
        err = p.stderr.strip() or p.stdout.strip()
        framed = re.findall(r"│\s*(.*?)\s*│", err)  # the CLI draws errors in a box; keep the text
        err = (" ".join(x for x in framed if x) or err)[-600:]
        # order matters: a missing app's message also names the environment it was looked up in
        hint = "→ `jx modal whoami` shows the profile; `modal token new --profile <p>` re-authenticates" if re.search(r"token|unauthenticated|credential", err, re.I) else \
               "→ `jx modal ls envs` lists environments; pass -e ENV" if re.search(r"environment '?[\w-]+'? (not found|does not exist)|no such environment", err, re.I) else \
               "→ `jx modal apps --state all` lists app names and ids; `jx modal containers` lists ta-* ids" if re.search(r"not found|no such|could not find|does not exist|No \w+ with (name|id) .* found", err, re.I) else ""
        sys.exit(err + (f"\n{hint}" if hint else ""))
    return json.loads(p.stdout) if parse and p.stdout.strip() else p.stdout

def ids_arg(arg, col):
    """One value, or every value from a TSV on stdin when arg is `-` (column `col` when the header has it, else the first column)."""
    if arg != "-": return [arg]
    lines = [l.rstrip("\n") for l in sys.stdin if l.strip()]
    if not lines: sys.exit(f"nothing on stdin; expected a TSV with a `{col}` column")
    head = lines[0].split("\t"); i = head.index(col) if col in head else 0
    out = []
    for l in (lines[1:] if col in head else lines):
        v = l.split("\t")[i].strip()
        if v and v not in out: out.append(v)
    if not out: sys.exit(f"no `{col}` values on stdin")
    return out

def each(a, attr, col, fn, fix=lambda v: v):
    vals = ids_arg(getattr(a, attr), col)
    for i, v in enumerate(vals):
        if len(vals) > 1: print(("" if i == 0 else "\n") + f"=== {v}"); sys.stdout.flush()
        setattr(a, attr, fix(v)); fn(a)
STDIN_HELP = "or - to read ids from a TSV on stdin (column `{}`, else the first column)"

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, a, raw=None): print(json.dumps(raw if raw is not None else rows, indent=1)) if a.json else tsv(rows, header)

def cmd_apps(a):
    d = modal(a, "app", "list")
    rows = [{"app_id": x["app_id"], "app": x["description"], "state": x["state"], "tasks": int(x.get("tasks") or 0), "created": utc(x.get("created_at")), "stopped": utc(x.get("stopped_at"))}
            for x in d if a.state == "all" or x["state"] == a.state]
    rows.sort(key=lambda r: (-r["tasks"], r["app"]))
    emit(rows, ["app_id", "app", "state", "tasks", "created", "stopped"], a, d); sys.stderr.write(f"{len(rows)} apps ({a.state}), {sum(r['tasks'] for r in rows)} tasks\n")

def containers(a):
    d = modal(a, "container", "list"); now = datetime.now(timezone.utc)
    rows = [{"container_id": c["container_id"], "app": c["app_name"], "app_id": c["app_id"], "started": utc(c["start_time"]),
             "age_s": int((now - datetime.fromisoformat(utc(c["start_time"]).replace("Z", "+00:00"))).total_seconds())} for c in d]
    return [r for r in rows if not a.app or a.app in (r["app"], r["app_id"])], d

def counts(rows):
    out = {}
    for r in rows: out[r["app"]] = out.get(r["app"], 0) + 1
    return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))

def cmd_containers(a):
    if a.watch:
        print("time\tapp\tcontainers"); end = time.time() + a.watch
        while True:
            rows, _ = containers(a); t = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            for app, n in counts(rows) or [(a.app or "(none)", 0)]: print(f"{t}\t{app}\t{n}")
            sys.stdout.flush()
            if time.time() + a.interval > end: return
            time.sleep(a.interval)
    rows, raw = containers(a)
    if a.count: emit([{"app": k, "containers": v} for k, v in counts(rows)], ["app", "containers"], a, raw)
    else: rows.sort(key=lambda r: (r["app"], -r["age_s"])); emit(rows, ["container_id", "app", "app_id", "started", "age_s"], a, raw)
    sys.stderr.write(f"{len(rows)} containers in {len(counts(rows))} apps\n")

LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d(?:\.\d+)?[+-]\d\d:\d\d) (.*)$")
def norm_line(line):
    m = LINE.match(line)
    return f"{utc(m.group(1))} {m.group(2)}" if m else line

def cmd_logs(a):
    if not a.target.strip(): sys.exit("empty target; `jx modal apps` lists app names, `jx modal containers` lists ta-* ids")
    kind = "container" if a.target.startswith("ta-") else "app"
    args = [kind, "logs", a.target, "--timestamps"] + (["--show-container-id"] if kind == "app" else [])
    if a.for_: args += ["-f"]
    else:  # --tail alone means "the last N lines whenever they were"; a default window would hide an idle container's log
        window = a.since or (None if a.tail else "10m")
        args += (["--since", window] if window else []) + (["--tail", str(a.tail)] if a.tail else [])
    if a.search: args += ["--search", a.search]
    if a.source: args += ["--source", a.source]
    out = modal(a, *args, parse=False, timeout=a.for_ or 120)
    lines = [norm_line(l) for l in out.splitlines() if l.strip()]
    if a.grep:
        rx = re.compile(a.grep); hits = [i for i, l in enumerate(lines) if rx.search(l)]; last = -1
        for i in hits:
            lo, hi = max(0, i - a.C), min(len(lines), i + a.C + 1)
            if lo > last + 1 and last >= 0: print("--")
            for k in range(max(lo, last + 1), hi): print(lines[k])
            last = hi - 1
        sys.stderr.write(f"{len(hits)} hits in {len(lines)} lines\n")
    else:
        print("\n".join(lines)); sys.stderr.write(f"{len(lines)} lines" + (f" in {a.for_}s of following" if a.for_ else f" since {a.since}" if a.since else "") + "\n")

def cmd_history(a):
    d = modal(a, "app", "history", a.app)
    rows = [{"version": x.get("version"), "deployed": utc(x.get("time_deployed")), "by": x.get("deployed_by"), "commit": x.get("commit"), "tag": x.get("tag"), "client": x.get("client")} for x in d]
    emit(rows, ["version", "deployed", "by", "commit", "tag", "client"], a, d)

def cmd_cost(a):
    args = ["billing", "report"] + (["--start", since(a.since).strftime("%Y-%m-%d")] if a.since else ["--for", a.for_ or "this month"])
    if a.hourly: args += ["-r", "h"]
    if a.tag: args += ["--tag-names", a.tag]
    d = modal(a, *args); total = sum(float(x["cost"]) for x in d); per = {}
    for x in d: per[(x["description"], x["environment"])] = per.get((x["description"], x["environment"]), 0.0) + float(x["cost"])
    top = sorted(per.items(), key=lambda kv: -kv[1])[:a.top]; keep = {k for k, _ in top}
    if a.daily or a.hourly:
        rows = [{"interval": utc(x["interval_start"]), "app": x["description"], "environment": x["environment"], "cost": round(float(x["cost"]), 2)} for x in d if (x["description"], x["environment"]) in keep]
        rows.sort(key=lambda r: (r["interval"], -r["cost"])); emit(rows, ["interval", "app", "environment", "cost"], a, d)
    else:
        rows = [{"app": k[0], "environment": k[1], "cost": round(v, 2), "share": f"{100 * v / total:.1f}%" if total else ""} for k, v in top]
        emit(rows, ["app", "environment", "cost", "share"], a, d)
    sys.stderr.write(f"total {total:.2f} USD over {len(per)} apps ({'since ' + a.since if a.since else a.for_ or 'this month'}); top {len(top)} shown\n")

def cmd_ls(a):
    kind = {"envs": ("environment", "list"), "secrets": ("secret", "list"), "volumes": ("volume", "list")}[a.kind]
    d = modal(a, *kind); cols = [c for c in ("name", "created_at", "last_used_at", "created_by", "active", "web_suffix") if d and c in d[0]]
    rows = [{c: (utc(x[c]) if c.endswith("_at") else x[c]) for c in cols} for x in d]
    emit(rows, cols, a, d); sys.stderr.write(f"{len(rows)} {a.kind}\n")

def cmd_whoami(a):
    cur = modal(a, "profile", "current", parse=False).strip(); envs = modal(a, "environment", "list")
    print(f"profile={a.profile or cur}\tactive_profile={cur}\tenvironments={','.join(e['name'] for e in envs)}\tdefault_env={next((e['name'] for e in envs if str(e.get('active')) == 'True'), '')}")

def add_globals(p, suppress=False):
    d = {"default": argparse.SUPPRESS} if suppress else {}
    p.add_argument("-p", "--profile", help="Modal profile (the active one)", **d); p.add_argument("-e", "--env", help="Modal environment (the profile's default)", **d)
    p.add_argument("--json", action="store_true", help="raw CLI JSON instead of TSV", **d)

def main(argv=None):
    global CMD
    ap = argparse.ArgumentParser(prog="jx modal", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); add_globals(ap)
    g = argparse.ArgumentParser(add_help=False); add_globals(g, suppress=True)
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sp.add_parser("apps", parents=[g], help="apps with state and live task count"); p.add_argument("--state", default="deployed", choices=["deployed", "stopped", "all"], help="(deployed)"); p.set_defaults(f=cmd_apps)
    p = sp.add_parser("containers", parents=[g], help="running containers, per-app counts, or the count sampled over time"); p.add_argument("app", nargs="?", help="app name or ap-ID (all apps)")
    p.add_argument("--count", action="store_true", help="containers per app"); p.add_argument("--watch", type=int, metavar="SECS", help="sample the per-app count for this many seconds (off)"); p.add_argument("--interval", type=int, default=5, help="seconds between samples (5)"); p.set_defaults(f=cmd_containers)
    p = sp.add_parser("logs", parents=[g], help="log lines of an app or one container, UTC timestamps"); p.add_argument("target", help="app name, ap-ID or ta-ID, " + STDIN_HELP.format("container_id"))
    p.add_argument("--since", help="window start: 30m, 6h, 2d or ISO (10m, or unbounded when --tail is given)"); p.add_argument("--tail", type=int, help="only the last N lines (all in the window)"); p.add_argument("--grep", help="python regex, client-side, with context")
    p.add_argument("-C", type=int, default=2, help="context lines around each --grep hit (2)"); p.add_argument("--search", help="server-side text filter"); p.add_argument("--source", choices=["stdout", "stderr", "system"], help="only this stream (all)")
    p.add_argument("--for", dest="for_", type=int, metavar="SECS", help="follow live for this many seconds instead of reading the past (off)"); p.set_defaults(f=lambda a: each(a, "target", "container_id", cmd_logs))
    p = sp.add_parser("history", parents=[g], help="deploy history of an app"); p.add_argument("app", help="app name or ap-ID"); p.set_defaults(f=cmd_history)
    p = sp.add_parser("cost", parents=[g], help="spend per app, or per interval with --daily/--hourly"); x = p.add_mutually_exclusive_group()
    x.add_argument("--for", dest="for_", help="today, yesterday, this week, last week, this month, last month (this month)"); x.add_argument("--since", help="window start: 7d, 30d")
    p.add_argument("--daily", action="store_true", help="one row per app per day"); p.add_argument("--hourly", action="store_true", help="one row per app per hour"); p.add_argument("--top", type=int, default=15, help="largest N apps (15)"); p.add_argument("--tag", help="comma-separated tag names to include"); p.set_defaults(f=cmd_cost)
    p = sp.add_parser("ls", parents=[g], help="environments, secrets or volumes"); p.add_argument("kind", choices=["envs", "secrets", "volumes"], help="what to list"); p.set_defaults(f=cmd_ls)
    sp.add_parser("whoami", parents=[g], aliases=["doctor"], help="active profile and environments").set_defaults(f=cmd_whoami)
    a = ap.parse_args(argv); CMD = a.cmd; a.f(a)

if __name__ == "__main__": main()
