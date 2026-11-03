#!/usr/bin/env python3
"""awsx — AWS CLI lever for agents. TSV by default, --json for the raw payload. Every call runs with TZ=UTC
and prints UTC timestamps, so nothing is ever read in local time.

  awsx [-p PROFILE] [--region R] <subcommand>

  whoami                                      account, role ARN, and whether the SSO token behind the profile is still valid
  cw <namespace> [metric] [--dim K=V ...] [--stat Average,SampleCount] [--since 6h] [--period 300]
                                              datapoints; without a metric, lists metrics + dimensions in the namespace
  alarms [--state ALARM] [--prefix NAME]      alarm name, state, since, metric, reason
  ecr-tags <repo> [--filter SUBSTR] [--limit 20]   newest images first: pushed, tags, digest, size
  cost [--since 30d] [--group SERVICE] [--granularity DAILY] [--metric UnblendedCost] [--top 15]
  logs-query <group[,group]> '<insights query>' [--since 1h] [--limit 100]   start, poll, print results
  eks <cluster> [addons|nodegroups]           cluster version/status, or addon versions, or nodegroup state

Gotchas this encodes: `sts get-caller-identity` succeeds on cached role creds for hours after the SSO
token expired (terraform then fails) — `whoami` reads ~/.aws/sso/cache expiresAt. CloudWatch gauges
summed over a period count samples, not things — default stat is Average plus SampleCount.
"""
import argparse, configparser, glob, json, os, re, subprocess, sys, time
from datetime import datetime, timedelta, timezone

def aws(args, a, ok=(0,)):
    cmd = ["aws", "--output", "json", "--no-cli-pager"] + (["--profile", a.profile] if a.profile else []) + (["--region", a.region] if a.region else []) + args
    p = subprocess.run(cmd, text=True, capture_output=True, env={**os.environ, "TZ": "UTC"})
    if p.returncode not in ok:
        err = p.stderr.strip()
        if re.search(r"token|sso|expired|InvalidGrant|credentials", err, re.I): err += f"\n  → `awsx -p {a.profile or '<profile>'} whoami`, then `aws sso login --profile {a.profile or '<profile>'}`"
        sys.exit(err)
    return json.loads(p.stdout) if p.stdout.strip() else {}

def since(s):
    m = re.fullmatch(r"(\d+)([mhd])", s or "")
    if m: return datetime.now(timezone.utc) - timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]: int(m.group(1))})
    return datetime.fromisoformat(s.replace("Z", "+00:00"))

def iso(dt): return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
def utc(s):  # normalise any aws timestamp string to Z form; a naive string (Logs Insights) is already UTC, never local
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError: return str(s)

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, a, raw=None):
    if a.json: print(json.dumps(raw if raw is not None else rows, indent=1, default=str))
    else: tsv(rows, header)

# ---------------- whoami ----------------
def sso_expiry(profile):
    cfg = configparser.ConfigParser(); cfg.read(os.path.expanduser("~/.aws/config"))
    sec = f"profile {profile}" if profile and profile != "default" else "default"
    if sec not in cfg: return None, "profile not in ~/.aws/config"
    p = cfg[sec]; start = p.get("sso_start_url")
    if not start and p.get("sso_session") and f"sso-session {p['sso_session']}" in cfg: start = cfg[f"sso-session {p['sso_session']}"].get("sso_start_url")
    if not start: return None, "not an SSO profile"
    for f in glob.glob(os.path.expanduser("~/.aws/sso/cache/*.json")):
        d = json.load(open(f))
        if d.get("startUrl") == start and d.get("expiresAt"): return datetime.fromisoformat(d["expiresAt"].replace("Z", "+00:00")), start
    return None, "no cached SSO token for this start URL"

def cmd_whoami(a):
    exp, note = sso_expiry(a.profile or os.environ.get("AWS_PROFILE", "default"))
    ident = aws(["sts", "get-caller-identity"], a)
    left = (exp - datetime.now(timezone.utc)).total_seconds() if exp else None
    row = {"profile": a.profile or os.environ.get("AWS_PROFILE", "default"), "account": ident.get("Account"), "arn": ident.get("Arn"),
           "sso_token": "n/a: " + note if left is None else ("EXPIRED" if left < 0 else f"valid {int(left//60)}m")}
    emit([row], ["profile", "account", "arn", "sso_token"], a)
    if left is not None and left < 0:
        sys.stderr.write(f"SSO token expired at {iso(exp)}; sts still works from cached role creds but terraform/SDKs will not. Run: aws sso login --profile {row['profile']}\n"); sys.exit(1)

# ---------------- cloudwatch ----------------
def cmd_cw(a):
    if not a.metric:
        ms = aws(["cloudwatch", "list-metrics", "--namespace", a.namespace] + (["--metric-name", a.metric] if a.metric else []), a).get("Metrics", [])
        rows = sorted({(m["MetricName"], ",".join(f"{d['Name']}={d['Value']}" for d in m.get("Dimensions", []))) for m in ms})
        emit([{"metric": m, "dimensions": d} for m, d in rows], ["metric", "dimensions"], a, ms); return
    stats = a.stat.split(",")
    dims = json.dumps([{"Name": k, "Value": v} for k, v in (d.split("=", 1) for d in a.dim)])  # JSON, so commas in values survive
    args = ["cloudwatch", "get-metric-statistics", "--namespace", a.namespace, "--metric-name", a.metric, "--start-time", iso(since(a.since)),
            "--end-time", iso(datetime.now(timezone.utc)), "--period", str(a.period), "--statistics", *stats]
    if a.dim: args += ["--dimensions", dims]
    d = aws(args, a); pts = sorted(d.get("Datapoints", []), key=lambda p: p["Timestamp"])
    rows = [{"time": utc(p["Timestamp"]), **{s: round(p.get(s, float("nan")), 4) for s in stats}, "unit": p.get("Unit", "")} for p in pts]
    emit(rows, ["time", *stats, "unit"], a, d)
    sys.stderr.write(f"{len(rows)} datapoints, period {a.period}s" + ("  (Sum of a gauge = value × samples; use Average)" if "Sum" in stats and "SampleCount" not in stats else "") + "\n")

def cmd_alarms(a):
    args = ["cloudwatch", "describe-alarms"] + (["--state-value", a.state] if a.state else []) + (["--alarm-name-prefix", a.prefix] if a.prefix else [])
    d = aws(args, a); al = d.get("MetricAlarms", []) + d.get("CompositeAlarms", [])
    rows = sorted([{"state": x["StateValue"], "since": utc(x.get("StateUpdatedTimestamp")), "alarm": x["AlarmName"],
                    "metric": f"{x.get('Namespace', '')}/{x.get('MetricName', '')}".strip("/"), "reason": (x.get("StateReason") or "")[:140]} for x in al],
                  key=lambda r: ({"ALARM": 0, "INSUFFICIENT_DATA": 1}.get(r["state"], 2), r["alarm"]))
    emit(rows, ["state", "since", "alarm", "metric", "reason"], a, al)

# ---------------- ecr ----------------
def cmd_ecr_tags(a):
    imgs = aws(["ecr", "describe-images", "--repository-name", a.repo], a).get("imageDetails", [])
    if a.filter: imgs = [i for i in imgs if any(a.filter in t for t in i.get("imageTags", []))]
    imgs.sort(key=lambda i: i["imagePushedAt"], reverse=True)
    rows = [{"pushed": utc(i["imagePushedAt"]), "tags": ",".join(i.get("imageTags", [])) or "<untagged>", "digest": i["imageDigest"][7:19],
             "mb": round(i.get("imageSizeInBytes", 0) / 1e6, 1)} for i in imgs[:a.limit]]
    emit(rows, ["pushed", "tags", "digest", "mb"], a, imgs[:a.limit])

# ---------------- cost ----------------
def cmd_cost(a):
    start = since(a.since).date().isoformat(); end = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()
    args = ["ce", "get-cost-and-usage", "--time-period", f"Start={start},End={end}", "--granularity", a.granularity, "--metrics", a.metric]
    if a.group:
        t, k = (a.group.split(":", 1) + [None])[:2] if ":" in a.group else ("DIMENSION", a.group)
        args += ["--group-by", f"Type={'TAG' if t.upper()=='TAG' else 'DIMENSION'},Key={k}"]
    d = aws(args, a); rows = []
    for r in d.get("ResultsByTime", []):
        if r.get("Groups"):
            for g in r["Groups"]: rows.append({"start": r["TimePeriod"]["Start"], "group": "|".join(g["Keys"]), "amount": round(float(g["Metrics"][a.metric]["Amount"]), 2), "unit": g["Metrics"][a.metric]["Unit"]})
        else: rows.append({"start": r["TimePeriod"]["Start"], "group": "", "amount": round(float(r["Total"][a.metric]["Amount"]), 2), "unit": r["Total"][a.metric]["Unit"]})
    if a.group and a.top:
        tot = {}
        for r in rows: tot[r["group"]] = tot.get(r["group"], 0) + r["amount"]
        keep = {k for k, _ in sorted(tot.items(), key=lambda kv: -kv[1])[:a.top]}; rows = [r for r in rows if r["group"] in keep]
    rows.sort(key=lambda r: (r["start"], -r["amount"]))
    emit(rows, ["start", "group", "amount", "unit"], a, d)
    sys.stderr.write(f"total {round(sum(float(g['Metrics'][a.metric]['Amount']) for r in d.get('ResultsByTime', []) for g in (r.get('Groups') or [{'Metrics': r['Total']}])), 2)} {a.metric} {start}..{end} (end exclusive)\n")

# ---------------- logs insights ----------------
def cmd_logs_query(a):
    args = ["logs", "start-query", "--start-time", str(int(since(a.since).timestamp())), "--end-time", str(int(time.time())), "--query-string", a.query]
    if not re.search(r"\|\s*limit\s+\d+", a.query): args += ["--limit", str(a.limit)]  # a --limit flag would override the query's own limit clause
    groups = a.group.split(","); args += ["--log-group-names", *groups] if len(groups) > 1 else ["--log-group-name", groups[0]]
    qid = aws(args, a)["queryId"]; deadline = time.time() + a.timeout
    while True:
        r = aws(["logs", "get-query-results", "--query-id", qid], a)
        if r["status"] in ("Complete", "Failed", "Cancelled", "Timeout"): break
        if time.time() > deadline: aws(["logs", "stop-query", "--query-id", qid], a, ok=(0, 254)); sys.exit(f"query {qid} still {r['status']} after {a.timeout}s")
        time.sleep(2)
    rows = [{f["field"].lstrip("@"): f["value"] for f in res if f["field"] != "@ptr"} for res in r.get("results", [])]
    header = list(rows[0]) if rows else []
    for row in rows:
        if "timestamp" in row: row["timestamp"] = utc(row["timestamp"].replace(" ", "T"))
    emit(rows, header, a, r)
    st = r.get("statistics", {}); sys.stderr.write(f"{r['status']}: {len(rows)} rows, scanned {st.get('recordsScanned', 0):.0f} records / {st.get('bytesScanned', 0)/1e6:.1f} MB\n")

# ---------------- eks ----------------
def cmd_eks(a):
    if a.what == "addons":
        rows = []
        for n in aws(["eks", "list-addons", "--cluster-name", a.cluster], a).get("addons", []):
            d = aws(["eks", "describe-addon", "--cluster-name", a.cluster, "--addon-name", n], a)["addon"]
            rows.append({"addon": n, "version": d["addonVersion"], "status": d["status"], "updated": utc(d.get("modifiedAt")), "issues": len(d.get("health", {}).get("issues", []))})
        emit(rows, ["addon", "version", "status", "updated", "issues"], a); return
    if a.what == "nodegroups":
        rows = []
        for n in aws(["eks", "list-nodegroups", "--cluster-name", a.cluster], a).get("nodegroups", []):
            d = aws(["eks", "describe-nodegroup", "--cluster-name", a.cluster, "--nodegroup-name", n], a)["nodegroup"]
            s = d.get("scalingConfig", {})
            rows.append({"nodegroup": n, "status": d["status"], "version": d.get("version"), "release": d.get("releaseVersion"), "desired": s.get("desiredSize"), "min": s.get("minSize"), "max": s.get("maxSize"),
                         "types": ",".join(d.get("instanceTypes", [])), "capacity": d.get("capacityType"), "issues": len(d.get("health", {}).get("issues", []))})
        emit(rows, ["nodegroup", "status", "version", "release", "desired", "min", "max", "types", "capacity", "issues"], a); return
    c = aws(["eks", "describe-cluster", "--name", a.cluster], a)["cluster"]
    row = {"cluster": c["name"], "version": c["version"], "platform": c.get("platformVersion"), "status": c["status"], "created": utc(c.get("createdAt")),
           "public_endpoint": c["resourcesVpcConfig"].get("endpointPublicAccess"), "private_endpoint": c["resourcesVpcConfig"].get("endpointPrivateAccess"),
           "public_cidrs": ",".join(c["resourcesVpcConfig"].get("publicAccessCidrs", [])), "auth_mode": c.get("accessConfig", {}).get("authenticationMode"), "support": c.get("upgradePolicy", {}).get("supportType")}
    emit([row], list(row), a, c)

def main():
    ap = argparse.ArgumentParser(prog="awsx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--profile", default=os.environ.get("AWS_PROFILE")); ap.add_argument("--region"); ap.add_argument("--json", action="store_true")
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("whoami").set_defaults(f=cmd_whoami)
    p = sp.add_parser("cw"); p.add_argument("namespace"); p.add_argument("metric", nargs="?"); p.add_argument("--dim", action="append", default=[], metavar="K=V")
    p.add_argument("--stat", default="Average,SampleCount", help="comma list: Average,Sum,Maximum,Minimum,SampleCount"); p.add_argument("--since", default="6h"); p.add_argument("--period", type=int, default=300); p.set_defaults(f=cmd_cw)
    p = sp.add_parser("alarms"); p.add_argument("--state", choices=["ALARM", "OK", "INSUFFICIENT_DATA"]); p.add_argument("--prefix"); p.set_defaults(f=cmd_alarms)
    p = sp.add_parser("ecr-tags"); p.add_argument("repo"); p.add_argument("--filter"); p.add_argument("--limit", type=int, default=20); p.set_defaults(f=cmd_ecr_tags)
    p = sp.add_parser("cost"); p.add_argument("--since", default="30d"); p.add_argument("--group", help="SERVICE | LINKED_ACCOUNT | USAGE_TYPE | REGION | TAG:key"); p.add_argument("--granularity", default="DAILY", choices=["DAILY", "MONTHLY", "HOURLY"])
    p.add_argument("--metric", default="UnblendedCost"); p.add_argument("--top", type=int, default=15); p.set_defaults(f=cmd_cost)
    p = sp.add_parser("logs-query"); p.add_argument("group"); p.add_argument("query"); p.add_argument("--since", default="1h"); p.add_argument("--limit", type=int, default=100); p.add_argument("--timeout", type=int, default=120); p.set_defaults(f=cmd_logs_query)
    p = sp.add_parser("eks"); p.add_argument("cluster"); p.add_argument("what", nargs="?", choices=["addons", "nodegroups"]); p.set_defaults(f=cmd_eks)
    a = ap.parse_args(); a.f(a)

if __name__ == "__main__": main()
