import concurrent.futures
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/agent.py"
KEY = "github.com/example/repo@" + "a" * 40


class AgentHookTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = {**os.environ, "XDG_STATE_HOME": self.temp.name}
        self.db = Path(self.temp.name) / "jx/agents.sqlite3"

    def invoke(self, event):
        p = subprocess.run([sys.executable, str(SCRIPT), "hook"], input=json.dumps(event),
                           text=True, capture_output=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout else {}

    def event(self, call="one", key=KEY, **args):
        return {"hook_event_name": "PreToolUse", "tool_name": "spawn_agent",
                "session_id": "parent", "tool_use_id": call,
                "tool_input": {"task_name": "code_review", "message": f"Review-Key: {key}\nReview the diff.", **args}}

    def finish(self, event, response):
        self.invoke({**event, "hook_event_name": "PostToolUse", "tool_response": response})

    def test_fresh_context_rewrite_preserves_task_and_other_arguments(self):
        for setting in ({}, {"fork_turns": "all"}, {"fork_turns": "none"}):
            args = {"task_name": "implement", "message": "Fix one thing", "agent_type": "worker", **setting}
            out = self.invoke({"hook_event_name": "PreToolUse", "tool_name": "collaboration.spawn_agent", "tool_input": args})
            if setting.get("fork_turns") == "none":
                self.assertEqual(out, {})
            else:
                self.assertEqual(out["permissionDecision"], "allow")
                self.assertEqual(out["updatedInput"], {**args, "fork_turns": "none"})
        self.assertFalse(self.db.exists())

    def test_byte_limit_applies_to_launch_followup_and_message_without_truncation(self):
        for name in ("spawn_agent", "followup_task", "send_message"):
            event = {"hook_event_name": "PreToolUse", "tool_name": name,
                     "tool_input": {"task_name": "worker", "fork_turns": "none", "message": "é" * 8192}}
            self.assertEqual(self.invoke(event), {})
            event["tool_input"]["message"] += "x"
            self.assertEqual(self.invoke(event)["permissionDecision"], "deny")
        self.assertFalse(self.db.exists())

    def test_review_labels_require_one_valid_commit_key(self):
        for label in ("final_audit", "fix-review", "code-reviewer"):
            for message in ("Check changes", "Review-Key: x@123", f"Review-Key: {KEY}\nReview-Key: {KEY}"):
                self.assertEqual(self.invoke(self.event(task_name=label, message=message))["permissionDecision"], "deny")
        self.assertFalse(self.db.exists())

    def test_changed_wording_or_name_cannot_repeat_same_review(self):
        first = self.event()
        self.invoke(first)
        self.finish(first, json.dumps({"task_name": "/root/code_review"}))
        out = self.invoke(self.event("two", task_name="final_audit", message=f"Review-Key: {KEY}\nAudit everything again."))
        self.assertEqual(out["permissionDecision"], "deny")
        self.assertIn("/root/code_review", out["permissionDecisionReason"])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT agent, expires FROM reviews").fetchone(), ("/root/code_review", None))

    def test_revised_commit_or_another_parent_session_is_independent(self):
        self.invoke(self.event())
        self.assertNotEqual(self.invoke(self.event("two", key=KEY[:-1] + "b")).get("permissionDecision"), "deny")
        self.assertNotEqual(self.invoke({**self.event("three"), "session_id": "other"}).get("permissionDecision"), "deny")

    def test_replayed_same_call_is_idempotent_and_concurrent_calls_have_one_winner(self):
        first = self.event()
        self.invoke(first)
        self.assertNotEqual(self.invoke(first).get("permissionDecision"), "deny")
        events = [{**self.event(str(i)), "session_id": "parallel"} for i in range(4)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.invoke, events))
        self.assertEqual(sum(x.get("permissionDecision") == "deny" for x in results), 3)

    def test_failed_spawn_releases_reservation_but_other_calls_cannot_release_it(self):
        first = self.event()
        self.invoke(first)
        self.finish(self.event("unrelated"), {"error": "spawn failed"})
        self.assertEqual(self.invoke(self.event("two"))["permissionDecision"], "deny")
        self.finish(first, "tool error")
        self.assertNotEqual(self.invoke(self.event("two")).get("permissionDecision"), "deny")

    def test_abandoned_reservation_expires_and_explicit_release_allows_repeat(self):
        first = self.event()
        self.invoke(first)
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE reviews SET expires=0")
        second = self.event("two")
        self.invoke(second)
        self.finish(second, {"agent_id": "reviewer-2"})
        p = subprocess.run([sys.executable, str(SCRIPT), "release", "parent", KEY, "--reason", "new comments"],
                           env=self.env, text=True, capture_output=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("released 1", p.stdout)
        self.assertNotEqual(self.invoke(self.event("three")).get("permissionDecision"), "deny")

    def test_guard_errors_deny_and_irrelevant_tools_do_nothing(self):
        event = self.event()
        del event["session_id"]
        self.assertEqual(self.invoke(event)["permissionDecision"], "deny")
        self.assertEqual(self.invoke({"hook_event_name": "PreToolUse", "tool_name": "Bash"}), {})
        self.db.parent.mkdir()
        self.db.write_text("broken database")
        p = subprocess.run([sys.executable, str(SCRIPT), "hook"], input=json.dumps(self.event()),
                           text=True, capture_output=True, env=self.env)
        self.assertEqual(p.returncode, 2)
        self.assertIn("jx agent:", p.stderr)

    def test_local_key_uses_actual_clean_head_and_normalizes_remote(self):
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        def git(*args):
            return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.PIPE).strip()
        git("init")
        git("-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-m", "baseline")
        git("remote", "add", "origin", "git@github.com:example/repo.git")
        p = subprocess.run([sys.executable, str(SCRIPT), "key"], cwd=repo, capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(), "Review-Key: github.com/example/repo@" + git("rev-parse", "HEAD"))
        (repo / "unstaged.txt").write_text("unreviewed change")
        p = subprocess.run([sys.executable, str(SCRIPT), "key"], cwd=repo, capture_output=True, text=True)
        self.assertEqual(p.returncode, 2)
        self.assertIn("uncommitted", p.stderr)



class AgentHintTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.env = {**os.environ, "TMPDIR": self.temp.name}

    def hint(self, command, session="s1", raw=None):
        event = {"hook_event_name": "PreToolUse", "tool_name": "Bash", "session_id": session, "tool_input": {"command": command}}
        p = subprocess.run([sys.executable, str(SCRIPT), "hint"], input=raw if raw is not None else json.dumps(event),
                           text=True, capture_output=True, env=self.env)
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        return json.loads(p.stdout)["hookSpecificOutput"] if p.stdout else None

    def test_raw_loop_gets_context_and_no_permission_decision(self):
        out = self.hint("gh pr view 87 --json state,headRefOid --jq .state")
        self.assertEqual(set(out), {"hookEventName", "additionalContext"})
        self.assertIn("jx pr show", out["additionalContext"])
        self.assertIn("jx git cat", self.hint("git show origin/main:docs/PLAN.md | sed -n 1,40p")["additionalContext"])

    def test_once_per_pattern_per_session(self):
        self.assertIsNotNone(self.hint("gh run view 1 --json jobs"))
        self.assertIsNone(self.hint("gh api repos/o/r/actions/jobs/2/logs"))
        self.assertIsNotNone(self.hint("gh run view 1 --json jobs", session="s2"))

    def test_quiet_on_ordinary_commands_and_bad_input(self):
        for c in ("ls -la", "git show --stat --format='%h:%s' HEAD", "jx pr show 87", "git show HEAD~1 -- a.py"):
            self.assertIsNone(self.hint(c), c)
        self.assertIsNone(self.hint("", raw="not json"))


if __name__ == "__main__":
    unittest.main()
