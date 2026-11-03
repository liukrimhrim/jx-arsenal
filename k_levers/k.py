#!/usr/bin/env python3
"""k — kubectl lever for agents, one environment per call. TSV by default, --json for raw kubectl JSON.

  k <env> pods [-n NS | -A] [-l SELECTOR]        name, ready, status/waiting reason, restarts, age, node (unhealthy first)
  k <env> img [DEPLOY] [-n NS] [--env-var NAME]  image per container of each deployment, plus one env var (default GIT_SHA)
  k <env> nodes [--pools]                        nodes with instance type, zone, pool, capacity type, age; --pools = nodeclaims by pool
  k <env> events [-n NS | -A] [--warn]           events newest first; --warn = Warning only
  k <env> logs <deploy/NAME | pod/NAME> [-n NS] [--since 10m] [--grep PAT] [-C N] [--tail N]
  k <env> rollout <deploy/NAME> [-n NS] [--timeout 300s]
  k <env> secret <NAME> <KEY> [-n NS]            one key, base64-decoded, to stdout
  k <env> doctor
  k all <subcommand ...>                         run against every env in $K_ENVS (comma list) or the map file

<env> is the kubectl context name unless ~/.config/jx-arsenal/k.json maps it:
  {"envs": {"prod": {"context": "arn:aws:eks:...:cluster/x"}, "stage": {"context": "stage-cluster"}}}
The file also defines the env list `k all` uses. Nothing cluster-specific lives in this script.
"""
import argparse, base64, contextlib, io, json, os, re, subprocess, sys
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
    if not envs: sys.exit(f"`k all` needs K_ENVS=a,b,c or an envs map in {CFG}")
    return envs

def kubectl(args, env, ns=None, all_ns=False, ok=(0,)):
    # namespace flags go AFTER the verb: kubectl treats a leading -A as a plugin name
    cmd = ["kubectl", "--context", context_of(env), "--request-timeout", "20s"] + args + (["-A"] if all_ns else ["-n", ns] if ns else [])
    try: p = subprocess.run(cmd, text=True, capture_output=True, timeout=int(os.environ.get("K_TIMEOUT", "45")))
    except subprocess.TimeoutExpired:  # kubectl's discovery retries ignore --request-timeout; bound the whole call
        sys.exit(f"{env}: no answer in {os.environ.get('K_TIMEOUT', '45')}s (VPN/tailnet down? wrong context?)  → run `k {env} doctor`")
    if p.returncode not in ok:
        err = p.stderr.strip()
        hint = "  → run `k {} doctor`".format(env) if re.search(r"context|credential|Unauthorized|token|exec plugin", err, re.I) else ""
        sys.exit(f"{err}{hint}")
    return p.stdout

def kget(kind, env, ns=None, all_ns=False, extra=()):
    return json.loads(kubectl(["get", kind, "-o", "json", *extra], env, ns, all_ns)).get("items", [])

def age(ts):
    if not ts: return ""
    s = int((datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace("Z", "+00:00"))).total_seconds())
    return f"{s//86400}d" if s >= 86400 else f"{s//3600}h" if s >= 3600 else f"{s//60}m" if s >= 60 else f"{s}s"

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
    ap = argparse.ArgumentParser(prog="k", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("env", help="environment / kubectl context, or `all`")
    ap.add_argument("--json", action="store_true", help="raw kubectl JSON instead of TSV")
    sp = ap.add_subparsers(dest="cmd", required=True)
    def ns(p, all_ns=True):
        p.add_argument("-n", metavar="NS");
        if all_ns: p.add_argument("-A", action="store_true", help="all namespaces")
    p = sp.add_parser("pods"); ns(p); p.add_argument("-l", metavar="SELECTOR"); p.set_defaults(f=cmd_pods)
    p = sp.add_parser("img"); p.add_argument("deploy", nargs="?"); ns(p, False); p.add_argument("--env-var", default="GIT_SHA"); p.add_argument("--full", action="store_true", help="full image refs"); p.set_defaults(f=cmd_img)
    p = sp.add_parser("nodes"); p.add_argument("--pools", action="store_true", help="Karpenter nodeclaims grouped by nodepool"); p.set_defaults(f=cmd_nodes)
    p = sp.add_parser("events"); ns(p); p.add_argument("--warn", action="store_true"); p.add_argument("--limit", type=int, default=50); p.set_defaults(f=cmd_events)
    p = sp.add_parser("logs"); p.add_argument("target", help="deploy/NAME, pod/NAME, sts/NAME ..."); ns(p, False); p.add_argument("--since", default="10m"); p.add_argument("--grep"); p.add_argument("-C", type=int, default=2); p.add_argument("--tail", type=int, default=2000); p.set_defaults(f=cmd_logs)
    p = sp.add_parser("rollout"); p.add_argument("target"); ns(p, False); p.add_argument("--timeout", default="300s"); p.set_defaults(f=cmd_rollout)
    p = sp.add_parser("secret"); p.add_argument("name"); p.add_argument("key"); ns(p, False); p.set_defaults(f=cmd_secret)
    sp.add_parser("doctor").set_defaults(f=cmd_doctor)
    args = ap.parse_args(argv)
    if args.env != "all": args.f(args); return
    if args.cmd in ("secret", "logs", "rollout"): sys.exit(f"`k all {args.cmd}` not supported; one env at a time")
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
