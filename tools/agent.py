#!/usr/bin/env python3
"""jx agent — local agent hooks; no model, network or transcript reads.

  jx agent key                         print a Review-Key for committed HEAD
  jx agent release SESSION KEY --reason TEXT   permit a deliberate repeat review
  jx agent hook                        read one Codex hook event from stdin
  jx agent hint                        read one Claude Code PreToolUse Bash event; name the jx command for a raw gh/git/uv loop

`hint` adds context only, never a permission decision, at most once per pattern per session, and
exits 0 on any error so a broken hint can never block a command.

Fresh contexts and a 16 KiB handoff limit apply to new agents. The handoff limit
also applies to followup_task/send_message. Named reviewers/auditors must carry
one Review-Key: REPOSITORY@FULL_COMMIT line; one launch per key per parent session.
This is a workflow guard, not a security boundary or semantic overlap detector.
"""
import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shlex
import sqlite3
import subprocess
import sys
import time
from urllib.parse import urlsplit

LIMIT = 16 * 1024
LEASE = 120  # A failed/interrupted spawn without a post-hook must remain retryable.
KEY = re.compile(r"^Review-Key: (\S+@[0-9a-f]{40}(?:[0-9a-f]{24})?)$", re.M)
REVIEW = re.compile(r"(?:^|[\s_/-])(?:review(?:er)?|audit(?:or)?)(?:$|[\s_/-])", re.I)


@contextmanager
def database():
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "jx"
    root.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(root / "agents.sqlite3", timeout=1)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS reviews (session TEXT, key TEXT, call TEXT, "
                       "agent TEXT, expires REAL, reason TEXT, PRIMARY KEY(session, key))")
            yield db
    finally:
        db.close()


def deny(reason):
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": "jx agent: " + reason}}


def hook(event):
    name = event.get("tool_name", "").rsplit(".", 1)[-1]
    if name not in ("spawn_agent", "followup_task", "send_message"):
        return None
    phase = event.get("hook_event_name")
    args = event.get("tool_input")
    if not isinstance(args, dict):
        return deny("expected an arguments object") if phase == "PreToolUse" else None
    session, call = event.get("session_id"), event.get("tool_use_id")
    message = args.get("message", "")
    if phase == "PostToolUse":
        if name != "spawn_agent" or not session or not call or not isinstance(message, str) or not KEY.search(message):
            return None
        response = event.get("tool_response", {})
        if isinstance(response, str):
            try:
                response = json.loads(response)
            except ValueError:
                response = {}
        agent = (response.get("task_name") or response.get("agent_id")) if isinstance(response, dict) else None
        with database() as db:
            if isinstance(agent, str) and agent:
                db.execute("UPDATE reviews SET agent=?, expires=NULL WHERE session=? AND call=?",
                           (agent, session, call))
            else:
                db.execute("DELETE FROM reviews WHERE session=? AND call=? AND agent IS NULL", (session, call))
        return None
    if phase != "PreToolUse":
        return None
    if not isinstance(message, str):
        return deny("message must be text")
    if len(message.encode("utf-8")) > LIMIT:
        return deny("handoff exceeds 16 KiB; keep task/revision/evidence pointers and put long material in a file")
    if name != "spawn_agent":
        return None
    keys = KEY.findall(message)
    labels = " ".join(str(args.get(k, "")) for k in ("agent_type", "task_name"))
    reviewer = bool(keys or REVIEW.search(labels) or re.match(r"\s*(?:review|audit)\b", message, re.I))
    if reviewer:
        if len(keys) != 1:
            return deny("reviewers need exactly one 'Review-Key: REPOSITORY@FULL_COMMIT' line; "
                        "copy it from jx review, or run jx agent key for committed local HEAD")
        if not session or not call:
            return deny("missing session_id/tool_use_id; cannot reserve a review safely")
        with database() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM reviews WHERE expires IS NOT NULL AND expires < ?", (time.time(),))
            row = db.execute("SELECT call, agent FROM reviews WHERE session=? AND key=?", (session, keys[0])).fetchone()
            if row and row[0] != call:
                release = shlex.join(["jx", "agent", "release", session, keys[0], "--reason",
                                      "new evidence or distinct required review"])
                return deny(f"review already launched/reserved for {keys[0]} by {row[1] or row[0]}; "
                            f"reuse its findings. For a necessary repeat: {release}")
            if not row:
                db.execute("INSERT INTO reviews VALUES (?, ?, ?, NULL, ?, NULL)",
                           (session, keys[0], call, time.time() + LEASE))
    # Rewrite in the original call: no model retry, no prompt truncation.
    if args.get("fork_turns") != "none":
        return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                       "updatedInput": {**args, "fork_turns": "none"}}}
    return None


# (key, pattern over the Bash command, the jx command that replaces it). Evidence: `jx tx cmds` over agent sessions.
HINTS = [
    ("pr-view", r"\bgh pr (?:view|list)\b.*--json", "`jx pr show <n> [n ...]` prints state, head sha, mergeable, check counts and unresolved threads, one row per PR (`--files`, `--body`)"),
    ("gh-jobs", r"\bgh api\b.*actions/(?:jobs|runs)/|\bgh run view\b", "`jx ci timings <run|->` (per-job seconds, pytest counts, slowest step), `jx ci jobs <run>`, `jx ci logs <job> --grep PAT`, `jx ci failed <run>`"),
    ("issue-comments", r"\bgh api\b.*issues/\d+/comments", "`jx pr comments <n> --since mine|push|6h` reads them; `jx pr comment <n> --body-file F --dry-run` posts and re-reads"),
    ("pr-edit", r"\bgh pr edit\b", "`jx pr edit <n> --body-file F --dry-run` diffs the body; without --dry-run it edits and re-reads to verify"),
    ("pr-checks", r"\bgh pr checks\b", "`jx pr ci <n> --wait`, or `jx pr triage <n> --wait` for checks plus failed-job log lines in one call"),
    ("git-show-path", r"\bgit show\s+(?!-)[^\s:|;&]+:[^\s|;&]", "`jx git cat <path> --ref <ref>` with `--grep PAT -C N -i`, `--lines A:B`, `--outline` or `--section HEADING` slices the file without sed or grep"),
    ("venv", r"\buv venv\b|\bpython3? -m venv\b", "`PY=$(jx venv path -p 3.12 -r reqs.txt PKG==V)` builds once and reuses the venv by path across sessions"),
    ("transcripts", r"\.claude/projects/\S*\.jsonl|\.claude/projects.*\bglob\b", "`jx tx sessions|grep|cmds|show` read this project's session transcripts"),
    ("kubectl-json", r"\bkubectl\b.*-o\s*json.*\|\s*(?:python|jq)", "`jx k <env> pods|images|nodes|events|logs` answer without -o json"),
    ("aws-pipe", r"\baws\s+[a-z].*\|\s*(?:jq|python)", "`jx aws whoami|cw|alarms|ecr-tags|cost|logs-query|eks`"),
    ("tf-show", r"\bterraform show\s+-json", "`jx tf diff <planfile>` prints the attribute-level diff of a saved plan"),
]

def hint(event):
    """additionalContext naming the jx equivalent of a raw command, once per pattern per session; None otherwise."""
    if event.get("hook_event_name") != "PreToolUse" or event.get("tool_name") != "Bash": return None
    command = (event.get("tool_input") or {}).get("command") or ""
    hits = [(k, h) for k, rx, h in HINTS if re.search(rx, command, re.S)]
    if not hits: return None
    # ponytail: one small file per session under $TMPDIR; the OS clears it, nothing else needs it
    seen_file = Path(os.environ.get("TMPDIR", "/tmp")) / f"jx-hints-{re.sub(r'[^A-Za-z0-9_-]', '', str(event.get('session_id', 'none')))}"
    seen = set(seen_file.read_text().split()) if seen_file.exists() else set()
    new = [(k, h) for k, h in hits if k not in seen]
    if not new: return None
    with open(seen_file, "a") as f: f.write("".join(k + "\n" for k, _ in new))
    text = "jx hint (shown once per session): " + " | ".join(h for _, h in new) + ". `jx ls` lists every command."
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}


def local_key():
    def git(*args):
        return subprocess.check_output(["git", *args], text=True, stderr=subprocess.PIPE).strip()
    if git("status", "--porcelain"):
        raise ValueError("commit the review target first; a commit key cannot identify uncommitted changes")
    head = git("rev-parse", "HEAD")
    try:
        remote = git("remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        remote = ""
    if remote.startswith("git@") and ":" in remote:
        repo = remote[4:].replace(":", "/", 1)
    elif "://" in remote:
        url = urlsplit(remote)
        repo = (url.hostname or "") + url.path
    else:
        repo = Path(git("rev-parse", "--show-toplevel")).resolve().as_uri()
    print(f"Review-Key: {repo.removesuffix('.git')}@{head}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="action", required=True)
    sub.add_parser("key")
    sub.add_parser("hook")
    sub.add_parser("hint")
    release = sub.add_parser("release")
    release.add_argument("session")
    release.add_argument("key")
    release.add_argument("--reason", required=True)
    args = ap.parse_args()
    if args.action == "hint":
        try:
            result = hint(json.load(sys.stdin))
            if result: print(json.dumps(result))
        except Exception:  # a hint must never block or error a command; say nothing instead
            pass
        return 0
    try:
        if args.action == "key":
            local_key()
        elif args.action == "release":
            if not args.reason.strip():
                ap.error("--reason must explain why another review is needed")
            with database() as db:
                count = db.execute("UPDATE reviews SET expires=0, reason=? WHERE session=? AND key=?",
                                   (args.reason, args.session, args.key)).rowcount
            print(f"released {count} review reservation(s): {args.reason}")
        else:
            result = hook(json.load(sys.stdin))
            if result:
                print(json.dumps(result))
    except (OSError, ValueError, sqlite3.Error, subprocess.CalledProcessError) as error:
        # Codex treats generic hook errors as fail-open. Explicitly deny a pre-tool
        # guard failure; post-hook errors cannot undo an already launched agent.
        print(f"jx agent: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
