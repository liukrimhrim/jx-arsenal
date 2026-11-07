#!/usr/bin/env python3
"""jx obs — Prometheus (Amazon Managed Prometheus) and Loki lever for agents. TSV by default, --json for raw.

  jx obs <env> q '<promql>' [--since 1h] [--step 5m]          instant query; --since makes it a range query, one row per series
  jx obs <env> metrics [PREFIX]                               metric names, filtered by prefix when given
  jx obs <env> series '<selector>' [--since 1h]               per metric: series count and the label names seen on it
  jx obs <env> labels '<selector>' [--since 1h]               label census: distinct values and up to 5 samples per label name
  jx obs <env> fresh '<selector>' [--max-age S]               last sample age per series, oldest first; --max-age exits 1 when any is older
  jx obs <env> rules [--firing]                               alerting/recording rules with state, health, active alert count
  jx obs <env> alerts                                         active alerts with state, active-since, labels, summary
  jx obs <env> logs '<logql>' [--since 1h] [--limit 100] [--fields event,message,level]   Loki lines newest first; JSON lines reduced to fields
  jx obs <env> logcount '<logql>' [--since 6h] [--step 1h] [--by LABEL]   log volume per step per label value (long format)
  jx obs <env> loki-labels [NAME] [--since 1h]                Loki stream label names, or the values of one label
  jx obs <env> doctor
  jx obs all <command ...>                                    run a command against every env in the config, env column prepended

<env> comes from ~/.config/jx-arsenal/obs.json:
  {"envs": {"dev": {"profile": "dev", "region": "us-east-1", "workspace": "ws-...", "loki": "http://dev-loki:3100"}}}
Nothing environment-specific lives in this script. AMP calls are SigV4-signed with credentials from
`aws configure export-credentials --profile <profile>`; no boto3. Loki is plain HTTP (reachable over the tailnet).
"""
import argparse, contextlib, datetime as dt, hashlib, hmac, io, json, os, re, signal, subprocess, sys, urllib.error, urllib.parse, urllib.request
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
CFG = os.path.expanduser("~/.config/jx-arsenal/obs.json")

def cfg():
    try: return json.load(open(CFG))["envs"]
    except FileNotFoundError: sys.exit(f"{CFG} missing. Create it with an `envs` map of profile/region/workspace/loki per environment (see `jx obs --help`).")

def env_of(name):
    envs = cfg()
    if name not in envs: sys.exit(f"env {name!r} not in {CFG}; known: {', '.join(envs)}")
    return envs[name]

def creds(profile):
    try: p = subprocess.run(["aws", "configure", "export-credentials", "--profile", profile, "--format", "process"], text=True, capture_output=True)
    except FileNotFoundError: sys.exit("`aws` not found on PATH; install the AWS CLI, then `jx aws -p <profile> whoami`")
    if p.returncode: sys.exit(p.stderr.strip() + f"\n→ `jx aws -p {profile} whoami`; if expired, `aws sso login --profile {profile}`")
    return json.loads(p.stdout)

def sigv4(method, url, body, region, c):
    """Sign one request for service `aps`. Canonical form per AWS SigV4; query string sorted and RFC3986-encoded."""
    u = urllib.parse.urlsplit(url); now = dt.datetime.now(dt.timezone.utc)
    amz, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    q = "&".join(f"{urllib.parse.quote(k, safe='-_.~')}={urllib.parse.quote(v, safe='-_.~')}" for k, v in sorted(urllib.parse.parse_qsl(u.query, keep_blank_values=True)))
    h = {"host": u.netloc, "x-amz-date": amz}
    if c.get("SessionToken"): h["x-amz-security-token"] = c["SessionToken"]
    if body: h["content-type"] = "application/x-www-form-urlencoded"
    signed = ";".join(sorted(h)); payload = hashlib.sha256(body or b"").hexdigest()
    canon = "\n".join([method, urllib.parse.quote(u.path, safe="/-_.~"), q, "".join(f"{k}:{h[k]}\n" for k in sorted(h)), signed, payload])
    scope = f"{day}/{region}/aps/aws4_request"
    sts = "\n".join(["AWS4-HMAC-SHA256", amz, scope, hashlib.sha256(canon.encode()).hexdigest()])
    k = f"AWS4{c['SecretAccessKey']}".encode()
    for part in (day, region, "aps", "aws4_request"): k = hmac.new(k, part.encode(), hashlib.sha256).digest()
    sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
    h["authorization"] = f"AWS4-HMAC-SHA256 Credential={c['AccessKeyId']}/{scope}, SignedHeaders={signed}, Signature={sig}"
    return h

def amp(env, path, params, method="POST"):
    """Call the Prometheus API on the env's AMP workspace; returns the `data` field. GET puts params in the query string."""
    qs = urllib.parse.urlencode(params, doseq=True)
    url = f"https://aps-workspaces.{env['region']}.amazonaws.com/workspaces/{env['workspace']}/api/v1/{path}"
    body = None
    if method == "POST": body = qs.encode()
    elif qs: url += "?" + qs
    req = urllib.request.Request(url, data=body, method=method, headers=sigv4(method, url, body, env["region"], creds(env["profile"])))
    try:
        with urllib.request.urlopen(req, timeout=60) as r: d = json.load(r)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:400]
        hint = {403: f"wrong profile/region/workspace in {CFG}, or expired SSO (`jx aws -p {env['profile']} whoami`)",
                400: "bad PromQL or selector; `jx obs <env> metrics <prefix>` lists real metric names, `jx obs <env> labels '<selector>'` real label names"}.get(e.code, "")
        sys.exit(f"AMP {e.code}: {msg}" + (f"\n→ {hint}" if hint else ""))
    if d.get("status") != "success": sys.exit(f"AMP: {d.get('errorType')}: {d.get('error')}")
    return d["data"]

def loki(env, path, params):
    """GET a Loki API call on the env's Loki; returns the `data` field."""
    url = f"{env['loki']}/loki/api/v1/{path}" + (f"?{urllib.parse.urlencode(params, doseq=True)}" if params else "")
    try:
        with urllib.request.urlopen(url, timeout=30) as r: d = json.load(r)
    except urllib.error.HTTPError as e:
        msg = e.read().decode(errors="replace")[:400]
        sys.exit(f"Loki {e.code}: {msg}\n→ 400: bad LogQL; `jx obs <env> loki-labels` lists stream labels, `jx obs <env> loki-labels <name>` their values.")
    except OSError as e:  # URLError and socket timeouts both land here
        sys.exit(f"Loki at {env['loki']} unreachable; tailnet down? `tailscale status` ({e})")
    if d.get("status") != "success": sys.exit(f"Loki: {d.get('errorType')}: {d.get('error')}")
    return d["data"]

def since(s):
    """Seconds back from now for 30m, 6h, 7d, 2w, or for an ISO time (a naive one is UTC)."""
    m = re.fullmatch(r"(\d+)([smhdw])", s or "")
    if m: return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    try: t = dt.datetime.fromisoformat((s or "").replace("Z", "+00:00")); t = t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except ValueError: sys.exit(f"bad time {s!r}; use 30m, 6h, 7d, 2w, or an ISO time like 2026-01-31T00:00:00Z")
    return int((dt.datetime.now(dt.timezone.utc) - t).total_seconds())
def window(s):
    now = dt.datetime.now(dt.timezone.utc).timestamp(); return now - since(s), now

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, a, raw=None):
    if a.json: print(json.dumps(raw if raw is not None else rows, indent=1))
    else: tsv(rows, header)
def labels_str(m, drop=("__name__",)): return ",".join(f"{k}={v}" for k, v in sorted(m.items()) if k not in drop)
def iso(ts): return dt.datetime.fromtimestamp(float(ts), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def cmd_q(a):
    env = env_of(a.env)
    if a.since:
        start, end = window(a.since)
        d = amp(env, "query_range", {"query": a.query, "start": start, "end": end, "step": a.step})
        rows = [{"metric": r["metric"].get("__name__", ""), "labels": labels_str(r["metric"]), "points": len(r["values"]),
                 "first": iso(r["values"][0][0]), "last": iso(r["values"][-1][0]), "min": min(float(v) for _, v in r["values"]), "max": max(float(v) for _, v in r["values"]),
                 "last_value": r["values"][-1][1]} for r in d["result"]]
        emit(rows[:a.limit], ["metric", "labels", "points", "first", "last", "min", "max", "last_value"], a, d)
    else:
        d = amp(env, "query", {"query": a.query})
        if d["resultType"] == "scalar": print(f"value\n{d['result'][1]}"); return
        rows = [{"metric": r["metric"].get("__name__", ""), "labels": labels_str(r["metric"]), "time": iso(r["value"][0]), "value": r["value"][1]} for r in d["result"]]
        emit(rows[:a.limit], ["metric", "labels", "time", "value"], a, d)
    sys.stderr.write(f"{len(d['result'])} series\n")

def cmd_metrics(a):
    names = [n for n in amp(env_of(a.env), "label/__name__/values", {}, "GET") if n.startswith(a.prefix or "")]
    emit([{"metric": n} for n in names], ["metric"], a, names)
    sys.stderr.write(f"{len(names)} metrics\n")

def series_of(a):
    start, end = window(a.since)
    return amp(env_of(a.env), "series", {"match[]": a.selector, "start": start, "end": end})

def cmd_series(a):
    d = series_of(a); by = {}
    for s in d:
        e = by.setdefault(s.get("__name__", ""), [0, set()]); e[0] += 1; e[1].update(k for k in s if k != "__name__")
    rows = [{"metric": m, "series": n, "label_names": ",".join(sorted(ks))} for m, (n, ks) in sorted(by.items())]
    emit(rows, ["metric", "series", "label_names"], a, d)
    sys.stderr.write(f"{len(d)} series, {len(rows)} metrics\n")

def cmd_labels(a):
    d = series_of(a); vals = {}
    for s in d:
        for k, v in s.items(): vals.setdefault(k, set()).add(v)
    rows = [{"label": k, "distinct": len(v), "sample": ",".join(sorted(v)[:5])} for k, v in sorted(vals.items())]
    emit(rows, ["label", "distinct", "sample"], a, d)
    sys.stderr.write(f"{len(d)} series, {len(rows)} labels\n")

def cmd_fresh(a):
    now = dt.datetime.now(dt.timezone.utc).timestamp(); d = amp(env_of(a.env), "query", {"query": f"timestamp({a.selector})"})
    m = re.match(r"[\w:]+", a.selector); name = m[0] if m else ""  # timestamp() drops __name__; recover it from the selector
    rows = [{"metric": r["metric"].get("__name__", name), "labels": labels_str(r["metric"]), "last_sample": iso(r["value"][1]), "age_s": int(now - float(r["value"][1]))} for r in d["result"]]
    rows.sort(key=lambda r: -r["age_s"])
    emit(rows, ["metric", "labels", "last_sample", "age_s"], a, d)
    oldest = rows[0]["age_s"] if rows else 0
    sys.stderr.write(f"{len(rows)} series, oldest {oldest}s\n")
    if a.max_age is not None and (not rows or oldest > a.max_age): sys.exit(f"stale: {sum(r['age_s'] > a.max_age for r in rows)} of {len(rows)} series older than {a.max_age}s")

def cmd_rules(a):
    d = amp(env_of(a.env), "rules", {}, "GET"); rows = []
    for g in d.get("groups", []):
        for r in g.get("rules", []):
            if a.firing and r.get("state") not in ("firing", "pending"): continue
            rows.append({"group": g["name"], "rule": r["name"], "type": r.get("type", ""), "state": r.get("state", ""), "health": r.get("health", ""),
                         "active": len(r.get("alerts") or []), "last_eval": (r.get("lastEvaluation") or "")[:19]})
    emit(rows, ["group", "rule", "type", "state", "health", "active", "last_eval"], a, d)
    sys.stderr.write(f"{len(rows)} rules\n")

def cmd_alerts(a):
    d = amp(env_of(a.env), "alerts", {}, "GET")
    rows = [{"alertname": al["labels"].get("alertname", ""), "state": al.get("state", ""), "since": (al.get("activeAt") or "")[:19],
             "labels": labels_str(al["labels"], ("__name__", "alertname")),
             "summary": (al.get("annotations", {}).get("summary") or al.get("annotations", {}).get("description") or "")[:120]} for al in d.get("alerts", [])]
    rows.sort(key=lambda r: (r["state"], r["alertname"]))
    emit(rows, ["alertname", "state", "since", "labels", "summary"], a, d)
    sys.stderr.write(f"{len(rows)} alerts\n")

def ns_window(s):
    start, end = window(s); return int(start * 1e9), int(end * 1e9)

def log_line(line, fields):
    try: j = json.loads(line)
    except ValueError: return line[:300]
    if not isinstance(j, dict): return line[:300]
    if isinstance(j.get("log_processed"), dict): j = {**j["log_processed"], **j}
    out = " ".join(f"{k}={j[k]}" for k in fields if k in j)
    return out or line[:300]

def cmd_logs(a):
    start, end = ns_window(a.since); fields = a.fields.split(",")
    d = loki(env_of(a.env), "query_range", {"query": a.query, "start": start, "end": end, "limit": a.limit, "direction": "backward"})
    if d.get("resultType") != "streams": sys.exit("that LogQL returns a matrix, not lines; use `jx obs <env> logcount` for metric queries")
    ents = sorted(((int(ts), s["stream"], ln) for s in d["result"] for ts, ln in s["values"]), key=lambda e: -e[0])[:a.limit]
    rows = [{"time": iso(ts / 1e9), "container": st.get("kubernetes_container_name", ""), "pod": st.get("kubernetes_pod_name", ""), "line": log_line(ln, fields)} for ts, st, ln in ents]
    emit(rows, ["time", "container", "pod", "line"], a, d)
    sys.stderr.write(f"{len(rows)} lines from {len(d['result'])} streams\n")

def cmd_logcount(a):
    start, end = ns_window(a.since)
    q = f"sum by ({a.by}) (count_over_time({a.query}[{a.step}]))"
    d = loki(env_of(a.env), "query_range", {"query": q, "start": start, "end": end, "step": a.step})
    rows = [{"time": iso(ts), a.by: r["metric"].get(a.by, ""), "count": int(float(v))} for r in d["result"] for ts, v in r["values"]]
    rows.sort(key=lambda r: (r["time"], r[a.by]))
    emit(rows, ["time", a.by, "count"], a, d)
    sys.stderr.write(f"{len(d['result'])} series, {len(rows)} rows\n")

def cmd_loki_labels(a):
    start, end = ns_window(a.since); env = env_of(a.env)
    d = loki(env, f"label/{a.name}/values" if a.name else "labels", {"start": start, "end": end})
    col = "value" if a.name else "label"
    emit([{col: v} for v in sorted(d)], [col], a, d)
    sys.stderr.write(f"{len(d)} {col}s\n")

def cmd_doctor(a):
    env = env_of(a.env); d = amp(env, "query", {"query": "count(up)"})
    up = d["result"][1] if d["resultType"] == "scalar" else d["result"][0]["value"][1]
    try: loki(env, "labels", {}); lk = "ok"
    except SystemExit as e: lk = f"ERR {str(e).splitlines()[0]}"
    print(f"env={a.env} profile={env['profile']} region={env['region']} workspace={env['workspace']} amp=ok up_series={up} loki_url={env.get('loki', 'unset')} loki={lk}")

def main():
    ap = argparse.ArgumentParser(prog="jx obs", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("env", help="environment key from the config file, or `all`"); ap.add_argument("--json", action="store_true", help="raw API JSON instead of TSV")
    g = argparse.ArgumentParser(add_help=False); g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw API JSON instead of TSV")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    def win(p, default): p.add_argument("--since", default=default, help=f"lookback window: 30m, 6h, 7d ({default})")
    p = sp.add_parser("q", parents=[g], help="PromQL instant query, or range query with --since"); p.add_argument("query", help="PromQL")
    p.add_argument("--since", help="range query window: 30m, 6h, 7d"); p.add_argument("--step", default="5m", help="range step (5m)"); p.add_argument("--limit", type=int, default=100, help="max series printed (100)"); p.set_defaults(f=cmd_q)
    p = sp.add_parser("metrics", parents=[g], help="metric names in the workspace"); p.add_argument("prefix", nargs="?", help="keep names starting with this (all)"); p.set_defaults(f=cmd_metrics)
    p = sp.add_parser("series", parents=[g], help="series count and label names per metric matching a selector"); p.add_argument("selector", help="PromQL selector, e.g. '{job=\"api\"}' or 'up'"); win(p, "1h"); p.set_defaults(f=cmd_series)
    p = sp.add_parser("labels", parents=[g], help="label census: distinct values and samples per label name"); p.add_argument("selector", help="PromQL selector"); win(p, "1h"); p.set_defaults(f=cmd_labels)
    p = sp.add_parser("fresh", parents=[g], help="age of the last sample per series, oldest first"); p.add_argument("selector", help="PromQL selector (instant vector)")
    p.add_argument("--max-age", type=int, help="exit 1 when any series' last sample is older than this many seconds (off)"); p.set_defaults(f=cmd_fresh)
    p = sp.add_parser("rules", parents=[g], help="alerting and recording rules with state and health"); p.add_argument("--firing", action="store_true", help="only rules in state firing or pending"); p.set_defaults(f=cmd_rules)
    sp.add_parser("alerts", parents=[g], help="active alerts with state, active-since, labels, summary").set_defaults(f=cmd_alerts)
    p = sp.add_parser("logs", parents=[g], help="Loki log lines newest first; JSON lines reduced to --fields"); p.add_argument("query", help="LogQL stream selector with optional filters"); win(p, "1h")
    p.add_argument("--limit", type=int, default=100, help="max lines (100)"); p.add_argument("--fields", default="event,message,level,exception", help="comma list of JSON keys to keep, also looked up under log_processed (event,message,level,exception)"); p.set_defaults(f=cmd_logs)
    p = sp.add_parser("logcount", parents=[g], help="log volume per step per label value"); p.add_argument("query", help="LogQL stream selector with optional filters"); win(p, "6h")
    p.add_argument("--step", default="1h", help="bucket width (1h)"); p.add_argument("--by", default="kubernetes_container_name", help="stream label to group by (kubernetes_container_name)"); p.set_defaults(f=cmd_logcount)
    p = sp.add_parser("loki-labels", parents=[g], help="Loki stream label names, or the values of one label"); p.add_argument("name", nargs="?", help="label name whose values to list (names)"); win(p, "1h"); p.set_defaults(f=cmd_loki_labels)
    sp.add_parser("doctor", parents=[g], help="config resolved, AMP reachable and signed, Loki reachable").set_defaults(f=cmd_doctor)
    a = ap.parse_args()
    if a.env != "all": a.f(a); return
    first = True
    for env in cfg():
        a.env = env; buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try: a.f(a)
            except SystemExit as e: sys.stderr.write(f"[{env}] {e}\n"); continue
        if a.json or a.cmd == "doctor": print(buf.getvalue(), end=""); continue
        lines = buf.getvalue().splitlines()
        if first and lines: print("env\t" + lines[0]); first = False
        for ln in lines[1:]: print(f"{env}\t{ln}")

if __name__ == "__main__": main()
