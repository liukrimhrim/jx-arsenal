import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
JX = ROOT / 'bin/jx'


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / 'repo'
        self.repo.mkdir()
        self.env = dict(os.environ)
        self.git('init', '-q', '-b', 'main')
        self.git('config', 'user.email', 'test@example.invalid')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'commit.gpgsign', 'false')
        self.git('config', 'core.hooksPath', '/dev/null')
        (self.repo / 'README.md').write_text('base\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'base')
        base = self.git('rev-parse', 'HEAD')
        remote = self.base / 'origin.git'
        self.git('init', '--bare', '-q', str(remote))
        self.git('remote', 'add', 'origin', str(remote))
        self.git('push', '-qu', 'origin', 'main')
        self.git('switch', '-qc', 'topic')
        (self.repo / 'change.py').write_text('answer = 42\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'change')
        self.head = self.git('rev-parse', 'HEAD')
        self.git('push', '-qu', 'origin', 'topic', 'HEAD:refs/pull/7/head')
        self.fixture = {
            'pr': dict(number=7, title='Change', body='Description', url='https://github.com/test/repo/pull/7',
                       state='OPEN', isDraft=False, headRefName='topic', baseRefName='main',
                       headRefOid=self.head, baseRefOid=base, mergeCommit=None,
                       mergeable='MERGEABLE', mergeStateStatus='CLEAN', statusCheckRollup=[]),
            'comments': [dict(id=1, body='Original finding', updated_at='2026-11-06T01:00:00Z',
                              user={'login': 'reviewer'}, html_url='https://example.invalid/1')],
            'reviews': [], 'inline': [], 'checks': []}
        self.fixture_path = self.base / 'fixture.json'
        self.save_fixture()
        bindir = self.base / 'bin'
        bindir.mkdir()
        gh = bindir / 'gh'
        gh.write_text('#!' + sys.executable + '\n' + '''import json, os, sys
f = json.load(open(os.environ['FIXTURE']))
a = sys.argv[1:]
if a[:2] == ['pr', 'view']: value = f['pr']
elif a[:2] == ['pr', 'checks']:
    if f.get('check_error'):
        print(f['check_error'], file=sys.stderr)
        raise SystemExit(1)
    value = f['checks']
elif a[:2] == ['api', 'graphql']:
    value = {'data': {'repository': {'pullRequest': {'createdAt': '2026-11-01T00:00:00Z', 'lastEditedAt': None}}}}
elif a[0] == 'api':
    path = next(x for x in a if x.startswith('repos/'))
    key = 'reviews' if '/reviews' in path else 'comments' if '/issues/' in path else 'inline'
    # Two pages exercise pagination/flattening, including an empty last page.
    value = [f[key][:1], f[key][1:]] if '--slurp' in a else f[key]
else: raise SystemExit('unexpected gh arguments: ' + repr(a))
print(json.dumps(value))
''')
        gh.chmod(0o755)
        self.env.update(PATH=str(bindir) + os.pathsep + self.env['PATH'], FIXTURE=str(self.fixture_path))

    def git(self, *args):
        return subprocess.run(['git', *args], cwd=self.repo, text=True, capture_output=True, check=True).stdout.strip()

    def save_fixture(self):
        self.fixture_path.write_text(json.dumps(self.fixture))

    def cli(self, *args):
        return subprocess.run([sys.executable, str(JX), *map(str, args)], cwd=self.repo,
                              env=self.env, text=True, capture_output=True)

    def snapshot(self, name, *args):
        out = self.base / name
        result = self.cli('review', 7, '--check-only', '--out', out, *args)
        return result, out / 'snapshot.json'

    def test_feedback_changes_without_new_commit(self):
        first, before = self.snapshot('before')
        self.assertEqual(first.returncode, 0, first.stderr)
        same, _ = self.snapshot('same', '--previous', before)
        self.assertEqual(same.returncode, 3, same.stderr)
        self.fixture['comments'][0]['body'] = 'Edited finding'
        self.fixture['comments'].append(dict(id=2, body='Second page', user=None))
        self.fixture['inline'] = [dict(id=3, body='Inline finding', path='change.py', line=1)]
        self.save_fixture()
        changed, after = self.snapshot('after', '--previous', before)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        changes = json.loads(after.read_text())['changes']
        self.assertFalse(changes['head'])
        self.assertEqual(changes['feedback']['edited'], ['comment:1'])
        self.assertEqual(changes['feedback']['added'], ['comment:2', 'inline:3'])
        self.assertIn('Inline finding', (after.parent / 'feedback.md').read_text())
        self.fixture['comments'] = []
        self.save_fixture()
        deleted, last = self.snapshot('deleted', '--previous', after)
        self.assertEqual(deleted.returncode, 0, deleted.stderr)
        self.assertEqual(json.loads(last.read_text())['changes']['feedback']['removed'], ['comment:1', 'comment:2'])

    def test_body_change_and_wrong_baseline(self):
        result, before = self.snapshot('before')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.fixture['pr']['body'] = 'Updated description'
        self.save_fixture()
        changed, after = self.snapshot('after', '--previous', before)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        self.assertTrue(json.loads(after.read_text())['changes']['description'])
        wrong = json.loads(before.read_text())
        wrong['pr']['url'] = 'https://github.com/another/repo/pull/7'
        before.write_text(json.dumps(wrong))
        invalid, _ = self.snapshot('invalid', '--previous', before)
        self.assertEqual(invalid.returncode, 2)

    def test_source_bundle_real_git_and_unchanged_head(self):
        out = self.base / 'bundle'
        result = self.cli('review', 7, '--out', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((out / 'tree/change.py').read_text(), 'answer = 42\n')
        self.assertIn('+answer = 42', (out / 'diff-code.patch').read_text())
        again = self.cli('review', 7, '--since', self.head, '--out', out)
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn('feedback', again.stdout.lower())

    def add_review_files(self):
        paths = ['build-output/run/report.json', 'package/locks/data.json', 'root.sha256',
                 'package/nested.sha256', 'SHA256SUMS', 'package/SHA256SUMS',
                 'root.lock', 'package/nested.lock', 'package/test_lock.py', 'change.py']
        for path in paths:
            target = self.repo / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('second revision\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'review paths')
        self.git('push', '-q', 'origin', 'topic', 'HEAD:refs/pull/7/head')
        self.fixture['pr']['headRefOid'] = self.git('rev-parse', 'HEAD')
        self.save_fixture()
        return paths

    def set_review_exclusions(self, patterns):
        config = self.repo / '.jx/review.json'
        config.parent.mkdir(exist_ok=True)
        config.write_text(json.dumps({'exclude': patterns}))
        return config

    def test_unconfigured_repo_includes_artifacts_and_locks(self):
        paths = self.add_review_files()
        out = self.base / 'unfiltered'
        result = self.cli('review', 7, '--out', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        patch = (out / 'diff-code.patch').read_text()
        for path in paths:
            self.assertIn(f'diff --git a/{path} b/{path}', patch)
        self.assertEqual(json.loads((out / 'snapshot.json').read_text())['exclude'], [])
        self.assertNotIn('files excluded', (out / 'diffstat.txt').read_text())

    def test_config_filters_full_delta_and_stats_with_real_git_globs(self):
        paths = self.add_review_files()
        patterns = ['build-output/**', '**/locks/**', '**/*.sha256', '**/SHA256SUMS', '**/*.lock']
        self.set_review_exclusions(patterns)
        out = self.base / 'filtered'
        result = self.cli('review', 7, '--since', self.head, '--out', out)
        self.assertEqual(result.returncode, 0, result.stderr)
        for artifact in ('diff-code.patch', 'delta-code.patch'):
            patch = (out / artifact).read_text()
            for path in paths[:-2]:
                self.assertNotIn(f'diff --git a/{path} b/{path}', patch)
                self.assertTrue((out / 'tree' / path).is_file())
            for path in paths[-2:]:
                self.assertIn(f'diff --git a/{path} b/{path}', patch)
        stats = (out / 'diffstat.txt').read_text()
        self.assertIn('files excluded', stats)
        self.assertIn('package/test_lock.py', stats)
        self.assertEqual(json.loads((out / 'snapshot.json').read_text())['exclude'], patterns)

    def test_config_change_and_removal_invalidate_same_head_artifacts(self):
        self.add_review_files()
        out = self.base / 'reused-config'
        first = self.cli('review', 7, '--out', out)
        self.assertEqual(first.returncode, 0, first.stderr)
        before = self.base / 'before-config.json'
        before.write_bytes((out / 'snapshot.json').read_bytes())
        config = self.set_review_exclusions(['build-output/**'])
        changed = self.cli('review', 7, '--check-only', '--previous', before, '--out', out)
        self.assertEqual(changed.returncode, 0, changed.stderr)
        snapshot = json.loads((out / 'snapshot.json').read_text())
        self.assertTrue(snapshot['changes']['exclusions'])
        self.assertIsNone(snapshot['source'])
        self.assertFalse((out / 'diff-code.patch').exists())
        self.assertFalse((out / 'tree').exists())
        rebuilt = self.cli('review', 7, '--out', out)
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        self.assertNotIn('build-output/run/report.json', (out / 'diff-code.patch').read_text())
        before.write_bytes((out / 'snapshot.json').read_bytes())
        config.unlink()
        removed = self.cli('review', 7, '--previous', before, '--out', out)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertTrue(json.loads((out / 'snapshot.json').read_text())['changes']['exclusions'])
        self.assertIn('build-output/run/report.json', (out / 'diff-code.patch').read_text())

    def test_config_errors_do_not_silently_fall_back(self):
        config = self.set_review_exclusions([])
        for value in ('{broken', '[]', '{"excludes": []}', '{"exclude": "*.lock"}', '{"exclude": [4]}'):
            with self.subTest(value=value):
                config.write_text(value)
                result = self.cli('review', 7, '--check-only', '--out', self.base / 'invalid-config')
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse((self.base / 'invalid-config').exists())

    def test_legacy_snapshot_without_exclusion_provenance_requires_review(self):
        first, before = self.snapshot('legacy')
        self.assertEqual(first.returncode, 0, first.stderr)
        legacy = json.loads(before.read_text())
        del legacy['exclude']  # Older clients applied implicit exclusions without recording them.
        before.write_text(json.dumps(legacy))
        changed, after = self.snapshot('after-upgrade', '--previous', before)
        self.assertEqual(changed.returncode, 0, changed.stdout + changed.stderr)
        self.assertTrue(json.loads(after.read_text())['changes']['exclusions'])

    def test_config_is_root_relative_and_output_is_caller_relative(self):
        self.add_review_files()
        self.set_review_exclusions(['build-output/**'])
        subdir = self.repo / 'package'
        result = subprocess.run([sys.executable, str(JX), 'review', '7', '--out', 'review-bundle'],
                                cwd=subdir, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        out = subdir / 'review-bundle'
        patch = (out / 'diff-code.patch').read_text()
        self.assertIn('root.lock', patch)
        self.assertNotIn('build-output/run/report.json', patch)

    def test_local_published_identity_and_dirty_checkout(self):
        result, snapshot = self.snapshot('published', '--local')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(snapshot.read_text())['local']['matches_published'])
        (self.repo / 'change.py').write_text('answer = 43\n')
        dirty, _ = self.snapshot('dirty', '--local')
        self.assertEqual(dirty.returncode, 1, dirty.stderr)
        self.git('commit', '-qam', 'unpublished change')
        unpublished, _ = self.snapshot('unpublished', '--local')
        self.assertEqual(unpublished.returncode, 1, unpublished.stderr)

    def test_new_head_invalidates_old_source_bundle(self):
        out = self.base / 'reused'
        first = self.cli('review', 7, '--out', out)
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual((out / 'tree/change.py').read_text(), 'answer = 42\n')
        (self.repo / 'change.py').write_text('answer = 43\n')
        self.git('commit', '-qam', 'next head')
        self.git('push', '-q', 'origin', 'topic', 'HEAD:refs/pull/7/head')
        self.fixture['pr']['headRefOid'] = self.git('rev-parse', 'HEAD')
        self.save_fixture()
        check = self.cli('review', 7, '--check-only', '--out', out)
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertFalse((out / 'tree').exists())
        self.assertFalse((out / 'diff-code.patch').exists())
        self.assertIsNone(json.loads((out / 'snapshot.json').read_text())['source'])
        same = self.cli('review', 7, '--since', self.fixture['pr']['headRefOid'], '--out', out)
        self.assertEqual(same.returncode, 0, same.stderr)
        self.assertFalse((out / 'tree').exists())
        rebuilt = self.cli('review', 7, '--out', out)
        self.assertEqual(rebuilt.returncode, 0, rebuilt.stderr)
        self.assertEqual((out / 'tree/change.py').read_text(), 'answer = 43\n')

    def test_real_cli_no_checks_response_and_auth_failure(self):
        for command in ('ci', 'triage'):
            for error, expected in [("no checks reported on the 'topic' branch", 8),
                                     ('HTTP 401: Bad credentials', 2)]:
                with self.subTest(command=command, error=error):
                    self.fixture['check_error'] = error
                    self.save_fixture()
                    result = self.cli('pr', command, 7)
                    self.assertEqual(result.returncode, expected, result.stderr)

    def test_ci_never_succeeds_when_unsettled_cancelled_or_missing(self):
        for command in ('ci', 'triage'):
            for bucket, expected in [('pending', 8), ('cancel', 1), ('pass', 0)]:
                with self.subTest(command=command, bucket=bucket):
                    self.fixture['checks'] = [dict(bucket=bucket, state=bucket.upper(), name='Test', link='', workflow='CI')]
                    self.save_fixture()
                    result = self.cli('pr', command, 7, '--wait', '--timeout', 0)
                    self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            self.fixture['checks'] = []
            self.save_fixture()
            self.assertEqual(self.cli('pr', command, 7).returncode, 8)

    def test_check_records_failure_and_does_not_flood_output(self):
        out = self.base / 'failure'
        result = self.cli('check', 'run', '--out', out, '--', sys.executable, '-c',
                          'print("x" * 20000); raise SystemExit(7)')
        self.assertEqual(result.returncode, 7, result.stderr)
        receipt = json.loads((out / 'result.json').read_text())
        self.assertEqual(receipt['command_exit'], 7)
        self.assertEqual(receipt['before']['head'], self.head)
        self.assertLess(len(result.stdout), 2000)
        self.assertGreater((out / 'output.log').stat().st_size, 20000)
        self.assertEqual(self.cli('check', 'show', out).returncode, 7)

    def test_junit_counts_actual_cases_and_rejects_missing_report(self):
        out = self.base / 'junit'
        xml = '<testsuites tests="999"><testsuite><testcase/><testcase><skipped/></testcase><testcase><failure/></testcase></testsuite></testsuites>'
        command = 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])'
        result = self.cli('check', 'run', '--out', out, '--junit', 'junit.xml', '--',
                          sys.executable, '-c', command, '{out}/junit.xml', xml)
        self.assertEqual(result.returncode, 1, result.stderr)
        receipt = json.loads((out / 'result.json').read_text())
        self.assertEqual(receipt['tests'], dict(total=3, passed=1, skipped=1, failures=1, errors=0))
        missing = self.cli('check', 'run', '--out', self.base / 'missing', '--junit', 'junit.xml', '--',
                           sys.executable, '-c', 'pass')
        self.assertEqual(missing.returncode, 2, missing.stderr)

    def test_check_pass_is_literal_and_output_is_not_overwritten(self):
        out = self.base / 'pass'
        literal = '$(touch SHOULD_NOT_EXIST); `echo unsafe`'
        result = self.cli('check', 'run', '--out', out, '--', sys.executable, '-c',
                          'import sys; print(sys.argv[1])', literal)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((out / 'output.log').read_text().strip(), literal)
        self.assertFalse((self.repo / 'SHOULD_NOT_EXIST').exists())
        original = (out / 'result.json').read_bytes()
        rerun = self.cli('check', 'run', '--out', out, '--', sys.executable, '-c', 'pass')
        self.assertNotEqual(rerun.returncode, 0)
        self.assertEqual((out / 'result.json').read_bytes(), original)

    def test_empty_or_invalid_junit_does_not_pass(self):
        for i, xml in enumerate(['<testsuites/>', '<broken']):
            result = self.cli('check', 'run', '--out', self.base / f'xml-{i}', '--junit', 'junit.xml', '--',
                              sys.executable, '-c', 'from pathlib import Path; import sys; Path(sys.argv[1]).write_text(sys.argv[2])',
                              '{out}/junit.xml', xml)
            self.assertEqual(result.returncode, 2, result.stderr)


if __name__ == '__main__':
    unittest.main()
