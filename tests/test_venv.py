"""`jx venv` against a stand-in `uv` that logs its calls and makes a venv whose python is this interpreter."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / 'tools' / 'venv.py'
UV = f'''#!{sys.executable}
import os, sys
a = sys.argv[1:]
open(os.environ["UV_LOG"], "a").write(" ".join(a) + "\\n")
if a[0] == "--version": print("uv 0.0-fake"); sys.exit()
if a[0] == "venv":
    d = a[-1]; os.makedirs(d + "/bin"); os.symlink(sys.executable, d + "/bin/python"); sys.exit()
if "boom==1" in a: sys.stderr.write("resolution failed\\n"); sys.exit(1)
'''


class VenvThroughFakeUv(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); self.d = d = Path(t.name)
        (d / 'bin').mkdir(); (d / 'bin' / 'uv').write_text(UV); (d / 'bin' / 'uv').chmod(0o755)
        self.env = {**os.environ, 'PATH': f'{d / "bin"}{os.pathsep}{os.environ["PATH"]}', 'JX_VENV_CACHE': str(d / 'cache'), 'UV_LOG': str(d / 'uv.log')}
        (d / 'a.txt').write_text('-r b.txt\nhttpx==0.28.1\n'); (d / 'b.txt').write_text('pytest==9.2.0\n')

    def jx(self, *a):
        return subprocess.run([sys.executable, TOOL, *a], text=True, capture_output=True, env=self.env, cwd=self.d)

    def calls(self, verb):
        return [l for l in (self.d / 'uv.log').read_text().splitlines() if l.startswith(verb)]

    def test_builds_once_then_reuses(self):
        first = self.jx('path', '-p', '3.12', '-r', 'a.txt', 'msgspec==0.22.0')
        self.assertEqual(first.returncode, 0, first.stderr); self.assertIn('built', first.stderr)
        py = first.stdout.strip(); self.assertTrue(py.endswith('/bin/python') and Path(py).exists())
        self.assertEqual(self.calls('pip'), [f'pip install -q -p {py} -r {self.d.resolve() / "a.txt"} msgspec==0.22.0'])
        again = self.jx('path', '-p', '3.12', '-r', 'a.txt', 'msgspec==0.22.0')
        self.assertEqual((again.stdout, again.stderr.split()[0]), (first.stdout, 'cached'))
        self.assertEqual(len(self.calls('venv')), 1)

    def test_editing_an_included_file_changes_the_key(self):
        key = lambda: json.loads(self.jx('path', '-r', 'a.txt', '--json').stdout)['key']
        before = key(); (self.d / 'b.txt').write_text('pytest==9.3.0\n')
        self.assertNotEqual(before, key())

    def test_failed_build_caches_nothing(self):
        bad = self.jx('path', 'boom==1')
        self.assertNotEqual(bad.returncode, 0); self.assertIn('resolution failed', bad.stderr); self.assertNotIn('Traceback', bad.stderr)
        self.assertEqual([p for p in (self.d / 'cache').iterdir() if p.is_dir()], [])

    def test_rm_dry_run_keeps_and_rm_removes(self):
        key = json.loads(self.jx('path', 'msgspec==0.22.0', '--json').stdout)['key']
        self.assertIn('DRY RUN', self.jx('rm', key[:6], '--dry-run').stdout)
        self.assertEqual(json.loads(self.jx('ls', '--json').stdout)[0]['key'], key)
        self.assertEqual(self.jx('rm', key[:6]).returncode, 0)
        self.assertEqual(json.loads(self.jx('ls', '--json').stdout), [])


if __name__ == '__main__':
    unittest.main()
