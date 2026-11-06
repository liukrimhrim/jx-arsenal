import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
JX = ROOT / 'bin/jx'
spec = importlib.util.spec_from_file_location('jx_modal', ROOT / 'tools/modal.py')
modal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(modal)

APPS = [{'app_id': 'ap-1', 'description': 'api', 'state': 'deployed', 'tasks': '2', 'created_at': '2026-07-09 11:05:03-08:00', 'stopped_at': None},
        {'app_id': 'ap-2', 'description': 'old', 'state': 'stopped', 'tasks': '0', 'created_at': '2026-01-01 00:00:00+00:00', 'stopped_at': '2026-02-01 00:00:00+00:00'}]
CONTAINERS = [{'container_id': 'ta-a', 'app_id': 'ap-1', 'app_name': 'api', 'start_time': '2026-11-06 11:29:00-08:00'},
              {'container_id': 'ta-b', 'app_id': 'ap-1', 'app_name': 'api', 'start_time': '2026-11-06 11:30:00-08:00'},
              {'container_id': 'ta-c', 'app_id': 'ap-3', 'app_name': 'worker', 'start_time': '2026-11-06 11:31:00-08:00'}]
BILLING = [{'object_id': 'ap-1', 'description': 'api', 'environment': 'main', 'interval_start': '2026-11-05T00:00:00', 'cost': '30.0'},
           {'object_id': 'ap-1', 'description': 'api', 'environment': 'main', 'interval_start': '2026-11-06T00:00:00', 'cost': '10.0'},
           {'object_id': 'ap-3', 'description': 'worker', 'environment': 'main', 'interval_start': '2026-11-05T00:00:00', 'cost': '60.0'}]


class PureFunctions(unittest.TestCase):
    def test_utc_converts_local_offsets_and_keeps_naive_as_utc(self):
        self.assertEqual(modal.utc('2026-07-09 11:05:03-08:00'), '2026-07-09T19:05:03Z')
        self.assertEqual(modal.utc('2026-11-05T00:00:00'), '2026-11-05T00:00:00Z')
        self.assertEqual(modal.utc(None), '')

    def test_log_line_timestamp_is_normalised_and_other_lines_pass_through(self):
        self.assertEqual(modal.norm_line('2026-11-06 11:29:52-08:00 ta-x restored'), '2026-11-06T19:29:52Z ta-x restored')
        self.assertEqual(modal.norm_line('no timestamp here'), 'no timestamp here')

    def test_counts_orders_by_size_then_name(self):
        rows = [{'app': 'b'}, {'app': 'a'}, {'app': 'b'}]
        self.assertEqual(modal.counts(rows), [('b', 2), ('a', 1)])


class ThroughFakeCli(unittest.TestCase):
    """Runs `jx modal` against a stand-in `modal` executable that prints canned JSON and records its argv."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.dir = Path(self.temp.name); self.argv = self.dir / 'argv'
        payload = {'app list': APPS, 'container list': CONTAINERS, 'billing report': BILLING}
        (self.dir / 'payload.json').write_text(json.dumps(payload))
        shim = self.dir / 'modal'
        shim.write_text('#!/usr/bin/env python3\nimport json, sys, pathlib\n'
                        f'd = pathlib.Path({str(self.dir)!r})\n'
                        '(d / "argv").write_text(" ".join(sys.argv[1:]))\n'
                        'key = " ".join(sys.argv[1:3])\n'
                        'p = json.loads((d / "payload.json").read_text())\n'
                        'if key not in p: sys.stderr.write("╭─ Error ─╮\\n│ No App with name \'nope\' found in the \'main\' environment. │\\n╰─────────╯"); sys.exit(1)\n'
                        'print(json.dumps(p[key]))\n')
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.env = {**os.environ, 'PATH': f'{self.dir}{os.pathsep}{os.environ["PATH"]}'}

    def jx(self, *args):
        return subprocess.run([str(JX), 'modal', *args], text=True, capture_output=True, env=self.env)

    def test_apps_filters_to_deployed_and_prints_utc(self):
        out = self.jx('apps', '-e', 'main')
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = out.stdout.splitlines()
        self.assertEqual(lines[0].split('\t'), ['app_id', 'app', 'state', 'tasks', 'created', 'stopped'])
        self.assertEqual(len(lines), 2)
        self.assertIn('2026-07-09T19:05:03Z', lines[1])
        self.assertIn('--env main', self.argv.read_text())

    def test_global_flags_are_accepted_after_the_subcommand(self):
        out = self.jx('apps', '--state', 'all', '--json', '-p', 'other')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(json.loads(out.stdout)), 2)
        self.assertIn('--profile other', self.argv.read_text())

    def test_container_count_groups_by_app_and_filters_by_name(self):
        out = self.jx('containers', '--count')
        self.assertEqual(out.stdout.splitlines()[1:], ['api\t2', 'worker\t1'])
        out = self.jx('containers', 'worker')
        self.assertEqual(len(out.stdout.splitlines()), 2)

    def test_cost_aggregates_per_app_with_share(self):
        out = self.jx('cost', '--for', 'this week')
        self.assertEqual(out.stdout.splitlines()[1:], ['worker\tmain\t60.0\t60.0%', 'api\tmain\t40.0\t40.0%'])
        self.assertIn('total 100.00 USD', out.stderr)
        self.assertNotIn('--env', self.argv.read_text())

    def test_missing_app_gets_the_listing_hint_not_the_environment_hint(self):
        out = self.jx('history', 'nope', '-e', 'main')
        self.assertEqual(out.returncode, 1)
        self.assertIn('jx modal apps --state all', out.stderr)
        self.assertNotIn('ls envs', out.stderr)
        self.assertNotIn('│', out.stderr)


if __name__ == '__main__':
    unittest.main()
