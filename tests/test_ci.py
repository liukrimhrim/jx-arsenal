"""`jx ci logs` against a stand-in `gh` that rejects unknown flags, like gh 2.92.0 does."""
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

JX = Path(__file__).resolve().parents[1] / 'bin' / 'jx'
LOG = ('2026-11-07T06:43:36.2741971Z \x1b[36;1mgit init "$RUNNER_TEMP/jx-arsenal"\x1b[0m\n'
       '2026-11-07T06:47:47.6045514Z 3 passed, 7 deselected in 262.20s\n')


class LogsThroughFakeGh(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        d = Path(self.temp.name)
        (d / 'log.txt').write_text(LOG)
        shim = d / 'gh'
        shim.write_text('#!/usr/bin/env python3\nimport sys, pathlib\n'
                        'a = sys.argv[1:]\n'
                        'bad = [x for x in a if x.startswith("--")]\n'
                        'if a[0] != "api" or bad: sys.stderr.write(f"unknown flag: {bad[0] if bad else a[0]}\\n"); sys.exit(1)\n'
                        f'sys.stdout.write(pathlib.Path({str(d / "log.txt")!r}).read_text())\n')
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, 'PATH': f'{d}{os.pathsep}{os.environ["PATH"]}'}

    def test_logs_strip_timestamps_and_ansi_without_extra_gh_flags(self):
        out = subprocess.run([str(JX), 'ci', 'logs', '123', '-R', 'o/r'], text=True, capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), ['git init "$RUNNER_TEMP/jx-arsenal"', '3 passed, 7 deselected in 262.20s'])


if __name__ == '__main__':
    unittest.main()
