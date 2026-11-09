"""jx preflight: each check gets a temp git repo holding the defect it exists for, and a clean variant that must stay quiet."""
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
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

    def test_fix_removal_and_collection(self):
        r = self.repo({"calc.py": "def ok(x):\n    return x\n", "tests/test_calc.py": "from calc import ok\ndef test_ok():\n    assert ok(1) == 1\n",
                       ".jx/preflight.json": self.config()})
        r.commit({"calc.py": "def ok(x):\n    return x if x >= 0 else 0\n",
                  "tests/test_calc.py": "from calc import ok\ndef test_ok():\n    assert ok(1) == 1\ndef test_negative():\n    assert ok(-1) == 0\ndef test_still_one():\n    assert ok(2) == 2\n"})
        rows = r.run("--full", "--only", "tests.fix-removal")[1]
        self.assertEqual([x["line"] for x in rows], [6])
        r.commit({"tests/test_missing_dep.py": "import some_absent_dependency\n"})
        code, rows, _ = r.run("--full", "--only", "tests.collect")
        self.assertEqual((code, rows[0]["severity"]), (0, "warn"))
        r.commit({"pkg/__init__.py": "", "tests/test_broken.py": "import pkg.gone\n"})
        code, rows, _ = r.run("--full", "--only", "tests.collect")
        self.assertEqual((code, rows[0]["severity"]), (1, "block"))


if __name__ == "__main__":
    unittest.main()
