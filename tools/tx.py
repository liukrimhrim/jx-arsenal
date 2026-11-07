#!/usr/bin/env python3
"""jx tx — the agent's own session transcripts for the current project. TSV by default, --json for raw.

  jx tx sessions [--since 7d] [--limit 30] [--grep REGEX]   sessions newest first: id, started, last, messages, title (or first prompt)
  jx tx usage [--since 7d] [--limit 50] [--grep REGEX] [--by session|day|week]
                                                 what each session cost: model calls, output/thinking/input tokens, wall vs model vs tool minutes, Bash calls split jx / heredoc / raw
  jx tx grep <REGEX> [--since 7d] [--role user|assistant|any] [--sessions N] [--limit 100] [--subagents]   matching messages: session, time, role, snippet
  jx tx cmds [--grep REGEX] [--since 7d] [--sessions N] [--limit 200] [--full]   Bash commands past sessions ran: session, time, description, command
  jx tx show <session-prefix> [--role R] [--limit 50] [--grep REGEX] [--tools]   one session's messages in order: time, role, text

Transcripts live in ~/.claude/projects/<slug>/<session>.jsonl (subagents under <session>/subagents/). <slug> is the cwd
with every non-alphanumeric character replaced by `-`; --project PATH picks another checkout. Nothing machine-specific lives in this script.
"""
import argparse, glob, json, os, re, signal, sys
from datetime import datetime, timedelta, timezone
signal.signal(signal.SIGPIPE, signal.SIG_DFL)

def proj_dir(a):
    root = os.path.abspath(a.project or os.getcwd())
    d = os.path.join(os.path.expanduser("~/.claude/projects"), re.sub(r"[^A-Za-z0-9_-]", "-", root))
    if not os.path.isdir(d): sys.exit(f"no transcripts for {root} ({d}); pass --project PATH")
    return d

def since(s):
    """30m, 6h, 7d, 2w: that long ago, in UTC. Anything else is parsed as an ISO time (a naive one is UTC)."""
    m = re.fullmatch(r"(\d+)([smhdw])", s or "")
    if m: return datetime.now(timezone.utc) - timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)]: int(m.group(1))})
    try: dt = datetime.fromisoformat((s or "").replace("Z", "+00:00")); return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError: sys.exit(f"bad time {s!r}; use 30m, 6h, 7d, 2w, or an ISO time like 2026-01-31T00:00:00Z")

def records(path):
    with open(path, errors="replace") as fh:
        for line in fh:
            try: yield json.loads(line)
            except ValueError: continue

def text_of(rec, tools=True):
    """Plain text of a user/assistant record: text parts joined; tool_use and tool_result parts summarised (dropped when tools=False); thinking parts skipped."""
    m = rec.get("message") or {}; c = m.get("content")
    if isinstance(c, str): return c
    out = []
    for p in c or []:
        t = p.get("type")
        if t == "text": out.append(p.get("text", ""))
        elif not tools: continue
        elif t == "tool_use": out.append(f"[tool_use {p.get('name')}] " + json.dumps(p.get("input", {}))[:400])
        elif t == "tool_result":
            r = p.get("content"); out.append("[tool_result] " + (r if isinstance(r, str) else " ".join(x.get("text", "") for x in r or [] if isinstance(x, dict)))[:400])
    return "\n".join(out)

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, a): print(json.dumps(rows, indent=1)) if a.json else tsv(rows, header)
def ts(s): return (s or "")[:19] + ("Z" if s else "")

TS_RX = re.compile(r'"timestamp":"([^"]+)"')

def session_summary(path):
    """One pass over the raw lines; JSON is parsed only for title records and until the first real user prompt is found."""
    sid = os.path.basename(path)[:-6]; first = last = title = prompt = None; n = 0
    with open(path, errors="replace") as fh:
        for line in fh:
            if '"type":"custom-title"' in line: title = json.loads(line).get("customTitle") or title; continue
            if '"type":"user"' not in line and '"type":"assistant"' not in line: continue
            n += 1; m = TS_RX.search(line)
            if m: first = first or m.group(1); last = m.group(1)
            if prompt is None and '"type":"user"' in line and '"isSidechain":true' not in line and '"tool_result"' not in line:
                txt = text_of(json.loads(line)).strip()
                if txt and not txt.startswith("<"): prompt = txt.splitlines()[0][:100]
    return {"session": sid, "started": ts(first), "last": ts(last), "messages": n, "title": title or prompt or ""}

def session_files(d, a, limit=None):
    """Session transcripts newest first by mtime, cut at --since and at `limit` files."""
    floor = since(a.since) if a.since else None; out = []
    for f in sorted(glob.glob(os.path.join(d, "*.jsonl")), key=os.path.getmtime, reverse=True):
        if floor and datetime.fromtimestamp(os.path.getmtime(f), timezone.utc) < floor: break
        out.append(f)
        if limit and len(out) >= limit: break
    return out

def cmd_sessions(a):
    d = proj_dir(a); rows = []
    for f in session_files(d, a):
        s = session_summary(f)
        if a.grep and not re.search(a.grep, s["title"], re.I): continue
        rows.append(s)
        if len(rows) >= a.limit: break
    emit(rows, ["session", "started", "last", "messages", "title"], a); sys.stderr.write(f"{len(rows)} sessions\n")

def literal(rx):
    """A lowercase substring every parsed match must also show on the raw JSON line, or None when the regex has metacharacters or JSON-escaped chars."""
    return None if re.search(r'[.^$*+?{}\[\]\\|()"\n]', rx) else rx.lower()

def messages(path, role, lit=None):
    """Yield (record, role) for user/assistant lines, cheapest filters on the raw line first."""
    want = ('"type":"user"', '"type":"assistant"') if role == "any" else (f'"type":"{role}"',)
    with open(path, errors="replace") as fh:
        for line in fh:
            if not any(w in line for w in want) or (lit and lit not in line.lower()): continue
            try: r = json.loads(line)
            except ValueError: continue
            if r.get("type") in ("user", "assistant"): yield r, r["type"]

def snippet(text, rx, n):
    for line in text.splitlines():
        if rx.search(line): return line.strip()[:n]
    return text.strip()[:n]

def walk(d, a, role, lit=None, subagents=False):
    """Yield (session prefix, role, record) over the newest sessions; subagent transcripts are tagged role `agent`."""
    for f in session_files(d, a, a.sessions):
        sid = os.path.basename(f)[:-6]; sources = [(f, None)]
        if subagents: sources += [(s, "agent") for s in sorted(glob.glob(os.path.join(d, sid, "subagents", "*.jsonl")), key=os.path.getmtime)]
        for path, tag in sources:
            for r, rrole in messages(path, role, lit): yield sid[:8], tag or rrole, r

def cmd_grep(a):
    d = proj_dir(a); rx = re.compile(a.regex, re.I); rows = []
    for sid, role, r in walk(d, a, a.role, literal(a.regex), a.subagents):
        txt = text_of(r)
        if rx.search(txt): rows.append({"session": sid, "time": ts(r.get("timestamp")), "role": role, "snippet": snippet(txt, rx, 160)})
        if len(rows) >= a.limit: break
    emit(rows, ["session", "time", "role", "snippet"], a); sys.stderr.write(f"{len(rows)} hits; `jx tx show <session>` prints one session\n")

def cmd_cmds(a):
    d = proj_dir(a); rx = re.compile(a.grep, re.I) if a.grep else None; rows = []
    for sid, _, r in walk(d, a, "assistant", '"name":"bash"'):
        for p in (r.get("message") or {}).get("content") or []:
            if not (isinstance(p, dict) and p.get("type") == "tool_use" and p.get("name") == "Bash"): continue
            cmd = (p.get("input") or {}).get("command", "")
            if rx and not rx.search(cmd): continue
            rows.append({"session": sid, "time": ts(r.get("timestamp")), "description": (p.get("input") or {}).get("description", ""),
                         "command": cmd.replace("\n", "\\n") if a.full else cmd.splitlines()[0][:140] if cmd else ""})
        if len(rows) >= a.limit: break
    emit(rows[:a.limit], ["session", "time", "description", "command"], a); sys.stderr.write(f"{min(len(rows), a.limit)} commands\n")

def cmd_show(a):
    d = proj_dir(a); fs = glob.glob(os.path.join(d, a.session + "*.jsonl"))
    if len(fs) != 1: sys.exit(f"{len(fs)} sessions match {a.session!r}" + (": " + ", ".join(sorted(os.path.basename(f)[:-6] for f in fs)) if fs else "\n→ `jx tx sessions` lists them"))
    rx = re.compile(a.grep, re.I) if a.grep else None; rows = []
    for r, role in messages(fs[0], a.role):
        txt = text_of(r, tools=a.tools).strip()
        if not txt or (rx and not rx.search(txt)): continue
        rows.append({"time": ts(r.get("timestamp")), "role": role, "text": txt[:240]})
        if len(rows) >= a.limit: break
    emit(rows, ["time", "role", "text"], a); sys.stderr.write(f"{len(rows)} messages\n")

# ---------------- usage: what each session cost ----------------
def usage_of(path, since_dt=None):
    """Per-session cost from the transcript: distinct model calls (requestId), tokens, wall time, model vs tool time, tool mix.
    Model time is the gap from a tool_result or user text to the next assistant record; tool time is tool_use to tool_result."""
    import collections
    calls = set(); tok = collections.Counter(); first = last = None; pending = {}; tool_s = 0.0; model_s = 0.0; last_input_ts = None
    bash = collections.Counter(); title = None; models = collections.Counter(); n_user = 0
    def P(s): return datetime.fromisoformat(s.replace("Z", "+00:00"))
    for r in records(path):
        t = r.get("type")
        if t == "custom-title": title = r.get("customTitle") or title
        if t not in ("user", "assistant"): continue
        t_s = r.get("timestamp"); m = r.get("message") or {}; c = m.get("content")
        first = first or t_s; last = t_s or last  # wall time spans the first user message to the last record of either side
        if t == "assistant":
            if r.get("requestId") not in calls and m.get("usage"):
                u = m["usage"]; calls.add(r.get("requestId")); models[m.get("model")] += 1
                for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens"): tok[k] += u.get(k) or 0
                tok["thinking_tokens"] += (u.get("output_tokens_details") or {}).get("thinking_tokens") or 0
                if last_input_ts and t_s: model_s += max(0.0, (P(t_s) - P(last_input_ts)).total_seconds()); last_input_ts = None
            for p in c or [] if isinstance(c, list) else []:
                if p.get("type") == "tool_use":
                    pending[p["id"]] = t_s
                    if p.get("name") == "Bash":
                        cmd = (p.get("input") or {}).get("command", "")
                        kind = "jx" if re.search(r"(^|[\s;&|(])jx\s", cmd) else "heredoc" if re.search(r"python3?\s+(-\w+\s+)*(-\s*<<|-c\b)", cmd) else "raw"
                        bash[kind] += 1
        else:
            n_user += 1
            if isinstance(c, list):
                for p in c:
                    if p.get("type") == "tool_result" and p.get("tool_use_id") in pending and t_s:
                        tool_s += max(0.0, (P(t_s) - P(pending.pop(p["tool_use_id"]))).total_seconds())
            last_input_ts = t_s
    wall = (P(last) - P(first)).total_seconds() if first and last else 0
    return {"session": os.path.basename(path)[:-6][:8], "title": (title or "")[:50], "model": "/".join(sorted(k for k in models if k)), "calls": len(calls),
            "out_tok": tok["output_tokens"], "think_tok": tok["thinking_tokens"], "in_tok": tok["input_tokens"] + tok["cache_creation_input_tokens"], "cache_read": tok["cache_read_input_tokens"],
            "wall_min": round(wall / 60, 1), "model_min": round(model_s / 60, 1), "tool_min": round(tool_s / 60, 1),
            "bash": sum(bash.values()), "jx": bash["jx"], "heredoc": bash["heredoc"], "raw": bash["raw"], "started": ts(first)}

def cmd_usage(a):
    d = proj_dir(a); floor = since(a.since) if a.since else None; rows = []
    for f in sorted(glob.glob(os.path.join(d, "*.jsonl")), key=os.path.getmtime, reverse=True):
        if floor and datetime.fromtimestamp(os.path.getmtime(f), timezone.utc) < floor: break
        u = usage_of(f)
        if a.grep and not re.search(a.grep, u["title"], re.I): continue
        if u["calls"] == 0: continue
        rows.append(u)
        if len(rows) >= a.limit: break
    if a.by in ("day", "week"):
        import collections
        g = collections.defaultdict(lambda: collections.Counter()); n = collections.Counter()
        for r in rows:
            k = r["started"][:10] if a.by == "day" else datetime.fromisoformat(r["started"].replace("Z", "+00:00")).strftime("%G-W%V")
            n[k] += 1
            for f in ("calls", "out_tok", "think_tok", "in_tok", "cache_read", "wall_min", "model_min", "tool_min", "bash", "jx", "heredoc", "raw"): g[k][f] += r[f]
        rows = [{a.by: k, "sessions": n[k], **{f: round(v, 1) for f, v in g[k].items()}} for k in sorted(g, reverse=True)]
        header = [a.by, "sessions", "calls", "out_tok", "think_tok", "in_tok", "cache_read", "wall_min", "model_min", "tool_min", "bash", "jx", "heredoc", "raw"]
    else: header = ["session", "started", "calls", "out_tok", "think_tok", "in_tok", "cache_read", "wall_min", "model_min", "tool_min", "bash", "jx", "heredoc", "raw", "model", "title"]
    emit(rows, header, a)
    if rows and a.by == "session": sys.stderr.write(f"{len(rows)} sessions; per session median calls {sorted(r['calls'] for r in rows)[len(rows)//2]}, median out_tok {sorted(r['out_tok'] for r in rows)[len(rows)//2]}\n")

def main():
    ap = argparse.ArgumentParser(prog="jx tx", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="JSON rows instead of TSV"); ap.add_argument("--project", help="checkout path whose transcripts to read (cwd)")
    g = argparse.ArgumentParser(add_help=False); g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON rows instead of TSV"); g.add_argument("--project", default=argparse.SUPPRESS, help="checkout path whose transcripts to read (cwd)")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sp.add_parser("sessions", parents=[g], help="sessions newest first with title or first prompt"); p.add_argument("--since", help="only sessions touched within 12h, 7d, 4w (all)"); p.add_argument("--limit", type=int, default=30, help="max sessions (30)"); p.add_argument("--grep", help="regex over the title (none)"); p.set_defaults(f=cmd_sessions)
    def scope(p, sessions_help="newest N sessions to read (all)"):
        p.add_argument("--since", help="only sessions touched within 12h, 7d, 4w (all)"); p.add_argument("--sessions", type=int, help=sessions_help)
    p = sp.add_parser("grep", parents=[g], help="messages matching a regex, newest sessions first"); p.add_argument("regex", help="case-insensitive regex over message text"); scope(p)
    p.add_argument("--role", choices=["user", "assistant", "any"], default="any", help="which side of the conversation (any)"); p.add_argument("--limit", type=int, default=100, help="stop after this many hits (100)")
    p.add_argument("--subagents", action="store_true", help="also read <session>/subagents/*.jsonl, shown as role agent (off)"); p.set_defaults(f=cmd_grep)
    p = sp.add_parser("cmds", parents=[g], help="every Bash command past sessions ran, newest first"); p.add_argument("--grep", help="regex over the command (none)"); scope(p)
    p.add_argument("--limit", type=int, default=200, help="stop after this many commands (200)"); p.add_argument("--full", action="store_true", help="whole command with newlines escaped instead of the first line, 140 chars (off)"); p.set_defaults(f=cmd_cmds)
    p = sp.add_parser("show", parents=[g], help="the messages of one session in order"); p.add_argument("session", help="session id or unique prefix; `jx tx sessions` lists them")
    p.add_argument("--role", choices=["user", "assistant", "any"], default="any", help="which side of the conversation (any)"); p.add_argument("--limit", type=int, default=50, help="max messages (50)")
    p.add_argument("--grep", help="regex over the message text (none)"); p.add_argument("--tools", action="store_true", help="include tool_use and tool_result parts (off)"); p.set_defaults(f=cmd_show)
    p = sp.add_parser("usage", parents=[g], help="per-session model calls, tokens, time split and Bash mix; --by aggregates per day or week")
    p.add_argument("--since", help="only sessions touched within 30m, 6h, 7d, 2w or since an ISO time"); p.add_argument("--limit", type=int, default=50, help="max sessions scanned (50)"); p.add_argument("--grep", help="regex over the title")
    p.add_argument("--by", default="session", choices=["session", "day", "week"], help="row granularity (session)"); p.set_defaults(f=cmd_usage)
    a = ap.parse_args(); a.f(a)

if __name__ == "__main__": main()
