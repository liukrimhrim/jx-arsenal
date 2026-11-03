#!/usr/bin/env python3
"""jx oncall — incident.io and PagerDuty lever for agents. TSV by default, --json for the raw payload. Read-only.

  jx oncall who [--at ISO]                      who is on call now (or at a time) on every schedule, both providers
  jx oncall incidents [--status open|resolved|all] [--since 7d] [--limit 50] [--provider incidentio|pagerduty|both]
                                                incidents of both providers newest first: id, ref, created, status, severity/urgency, title, owner, url
  jx oncall alerts [--status firing|resolved|all] [--since 24h] [--limit 100] [--source NAME] [--group]
                                                incident.io alerts: created, status, source, title, dedup_key, resolved; --group = dedup_key_prefix, count, firing_now, example_id
  jx oncall show <id>                           one incident, provider detected from the id: item/value, one `alert` row each, PagerDuty `event` rows (trigger/ack/resolve/escalate/notify)
  jx oncall escalations [--since 24h] [--limit 50]   incident.io pages: created, status, priority, title, path, alerts, incidents, acked_by
  jx oncall users [QUERY]                       users of both providers (provider, name, email, role, id); QUERY = substring of name or email
  jx oncall routes                              incident.io alert routes and alert sources in one table: kind, name, enabled, type, id
  jx oncall reconcile [--since 24h]             provider, kind (alerts|incidents), count, firing_or_open — do incident.io and PagerDuty agree on the window
  jx oncall api <incidentio|pagerduty> <path> [--param k=v ...] [--all]   one GET, paginated with --all; the raw-curl replacement
  jx oncall doctor

Tokens: env INCIDENTIO_API_KEY / PAGERDUTY_API_KEY win; else ~/.config/jx-arsenal/oncall.json runs a command per provider:
  {"incidentio": {"token_cmd": "op read op://vault/item/field"}, "pagerduty": {"token_cmd": "..."}}
Nothing secret or company-specific lives in this script. Tokens are never printed.
"""
import argparse, json, os, re, signal, subprocess, sys, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
CFG = os.path.expanduser("~/.config/jx-arsenal/oncall.json")
BASE = {"incidentio": "https://api.incident.io", "pagerduty": "https://api.pagerduty.com"}
_tok = {}

def token(provider):
    if provider in _tok: return _tok[provider]
    env = {"incidentio": "INCIDENTIO_API_KEY", "pagerduty": "PAGERDUTY_API_KEY"}[provider]
    t = os.environ.get(env)
    if not t:
        try: cmd = json.load(open(CFG))[provider]["token_cmd"]
        except (FileNotFoundError, KeyError): sys.exit(f"no token for {provider}: export {env}, or set {provider}.token_cmd in {CFG}")
        p = subprocess.run(cmd, shell=True, text=True, capture_output=True)
        if p.returncode or not p.stdout.strip(): sys.exit(f"{provider} token_cmd failed: {p.stderr.strip()[:300]}\n→ run it by hand: {cmd}")
        t = p.stdout.strip()
    _tok[provider] = t; return t

def get(provider, path, params=None, all_pages=False):
    """GET one page, or every page when all_pages. incident.io pages with pagination_meta.after; PagerDuty with offset/more."""
    base = BASE[provider]; items, page_key = [], None
    params = dict(params or {})
    hdr = {"Authorization": f"Bearer {token(provider)}"} if provider == "incidentio" else {"Authorization": f"Token token={token(provider)}", "Accept": "application/vnd.pagerduty+json;version=2"}
    while True:
        url = base + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60) as r: d = json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:300]
            hint = {401: f"token rejected; `jx oncall doctor` shows which source was used", 403: "token lacks this scope; a read-only key cannot see some objects", 404: f"no such path; `jx oncall api {provider} <path>` takes the path exactly as the API docs show it", 429: "rate limited; wait a minute"}.get(e.code, "")
            sys.exit(f"{provider} {e.code} {path}: {body}" + (f"\n→ {hint}" if hint else ""))
        if not all_pages: return d
        if page_key is None: page_key = next((k for k, v in d.items() if isinstance(v, list)), None)
        items += d.get(page_key, []) if page_key else []
        if callable(all_pages) and (not items or all_pages(items)): break  # a stop predicate: lists are newest-first, so stop once the window or the limit is passed
        if provider == "incidentio":
            after = (d.get("pagination_meta") or {}).get("after")
            if not after: break
            params["after"] = after
        else:
            if not d.get("more"): break
            params["offset"] = d.get("offset", 0) + d.get("limit", len(d.get(page_key, [])))
    return {page_key: items} if page_key else {}

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, a, raw=None):
    if a.json: print(json.dumps(raw if raw is not None else rows, indent=1))
    else: tsv(rows, header)
def iso(dt): return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
def parse_ts(s): return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
def ts(s): return iso(parse_ts(s)) if s else ""  # PagerDuty sub-resources carry a local offset; everything is printed as UTC
def since(s):
    m = re.fullmatch(r"(\d+)([mhd])", s or "")
    if not m: sys.exit(f"bad duration {s!r}; use 30m, 6h, 7d")
    return datetime.now(timezone.utc) - timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[m.group(2)]: int(m.group(1))})
def clip(s, n): s = s or ""; return s if len(s) <= n else s[:n - 1] + "…"
def enough(limit, floor):
    """Stop predicate for get(): past the row limit, or the newest-first page has crossed the --since floor."""
    return lambda items: len(items) >= limit or bool(floor and parse_ts(items[-1]["created_at"]) < floor)
def in_window(rows, floor, limit, key="created"):
    rows = [r for r in rows if not floor or parse_ts(r[key]) >= floor]
    rows.sort(key=lambda r: r[key], reverse=True); return rows[:limit]
def pd_id(s): return bool(re.fullmatch(r"[A-Z0-9]{12,14}", s))  # PagerDuty ids; incident.io ids are 26-char ULIDs

def cmd_api(a):
    params = dict(kv.split("=", 1) for kv in a.param)
    d = get(a.provider, a.path, params, a.all)
    print(json.dumps(d, indent=1))

def cmd_who(a):
    at = a.at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"); rows = []
    io = get("incidentio", "/v2/schedules", {"page_size": 50}, True).get("schedules", [])
    for s in io:
        shifts = s.get("current_shifts") or []
        for sh in shifts:
            u = sh.get("user") or {}; rows.append({"provider": "incidentio", "schedule": s["name"], "user": u.get("name", ""), "email": u.get("email", ""), "until": ts(sh.get("end_at")), "layer": sh.get("rotation_id") or ""})
        if not shifts: rows.append({"provider": "incidentio", "schedule": s["name"], "user": "(nobody)", "email": "", "until": "", "layer": ""})
    until = (datetime.fromisoformat(at.replace("Z", "+00:00")) + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pd = get("pagerduty", "/oncalls", {"since": at, "until": until, "limit": 100, "include[]": ["users"]}, True).get("oncalls", [])
    for o in pd:
        if not o.get("schedule"): continue  # escalation-level entries without a schedule are the policy's static targets
        rows.append({"provider": "pagerduty", "schedule": o["schedule"]["summary"], "user": o["user"]["summary"], "email": o["user"].get("email", ""), "until": ts(o.get("end")), "layer": f"L{o.get('escalation_level', '')}"})
    rows.sort(key=lambda r: (r["provider"], r["schedule"], r["layer"]))
    emit(rows, ["provider", "schedule", "user", "email", "until", "layer"], a)
    sys.stderr.write(f"at {at}: {len(io)} incident.io schedules, {len({o['schedule']['id'] for o in pd if o.get('schedule')})} PagerDuty schedules\n")

IO_OPEN = ["triage", "active", "paused"]

def io_owner(i):
    ra = [r for r in i.get("incident_role_assignments") or [] if r.get("assignee")]
    lead = next((r for r in ra if "lead" in r["role"]["name"].lower()), None) or (ra[0] if ra else {})
    return (lead.get("assignee") or {}).get("name", "")

def cmd_incidents(a):
    floor = None if a.status == "open" else since(a.since); io, pd = [], []
    if a.provider != "pagerduty":
        p = {"page_size": min(a.limit, 250)}
        if a.status != "all": p["status_category[one_of]"] = IO_OPEN if a.status == "open" else ["closed"]
        if floor: p["created_at[gte]"] = floor.strftime("%Y-%m-%d")  # the API filter takes whole days; in_window applies the exact cut
        io = get("incidentio", "/v2/incidents", p, enough(a.limit, floor)).get("incidents", [])
    if a.provider != "incidentio":
        p = {"limit": min(a.limit, 100), "sort_by": "created_at:desc", "time_zone": "UTC"}
        if a.status != "all": p["statuses[]"] = ["triggered", "acknowledged"] if a.status == "open" else ["resolved"]
        if floor: p["since"] = iso(floor)
        else: p["date_range"] = "all"  # without it PagerDuty silently limits to the last 30 days
        pd = get("pagerduty", "/incidents", p, enough(a.limit, floor)).get("incidents", [])
    rows = [{"provider": "incidentio", "id": i["id"], "ref": i.get("reference", ""), "created": ts(i["created_at"]), "status": (i.get("incident_status") or {}).get("name", ""),
             "severity_or_urgency": (i.get("severity") or {}).get("name", ""), "title": clip(i.get("name"), 100), "owner": io_owner(i), "url": i.get("permalink", "")} for i in io]
    rows += [{"provider": "pagerduty", "id": i["id"], "ref": f"#{i['incident_number']}", "created": ts(i["created_at"]), "status": i["status"],
              "severity_or_urgency": f"{(i.get('priority') or {}).get('summary', '')} {i.get('urgency', '')}".strip(), "title": clip(i.get("title"), 100),
              "owner": ((i.get("assignments") or [{}])[0].get("assignee") or {}).get("summary", ""), "url": i.get("html_url", "")} for i in pd]
    rows = in_window(rows, floor, a.limit)
    emit(rows, ["provider", "id", "ref", "created", "status", "severity_or_urgency", "title", "owner", "url"], a, {"incidentio": io, "pagerduty": pd})
    sys.stderr.write(f"{len(rows)} incidents ({a.status}{'' if a.status == 'open' else ', since ' + a.since}); `jx oncall show <id>` for alerts and the timeline of one\n")

def alert_sources():
    return {s["id"]: s for s in get("incidentio", "/v2/alert_sources").get("alert_sources", [])}

def cmd_alerts(a):
    floor = since(a.since); src = alert_sources(); p = {"page_size": 50}
    if a.status != "all": p["status[one_of]"] = [a.status]
    if a.source:
        hits = [s for s in src.values() if a.source.lower() in s["name"].lower()]
        if len(hits) != 1: sys.exit(f"--source {a.source!r} matches {len(hits)} alert sources: {', '.join(s['name'] for s in hits) or 'none'}\n→ `jx oncall routes` lists them")
        p["alert_source_id"] = hits[0]["id"]
    alerts = get("incidentio", "/v2/alerts", p, enough(a.limit if not a.group else 10**9, floor)).get("alerts", [])
    alerts = in_window(alerts, floor, 10**9, "created_at")
    if a.group:
        # Trailing hyphen-segments with digits are instance ids or counters; dropping them folds one rule's alerts together.
        prefix = lambda k: re.sub(r"(-[^-]*\d[^-]*)+$", "", k or "")
        g = {}
        for al in alerts: g.setdefault(prefix(al.get("deduplication_key")), []).append(al)
        rows = sorted(({"dedup_key_prefix": k, "count": len(v), "firing_now": sum(x["status"] == "firing" for x in v), "example_id": v[0]["id"]} for k, v in g.items()), key=lambda r: -r["count"])[:a.limit]
        emit(rows, ["dedup_key_prefix", "count", "firing_now", "example_id"], a, alerts)
    else:
        rows = [{"created": ts(al["created_at"]), "status": al["status"], "source": src.get(al.get("alert_source_id"), {}).get("name", ""), "title": clip(al.get("title"), 100),
                 "dedup_key": clip(al.get("deduplication_key"), 60), "resolved": ts(al.get("resolved_at"))} for al in alerts[:a.limit]]
        emit(rows, ["created", "status", "source", "title", "dedup_key", "resolved"], a, alerts)
    sys.stderr.write(f"{len(alerts)} alerts since {a.since} ({a.status}){' — hit --limit' if not a.group and len(alerts) >= a.limit else ''}; `jx oncall escalations` shows which ones paged\n")

PD_EVENTS = ("trigger", "acknowledge", "resolve", "escalate", "notify")

def show_pagerduty(a, id):
    i = get("pagerduty", f"/incidents/{id}", {"time_zone": "UTC"}).get("incident", {})
    alerts = get("pagerduty", f"/incidents/{id}/alerts", {"limit": 100, "time_zone": "UTC"}, True).get("alerts", [])
    log = get("pagerduty", f"/incidents/{id}/log_entries", {"limit": 100, "time_zone": "UTC"}, True).get("log_entries", [])
    ack = (i.get("acknowledgements") or [{}])[0]
    items = [("provider", "pagerduty"), ("id", i.get("id")), ("ref", f"#{i.get('incident_number', '')}"), ("status", i.get("status")), ("urgency", i.get("urgency")), ("priority", (i.get("priority") or {}).get("summary", "")),
             ("service", (i.get("service") or {}).get("summary", "")), ("policy", (i.get("escalation_policy") or {}).get("summary", "")), ("created", ts(i.get("created_at"))),
             ("acknowledged_by", (ack.get("acknowledger") or {}).get("summary", "")), ("acknowledged_at", ts(ack.get("at"))), ("resolved", ts(i.get("resolved_at"))), ("title", i.get("title")), ("url", i.get("html_url"))]
    items += [("alert", f"{ts(al.get('created_at'))} {al.get('status')} {clip(al.get('summary'), 120)}") for al in alerts]
    for l in sorted(log, key=lambda l: l.get("created_at", "")):
        kind = l.get("type", "").replace("_log_entry", "")
        who = (l.get("user") or l.get("agent") or {}).get("summary", "")  # notify entries name the person in `user`; the rest in `agent`
        if kind in PD_EVENTS: items.append(("event", f"{ts(l.get('created_at'))} {kind} {who}".rstrip()))
    emit([{"item": k, "value": v} for k, v in items], ["item", "value"], a, {"incident": i, "alerts": alerts, "log_entries": log})

def show_incidentio(a, id):
    i = get("incidentio", f"/v2/incidents/{id}").get("incident", {})
    ia = get("incidentio", "/v2/incident_alerts", {"incident_id": id, "page_size": 50}, True).get("incident_alerts", [])
    items = [("provider", "incidentio"), ("id", i.get("id")), ("ref", i.get("reference")), ("status", (i.get("incident_status") or {}).get("name", "")), ("severity", (i.get("severity") or {}).get("name", "")),
             ("mode", i.get("mode")), ("created", ts(i.get("created_at"))), ("lead", io_owner(i)), ("slack_channel", i.get("slack_channel_name", "")), ("url", i.get("permalink")), ("title", i.get("name")), ("summary", clip(i.get("summary"), 300))]
    items += [("alert", f"{ts(x['alert'].get('created_at'))} {x['alert'].get('status')} {clip(x['alert'].get('title'), 120)}") for x in ia if x.get("alert")]
    emit([{"item": k, "value": v} for k, v in items], ["item", "value"], a, {"incident": i, "incident_alerts": ia})

def cmd_show(a):
    if pd_id(a.id): show_pagerduty(a, a.id)
    elif re.fullmatch(r"01[A-Z0-9]{24}", a.id): show_incidentio(a, a.id)
    else: sys.exit(f"{a.id!r} is neither a PagerDuty id (12–14 uppercase alphanumerics) nor an incident.io id (26-char ULID); `jx oncall incidents` lists ids")

def cmd_escalations(a):
    floor = since(a.since)
    paths = {p["id"]: p["name"] for p in get("incidentio", "/v2/escalation_paths", {"page_size": 25}, True).get("escalation_paths", [])}
    esc = in_window(get("incidentio", "/v2/escalations", {"page_size": 50}, enough(a.limit, floor)).get("escalations", []), floor, a.limit, "created_at")
    rows = [{"created": ts(e["created_at"]), "status": e.get("status"), "priority": (e.get("priority") or {}).get("name", ""), "title": clip(e.get("title"), 100), "path": paths.get(e.get("escalation_path_id"), ""),
             "alerts": len(e.get("related_alerts") or []), "incidents": len(e.get("related_incidents") or []),
             "acked_by": ",".join(u.get("name", "") for ev in e.get("events") or [] if ev.get("event") == "acked" for u in ev.get("users") or [])} for e in esc]
    emit(rows, ["created", "status", "priority", "title", "path", "alerts", "incidents", "acked_by"], a, esc)
    sys.stderr.write(f"{len(rows)} escalations since {a.since}; `jx oncall alerts --since {a.since}` lists the alerts behind them\n")

def cmd_users(a):
    q = (a.query or "").lower()
    io = get("incidentio", "/v2/users", {"page_size": 250}, True).get("users", [])
    pd = get("pagerduty", "/users", {"limit": 100, **({"query": a.query} if a.query else {})}, True).get("users", [])
    rows = [{"provider": "incidentio", "name": u.get("name", ""), "email": u.get("email", ""), "role": u.get("role", ""), "id": u.get("id")} for u in io]
    rows += [{"provider": "pagerduty", "name": u.get("name", ""), "email": u.get("email", ""), "role": u.get("role", ""), "id": u.get("id")} for u in pd]
    rows = sorted((r for r in rows if q in r["name"].lower() or q in r["email"].lower()), key=lambda r: (r["name"].lower(), r["provider"]))
    emit(rows, ["provider", "name", "email", "role", "id"], a, {"incidentio": io, "pagerduty": pd})
    sys.stderr.write(f"{len(rows)} users{' matching ' + repr(a.query) if a.query else ''}; `jx oncall who` shows who is on call\n")

def cmd_routes(a):
    routes = get("incidentio", "/v2/alert_routes", {"page_size": 50}, True).get("alert_routes", [])
    src = alert_sources()
    rows = [{"kind": "route", "name": r.get("name", ""), "enabled": str(r.get("enabled", "")).lower(), "type": "", "id": r.get("id")} for r in routes]
    rows += [{"kind": "source", "name": s.get("name", ""), "enabled": "", "type": s.get("source_type", ""), "id": s.get("id")} for s in src.values()]
    emit(rows, ["kind", "name", "enabled", "type", "id"], a, {"alert_routes": routes, "alert_sources": list(src.values())})
    sys.stderr.write(f"{len(routes)} alert routes, {len(src)} alert sources; `jx oncall alerts --source NAME` filters by source\n")

def cmd_reconcile(a):
    floor = since(a.since); day = floor.strftime("%Y-%m-%d")
    al = in_window(get("incidentio", "/v2/alerts", {"page_size": 50}, enough(10**9, floor)).get("alerts", []), floor, 10**9, "created_at")
    io = in_window(get("incidentio", "/v2/incidents", {"page_size": 250, "created_at[gte]": day}, enough(10**9, floor)).get("incidents", []), floor, 10**9, "created_at")
    pd = get("pagerduty", "/incidents", {"limit": 100, "since": iso(floor), "sort_by": "created_at:desc", "time_zone": "UTC"}, True).get("incidents", [])
    rows = [{"provider": "incidentio", "kind": "alerts", "count": len(al), "firing_or_open": sum(x.get("status") == "firing" for x in al)},
            {"provider": "incidentio", "kind": "incidents", "count": len(io), "firing_or_open": sum((x.get("incident_status") or {}).get("category") in IO_OPEN for x in io)},
            {"provider": "pagerduty", "kind": "incidents", "count": len(pd), "firing_or_open": sum(x.get("status") in ("triggered", "acknowledged") for x in pd)}]
    emit(rows, ["provider", "kind", "count", "firing_or_open"], a, {"alerts": al, "incidentio_incidents": io, "pagerduty_incidents": pd})
    sys.stderr.write(f"window {iso(floor)} .. now ({a.since}); `jx oncall incidents --since {a.since}` and `jx oncall alerts --since {a.since} --group` break these down\n")

def cmd_doctor(a):
    for p in ("incidentio", "pagerduty"):
        src = "env" if os.environ.get({"incidentio": "INCIDENTIO_API_KEY", "pagerduty": "PAGERDUTY_API_KEY"}[p]) else f"token_cmd in {CFG}"
        try:
            d = get(p, "/v2/users" if p == "incidentio" else "/users", {"page_size": 1} if p == "incidentio" else {"limit": 1})
            print(f"{p}\tok\tsource={src}\tusers_visible={'yes' if (d.get('users') or []) else 'no'}")
        except SystemExit as e: print(f"{p}\tFAIL\t{str(e).splitlines()[0][:120]}")

def main():
    ap = argparse.ArgumentParser(prog="jx oncall", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="raw API JSON instead of TSV")
    g = argparse.ArgumentParser(add_help=False); g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw API JSON instead of TSV")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sp.add_parser("who", parents=[g], help="who is on call on every schedule of both providers"); p.add_argument("--at", help="ISO time (now)"); p.set_defaults(f=cmd_who)
    p = sp.add_parser("incidents", parents=[g], help="incidents of both providers, newest first")
    p.add_argument("--status", choices=["open", "resolved", "all"], default="all", help="open = incident.io triage/active/paused + PagerDuty triggered/acknowledged, any age; resolved = closed/resolved (all)")
    p.add_argument("--since", default="7d", help="created in this window, e.g. 2d, 12h; ignored for --status open (7d)"); p.add_argument("--limit", type=int, default=50, help="rows after merging (50)")
    p.add_argument("--provider", choices=["incidentio", "pagerduty", "both"], default="both", help="(both)"); p.set_defaults(f=cmd_incidents)
    p = sp.add_parser("alerts", parents=[g], help="incident.io alerts newest first; --group counts by dedup key prefix")
    p.add_argument("--status", choices=["firing", "resolved", "all"], default="all", help="(all)"); p.add_argument("--since", default="24h", help="created in this window (24h)")
    p.add_argument("--limit", type=int, default=100, help="rows (100)"); p.add_argument("--source", help="alert source name, case-insensitive substring; `jx oncall routes` lists them")
    p.add_argument("--group", action="store_true", help="dedup_key_prefix, count, firing_now, example_id over the whole window"); p.set_defaults(f=cmd_alerts)
    p = sp.add_parser("show", parents=[g], help="one incident of either provider: item/value, then its alerts and timeline"); p.add_argument("id", help="PagerDuty or incident.io incident id; `jx oncall incidents` lists them"); p.set_defaults(f=cmd_show)
    p = sp.add_parser("escalations", parents=[g], help="incident.io escalations (pages) newest first with path, counts and who acked")
    p.add_argument("--since", default="24h", help="created in this window (24h)"); p.add_argument("--limit", type=int, default=50, help="rows (50)"); p.set_defaults(f=cmd_escalations)
    p = sp.add_parser("users", parents=[g], help="users of both providers: name, email, role, id"); p.add_argument("query", nargs="?", help="case-insensitive substring of name or email (all users)"); p.set_defaults(f=cmd_users)
    sp.add_parser("routes", parents=[g], help="incident.io alert routes (enabled) and alert sources (type), one table with a kind column").set_defaults(f=cmd_routes)
    p = sp.add_parser("reconcile", parents=[g], help="do the providers agree: alert and incident counts per provider in one window, with how many are still firing/open")
    p.add_argument("--since", default="24h", help="created in this window (24h)"); p.set_defaults(f=cmd_reconcile)
    p = sp.add_parser("api", parents=[g], help="one GET against a provider, raw JSON; --all follows pagination"); p.add_argument("provider", choices=["incidentio", "pagerduty"]); p.add_argument("path", help="e.g. /v2/alerts or /incidents")
    p.add_argument("--param", action="append", default=[], metavar="k=v", help="query parameter, repeatable"); p.add_argument("--all", action="store_true", help="follow pagination and merge the list"); p.set_defaults(f=cmd_api)
    sp.add_parser("doctor", parents=[g], help="token source and a 1-item read per provider").set_defaults(f=cmd_doctor)
    a = ap.parse_args(); a.f(a)

if __name__ == "__main__": main()
