#!/usr/bin/env python3
"""jx k — kubectl lever for agents, one environment per call. TSV by default, --json for raw kubectl JSON.

  jx k <env> pods [-n NS | -A] [-l SELECTOR]        name, ready, status/waiting reason, restarts, age, node (unhealthy first)
  jx k <env> images [DEPLOY] [-n NS] [--env-var NAME]  image per container of each deployment, plus one env var (default GIT_SHA)
  jx k <env> nodes [--pools]                        nodes with instance type, zone, pool, capacity type, age; --pools = nodeclaims by pool
  jx k <env> events [-n NS | -A] [--warn]           events newest first; --warn = Warning only
  jx k <env> logs <deploy/NAME | pod/NAME | -> [-n NS] [--since 10m] [--grep PAT] [-C N] [--tail N]
  jx k <env> rollout <deploy/NAME> [-n NS] [--timeout 300s]
  jx k <env> secret <NAME> <KEY> [-n NS]            one key, base64-decoded, to stdout
  jx k <env> doctor
  jx k all <subcommand ...>                         run against every env in $K_ENVS (comma list) or the map file

<env> is the kubectl context name unless ~/.config/jx-arsenal/k.json maps it:
  {"envs": {"prod": {"context": "arn:aws:eks:...:cluster/x"}, "stage": {"context": "stage-cluster"}}}
The file also defines the env list `jx k all` uses. Nothing cluster-specific lives in this script.
"""
import argparse, base64, contextlib, io, json, os, re, subprocess, sys, signal
signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # `| head` must not traceback
from datetime import datetime, timezone

CFG = os.path.expanduser("~/.config/jx-arsenal/k.json")

def cfg():
    try: return json.load(open(CFG))
    except FileNotFoundError: return {"envs": {}}

def context_of(env):
    return cfg()["envs"].get(env, {}).get("context", env)

def all_envs():
    if os.environ.get("K_ENVS"): return os.environ["K_ENVS"].split(",")
    envs = list(cfg()["envs"])
    if not envs: sys.exit(f"`jx k all` needs K_ENVS=a,b,c or an envs map in {CFG}")
    return envs

CMD = ""  # current subcommand, for error hints
NOT_FOUND = {
    "images": "deployment not found in that namespace. `jx k <env> images -n <ns>` lists every deployment; `jx k <env> pods -A | cut -f1 | sort -u` lists namespaces.",
    "logs": "target not found. Use kind/name (deploy/x, pod/x, sts/x) and the right -n; `jx k <env> pods -n <ns>` lists pods, `jx k <env> images -n <ns>` lists deployments.",
    "rollout": "deployment not found. `jx k <env> images -n <ns>` lists deployments in that namespace.",
    "secret": "secret not found in that namespace. `kubectl --context <ctx> -n <ns> get secrets` lists names; check -n.",
}

def check_ns(env, ns):
    """A query scoped to a namespace that does not exist returns an empty list, not an error. Make it an error."""
    if ns and subprocess.run(["kubectl", "--context", context_of(env), "--request-timeout", "20s", "get", "ns", ns], capture_output=True).returncode:
        sys.exit(f"namespace {ns!r} does not exist in {env}. `jx k {env} pods -A | cut -f1 | sort -u` lists namespaces.")

def kubectl(args, env, ns=None, all_ns=False, ok=(0,)):
    # namespace flags go AFTER the verb: kubectl treats a leading -A as a plugin name
    cmd = ["kubectl", "--context", context_of(env), "--request-timeout", "20s"] + args + (["-A"] if all_ns else ["-n", ns] if ns else [])
    try: p = subprocess.run(cmd, text=True, capture_output=True, timeout=int(os.environ.get("K_TIMEOUT", "45")))
    except subprocess.TimeoutExpired:  # kubectl's discovery retries ignore --request-timeout; bound the whole call
        sys.exit(f"{env}: no answer in {os.environ.get('K_TIMEOUT', '45')}s (VPN/tailnet down? wrong context?)  → run `jx k {env} doctor`")
    if p.returncode not in ok:
        err = p.stderr.strip()
        if re.search(r"context|credential|Unauthorized|token|exec plugin|forbidden", err, re.I): hint = f"→ run `jx k {env} doctor`; a cluster behind a VPN/tailnet needs it up first"
        elif re.search(r"NotFound|not found", err): hint = "→ " + NOT_FOUND.get(CMD, f"`jx k {env} pods -A` shows what exists")
        else: hint = ""
        sys.exit(f"{err}\n{hint}" if hint else err)
    return p.stdout

def kget(kind, env, ns=None, all_ns=False, extra=()):
    return json.loads(kubectl(["get", kind, "-o", "json", *extra], env, ns, all_ns)).get("items", [])

def age(ts):
    if not ts: return ""
    s = int((datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds())
    return f"{s//86400}d" if s >= 86400 else f"{s//3600}h" if s >= 3600 else f"{s//60}m" if s >= 60 else f"{s}s"

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

def emit(rows, header, args, raw=None):
    if args.json: print(json.dumps(raw if raw is not None else rows, indent=1))
    else: tsv(rows, header)

def short_image(img):
    return img.rsplit("/", 1)[-1] if "@sha256:" not in img else img.rsplit("/", 1)[-1].split("@")[0] + "@" + img.split("@sha256:")[1][:12]

# ---------------- pods ----------------
def cmd_pods(args):
    extra = ["-l", args.l] if args.l else []
    items = kget("pods", args.env, args.n, args.A, extra)
    if not items: check_ns(args.env, args.n)
    rows = []
    for p in items:
        cs = p.get("status", {}).get("containerStatuses", []) or []
        ready = sum(1 for c in cs if c.get("ready")); total = len(p["spec"]["containers"])
        status = p["status"].get("phase", "")
        for c in cs:
            st = c.get("state", {})
            if "waiting" in st: status = st["waiting"].get("reason") or "Waiting"
            elif "terminated" in st and st["terminated"].get("reason") not in (None, "Completed"): status = st["terminated"]["reason"]
        if p["metadata"].get("deletionTimestamp"): status = "Terminating"
        rows.append({"ns": p["metadata"]["namespace"], "pod": p["metadata"]["name"], "ready": f"{ready}/{total}", "status": status,
                     "restarts": sum(c.get("restartCount", 0) for c in cs), "age": age(p["metadata"].get("creationTimestamp")),
                     "node": p["spec"].get("nodeName", "")})
    healthy = lambda r: r["status"] in ("Running", "Succeeded") and r["ready"].split("/")[0] == r["ready"].split("/")[1]
    rows.sort(key=lambda r: (healthy(r), r["ns"], r["pod"]))
    emit(rows, ["ns", "pod", "ready", "status", "restarts", "age", "node"], args, items)
    sys.stderr.write(f"{len(rows)} pods, {sum(not healthy(r) for r in rows)} not ready/running\n")

# ---------------- img ----------------
def cmd_img(args):
    items = [json.loads(kubectl(["get", "deploy", args.deploy, "-o", "json"], args.env, args.n))] if args.deploy else kget("deployments", args.env, args.n)
    if not items: check_ns(args.env, args.n)
    rows = []
    for d in items:
        st = d.get("status", {})
        for c in d["spec"]["template"]["spec"]["containers"]:
            ev = next((e.get("value", "") for e in c.get("env", []) if e.get("name") == args.env_var), "")
            rows.append({"ns": d["metadata"]["namespace"], "deploy": d["metadata"]["name"], "container": c["name"],
                         "image": c["image"] if args.full else short_image(c["image"]), args.env_var: ev,
                         "ready": f"{st.get('readyReplicas', 0)}/{d['spec'].get('replicas', 0)}", "updated": age(d["metadata"].get("creationTimestamp"))})
    emit(rows, ["ns", "deploy", "container", "image", args.env_var, "ready", "updated"], args, items)

# ---------------- nodes ----------------
def cmd_nodes(args):
    if args.pools:
        items = kget("nodeclaims", args.env)
        rows = [{"nodeclaim": n["metadata"]["name"], "pool": n["metadata"].get("labels", {}).get("karpenter.sh/nodepool", ""),
                 "type": n["metadata"].get("labels", {}).get("node.kubernetes.io/instance-type", ""),
                 "capacity": n["metadata"].get("labels", {}).get("karpenter.sh/capacity-type", ""),
                 "ready": next((c["status"] for c in n.get("status", {}).get("conditions", []) if c["type"] == "Ready"), ""),
                 "age": age(n["metadata"].get("creationTimestamp")), "node": n.get("status", {}).get("nodeName", "")} for n in items]
        rows.sort(key=lambda r: (r["pool"], r["age"]))
        emit(rows, ["nodeclaim", "pool", "type", "capacity", "ready", "age", "node"], args, items)
        from collections import Counter
        sys.stderr.write(" ".join(f"{k}={v}" for k, v in sorted(Counter(r["pool"] for r in rows).items())) + "\n"); return
    items = kget("nodes", args.env)
    rows = []
    for n in items:
        L = n["metadata"].get("labels", {}); conds = {c["type"]: c["status"] for c in n.get("status", {}).get("conditions", [])}
        rows.append({"node": n["metadata"]["name"], "ready": conds.get("Ready", ""), "type": L.get("node.kubernetes.io/instance-type", ""),
                     "zone": L.get("topology.kubernetes.io/zone", ""), "pool": L.get("karpenter.sh/nodepool", L.get("eks.amazonaws.com/nodegroup", "")),
                     "capacity": L.get("karpenter.sh/capacity-type", ""), "version": n["status"].get("nodeInfo", {}).get("kubeletVersion", ""),
                     "age": age(n["metadata"].get("creationTimestamp")), "unschedulable": bool(n["spec"].get("unschedulable"))})
    rows.sort(key=lambda r: (r["ready"] == "True", r["pool"], r["node"]))
    emit(rows, ["node", "ready", "type", "zone", "pool", "capacity", "version", "age", "unschedulable"], args, items)
    sys.stderr.write(f"{len(rows)} nodes, {sum(r['ready'] != 'True' for r in rows)} not ready\n")

# ---------------- events ----------------
def cmd_events(args):
    items = kget("events", args.env, args.n, args.A)
    if not items: check_ns(args.env, args.n)
    if args.warn: items = [e for e in items if e.get("type") == "Warning"]
    ts = lambda e: e.get("lastTimestamp") or e.get("eventTime") or e.get("firstTimestamp") or ""
    items.sort(key=ts, reverse=True)
    rows = [{"last": ts(e), "count": e.get("count", 1), "type": e.get("type"), "reason": e.get("reason"),
             "object": f"{e['involvedObject'].get('kind')}/{e['involvedObject'].get('name')}", "ns": e["metadata"]["namespace"],
             "message": (e.get("message") or "")[:160]} for e in items[:args.limit]]
    emit(rows, ["last", "count", "type", "reason", "ns", "object", "message"], args, items[:args.limit])

# ---------------- logs ----------------
def cmd_logs(args):
    out = kubectl(["logs", args.target, "--all-containers", "--prefix", "--since", args.since, "--tail", str(args.tail)], args.env, args.n)
    lines = out.splitlines()
    if not args.grep: print("\n".join(lines)); return
    rx = re.compile(args.grep); hits = [i for i, l in enumerate(lines) if rx.search(l)]; last = -1
    for i in hits:
        lo, hi = max(0, i - args.C), min(len(lines), i + args.C + 1)
        if lo > last + 1 and last >= 0: print("--")
        for k in range(max(lo, last + 1), hi): print(lines[k])
        last = hi - 1
    sys.stderr.write(f"{len(hits)} hits in {len(lines)} lines\n")

def cmd_rollout(args):
    print(kubectl(["rollout", "status", args.target, "--timeout", args.timeout], args.env, args.n, ok=(0, 1)).strip())

def cmd_secret(args):
    d = json.loads(kubectl(["get", "secret", args.name, "-o", "json"], args.env, args.n)).get("data", {})
    if args.key not in d: sys.exit(f"key {args.key!r} not in secret; keys: {', '.join(sorted(d))}")
    sys.stdout.write(base64.b64decode(d[args.key]).decode(errors="replace"))

def cmd_doctor(args):
    ctx = context_of(args.env)
    ctxs = subprocess.run(["kubectl", "config", "get-contexts", "-o", "name"], text=True, capture_output=True).stdout.split()
    if ctx not in ctxs: sys.exit(f"context {ctx!r} not in kubeconfig. known: {', '.join(ctxs)}\nmap {args.env!r} to a context in {CFG}")
    v = json.loads(kubectl(["version", "-o", "json"], args.env)).get("serverVersion", {}).get("gitVersion", "?")
    can = kubectl(["auth", "can-i", "get", "pods", "-A"], args.env, ok=(0, 1)).strip()
    print(f"env={args.env} context={ctx} server={v} can-get-pods-all-ns={can} map={'yes' if os.path.exists(CFG) else 'no'}")

def main(argv=None):
    global CMD
    ap = argparse.ArgumentParser(prog="jx k", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("env", help="environment: a kubectl context name, a key in the map file, or `all`")
    ap.add_argument("--json", action="store_true", help="raw kubectl JSON instead of TSV")
    g = argparse.ArgumentParser(add_help=False); g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw kubectl JSON instead of TSV")  # accepted after the subcommand too
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    def ns(p, all_ns=True):
        p.add_argument("-n", metavar="NS", help="namespace; default is the context's namespace")
        if all_ns: p.add_argument("-A", action="store_true", help="all namespaces")
    p = sp.add_parser("pods", parents=[g], help="pods, unhealthy first, with waiting reason, restarts, age, node"); ns(p); p.add_argument("-l", metavar="SELECTOR", help="label selector, e.g. app=api"); p.set_defaults(f=cmd_pods)
    p = sp.add_parser("images", parents=[g], help="image per container of each deployment, plus one env var"); p.add_argument("deploy", nargs="?", help="one deployment; default is every deployment in the namespace"); ns(p, False)
    p.add_argument("--env-var", default="GIT_SHA", help="container env var to show next to the image (GIT_SHA)"); p.add_argument("--full", action="store_true", help="full image refs with registry and digest"); p.set_defaults(f=cmd_img)
    p = sp.add_parser("nodes", parents=[g], help="nodes with instance type, zone, pool, capacity type, kubelet version, age"); p.add_argument("--pools", action="store_true", help="Karpenter nodeclaims grouped by nodepool instead of nodes"); p.set_defaults(f=cmd_nodes)
    p = sp.add_parser("events", parents=[g], help="events newest first"); ns(p); p.add_argument("--warn", action="store_true", help="Warning events only"); p.add_argument("--limit", type=int, default=50, help="max events (50)"); p.set_defaults(f=cmd_events)
    p = sp.add_parser("logs", parents=[g], help="container logs of a workload, optionally grepped with context"); p.add_argument("target", help="deploy/NAME, pod/NAME, sts/NAME, ds/NAME, " + STDIN_HELP.format("pod") + "; a bare name is a pod"); ns(p, False)
    p.add_argument("--since", default="10m", help="kubectl duration (10m)"); p.add_argument("--grep", help="python regex; prints hits with context"); p.add_argument("-C", type=int, default=2, help="context lines around each hit (2)"); p.add_argument("--tail", type=int, default=2000, help="lines fetched per container before grep (2000)"); p.set_defaults(f=lambda a: each(a, "target", "pod", cmd_logs, fix=lambda v: v if "/" in v else f"pod/{v}"))
    p = sp.add_parser("rollout", parents=[g], help="rollout status of a workload"); p.add_argument("target", help="deploy/NAME, sts/NAME, ds/NAME"); ns(p, False); p.add_argument("--timeout", default="300s", help="kubectl duration to wait (300s)"); p.set_defaults(f=cmd_rollout)
    p = sp.add_parser("secret", parents=[g], help="one key of a secret, base64-decoded, to stdout"); p.add_argument("name", help="secret name"); p.add_argument("key", help="data key; a wrong key lists the real ones"); ns(p, False); p.set_defaults(f=cmd_secret)
    sp.add_parser("doctor", parents=[g], help="context exists, server reachable, version, can-i get pods").set_defaults(f=cmd_doctor)
    args = ap.parse_args(argv); CMD = args.cmd
    if args.env != "all": args.f(args); return
    if args.cmd in ("secret", "logs", "rollout"): sys.exit(f"`jx k all {args.cmd}` not supported; one env at a time")
    first = True
    for env in all_envs():
        args.env = env; buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try: args.f(args)
            except SystemExit as e: sys.stderr.write(f"[{env}] {e}\n"); continue
        lines = buf.getvalue().splitlines()
        if args.json: print(buf.getvalue()); continue
        if first and lines: print("env\t" + lines[0]); first = False
        for ln in lines[1:]: print(f"{env}\t{ln}")

if __name__ == "__main__": main()
