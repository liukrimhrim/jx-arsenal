# Mechanical work in one command

These commands replace repeated collection and reporting. They do not choose tests,
judge a review finding, approve an external action, or establish runtime behavior.

## Delegation guard (Codex)

`tools/agent.py hook` runs only on agent launches and handoffs. It rewrites
`spawn_agent` to `fork_turns: "none"` in the same call, preserving other arguments.
It denies messages over 16 KiB (UTF-8 bytes, not a token count); use file pointers
for long evidence. It does not limit system instructions, skills, subsequent file
reads, or the agent's accumulated context. Normal successful calls produce no
extra model-visible prose.

A reviewer/auditor role or task name, a prompt starting with Review/Audit, or an
explicit key activates review deduplication. Include exactly one line copied from
`jx review`, or from `jx agent key` for a clean committed local checkout:

```text
Review-Key: github.com/OWNER/REPO@FULL_COMMIT_SHA
```

One new reviewer may launch for that repository/commit per parent chat, regardless
of its name or prompt wording. A changed commit permits another review. A necessary
repeat of the same commit (new comments, failed reviewer, distinct required review)
uses `jx agent release SESSION KEY --reason 'specific new evidence'`; the denial
prints the exact session/key. Choosing that exception still requires judgment.
The key must name the actual target. This does not recognize disguised review
tasks, compare semantic scope, deduplicate across chats/clients, or prevent someone
from asking an existing agent to review again. Followups are size-limited only.

SQLite reserves launches atomically, including concurrent calls. PostToolUse
records the successful agent ID/name or releases a failed spawn. If the client
never delivers PostToolUse, a pending reservation expires after two minutes;
successful records last for the chat. State is in
`${XDG_STATE_HOME:-~/.local/state}/jx/agents.sqlite3`. No network, model, transcript
scan, or background process is used. This is an efficiency guard, not a security
boundary: hooks must be enabled/trusted, and client hook failures can fail open.

Install the entries in `hooks/agent-guard.toml` into the existing `[hooks]` arrays
in `~/.codex/config.toml`, adjusting the absolute script path if needed. Preserve
existing entries. Run `/hooks` in Codex CLI and review/trust both new definitions.
Do not edit trust hashes. Codex skips untrusted definitions; installation alone
does not establish that enforcement is active. See the official
[hook contract and trust flow](https://learn.chatgpt.com/docs/hooks).

## Review intake and rechecks

```sh
jx review 76 --out /tmp/pr76-before
jx review 76 --since REVIEWED_SHA --previous /tmp/pr76-before/snapshot.json --out /tmp/pr76-after
```

The first command saves the PR body, all top-level comments, review bodies and inline
comments, exact-head source snapshot, code diff, and grouped diffstat. All changed
files are included unless the checkout root contains `.jx/review.json`:

```json
{"exclude": ["build-output/**"]}
```

Patterns use Git's root-relative `glob` pathspec syntax (`*` stays within one path
component; `**/` spans zero or more directories). Git applies the same exclusions
to full/delta patches and file grouping. Excluded files remain in the source tree
and grouped diffstat; inspect them separately when relevant. Configuration comes
from the local checkout, not the PR branch. The effective list is printed and saved
in `snapshot.json`; invalid JSON or an invalid config shape fails instead of silently
using defaults. Missing config means no exclusions. `--paths` limits the source export.
Legacy snapshots without recorded exclusions count as changed on their first recheck.

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

When source inputs or effective exclusions change, old source/patch files
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

The command runs with the caller's normal permissions. Repository-specific isolation,
verification and cleanup belong in that repository's tools and instructions.

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
