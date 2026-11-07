"""`jx git cat` slicing flags against a throwaway repository."""
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "git.py"
DOC = "# Title\nintro\n## Speed\nfast\n### Detail\nd\n```\n# not a heading\n```\n## Quality\nq\n"


class CatSlices(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); self.d = Path(t.name)
        (self.d / "doc.md").write_text(DOC); (self.d / "m.py").write_text("import os\ndef f():\n    pass\nclass C:\n    def g(self): ...\n")
        for c in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x"]):
            subprocess.run(["git", *c], cwd=self.d, check=True)

    def cat(self, *a):
        return subprocess.run([sys.executable, TOOL, "cat", *a, "--ref", "HEAD"], cwd=self.d, text=True, capture_output=True)

    def test_section_runs_to_the_next_heading_of_its_level_and_skips_fences(self):
        self.assertEqual(self.cat("doc.md", "--section", "speed").stdout.splitlines(),
                         ["3: ## Speed", "4: fast", "5: ### Detail", "6: d", "7: ```", "8: # not a heading", "9: ```"])

    def test_outline_lists_headings_or_defs(self):
        self.assertEqual(self.cat("doc.md", "--outline").stdout.splitlines(), ["1: # Title", "3: ## Speed", "5: ### Detail", "10: ## Quality"])
        self.assertEqual(self.cat("m.py", "--outline").stdout.splitlines(), ["2: def f():", "4: class C:", "5:     def g(self): ..."])

    def test_grep_with_context_and_lines(self):
        self.assertEqual(self.cat("doc.md", "--grep", "FAST|^q", "-i", "-C", "0").stdout.splitlines(), ["4: fast", "--", "11: q"])
        self.assertEqual(self.cat("doc.md", "--lines", "2:3").stdout.splitlines(), ["2: intro", "3: ## Speed"])

    def test_no_match_exits_1_and_names_the_next_command(self):
        p = self.cat("doc.md", "--section", "nope")
        self.assertEqual(p.returncode, 1); self.assertIn("--outline", p.stderr)
        self.assertEqual(self.cat("doc.md").stdout, DOC)  # no flag: the whole file, unchanged


if __name__ == "__main__":
    unittest.main()
