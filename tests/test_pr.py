"""`jx pr show` and `jx pr edit` against a stand-in `gh` that keeps one PR in a JSON file."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

JX = Path(__file__).resolve().parents[1] / 'bin' / 'jx'
GH = f'''#!{sys.executable}
import json, os, sys
a = sys.argv[1:]; f = os.environ["FAKE_PR"]; pr = json.load(open(f))
if a[:2] == ["pr", "view"]: print(json.dumps(pr)); sys.exit()
if a[:2] == ["api", "graphql"]:
    print(json.dumps({{"data": {{"repository": {{"pullRequest": {{"reviewThreads": {{"nodes": [{{"isResolved": False}}, {{"isResolved": True}}]}},
        "commits": {{"totalCount": 3, "nodes": [{{"commit": {{"committedDate": "2026-11-07T08:00:00Z"}}}}]}}}}}}}}}})); sys.exit()
if a[0] == "api" and a[-1] == "-":  # PATCH --input -
    change = json.load(sys.stdin)
    if os.environ.get("FAKE_DROP_BODY"): change.pop("body", None)
    pr.update(change); json.dump(pr, open(f, "w"))
print(json.dumps(pr))
'''
PR = {'number': 7, 'state': 'OPEN', 'isDraft': True, 'title': 'Old', 'body': 'old body', 'author': {'login': 'me'}, 'headRefName': 'b',
      'headRefOid': 'abcdef1234567890', 'baseRefName': 'main', 'mergeable': 'MERGEABLE', 'mergeStateStatus': 'CLEAN', 'mergeCommit': None,
      'reviewDecision': '', 'additions': 5, 'deletions': 1, 'changedFiles': 2, 'comments': [{}], 'updatedAt': 'x', 'url': 'u', 'html_url': 'u',
      'statusCheckRollup': [{'__typename': 'CheckRun', 'status': 'COMPLETED', 'conclusion': 'FAILURE'},
                            {'__typename': 'CheckRun', 'status': 'IN_PROGRESS', 'conclusion': ''},
                            {'__typename': 'StatusContext', 'state': 'SUCCESS'},
                            {'__typename': 'CheckRun', 'status': 'COMPLETED', 'conclusion': 'SKIPPED'}]}


class PrThroughFakeGh(unittest.TestCase):
    def setUp(self):
        t = tempfile.TemporaryDirectory(); self.addCleanup(t.cleanup); d = Path(t.name)
        (d / 'gh').write_text(GH); (d / 'gh').chmod(0o755); (d / 'pr.json').write_text(json.dumps(PR)); (d / 'new.md').write_text('new body\n')
        self.d, self.env = d, {**os.environ, 'PATH': f'{d}{os.pathsep}{os.environ["PATH"]}', 'FAKE_PR': str(d / 'pr.json')}

    def jx(self, *a, **env):
        return subprocess.run([str(JX), 'pr', *a, '-R', 'o/r'], text=True, capture_output=True, env={**self.env, **env})

    def test_show_counts_checks_threads_and_marks_drafts(self):
        out = self.jx('show', '7', '--json'); self.assertEqual(out.returncode, 0, out.stderr)
        row = json.loads(out.stdout)[0]
        self.assertEqual({k: row[k] for k in ('state', 'head_sha', 'checks', 'unresolved', 'commits', 'comments', 'last_commit')},
                         {'state': 'DRAFT', 'head_sha': 'abcdef1234', 'checks': 'fail=1 pending=1 pass=1 skip=1', 'unresolved': 1,
                          'commits': 3, 'comments': 1, 'last_commit': '2026-11-07T08:00:00Z'})

    def test_edit_dry_run_changes_nothing(self):
        out = self.jx('edit', '7', '--title', 'New', '--body-file', str(self.d / 'new.md'), '--dry-run')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("'Old' -> 'New'", out.stdout); self.assertIn('-old body\n+new body', out.stdout)
        self.assertEqual(json.loads((self.d / 'pr.json').read_text())['title'], 'Old')

    def test_edit_verifies_by_rereading(self):
        out = self.jx('edit', '7', '--title', 'New', '--body-file', str(self.d / 'new.md'))
        self.assertEqual((out.returncode, json.loads(out.stdout)['verified']), (0, True), out.stderr)
        lost = self.jx('edit', '7', '--body-file', str(self.d / 'pr.json'), FAKE_DROP_BODY='1')
        self.assertEqual((lost.returncode, json.loads(lost.stdout)['verified']), (1, False))


if __name__ == '__main__':
    unittest.main()
