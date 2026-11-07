import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec = importlib.util.spec_from_file_location(f"jx_{name}", ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tx = load("tx")
bench = load("bench")


def rec(kind, ts, content, request_id=None, usage=None):
    r = {"type": kind, "timestamp": ts, "message": {"content": content}}
    if request_id:
        r["requestId"] = request_id
        r["message"]["model"] = "m"
        r["message"]["usage"] = usage
    return r


class UsageOfSyntheticTranscript(unittest.TestCase):
    def test_counts_calls_tokens_time_split_and_bash_mix(self):
        u1 = {"input_tokens": 1, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10, "output_tokens": 50, "output_tokens_details": {"thinking_tokens": 20}}
        u2 = {"input_tokens": 1, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0, "output_tokens": 30, "output_tokens_details": {"thinking_tokens": 0}}
        lines = [
            rec("user", "2026-11-06T00:00:00Z", [{"type": "text", "text": "do it"}]),
            rec("assistant", "2026-11-06T00:00:10Z", [{"type": "text", "text": "ok"}], "r1", u1),
            rec("assistant", "2026-11-06T00:00:10Z", [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "jx k dev pods"}}], "r1", u1),
            rec("user", "2026-11-06T00:00:13Z", [{"type": "tool_result", "tool_use_id": "t1", "content": "x"}]),
            rec("assistant", "2026-11-06T00:00:20Z", [{"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "python3 - <<'EOF'\nprint(1)\nEOF"}}], "r2", u2),
            rec("user", "2026-11-06T00:00:21Z", [{"type": "tool_result", "tool_use_id": "t2", "content": "1"}]),
            rec("assistant", "2026-11-06T00:00:30Z", [{"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "kubectl get pods"}}], "r3", u2),
            {"type": "custom-title", "customTitle": "Synthetic"},
        ]
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "abcdef12-0000.jsonl")
            with open(p, "w") as fh:
                fh.write("\n".join(json.dumps(l) for l in lines) + "\n")
            u = tx.usage_of(p)
        self.assertEqual(u["calls"], 3)
        self.assertEqual(u["out_tok"], 110)
        self.assertEqual(u["think_tok"], 20)
        self.assertEqual(u["cache_read"], 300)
        self.assertEqual((u["bash"], u["jx"], u["heredoc"], u["raw"]), (3, 1, 1, 1))
        self.assertEqual(u["title"], "Synthetic")
        self.assertAlmostEqual(u["tool_min"], round(4 / 60, 1))
        self.assertAlmostEqual(u["model_min"], round((10 + 7 + 9) / 60, 1))
        self.assertEqual(u["wall_min"], 0.5)


class Grading(unittest.TestCase):
    def test_answer_line_extraction(self):
        self.assertEqual(bench.answer_line("blah\nANSWER: 42 pods\n"), "42 pods")
        self.assertEqual(bench.answer_line("no marker"), "no marker")

    def test_grade_kinds(self):
        self.assertTrue(bench.grade("number", "3\n", "ANSWER: there are 3 pods"))
        self.assertFalse(bench.grade("number", "3\n", "ANSWER: 13 pods"))
        self.assertTrue(bench.grade("names", "Ada Lovelace\nAlan Turing\n", "Lovelace on PD and Turing on incident.io"))
        self.assertFalse(bench.grade("names", "Ada Lovelace\nAlan Turing\n", "only Lovelace"))
        self.assertTrue(bench.grade("tokens", "#3570\n#3499\n", "open: #3499 and #3570"))
        self.assertFalse(bench.grade("tokens", "#3570\n#3499\n", "open: #3570"))
        self.assertTrue(bench.grade("words", "Pipeline for tier agentic version not found\n", "The common failure says the pipeline for tier agentic version was not found"))
        self.assertFalse(bench.grade("words", "Pipeline for tier agentic version not found\n", "timeouts everywhere"))

    def test_grade_tolerance_and_timestamp_normalisation(self):
        self.assertTrue(bench.grade("number", "86 days\n", "about 87 days", tolerance=2))
        self.assertFalse(bench.grade("number", "86 days\n", "about 90 days", tolerance=2))
        self.assertTrue(bench.grade("tokens", "buildcache\n2026-06-04T19:33\n", "`buildcache`, pushed 2026-06-04 19:33 UTC"))

    def test_off_arm_gets_the_fence_in_its_command(self):
        seen = {}
        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            class R: stdout = '{"result": "x", "session_id": "", "usage": {}}'; stderr = ""
            return R()
        saved = bench.subprocess.run; bench.subprocess.run = fake_run
        try:
            bench.run_session("q", "/", {}, 3, "off"); self.assertIn(bench.FENCE, seen["cmd"])
            bench.run_session("q", "/", {}, 3, "on"); self.assertNotIn("--append-system-prompt", seen["cmd"])
        finally:
            bench.subprocess.run = saved


class SkillToggleRestores(unittest.TestCase):
    def test_off_hides_and_restore_puts_the_original_back(self):
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "orig"); os.mkdir(target)
            link = os.path.join(d, "jx"); os.symlink(target, link)
            saved = bench.SKILL
            try:
                bench.SKILL = link
                t = bench.SkillToggle()
                t.set("off"); self.assertFalse(os.path.lexists(link))
                t.set("on"); self.assertEqual(os.readlink(link), bench.ROOT)
                t.restore(); self.assertEqual(os.readlink(link), target)
            finally:
                bench.SKILL = saved


if __name__ == "__main__":
    unittest.main()
