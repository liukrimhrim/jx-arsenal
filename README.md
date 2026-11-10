# jx-arsenal

Small, agent-friendly CLIs distilled from recurring command sequences in my coding-agent
sessions. Each lever replaces a hand-rolled `tool --json | python3 -c '...'` chain with one
command that already emits the answer shape (TSV by default, `--json` for raw payloads).

Design rules: stdlib only where possible · machine-readable output · `--dry-run` on anything
that posts or mutates · `--help` and a `doctor` subcommand per tool · errors name the next command.

| Tool | What it covers |
|---|---|
| `jx pr` | one-row PR state for several PRs (`show`), top-level comments and review verdicts since a point (`comments`), review threads with comment ids, verified reply/comment/title-and-body edit, commits since last review, check rollup, `triage` = checks + failed logs in one call |
| `jx review` | exact-head source/diff preparation, paginated feedback snapshots, edited/deleted comment detection, local/published identity check |
| `jx check` | run a chosen command once, retain full logs and Git state, derive counts from JUnit, print a compact reusable receipt |
| `jx preflight` | the checks reviewers ran after a PR opened, run by the author before it: stale references to changed names, broken callers, registry and citation drift, PR-body claims vs `jx check` receipts, CI coverage of new tests, Modal image order, parser/process lints, credential shapes; `--full` adds test collection, a fix-removal run and mutation of changed lines, sandboxed on macOS so no test run can signal a process outside its own tree. Each check names the past miss it came from; repo packs add checks via `.jx/preflight.json`; `jx preflight vendor` pins a copy of the engine in a repo so the run works there without jx |
| `jx agent` | Codex delegation hook (fresh contexts, bounded handoffs, one reviewer per repository/commit per chat) and a Claude Code PreToolUse hint that names the jx command for a raw `gh`/`git show`/`uv venv` loop, once per session |
| `jx ci` | Actions runs, jobs by attempt, failed-job log triage, per-job and per-step timings with pytest counts across runs, dispatch with `--dry-run`, wait; ids chain via stdin `-` |
| `jx venv` | venvs keyed by Python request, package pins and requirements-file bytes, built once with uv and reused by path across sessions; for tests that take pinned interpreters by path |
| `jx k` | kubectl by environment: pods with waiting reasons, images + GIT_SHA, Karpenter node census, events, logs, rollout, secrets; `jx k all` fans out |
| `jx aws` | SSO-aware whoami, CloudWatch metrics/alarms, ECR tags, Cost Explorer, Logs Insights, EKS; every timestamp UTC |
| `jx obs` | Prometheus (AMP) and Loki by environment: PromQL instant/range, metric and label discovery, sample freshness, rules and alerts, LogQL lines and per-bucket counts; `jx obs all` fans out. Hand SigV4, no boto3 |
| `jx tf` | Terraform roots: discover roots (changed vs main), fmt + validate, lock-free plan summarized to one row per change with the exit code terraform uses, attribute diff of a saved plan, state list, doctor. Never applies |
| `jx git` | git idioms: read a file or listing at origin/main or a PR head without checkout (whole, or sliced by outline, section, grep or line range), what changed since main, a guarded `commit` that stages only named paths and refuses the wrong branch, worktrees, archive export, open PRs touching the same files |
| `jx temporal` | Temporal by environment through a per-call port-forward: counts and lists by type/status/queue/time, describe with the innermost failure, history with failures only, failed-run grouping, task-queue pollers, schedules |
| `jx oncall` | incident.io and PagerDuty, read-only: who is on call on every schedule, open incidents across both, alerts and alert grouping, one incident with its alerts and timeline, escalations, users, routes, a provider reconciliation count, and a raw `api` GET with pagination. Tokens from env or a configured command, never printed |
| `jx mem`, `jx tx` | the agent's own bookkeeping: memory files of the current project (list, show, grep, add with a duplicate check, append, edit, doctor for index drift) and session transcripts (sessions, grep, the Bash commands run, one session's messages) |
| `jx modal` | Modal, read-only: apps and task counts, running containers with per-app counts sampled over time, past or briefly followed logs with UTC timestamps, deploy history, spend per app with share, secrets/volumes/environments |
| `jx bench`, `jx tx usage` | measure the arsenal itself: what each session cost in model calls, tokens, model vs tool time and Bash mix, and a paired benchmark that answers the same factual questions with the tools present and hidden |

`jx ls` prints every command on one line each. Dispatcher: `bin/jx`; tools: `tools/*.py`; notes: `docs/`.

Review/check examples and exit codes: [agent workflow commands](docs/agent-workflow.md).

## Install

```bash
ln -sfn "$PWD/bin/jx" /opt/homebrew/bin/jx        # or any PATH dir
ln -sfn "$PWD" ~/.claude/skills/jx                # one skill entry; SKILL.md at the repo root
```
