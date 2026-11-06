# Mechanical work in one command

These commands replace repeated collection and reporting. They do not choose tests,
judge a review finding, approve an external action, or certify GPU behavior.

## Review intake and rechecks

```sh
jx review 76 --out /tmp/pr76-before
jx review 76 --since REVIEWED_SHA --previous /tmp/pr76-before/snapshot.json --out /tmp/pr76-after
```

The first command saves the PR body, all top-level comments, review bodies and inline
comments, exact-head source snapshot, code diff, and grouped diffstat. Generated evidence
and locks are listed in the diffstat but excluded from the code patch; inspect them
separately when relevant. `--paths` limits the source export.

For a status/feedback recheck, skip the source export:

```sh
jx review 76 --check-only --previous /tmp/pr76-before/snapshot.json --out /tmp/pr76-status
jx review 76 --check-only --local --out /tmp/pr76-published
```

`--previous` reports added, edited and removed feedback even when the commit is unchanged.
Use the snapshot from the last review as the baseline. Exit 3 means review inputs are
unchanged, not that findings were fixed or CI passed. Exit 0 means a snapshot was captured
or inputs changed. Read `feedback.md` for actual findings. The snapshot is an observation,
not an atomic transaction or authorization.

When an output directory is reused for different source inputs, old source/patch files
are removed even in `--check-only` mode. The snapshot's `source` field is null until a
full export succeeds; new metadata cannot silently label an old source tree.

`--local` is for the author's checkout: local HEAD and the remote branch must match
the PR head, with a clean checkout. Mismatch/dirty state exits 1. It never pushes.

## Run a chosen check and report its actual result

```sh
jx check run --out /tmp/my-check --junit junit.xml -- \
  python -m pytest path/to/tests -q --junitxml '{out}/junit.xml'
jx check show /tmp/my-check
```

Or wrap any existing command without `--junit`. The output directory must be new;
omit `--out` to use a fresh temporary directory. The wrapper runs an argv directly,
without a shell or retries. Full stdout/stderr go to `output.log`. `result.json` records
the exact command, working directory, start time, elapsed time, exit status, and Git
state before/after. `summary.txt` is the short, reusable report. `show` does not rerun it.

JUnit counts come from actual test cases. Missing, malformed or empty requested XML
cannot pass, and failing cases cannot pass even if the command returns zero. Skipped
cases remain visible. Without JUnit, the receipt reports command success only.

A dirty checkout is labeled dirty; its HEAD is not a claim that all tested files were
committed. Tracked diffs are fingerprinted, but untracked file contents and external
dependencies are not. Receipts are historical observations, never cached permission
to skip tests after a change. Commands/outputs may contain sensitive data: pass secrets
through the environment, not command arguments, and inspect logs before sharing them.

The command runs with the caller's normal permissions. For guarded OCRia offline
verification, existing feature checks and mutation cleanup, use
`.claude/skills/verify-ocria-inference/scripts/verify-ocria` from the intended checkout.
This wrapper is not a replacement for its isolation or cleanup.

## CI

```sh
jx pr triage 76 --wait --timeout 600
```

Existing waiting and failure-log collection stay inside the script. CI commands exit
0 only when reported checks have settled without failures/cancellations; 1 means
failed/cancelled, 8 means pending/no checks, and 2 means an API/auth/usage error.
Skipped checks remain skipped; this does not establish required coverage. Run long
waits in a background tool session so the agent can still respond to the user.

No command here posts a comment, edits a PR, merges or deploys. Approval requirements
still apply to those actions.
