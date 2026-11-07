#!/usr/bin/env python3
"""jx bench — paired benchmark: the same factual questions answered by fresh agent sessions with the arsenal available and with
it hidden, so model calls, tokens, time and correctness can be compared on identical work. TSV by default, --json for raw.

  jx bench run [--questions FILE] [--only ID,ID] [--arms on,off] [--max-turns 25] [--out DIR]
                                       for each question: compute the ground truth, run one session per arm, grade, record
  jx bench report <DIR>                paired table per question plus medians and on/off ratios

Questions live in ~/.config/jx-arsenal/bench/questions.json (they name your systems; keep them out of public repos):
  {"cwd": "/path/to/checkout", "suffix": " ... ANSWER: <answer>", "questions": [
     {"id": "x", "prompt": "...", "truth": "shell command whose stdout is the truth", "grade": "tokens|words|names|number",
      "setup": "optional shell command whose stdout fills {setup_key} in prompt and truth", "setup_key": "run_id", "tolerance": 0}]}

The off arm hides the arsenal three ways for the duration of one session: a `jx` shim first on PATH that exits 127, the
skill symlink moved aside so the agent's prefill does not list it, and a system-prompt line saying the command and the
checkout do not exist. The report marks an off session that still touched the checkout by path. Both are restored before the next run, including on error.
Every session is `claude -p` in the configured cwd with Bash, Read, Grep and Glob allowed; the result JSON gives calls, tokens
and cost; the session transcript gives the Bash mix (jx / heredoc / raw). Correctness is graded against the truth tokens.
"""
import argparse, importlib.util, json, os, re, shutil, signal, statistics, subprocess, sys, tempfile, time
from datetime import datetime, timezone
signal.signal(signal.SIGPIPE, signal.SIG_DFL)
HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.dirname(HERE); JX = os.path.join(ROOT, "bin")
SKILL = os.path.expanduser("~/.claude/skills/jx"); QFILE = os.path.expanduser("~/.config/jx-arsenal/bench/questions.json")

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, a): print(json.dumps(rows, indent=1)) if a.json else tsv(rows, header)

def sh(cmd, cwd, env=None):
    p = subprocess.run(cmd, shell=True, cwd=cwd, text=True, capture_output=True, env=env, timeout=300)
    if p.returncode: sys.exit(f"command failed ({p.returncode}): {cmd}\n{p.stderr.strip()[-400:]}")
    return p.stdout.strip()

def arm_env(arm, shim_dir):
    """PATH for one arm: the arsenal's bin first (on), or a failing `jx` shim first (off)."""
    env = dict(os.environ)
    env["PATH"] = (JX if arm == "on" else shim_dir) + os.pathsep + env["PATH"]
    return env

class SkillToggle:
    """Point the skill symlink at this checkout for the on arm; hide it for the off arm; always restore."""
    def __init__(self): self.orig = os.readlink(SKILL) if os.path.islink(SKILL) else None; self.existed = os.path.lexists(SKILL)
    def set(self, arm):
        if os.path.lexists(SKILL): os.unlink(SKILL)
        if arm == "on": os.symlink(ROOT, SKILL)
    def restore(self):
        if os.path.lexists(SKILL): os.unlink(SKILL)
        if self.existed and self.orig: os.symlink(self.orig, SKILL)

FENCE = ("For this task the `jx` command and the jx-arsenal checkout do not exist: do not run, read, import or look for them; "
         "use the ordinary CLIs (gh, kubectl, aws, terraform, git, temporal, modal, curl) and your own scripts.")
def run_session(prompt, cwd, env, max_turns, arm="on"):
    cmd = ["claude", "-p", prompt, "--output-format", "json", "--max-turns", str(max_turns), "--allowedTools", "Bash,Read,Grep,Glob", "--permission-mode", "bypassPermissions"]
    if arm == "off": cmd += ["--append-system-prompt", FENCE]  # the shim and the hidden skill are not enough: an agent that knows the checkout path runs bin/jx directly
    t0 = time.time(); p = subprocess.run(cmd, cwd=cwd, env=env, text=True, capture_output=True, stdin=subprocess.DEVNULL, timeout=1800)
    try: d = json.loads(p.stdout)
    except ValueError: d = {"result": p.stdout[-500:], "is_error": True, "stderr": p.stderr[-500:]}
    d["wall_s"] = round(time.time() - t0, 1); return d

def transcript_mix(session_id, cwd):
    """Bash call mix from the session transcript, through tx.usage_of."""
    spec = importlib.util.spec_from_file_location("jx_tx", os.path.join(HERE, "tx.py")); tx = importlib.util.module_from_spec(spec); spec.loader.exec_module(tx)
    slug = re.sub(r"[^A-Za-z0-9_-]", "-", os.path.abspath(cwd)); path = os.path.join(os.path.expanduser("~/.claude/projects"), slug, session_id + ".jsonl")
    for _ in range(20):
        if os.path.exists(path): break
        time.sleep(0.5)
    if not os.path.exists(path): return {}
    u = tx.usage_of(path)
    # contamination: did the session touch the arsenal by path even though `jx` was hidden?
    u["touched_arsenal"] = any("jx-arsenal" in json.dumps((p.get("input") or {})) for r in tx.records(path) if r.get("type") == "assistant"
                               for p in ((r.get("message") or {}).get("content") or []) if isinstance(p, dict) and p.get("type") == "tool_use")
    return u

def answer_line(result): m = re.search(r"ANSWER:\s*(.+)", result or "", re.S); return (m.group(1) if m else result or "").strip()
def toks(s):
    s = re.sub(r"(\d)t(\d)", r"\1 \2", s.lower())  # 2026-06-04T19:33 and "2026-06-04 19:33" are the same answer
    return {w for w in re.findall(r"[a-z0-9][a-z0-9._#-]*", s) if len(w) >= 2}
def grade(kind, truth, answer, tolerance=0):
    a = answer.lower(); tt = [t for t in truth.splitlines() if t.strip()]
    if kind == "number":
        if not tt: return False
        want = float(re.search(r"-?\d+(?:\.\d+)?", tt[0]).group()); found = [float(x) for x in re.findall(r"(?<![\d.])-?\d+(?:\.\d+)?(?![\d.])", a)]
        return any(abs(x - want) <= tolerance for x in found)
    if kind == "names": return all(any(part in a for part in t.lower().split() if len(part) >= 3) for t in tt)
    if kind == "words":
        want = {w for t in tt for w in re.findall(r"[a-z0-9]{4,}", t.lower())}; return bool(want) and len(want & toks(a)) / len(want) >= 0.6
    want = {w for t in tt for w in toks(t)}; return bool(want) and want <= toks(a)

def cmd_run(a):
    cfg = json.load(open(a.questions)); cwd = cfg["cwd"]; qs = [q for q in cfg["questions"] if not a.only or q["id"] in a.only.split(",")]
    arms = a.arms.split(","); out = a.out or os.path.join(os.path.expanduser("~/.config/jx-arsenal/bench/results"), datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    os.makedirs(out, exist_ok=True); shim = tempfile.mkdtemp(prefix="jx-off-")
    with open(os.path.join(shim, "jx"), "w") as f: f.write("#!/bin/sh\necho 'jx: command not found' >&2\nexit 127\n")
    os.chmod(os.path.join(shim, "jx"), 0o755); toggle = SkillToggle(); rows = []
    sys.stderr.write(f"results → {out}\nwarning: the skill symlink {SKILL} is repointed/hidden during each session and restored after\n")
    try:
        for i, q in enumerate(qs):
            fill = lambda text: text  # plain token substitution, because truth commands carry awk braces that str.format would eat
            if q.get("setup"):
                value = sh(q["setup"], cwd, arm_env("on", shim)); key = "{" + q["setup_key"] + "}"; fill = lambda text, k=key, v=value: text.replace(k, v)
            prompt = fill(q["prompt"]) + cfg.get("suffix", ""); truth = sh(fill(q["truth"]), cwd, arm_env("on", shim))
            order = arms if i % 2 == 0 else list(reversed(arms))  # alternate which arm goes first so drift does not favour one
            for arm in order:
                toggle.set(arm); sys.stderr.write(f"[{q['id']}] {arm} …"); sys.stderr.flush()
                d = run_session(prompt, cwd, arm_env(arm, shim), a.max_turns, arm); toggle.restore()
                mix = transcript_mix(d.get("session_id", ""), cwd) if d.get("session_id") else {}
                u = d.get("usage") or {}; ans = answer_line(d.get("result"))
                row = {"question": q["id"], "arm": arm, "pass": grade(q.get("grade", "tokens"), truth, ans, q.get("tolerance", 0)), "calls": mix.get("calls") or d.get("num_turns"), "out_tok": u.get("output_tokens"),
                       "think_tok": (u.get("output_tokens_details") or {}).get("thinking_tokens"), "in_tok": (u.get("input_tokens") or 0) + (u.get("cache_creation_input_tokens") or 0), "cache_read": u.get("cache_read_input_tokens"),
                       "cost_usd": round(d.get("total_cost_usd") or 0, 2), "wall_s": d.get("wall_s"), "tool_min": mix.get("tool_min"), "bash": mix.get("bash"), "jx": mix.get("jx"), "heredoc": mix.get("heredoc"), "raw": mix.get("raw"),
                       "session": d.get("session_id"), "truth": truth.replace("\n", " | ")[:120], "answer": ans[:160], "error": bool(d.get("is_error")), "touched_arsenal": mix.get("touched_arsenal")}
                rows.append(row); json.dump({**row, "prompt": prompt, "result": d.get("result"), "raw": {k: v for k, v in d.items() if k != "result"}}, open(os.path.join(out, f"{q['id']}-{arm}.json"), "w"), indent=1)
                sys.stderr.write(f" {'PASS' if row['pass'] else 'FAIL'} calls={row['calls']} out={row['out_tok']} ${row['cost_usd']} {row['wall_s']}s\n")
    finally:
        toggle.restore(); shutil.rmtree(shim, ignore_errors=True)
    json.dump(rows, open(os.path.join(out, "summary.json"), "w"), indent=1)
    emit(rows, ["question", "arm", "pass", "calls", "out_tok", "think_tok", "in_tok", "cost_usd", "wall_s", "tool_min", "bash", "jx", "heredoc", "raw", "touched_arsenal", "answer"], a)
    sys.stderr.write(f"summary: {out}/summary.json  →  `jx bench report {out}`\n")

def cmd_report(a):
    rows = json.load(open(os.path.join(a.dir, "summary.json"))); arms = sorted({r["arm"] for r in rows}); fields = ["calls", "out_tok", "think_tok", "cost_usd", "wall_s"]
    by = {(r["question"], r["arm"]): r for r in rows}; qs = sorted({r["question"] for r in rows}); out = []
    for q in qs:
        row = {"question": q}
        for arm in arms:
            r = by.get((q, arm), {}); row[f"{arm}_pass"] = r.get("pass"); [row.__setitem__(f"{arm}_{f}", r.get(f)) for f in fields]
        out.append(row)
    med = {"question": "MEDIAN"}; ratio = {"question": "on/off"}
    for arm in arms:
        med[f"{arm}_pass"] = f"{sum(1 for q in qs if by.get((q, arm), {}).get('pass'))}/{len(qs)}"
        for f in fields:
            vals = [by[(q, arm)][f] for q in qs if (q, arm) in by and by[(q, arm)].get(f) is not None]; med[f"{arm}_{f}"] = round(statistics.median(vals), 1) if vals else None
    if {"on", "off"} <= set(arms):
        for f in fields:
            ratio[f"on_{f}"] = round(med[f"on_{f}"] / med[f"off_{f}"], 2) if med.get(f"off_{f}") else None
    header = ["question"] + [f"{arm}_{c}" for arm in arms for c in ["pass"] + fields]
    emit(out + [med, ratio], header, a)

def main():
    ap = argparse.ArgumentParser(prog="jx bench", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="JSON rows instead of TSV")
    g = argparse.ArgumentParser(add_help=False); g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON rows instead of TSV")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sp.add_parser("run", parents=[g], help="run every question through one fresh session per arm, grade, and record")
    p.add_argument("--questions", default=QFILE, help=f"question set ({QFILE})"); p.add_argument("--only", help="comma-separated question ids (all)"); p.add_argument("--arms", default="on,off", help="arms to run (on,off)")
    p.add_argument("--max-turns", type=int, default=25, help="turn cap per session (25)"); p.add_argument("--out", help="results directory (a timestamped dir under the question set's results/)"); p.set_defaults(f=cmd_run)
    p = sp.add_parser("report", parents=[g], help="paired table with medians and on/off ratios from a results directory"); p.add_argument("dir", help="results directory from `jx bench run`"); p.set_defaults(f=cmd_report)
    a = ap.parse_args(); a.f(a)

if __name__ == "__main__": main()
