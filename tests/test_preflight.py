"""jx preflight: each check gets a temp git repo holding the defect it exists for, and a clean variant that must stay quiet."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JX = ROOT / "bin" / "jx"


class Repo:
    def __init__(self, base_files):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "repo"
        self.path.mkdir()
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@example.invalid"), ("config", "user.name", "T"),
                     ("config", "commit.gpgsign", "false"), ("config", "core.hooksPath", "/dev/null")):
            self.git(*args)
        self.write(base_files)
        self.git("add", "-A"); self.git("commit", "-qm", "base")
        self.git("switch", "-qc", "topic")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.path, text=True, capture_output=True, check=True).stdout.strip()

    def write(self, files):
        for name, text in files.items():
            p = self.path / name
            if text is None:
                p.unlink(); continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(textwrap.dedent(text).lstrip("\n"))

    def commit(self, files, msg="change"):
        self.write(files); self.git("add", "-A"); self.git("commit", "-qm", msg)

    def run(self, *args, env=None):
        p = subprocess.run([sys.executable, str(JX), "preflight", "run", "--base", "main", "--json", *map(str, args)],
                           cwd=self.path, text=True, capture_output=True, env=env)
        try:
            rows = json.loads(p.stdout)
        except json.JSONDecodeError:
            raise AssertionError(f"no JSON from preflight (exit {p.returncode}): {p.stdout}\n{p.stderr}")
        return p.returncode, rows, p.stderr

    def close(self):
        self.temp.cleanup()


class Base(unittest.TestCase):
    def repo(self, files):
        r = Repo(files); self.addCleanup(r.close); return r

    def hits(self, rows, check, severity=None, path=None):
        return [r for r in rows if r["check"] == check and (severity is None or r["severity"] == severity) and (path is None or r["path"] == path)]


class ReachTests(Base):
    def test_removed_name_still_imported_elsewhere_blocks(self):
        r = self.repo({"pkg/a.py": "def helper():\n    return 1\n", "pkg/b.py": "from pkg.a import helper\nhelper()\n", "README.md": "Use `helper` from pkg.a.\n"})
        r.commit({"pkg/a.py": "def other():\n    return 2\n"})
        code, rows, _ = r.run("--only", "refs.removed")
        self.assertEqual(code, 1)
        self.assertTrue(self.hits(rows, "refs.removed", "block", "pkg/b.py"))
        self.assertTrue(self.hits(rows, "refs.removed", "warn", "README.md"))

    def test_name_kept_as_an_import_alias_is_not_removed(self):
        r = self.repo({"pkg/a.py": "def _redact():\n    return 1\n", "pkg/c.py": "def redact():\n    return 1\n", "pkg/b.py": "from pkg.a import _redact\n"})
        r.commit({"pkg/a.py": "from pkg.c import redact as _redact\n"})
        code, rows, _ = r.run("--only", "refs.removed")
        self.assertEqual((code, rows), (0, []))

    def test_a_dropped_import_is_only_missed_by_code_that_reached_it_through_the_module(self):
        files = {"pkg/a.py": "import json\n", "pkg/b.py": "import json\njson.dumps(1)\n", "README.md": "It prints json.\n",
                 "pkg/d.py": "RECORD = 'out/a.json'  # a file name, not module a's json\n"}
        r = self.repo(files)
        r.commit({"pkg/a.py": "X = 1\n"})
        self.assertEqual(r.run("--only", "refs.removed")[:2], (0, []))  # `json` elsewhere is each file's own import
        r = self.repo({**files, "pkg/b.py": "from pkg.a import json\njson.dumps(1)\n"})
        r.commit({"pkg/a.py": "X = 1\n"})
        code, rows, _ = r.run("--only", "refs.removed")
        self.assertEqual((code, [(x["severity"], x["path"]) for x in rows]), (1, [("block", "pkg/b.py")]))
        r = self.repo({**files, "pkg/b.py": "import pkg.a\npkg.a.json.dumps(1)\n"})  # reached as the module's attribute
        r.commit({"pkg/a.py": "X = 1\n"})
        self.assertEqual([(x["severity"], x["path"]) for x in r.run("--only", "refs.removed")[1]], [("block", "pkg/b.py")])

    def test_an_env_var_the_code_only_sets_is_not_one_it_reads(self):
        r = self.repo({"a.py": "x = 1\n"})
        r.commit({"a.py": "import os\nos.environ['CHILD_ONLY_VAR'] = '1'\nmode = os.environ['READ_HERE_VAR']\n"})
        code, rows, _ = r.run("--only", "env.undocumented")
        self.assertEqual([x["message"].split("`")[1] for x in rows], ["READ_HERE_VAR"])

    def test_changed_signature_breaks_untouched_caller(self):
        r = self.repo({"lib.py": "def speed(plan, target):\n    return plan\n", "tests/protocol/rehearsal.py": "from lib import speed\nspeed(1, 2)\n"})
        r.commit({"lib.py": "def speed(plan, target, *, root):\n    return plan\n"})
        code, rows, _ = r.run("--only", "refs.callers")
        self.assertEqual(code, 1)
        self.assertIn("root", self.hits(rows, "refs.callers", "block", "tests/protocol/rehearsal.py")[0]["message"])
        r.commit({"tests/protocol/rehearsal.py": "from lib import speed\nspeed(1, 2, root=3)\n"})
        self.assertEqual(r.run("--only", "refs.callers")[1], [])

    def test_old_default_value_still_named_as_current(self):
        r = self.repo({"make_lock.py": 'DEFAULT_LOCK = "locks/release_r2.json"\n', "README.md": "The default lock is release_r2.json.\nHistory: release_r2.json was cut on day one.\n"})
        r.commit({"make_lock.py": 'DEFAULT_LOCK = "locks/release_r3.json"\n'})
        rows = r.run("--only", "refs.old-literal")[1]
        self.assertEqual([x["line"] for x in self.hits(rows, "refs.old-literal")], [1])

    def test_namesake_doc_naming_what_changed_asks(self):
        base = {"pkg/engine_smoke.py": 'ARMS = ("stock-w4a16",)\n', "pkg/other.py": "X = 1\n",
                "pkg/ENGINE_SMOKE.md": "# Engine smoke\n\nArms: `stock-w4a16` and `optimized-d2b`, maps under pkg_dir.\n"}
        r = self.repo(base)
        r.commit({"pkg/engine_smoke.py": 'ARMS = ("stock-w4a16", "optimized-d2b")\nPIN = {"optimized-d2b": "pkg_dir/x.json"}\n'})
        _, rows, _ = r.run()
        hits = self.hits(rows, "docs.namesake", "ask", "pkg/ENGINE_SMOKE.md")
        self.assertEqual([x["line"] for x in hits], [3])
        self.assertIn("`optimized-d2b`", hits[0]["message"])
        self.assertNotIn("pkg_dir", hits[0]["message"])  # a path segment is not a name the doc documents
        r = self.repo(base)
        r.commit({"pkg/engine_smoke.py": 'ARMS = ("stock-w4a16",)\nTIMEOUT_S = 5\n', "pkg/other.py": "X = 2\n"})
        _, rows, _ = r.run()
        self.assertEqual(self.hits(rows, "docs.namesake"), [])

    def test_path_constant_defined_in_another_module_warns(self):
        r = self.repo({"pkg/a.py": 'PIN_MAP = "cdl/port/pins/map_h200.json"\n', "pkg/b.py": "X = 1\n"})
        r.commit({"pkg/b.py": 'MAPS = {"optimized": "cdl/port/pins/map_h200.json"}\n'
                              'INPUTS = ("cdl/port/pins/map_h200.json",)\nOWN = "cdl/port/pins/map_b300.json"\n'})
        _, rows, _ = r.run()
        hits = self.hits(rows, "dup.constants", "warn")
        self.assertEqual([(x["path"], x["line"]) for x in hits], [("pkg/b.py", 1)])
        self.assertIn("pkg/a.py:1", hits[0]["message"])

    def test_new_sibling_missing_from_registry(self):
        r = self.repo({"exp/A_EVIDENCE.md": "a\n", "exp/B_EVIDENCE.md": "b\n", "exp/INDEX.md": "- A_EVIDENCE.md\n- B_EVIDENCE.md\n"})
        r.commit({"exp/C_EVIDENCE.md": "c\n"})
        self.assertTrue(self.hits(r.run("--only", "refs.registry")[1], "refs.registry", path="exp/INDEX.md"))
        r.commit({"exp/INDEX.md": "- A_EVIDENCE.md\n- B_EVIDENCE.md\n- C_EVIDENCE.md\n"})
        self.assertEqual(r.run("--only", "refs.registry")[1], [])

    def test_fixed_line_still_present_at_a_sibling_site(self):
        line = "            except ProcessLookupError: pass  # group gone\n"
        body = "import os\ndef stop(pid):\n    try:\n        os.killpg(pid, 9)\n" + line
        r = self.repo({"a.py": body, "b.py": body})
        r.commit({"a.py": body.replace(line, "            except (ProcessLookupError, PermissionError): pass  # group gone\n")})
        rows = r.run("--only", "refs.old-pattern")[1]
        self.assertEqual([x["path"] for x in rows], ["b.py"])


class CitationTests(Base):
    def test_citation_into_moved_line_drifts(self):
        r = self.repo({"mod.py": "a = 1\nb = 2\ndef target():\n    pass\n", "GUIDE.md": "See `target` at mod.py:3.\n"})
        r.commit({"mod.py": "import os\nimport sys\na = 1\nb = 2\ndef target():\n    pass\n"})
        rows = r.run("--only", "cite.drift")[1]
        self.assertIn("3→5", rows[0]["message"])

    def test_out_of_range_blocks_and_wrong_line_warns(self):
        r = self.repo({"mod.py": "x = 1\ny = 2\ndef phase():\n    pass\n", "GUIDE.md": "intro\n"})
        r.commit({"GUIDE.md": "intro\nBad: mod.py:40.\nWrong line: `phase` at mod.py:1.\nRight: `phase` at mod.py:3.\n"})
        rows = r.run("--only", "cite.resolve,cite.content")[1]
        self.assertTrue(self.hits(rows, "cite.resolve", "block"))
        content = self.hits(rows, "cite.content")
        self.assertEqual([x["line"] for x in content], [3])


class ClaimTests(Base):
    def receipt(self, r, d, **tests):
        head = r.git("rev-parse", "HEAD")
        import hashlib
        diff = subprocess.run(["git", "diff", "--binary", "HEAD"], cwd=r.path, capture_output=True).stdout
        d.mkdir(parents=True)
        (d / "result.json").write_text(json.dumps(dict(exit_code=0, before=dict(head=head, tracked_diff_sha256=hashlib.sha256(diff).hexdigest()), tests=tests)))

    def test_counts_need_a_matching_fresh_receipt(self):
        r = self.repo({"a.py": "x = 1\n"})
        r.commit({"a.py": "x = 2\n"})
        body = r.path.parent / "body.md"; body.write_text("## Verification\n\n12 passed, 2 skipped.\n")
        self.assertIn("no --receipts", r.run("--only", "claims.counts", "--body", body)[1][0]["message"])
        rec = r.path.parent / "rec"
        self.receipt(r, rec, total=12, passed=10, failures=0, errors=0, skipped=2)
        rows = r.run("--only", "claims.counts", "--body", body, "--receipts", rec)[1]
        self.assertEqual(rows, [])
        body.write_text("## Verification\n\n15 passed.\n")
        self.assertTrue(r.run("--only", "claims.counts", "--body", body, "--receipts", rec)[1])


class TestChecks(Base):
    WF = """
    on: pull_request
    jobs:
      t:
        strategy:
          matrix:
            include:
              - shard: one
                files: >-
                  tests/test_a.py
              - shard: two
                extra: true
        steps:
          - name: unit
            if: matrix.files
            env:
              FILES: ${{ matrix.files }}
            run: python -m pytest -q $FILES
          - name: extra
            if: matrix.extra
            run: >-
              python -m pytest -q
              tests/test_b.py
    """

    def test_new_test_file_not_in_ci_and_lost_selection(self):
        r = self.repo({".github/workflows/ci.yml": self.WF, "tests/test_a.py": "def test_a():\n    assert 1 + 1 == 2\n",
                       "tests/test_b.py": "def test_b():\n    assert 2 * 2 == 4\n"})
        r.commit({"tests/test_c.py": "def test_c():\n    assert 3 == 3 + 0\n"})
        rows = r.run("--only", "tests.ci,tests.ci-diff")[1]
        self.assertEqual([(x["check"], x["path"]) for x in rows], [("tests.ci", "tests/test_c.py")])
        r.commit({".github/workflows/ci.yml": re.sub(r"\n\s*extra: true", "", self.WF)})
        code, rows, _ = r.run("--only", "tests.ci-diff")
        self.assertEqual((code, [x["path"] for x in rows]), (1, ["tests/test_b.py"]))

    def test_spec_ledger_topics(self):
        doc = "# Plan\n\n## Timing bars\n\n- report step ms per level\n\n## Other\n\n- speed must be paired\n- settings must restore X\n"
        r = self.repo({"PLAN.md": doc, ".jx/preflight.json": json.dumps({"governing_docs": ["PLAN.md"]})})
        r.commit({"timing_reader.py": "x = 1\n", "settings.json": "{}\n"})
        self.assertEqual([x["line"] for x in r.run("--only", "spec.ledger")[1]], [5])
        self.assertEqual(sorted(x["line"] for x in r.run("--only", "spec.ledger", "--topic", "speed")[1]), [5, 9])

    def test_a_test_file_run_directly_counts_as_ci(self):
        wf = "on: pull_request\njobs:\n  t:\n    steps:\n      - run: |\n          python3 .jx/test_gate.py\n          python -m unittest tests/test_u.py\n"
        r = self.repo({".github/workflows/ci.yml": wf, "a.py": "x = 1\n"})
        r.commit({".jx/test_gate.py": "import unittest\n", "tests/test_u.py": "import unittest\n", "tests/test_v.py": "import unittest\n"})
        self.assertEqual([x["path"] for x in r.run("--only", "tests.ci")[1]], ["tests/test_v.py"])

    def test_patch_targets_skips_and_vacuous_asserts(self):
        r = self.repo({"app.py": "FLAG = 1\ndef run():\n    return FLAG\n", "tests/helpers.py": "def check_ok(x):\n    assert x\n"})
        r.commit({"tests/test_app.py": """
            import pytest
            import app
            from tests.helpers import check_ok
            np = pytest.importorskip("numpy")
            def test_patch(monkeypatch):
                monkeypatch.setattr(app, "NEVER_READ", 2)
                monkeypatch.setattr(app, "FLAG", 2)
                assert app.run() == 2
            def test_self():
                x = 1
                assert x == x
            def test_helper():
                check_ok(app.run())
            def test_nothing():
                app.run()
            """})
        rows = r.run("--only", "tests.patch-targets,tests.skips,tests.vacuous")[1]
        got = sorted((x["check"], x["line"]) for x in rows)
        self.assertEqual(got, [("tests.patch-targets", 6), ("tests.skips", 4), ("tests.vacuous", 11), ("tests.vacuous", 14)])


class LintTests(Base):
    def test_modal_image_build_step_after_local_mount_blocks(self):
        bad = 'import modal\nimage = (modal.Image.debian_slim().pip_install("x").add_local_dir("d", "/d")\n         .env({"A": "1"}))\n'
        r = self.repo({"other.py": "x = 1\n"})
        r.commit({"img.py": bad})
        code, rows, _ = r.run("--only", "py.modal-image-order")
        self.assertEqual((code, rows[0]["line"]), (1, 2))
        r.commit({"img.py": 'import modal\nimage = modal.Image.debian_slim().env({"A": "1"})\nimage = image.add_local_dir("d", "/d")\nimage = image.add_local_file("f", "/f")\n'})
        self.assertEqual(r.run("--only", "py.modal-image-order")[1], [])
        r.commit({"img.py": 'import modal\nimage = modal.Image.debian_slim()\nif True:\n    image = image.add_local_dir("d", "/d")\nimage = image.run_commands("ls")\n'})
        self.assertEqual(r.run("--only", "py.modal-image-order")[1][0]["line"], 5)

    def test_parser_and_process_lints(self):
        r = self.repo({"old.py": "x = 1\n", "data/prod40_a.json": '[{"idx": 1}]\n'})
        r.commit({"new.py": """
            import json, math, os, subprocess
            def read(text):
                try:
                    return json.loads(text)
                except ValueError:
                    return None
            def finite(x):
                return math.isfinite(x)
            def stop(pid):
                try:
                    os.killpg(pid, 9)
                except ProcessLookupError:
                    pass
            def run():
                subprocess.run(["true"], timeout=5)
            def refs(path):
                rf = path / f"prod40_{1}.json"
                return json.loads(rf.read_text())["pages"]
            """})
        rows = r.run("--only", "py.*")[1]
        got = sorted((x["check"], x["line"]) for x in rows)
        self.assertEqual(got, [("py.isfinite-int", 8), ("py.json-recursion", 4), ("py.json-subscript", 18), ("py.kill-eperm", 11), ("py.timeout-unhandled", 15)])

    def test_float_ratio_against_a_fractional_threshold_warns(self):
        r = self.repo({"gate.py": "TILT_MIN = 0.97\n"})
        r.commit({"gate.py": """
            TILT_MIN = 0.97
            TAU_MIN, TAU_K = 0.99, 31


            def tilt(c, r, n_p, n_a, p):
                return (c / n_p) / (r / n_a) < TILT_MIN or p["ratio"] < TAU_MIN


            def small(n, ratio):
                return n < 0.5 or ratio >= 1.0
            """})
        _, rows, _ = r.run()
        self.assertEqual([(x["path"], x["line"]) for x in self.hits(rows, "py.float-threshold", "warn")],
                         [("gate.py", 6), ("gate.py", 6)])

    def test_path_and_range_code_asks_the_references_checklist(self):
        r = self.repo({"refs.py": "import os\n"})
        r.commit({"refs.py": "import os\nbeside = os.path.normpath('a/../b.py')\n"})
        _, rows, _ = r.run()
        self.assertTrue([x for x in self.hits(rows, "prompts", "ask", "refs.py") if "[prompt.references]" in x["message"]])

    def test_a_command_gate_change_asks_the_gate_checklist(self):
        r = self.repo({"hook.py": "import json\n"})
        r.commit({"hook.py": """
            import json, shlex, sys
            event = json.load(sys.stdin)
            words = shlex.split(event["tool_input"]["command"])
            """})
        _, rows, _ = r.run()
        self.assertTrue([x for x in self.hits(rows, "prompts", "ask", "hook.py") if "[prompt.command-gate]" in x["message"]])
        r.commit({"hook.py": "import json\nprint(json.loads('{}'))\n"})
        _, rows, _ = r.run()
        self.assertFalse([x for x in rows if "[prompt.command-gate]" in x["message"]])

    def test_a_fix_sweeps_the_same_pattern_elsewhere(self):
        bad = "import os\ndef stop(pid):\n    try:\n        os.killpg(pid, 9)\n    except ProcessLookupError:\n        pass\n"
        r = self.repo({"a.py": bad, "b.py": bad})
        r.commit({"a.py": bad.replace("except ProcessLookupError:", "except (ProcessLookupError, PermissionError):")})
        rows = r.run("--only", "py.kill-eperm")[1]
        self.assertEqual([(x["path"], x["line"]) for x in rows], [("b.py", 4)])


class HygieneAndRunnerTests(Base):
    def test_secret_blocks_digest_does_not_and_scope_blocks(self):
        r = self.repo({"a.py": "x = 1\n"})
        r.commit({"conf.py": 'TOKEN = "ghp_' + "a1B2" * 9 + '"\nDIGEST = "' + "ab12" * 16 + '"\n', "other/x.txt": "x\n"})
        code, rows, _ = r.run("--only", "hygiene.secrets,scope.owned", "--owned", "conf.py")
        self.assertEqual(code, 1)
        self.assertEqual(sorted((x["check"], x["path"], x["line"]) for x in rows), [("hygiene.secrets", "conf.py", 1), ("scope.owned", "other/x.txt", "")])

    def test_only_and_skip_select_pack_checks_as_they_do_jx_checks(self):
        pack = 'print("pack.demo\\tblock\\ta.py\\t1\\tdemo finding\\tPR 1")\n'
        r = self.repo({"a.py": "x = 1\n", ".jx/pack.py": pack, ".jx/preflight.json": json.dumps({"checks": [{"id": "pack.demo", "cmd": "python3 .jx/pack.py", "when": ["*.py"]}]})})
        r.commit({"a.py": "x = 2\n"})
        self.assertEqual(r.run("--only", "pack.demo")[0], 1)
        self.assertEqual(r.run("--only", "pack.demo", "--skip", "pack.demo")[:2], (0, []))
        self.assertEqual(r.run("--only", "pack.*,hygiene.diff-check", "--skip", "pack.*")[:2], (0, []))

    def test_dispositions_and_pack_contract(self):
        pack = 'import json, sys\nc = json.load(open(sys.argv[1]))\nprint("pack.demo\\tblock\\t" + c["files"][0]["path"] + "\\t1\\tdemo finding\\tPR 1")\n'
        r = self.repo({"a.py": "x = 1\n", ".jx/pack.py": pack, ".jx/preflight.json": json.dumps({"checks": [{"id": "pack.demo", "cmd": "python3 .jx/pack.py", "when": ["*.py"]}]})})
        r.commit({"a.py": "x = 2\n"})
        code, rows, err = r.run("--only", "pack.demo")
        self.assertEqual((code, rows[0]["check"], rows[0]["severity"]), (1, "pack.demo", "block"))
        ledger = r.path.parent / "ledger.md"
        ledger.write_text(f"- {rows[0]['id']} decision: accepted in review\n")
        code, rows, _ = r.run("--only", "pack.demo", "--ledger", ledger)
        self.assertEqual((code, rows[0]["disposition"]), (0, "decision: accepted in review"))
        r.write({".jx/preflight/DISPOSITIONS.md": "- pack.* a.py: standing decision\n"})
        code, rows, _ = r.run("--only", "pack.demo")
        self.assertEqual((code, rows[0]["disposition"]), (0, "standing decision"))

    def test_crashing_pack_script_is_a_block(self):
        r = self.repo({"a.py": "x = 1\n", ".jx/preflight.json": json.dumps({"checks": [{"id": "pack.bad", "cmd": "python3 -c 'raise SystemExit(3)'"}]})})
        r.commit({"a.py": "x = 2\n"})
        code, rows, _ = r.run("--only", "pack.bad")
        self.assertEqual((code, rows[0]["severity"]), (1, "block"))

    def test_pack_checks_run_side_by_side(self):
        nap = 'import time; time.sleep(3); print("{}\\twarn\\ta.py\\t1\\tslept\\tPR 1")'
        checks = [{"id": f"pack.nap{i}", "cmd": f"python3 -c '{nap.format(f'pack.nap{i}')}'"} for i in range(3)]
        r = self.repo({"a.py": "x = 1\n", ".jx/preflight.json": json.dumps({"checks": checks})})
        r.commit({"a.py": "x = 2\n"})
        t0 = time.time()
        rows = r.run("--only", "pack.nap*")[1]
        self.assertEqual(sorted(x["check"] for x in rows), ["pack.nap0", "pack.nap1", "pack.nap2"])
        self.assertLess(time.time() - t0, 8)  # one after another they take 9 s

    def test_receipt_records_head_tier_and_open_items(self):
        r = self.repo({"a.py": "x = 1\n", ".jx/pack.py": 'print("pack.q\\task\\ta.py\\t1\\tanswer me\\tPR 1")\n',
                       ".jx/preflight.json": json.dumps({"checks": [{"id": "pack.q", "cmd": "python3 .jx/pack.py"}]})})
        r.commit({"a.py": "x = 2\n"})
        code, rows, _ = r.run("--only", "pack.q")
        rec = json.loads((r.path / ".git" / "preflight" / f"{r.git('rev-parse', 'HEAD')}.fast.json").read_text())
        self.assertEqual((code, rec["tier"], rec["dirty"], rec["partial"]), (0, "fast", False, True))
        self.assertEqual([x["id"] for x in rec["open"]], [rows[0]["id"]])

    def test_unreachable_remote_falls_back_to_the_local_base(self):
        r = self.repo({"a.py": "x = 1\n"})
        r.git("remote", "add", "origin", "/nonexistent/remote.git")
        r.git("update-ref", "refs/remotes/origin/main", r.git("rev-parse", "main"))
        r.commit({"a.py": "x = 2\n"})
        p = subprocess.run([sys.executable, str(JX), "preflight", "run", "--base", "origin/main", "--only", "refs.removed", "--json"],
                           cwd=r.path, text=True, capture_output=True)
        self.assertEqual((p.returncode, json.loads(p.stdout)), (0, []))
        self.assertIn("using the local ref", p.stderr)

    def test_dry_run_and_ls(self):
        r = self.repo({"a.py": "x = 1\n"})
        p = subprocess.run([sys.executable, str(JX), "preflight", "run", "--base", "main", "--dry-run"], cwd=r.path, text=True, capture_output=True)
        self.assertEqual(p.returncode, 0)
        self.assertIn("refs.callers", p.stdout)
        self.assertNotIn("tests.mutate", p.stdout)
        p = subprocess.run([sys.executable, str(JX), "preflight", "ls"], cwd=r.path, text=True, capture_output=True)
        self.assertIn("tests.mutate\tfull", p.stdout)


class VendorTests(Base):
    """A repository's own copy of the engine: made by `vendor`, run without jx, and what jx runs there."""

    def jx(self, r, *args, env=None):
        return subprocess.run([sys.executable, str(JX), "preflight", *map(str, args)], cwd=r.path, text=True, capture_output=True,
                              env={**os.environ, **(env or {})})

    def test_vendor_writes_a_pinned_copy_that_runs_without_jx(self):
        import hashlib
        r = self.repo({"a.py": "x = 1\n"})
        engine = r.path / ".jx/preflight/engine"
        dry = self.jx(r, "vendor", "--dry-run", "--allow-dirty")
        self.assertEqual((dry.returncode, engine.exists()), (0, False), dry.stderr)
        self.assertEqual(dry.stdout.count("\tnew"), 4)
        made = self.jx(r, "vendor", "--allow-dirty")  # this checkout's engine may have uncommitted edits while it is worked on
        self.assertEqual(made.returncode, 0, made.stderr)
        manifest = json.loads((engine / "ENGINE.json").read_text())
        self.assertEqual(sorted(manifest["files"]), ["_preflight_dynamic.py", "_preflight_static.py", "preflight.py"])
        for name, digest in manifest["files"].items():
            self.assertEqual(hashlib.sha256((engine / name).read_bytes()).hexdigest(), digest)
            self.assertEqual((engine / name).read_bytes(), (ROOT / "tools" / name).read_bytes())
        self.assertRegex(manifest["commit"], r"^[0-9a-f]{40}(\+dirty)?$")
        self.assertEqual(self.jx(r, "vendor", "--allow-dirty").stdout.count("\tsame"), 4)
        alone = subprocess.run([sys.executable, str(engine / "preflight.py"), "ls"], cwd=r.path, text=True, capture_output=True,
                               env={"PATH": os.path.dirname(shutil.which("git")), "PREFLIGHT_PROG": "python3 run.py"})
        self.assertEqual(alone.returncode, 0, alone.stderr)
        self.assertIn("refs.removed\t", alone.stdout)
        again = subprocess.run([sys.executable, str(engine / "preflight.py"), "vendor"], cwd=r.path, text=True, capture_output=True)
        self.assertIn("this is a repository's copy", again.stderr)  # a copy is not a source of copies
        with open(engine / "_preflight_static.py", "a") as f: f.write("# a fix made in place\n")
        edited = subprocess.run([sys.executable, str(engine / "preflight.py"), "ls"], cwd=r.path, text=True, capture_output=True)
        self.assertEqual(edited.returncode, 1)
        self.assertIn("_preflight_static.py in", edited.stderr)
        self.assertIn("no longer match ENGINE.json", edited.stderr)

    def test_in_a_repository_with_a_copy_jx_runs_the_copy(self):
        r = self.repo({"a.py": "x = 1\n", ".jx/preflight/engine/preflight.py": "import sys\nprint('the copy ran', *sys.argv[1:])\n"})
        self.assertEqual(self.jx(r, "ls", "--json").stdout, "the copy ran ls --json\n")
        own = self.jx(r, "ls", env={"JX_PREFLIGHT_OWN": "1"})
        self.assertIn("refs.removed\t", own.stdout)
        self.assertEqual(self.jx(r, "vendor", "--dry-run", "--allow-dirty").stdout.count("\tupdate"), 1)  # vendor stays jx's own

    def test_the_test_python_command_gets_only_what_is_left_of_the_budget(self):
        r = self.repo({"a.py": "x = 1\n"})
        code = ("import argparse, sys, time\nsys.path.insert(0, sys.argv[1])\nimport preflight as p\n"
                "args = argparse.Namespace(base='main', body=None, pr=None, budget=1)\n"
                "c = p.Change(sys.argv[3], args, {**p.DEFAULTS, 'test_python': sys.argv[2]})\n"
                "c.deadline = time.time() + 1\n"
                "for _ in range(2):\n"
                "    try: print(c.test_python())\n"
                "    except p.Skipped as e: print('skipped:', e)\n")
        slow = f"{sys.executable} -c 'import time; time.sleep(30)'"  # a cold `jx venv path` builds an environment
        began = time.monotonic()
        out = subprocess.run([sys.executable, "-c", code, str(ROOT / "tools"), slow, str(r.path)], cwd=r.path, text=True, capture_output=True).stdout
        self.assertLess(time.monotonic() - began, 10)
        self.assertEqual(out.count("skipped: the test python command did not finish within the run's 1 s budget"), 2, out)  # asked twice, waited once

    def test_a_machine_without_the_test_python_command_uses_the_fallback(self):
        r = self.repo({"a.py": "x = 1\n", "venv/bin/python": "#!/bin/sh\n"})
        code = ("import argparse, sys\nsys.path.insert(0, sys.argv[1])\nimport preflight as p\n"
                "config = {**p.DEFAULTS, 'test_python': 'no-such-program-xyz path', 'test_python_fallback': sys.argv[2]}\n"
                "try: print(p.Change(sys.argv[3], argparse.Namespace(base='main', body=None, pr=None), config).test_python())\n"
                "except p.Skipped as e: print('skipped:', e)\n")
        def resolved(spare, **env):
            return subprocess.run([sys.executable, "-c", code, str(ROOT / "tools"), spare, str(r.path)], cwd=r.path, text=True, capture_output=True,
                                  env={**os.environ, **env}).stdout.strip()
        self.assertEqual(resolved("venv/bin/python"), str(r.path / "venv/bin/python"))
        self.assertEqual(resolved("venv/bin/python", PREFLIGHT_TEST_PYTHON="/opt/py"), "/opt/py")
        self.assertEqual(resolved("missing/python"), "skipped: no test python: `no-such-program-xyz` is not on PATH and missing/python "
                         "does not exist; build it, or set PREFLIGHT_TEST_PYTHON")
        r.write({".jx/preflight.json": json.dumps({"test_python": "no-such-program-xyz path", "test_python_fallback": "venv/bin/python"})})
        row = next(x for x in json.loads(self.jx(r, "doctor", "--base", "main", "--json").stdout) if x["item"] == "test_python")
        self.assertEqual((row["state"], row["detail"]), ("ok", "venv/bin/python (`no-such-program-xyz` is not on PATH)"))


def pytest_python():
    """A python with pytest for the --full checks: $JX_PREFLIGHT_TEST_PYTHON, or a `jx venv` (skipped when uv cannot build one)."""
    if os.environ.get("JX_PREFLIGHT_TEST_PYTHON"): return os.environ["JX_PREFLIGHT_TEST_PYTHON"]
    p = subprocess.run([sys.executable, str(JX), "venv", "path", "-p", "3.12", "pytest==9.2.0"], text=True, capture_output=True, timeout=600)
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


class DynamicTests(Base):
    @classmethod
    def setUpClass(cls):
        cls.python = pytest_python()
        if not cls.python: raise unittest.SkipTest("no python with pytest (set JX_PREFLIGHT_TEST_PYTHON or install uv)")

    def config(self, **extra):
        return json.dumps({"test_python": f"echo {self.python}", "test_commands": ["{python} -m pytest -q tests"], **extra})

    def test_mutation_finds_the_unpinned_boundary(self):
        r = self.repo({"calc.py": "def ok(x):\n    return x\n", "tests/test_calc.py": "from calc import ok\ndef test_ok():\n    assert ok(1) == 1\n",
                       ".jx/preflight.json": self.config()})
        r.commit({"calc.py": 'def ok(x):\n    if x > 10:\n        return "fail"\n    return x\n'})
        rows = r.run("--full", "--only", "tests.mutate", "--budget", "120")[1]
        survivors = [x for x in rows if x["severity"] == "warn"]
        self.assertTrue(survivors and all(x["path"] == "calc.py" for x in survivors), rows)
        self.assertTrue(any(x["severity"] == "info" and "survived" in x["message"] for x in rows))

    def test_mutant_cannot_signal_outside_its_tree(self):
        # The mutant `if not owned` → `if (owned)` makes reap() SIGKILL the pid it was told to leave alone. The only pids
        # reap() ever receives are this test's sentinel and the test file's own child, so a broken guard kills only the sentinel.
        sentinel = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        self.addCleanup(sentinel.wait); self.addCleanup(sentinel.kill)
        r = self.repo({".jx/preflight.json": self.config()})
        r.commit({"reaper.py": "import os, signal\ndef reap(pid, owned):\n    if not owned:\n        return False\n    os.kill(pid, signal.SIGKILL)\n    return True\n",
                  "tests/test_reaper.py": "import os, subprocess, sys\nfrom reaper import reap\n"
                                          "def test_leaves_a_foreign_process_alone():\n    assert reap(int(os.environ['SENTINEL']), owned=False) is False\n"
                                          "def test_reaps_its_own_child():\n    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                                          "    try:\n        assert reap(p.pid, owned=True) is True\n        assert p.wait(10) == -9\n    finally:\n        p.kill(); p.wait()\n"})
        rows = r.run("--full", "--only", "tests.mutate", "--budget", "120", env=dict(os.environ, SENTINEL=str(sentinel.pid)))[1]
        self.assertIsNone(sentinel.poll(), "a mutant signalled a process outside its test run")
        self.assertTrue(any(x["severity"] == "info" and x["message"].startswith("mutation: 3 killed, 0 survived") for x in rows), rows)

    def test_without_a_sandbox_the_tests_do_not_run(self):
        r = self.repo({".jx/preflight.json": self.config()})
        r.commit({"calc.py": "def ok(x):\n    return x > 1\n", "tests/test_calc.py": "from calc import ok\ndef test_ok():\n    assert ok(2)\n"})
        p = subprocess.run(["sandbox-exec", "-p", "(version 1)(allow default)", sys.executable, str(JX), "preflight", "run", "--base", "main", "--json",
                            "--full", "--only", "tests.mutate"], cwd=r.path, text=True, capture_output=True)  # a sandbox cannot nest
        self.assertEqual([(x["severity"], x["message"][:13]) for x in json.loads(p.stdout)], [("warn", "check skipped")], p.stderr)

    def mutate(self, r, *args):
        rows = r.run("--full", "--only", "tests.mutate", "--budget", "120", *args)[1]
        return {x["line"] for x in rows if x["severity"] == "warn"}, next(x["message"] for x in rows if x["severity"] == "info")

    def test_a_later_full_run_reuses_the_verdicts_of_unchanged_lines(self):
        r = self.repo({"calc.py": "def ok(x):\n    return x\n", "tests/test_calc.py": "from calc import ok\ndef test_ok():\n    assert ok(1) == 1\n",
                       ".jx/preflight.json": self.config()})
        ok = 'def ok(x):\n    if x > 10:\n        return "fail"\n    return x\n'
        r.commit({"calc.py": ok})
        first, info = self.mutate(r)
        verdicts = sum(int(n) for n in re.findall(r"(\d+) (?:killed|survived)", info))
        r.commit({"calc.py": ok + "\n\ndef twice(x):\n    return x * 2\n"})
        lines, info = self.mutate(r)
        self.assertIn(f"{verdicts} verdicts reused from the full run of", info)
        self.assertEqual(lines, first | {8})  # ok()'s survivors still show, from the first run's verdicts
        lines, info = self.mutate(r, "--mutate-all")
        self.assertEqual((lines, "reused" in info), (first | {8}, False))
        r.commit({"tests/test_calc.py": "from calc import ok\ndef test_ok():\n    assert ok(1) == 1\n    assert ok(11) == 'fail'\n"})
        lines, info = self.mutate(r)  # a changed test file reruns the survivors, and the new assertion kills some
        self.assertLess(len(lines), len(first | {8}))
        self.assertNotIn(f"{verdicts} verdicts reused", info)

    def test_a_run_its_budget_cut_short_resumes(self):
        slow = "import time\ntime.sleep(2)\nfrom calc import ok\ndef test_ok():\n    assert ok(1) == 1\n"  # every pytest run costs 2 s
        r = self.repo({"calc.py": "def ok(x):\n    return x\n", "tests/test_calc.py": slow, ".jx/preflight.json": self.config()})
        r.commit({"calc.py": "def ok(x):\n    return x if x < 10 else 0\n"})
        _, info = self.mutate(r, "--budget", "0")
        self.assertRegex(info, r"^mutation: 0 killed, 0 survived, .* [1-9]\d* not run")
        _, info = self.mutate(r)  # the same commit: nothing to reuse, so everything runs
        ran = sum(int(n) for n in re.findall(r"(\d+) (?:killed|survived)", info))
        self.assertTrue(ran and "reused" not in info, info)
        t0 = time.time()
        _, info = self.mutate(r)
        self.assertIn(f"{ran} verdicts reused", info)
        self.assertLess(time.time() - t0, 3)  # every verdict held, so no baseline, control mutant or worktree

    def test_the_whole_run_ends_within_its_budget(self):
        slow = "import time\nfrom calc import ok\ndef test_ok():\n    time.sleep(60)\n    assert ok(1) == {}\n"
        r = self.repo({"calc.py": "def ok(x):\n    return x\n", "tests/test_calc.py": slow.format(1),
                       ".jx/preflight.json": self.config(checks=[{"id": "pack.slow", "cmd": "python3 -c 'import time; time.sleep(60)'"}])})
        r.commit({"calc.py": "def ok(x):\n    return x if x < 10 else 0\n", "tests/test_calc.py": slow.format("1 ")})
        t0 = time.time()
        code, rows, _ = r.run("--full", "--budget", "12")
        self.assertLess(time.time() - t0, 14)  # the tests and the pack script each sleep 60 s
        said = {c: " | ".join(x["message"] for x in rows if x["check"] == c) for c in ("pack.slow", "tests.mutate")}
        self.assertIn("did not finish within the run's 12 s budget", said["pack.slow"])
        self.assertIn("did not finish once within the run's 12 s budget", said["tests.mutate"])
        self.assertEqual(code, 0)  # an unfinished check is not a block

    def test_each_mutant_runs_only_the_tests_that_execute_its_line(self):
        r = self.repo({"calc.py": "def ok(x):\n    return x\n\n\ndef other(x):\n    return x\n",
                       "tests/test_calc.py": "def test_none():\n    pass\n", ".jx/preflight.json": self.config()})
        child = 'subprocess.run([sys.executable, "-c", "from calc import other; assert other(1) in (0, 2)"], check=True{})'
        test = ("import os, subprocess, sys\nfrom calc import ok\n\n\ndef test_ok():\n    assert ok(2) and not ok(1)\n\n\n"
                "def test_other_in_a_child():\n    " + child.format("") + "\n")
        r.commit({"calc.py": "def ok(x):\n    return x > 1\n\n\ndef other(x):\n    return x + 1\n", "tests/test_calc.py": test})
        rows = r.run("--full", "--only", "tests.mutate", "--budget", "120")[1]
        self.assertEqual([(x["line"], x["message"].split(":")[0]) for x in rows if x["severity"] == "warn"],
                         [(6, "mutant survived the 1 test(s) that run line 6")], rows)  # `x - 1`, seen only in the child
        own_env = "\n\n\ndef test_own_env():\n    " + child.format(", env={'PATH': os.environ['PATH']}").replace("other(1) in (0, 2)", "True") + "\n"
        r.commit({"tests/test_calc.py": test + own_env})
        rows = r.run("--full", "--only", "tests.mutate", "--budget", "120")[1]
        self.assertIn("mutant survived the 2 test(s) that run line 6", [x["message"].split(":")[0] for x in rows if x["severity"] == "warn"], rows)

    def test_fix_removal_and_collection(self):
        r = self.repo({"calc.py": "def ok(x):\n    return x\n", "tests/test_calc.py": "from calc import ok\ndef test_ok():\n    assert ok(1) == 1\n",
                       ".jx/preflight.json": self.config()})
        r.commit({"calc.py": "def ok(x):\n    return x if x >= 0 else 0\n",
                  "tests/test_calc.py": "import pytest\nfrom calc import ok\ndef test_ok():\n    assert ok(1) == 1\ndef test_negative():\n    assert ok(-1) == 0\ndef test_still_one():\n    assert ok(2) == 2\n"
                                        "@pytest.mark.parametrize('x,y', [(1, 1), (-1, 0)])\ndef test_cases_one_catches(x, y):\n    assert ok(x) == y\n"
                                        "@pytest.mark.parametrize('x', [1, 2])\ndef test_cases_none_catch(x):\n    assert ok(x) == x\n"})
        rows = r.run("--full", "--only", "tests.fix-removal")[1]
        self.assertEqual([x["line"] for x in rows], [7, 13])
        r.commit({"tests/test_missing_dep.py": "import some_absent_dependency\n"})
        code, rows, _ = r.run("--full", "--only", "tests.collect")
        self.assertEqual((code, rows[0]["severity"]), (0, "warn"))
        r.commit({"pkg/__init__.py": "", "tests/test_broken.py": "import pkg.gone\n"})
        code, rows, _ = r.run("--full", "--only", "tests.collect")
        self.assertEqual((code, rows[0]["severity"]), (1, "block"))


if __name__ == "__main__":
    unittest.main()
