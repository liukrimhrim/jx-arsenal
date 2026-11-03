---
name: gh_levers
description: Agent-friendly CLIs for PR review threads, replies, CI check rollups, Actions runs/jobs/logs by attempt, dispatch and wait. Use instead of hand-rolled `gh api | python3 -c` chains.
---

# gh levers: `pr` and `ci`

Two stdlib-only Python CLIs over `gh`, personal tooling in `~/.claude/skills` (not in any repo). TSV output by default, `--json` for the raw payload,
`-R owner/name` to target another repo (default: the current checkout). Each has `doctor`.
Run with `--help` for the full command list; the summary below is what to reach for.

```bash
L=~/.claude/skills/gh_levers   # symlink to this repo (see README)
```

## PR review loop

```bash
$L/pr.py threads 1234                  # unresolved review threads: comment_id, path, line, author, body (200 chars)
$L/pr.py threads 1234 --all --full     # include resolved, full bodies
$L/pr.py since 1234                    # commits + per-file diffstat pushed after the last review by someone else
$L/pr.py reply 1234 <comment_id> --body-file reply.md --dry-run   # show exactly what would post
$L/pr.py reply 1234 <comment_id> --body-file reply.md             # post, then re-read and print {"verified": true}
$L/pr.py comment 1234 --body-file note.md                         # top-level comment, same verify
```

`reply` and `comment` are outward actions. Always run `--dry-run` first and post only with the
user's approval of that exact text. Use the `comment_id` of the thread's root comment (`root=True`).

## CI on a PR

```bash
$L/pr.py ci 1234                 # checks sorted fail > pending > pass; exit 1 if anything failed
$L/pr.py ci 1234 --wait          # poll every 30s until nothing is pending (--interval, --timeout)
$L/pr.py ci 1234 --failed        # job_id / run_id of each failed check, ready for `ci failed` or `ci logs`
```

## Actions runs, jobs, logs

```bash
$L/ci.py runs ci.yml --branch main --limit 5   # workflow by file name or display name
$L/ci.py jobs <run_id>                      # failed jobs first; --attempt 1 reads the ORIGINAL attempt after a rerun
$L/ci.py failed <run_id> [--attempt 1]      # every failed job: failed steps + error-ish log lines with context
$L/ci.py logs <job_id> --grep 'pattern' -C 3   # one job's log, timestamps and ANSI stripped; --tail N without --grep
$L/ci.py dispatch some.yml --ref my-branch -f key=value --wait   # workflow_dispatch, resolve the new run id, wait
$L/ci.py wait <run_id>                      # exit 0 only on success
```

`failed` greps for `##[error]`, `error`, `traceback`, `failed`, `exit code N` by default. A job can
fail with no `FAILED` line, so read the hits, not just the count. `gh run view --log-failed` only
shows the latest attempt; `--attempt` is how to see what originally broke.

## Composing

```bash
$L/pr.py ci 1234 --failed | awk -F'\t' 'NR>1{print $2}' | sort -u | xargs -n1 $L/ci.py failed
```
