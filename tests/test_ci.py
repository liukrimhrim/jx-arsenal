"""`jx ci logs` against a stand-in `gh` that rejects unknown flags, like gh 2.92.0 does; `jx ci timings` against one serving jobs and a log."""
import json
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



JOBS = {'jobs': [
    {'id': 11, 'name': 'shard-b', 'status': 'completed', 'conclusion': 'success', 'created_at': '2026-11-07T07:00:00Z',
     'started_at': '2026-11-07T07:00:05Z', 'completed_at': '2026-11-07T07:05:05Z', 'run_attempt': 1, 'html_url': 'u',
     'steps': [{'name': 'setup', 'conclusion': 'success', 'started_at': '2026-11-07T07:00:05Z', 'completed_at': '2026-11-07T07:00:35Z'},
               {'name': 'pytest', 'conclusion': 'success', 'started_at': '2026-11-07T07:00:35Z', 'completed_at': '2026-11-07T07:05:00Z'}]},
    {'id': 12, 'name': 'shard-a', 'status': 'in_progress', 'conclusion': None, 'created_at': '2026-11-07T07:00:00Z',
     'started_at': '2026-11-07T07:00:09Z', 'completed_at': None, 'run_attempt': 1, 'html_url': 'u', 'steps': []}]}
SHARD_LOG = ('2026-11-07T07:01:00.1Z ===== 1 failed, 40 passed, 2 skipped, 1 warning in 100.50s (0:01:40) =====\n'
             '2026-11-07T07:04:00.1Z 3 passed, 7 deselected in 20.00s\n')


class TimingsThroughFakeGh(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        d = Path(self.temp.name)
        (d / 'jobs.json').write_text(json.dumps(JOBS)); (d / 'log.txt').write_text(SHARD_LOG)
        shim = d / 'gh'
        shim.write_text('#!/usr/bin/env python3\nimport sys, pathlib\n'
                        f'd = pathlib.Path({str(d)!r}); path = sys.argv[-1]\n'
                        'if "/jobs/11/logs" in path: sys.stdout.write((d / "log.txt").read_text())\n'
                        'elif "/runs/7/jobs" in path: sys.stdout.write((d / "jobs.json").read_text())\n'
                        'else: sys.stderr.write(f"unexpected {sys.argv[1:]}\\n"); sys.exit(1)\n')
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, 'PATH': f'{d}{os.pathsep}{os.environ["PATH"]}'}

    def test_rows_sum_pytest_lines_and_skip_logs_of_unfinished_jobs(self):
        out = subprocess.run([str(JX), 'ci', 'timings', '7', '-R', 'o/r', '--json'], text=True, capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        a, b = json.loads(out.stdout)
        self.assertEqual((a['name'], a['queued'], a['secs'], a.get('passed')), ('shard-a', 9, None, None))
        self.assertEqual({k: b[k] for k in ('secs', 'passed', 'failed', 'skipped', 'deselected', 'warnings', 'test_secs', 'pytest_runs', 'slowest_step', 'step_secs')},
                         {'secs': 300, 'passed': 43, 'failed': 1, 'skipped': 2, 'deselected': 7, 'warnings': 1, 'test_secs': 120.5, 'pytest_runs': 2,
                          'slowest_step': 'pytest', 'step_secs': 265})
        self.assertIn('run 7: wall 305s, longest job shard-b 300s', out.stderr)

    def test_steps_reads_no_logs(self):
        out = subprocess.run([str(JX), 'ci', 'timings', '7', '-R', 'o/r', '--steps'], text=True, capture_output=True, env=self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.splitlines(), ['run_id\tjob\tstep\tconclusion\tsecs', '7\tshard-b\tsetup\tsuccess\t30', '7\tshard-b\tpytest\tsuccess\t265'])


if __name__ == '__main__':
    unittest.main()
