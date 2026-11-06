#!/usr/bin/env python3
"""jx check — run a chosen check once and keep its command, logs and measured results.

  jx check run [--out DIR] [--junit FILE] -- COMMAND [ARGS...]
  jx check show DIR

DIR must be new; default is a temporary directory. {out} in COMMAND expands to DIR.
--junit names a fresh XML file inside DIR, written by COMMAND. Counts come from test
cases, not console text. No command selection, automatic retries or cached PASS.
Full output stays in output.log; stdout contains only a short result and file paths.
Commands run with their normal environment and permissions; this is not a sandbox.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET


def source_state():
    def git(*args):
        result = subprocess.run(['git', *args], capture_output=True)
        return result.stdout if result.returncode == 0 else None
    head = git('rev-parse', 'HEAD')
    if head is None:
        return None
    return dict(head=head.decode().strip(), status=git('status', '--porcelain=v1').decode(),
                tracked_diff_sha256=hashlib.sha256(git('diff', '--binary', 'HEAD')).hexdigest())


def test_counts(path):
    counts = dict(total=0, passed=0, failures=0, errors=0, skipped=0)
    for case in ET.parse(path).iter('testcase'):
        counts['total'] += 1
        outcome = next((plural for tag, plural in [('error', 'errors'), ('failure', 'failures'), ('skipped', 'skipped')]
                        if case.find(tag) is not None), 'passed')
        counts[outcome] += 1
    return counts


def render(result, out):
    lines = [f"exit={result['exit_code']} command_exit={result['command_exit']} elapsed={result['seconds']:.2f}s",
             f"command: {shlex.join(result['argv'])}"]
    if result['before']:
        lines.append(f"head: {result['before']['head']} ({'dirty' if result['before']['status'] else 'clean'})")
        if result['before'] != result['after']:
            lines.append('Source state changed during the command; do not attribute this result to one revision.')
    if result.get('tests') is not None:
        lines.append('tests: ' + ' '.join(f'{k}={v}' for k, v in result['tests'].items()))
    if result.get('report_error'):
        lines.append('report error: ' + result['report_error'])
    lines.extend([f"log: {out / 'output.log'}", f"receipt: {out / 'result.json'}"])
    return '\n'.join(lines) + '\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='operation', required=True)
    run = commands.add_parser('run')
    run.add_argument('--out', type=Path)
    run.add_argument('--junit', help='XML path relative to the new output directory')
    run.add_argument('argv', nargs=argparse.REMAINDER)
    show = commands.add_parser('show')
    show.add_argument('out', type=Path)
    args = parser.parse_args()
    if args.operation == 'show':
        result = json.loads((args.out / 'result.json').read_text())
        print(render(result, args.out), end='')
        return result['exit_code']
    argv = args.argv[1:] if args.argv[:1] == ['--'] else args.argv
    if not argv:
        parser.error('provide a command after --')
    out = args.out.resolve() if args.out else Path(tempfile.mkdtemp(prefix='jx-check-'))
    if args.out:
        out.mkdir(parents=True, exist_ok=False)
    junit = (out / args.junit).resolve() if args.junit else None
    if junit and not junit.is_relative_to(out):
        parser.error('--junit must name a fresh report inside --out')
    argv = [value.replace('{out}', str(out)) for value in argv]
    result = dict(argv=argv, cwd=str(Path.cwd()), started_at=datetime.now(timezone.utc).isoformat(),
                  before=source_state())
    started = time.monotonic()
    with (out / 'output.log').open('wb') as log:
        try:
            code = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT).returncode
        except OSError as error:
            log.write(str(error).encode())
            code = 127
    result.update(command_exit=code, exit_code=code if code >= 0 else 128 - code,
                  seconds=time.monotonic() - started, after=source_state())
    if junit:
        try:
            result['tests'] = test_counts(junit)
            if result['tests']['failures'] or result['tests']['errors']:
                result['exit_code'] = result['exit_code'] or 1
            if not result['tests']['total']:
                raise ValueError('JUnit report contains no test cases')
        except (OSError, ET.ParseError, ValueError) as error:
            result['report_error'] = str(error)
            result['exit_code'] = result['exit_code'] or 2
    (out / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    summary = render(result, out)
    (out / 'summary.txt').write_text(summary)
    print(summary, end='')
    return result['exit_code']


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        sys.exit(f'jx check: {error}')
