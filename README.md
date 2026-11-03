# jx-arsenal

Small, agent-friendly CLIs distilled from recurring command sequences in my coding-agent
sessions. Each lever replaces a hand-rolled `tool --json | python3 -c '...'` chain with one
command that already emits the answer shape (TSV by default, `--json` for raw payloads).

Design rules: stdlib only where possible · machine-readable output · `--dry-run` on anything
that posts or mutates · `--help` and a `doctor` subcommand per tool · errors name the next command.

| Tool | What it covers |
|---|---|
| `jx pr` | review threads with comment ids, verified reply/comment, commits since last review, check rollup, `triage` = checks + failed logs in one call |
| `jx ci` | Actions runs, jobs by attempt, failed-job log triage, dispatch with `--dry-run`, wait; ids chain via stdin `-` |
| `jx k` | kubectl by environment: pods with waiting reasons, images + GIT_SHA, Karpenter node census, events, logs, rollout, secrets; `jx k all` fans out |
| `jx aws` | SSO-aware whoami, CloudWatch metrics/alarms, ECR tags, Cost Explorer, Logs Insights, EKS; every timestamp UTC |
| `jx obs` | Prometheus (AMP) and Loki by environment: PromQL instant/range, metric and label discovery, sample freshness, rules and alerts, LogQL lines and per-bucket counts; `jx obs all` fans out. Hand SigV4, no boto3 |
| `jx tf` | Terraform roots: discover roots (changed vs main), fmt + validate, lock-free plan summarized to one row per change with the exit code terraform uses, attribute diff of a saved plan, state list, doctor. Never applies |
| `jx git` | git idioms: read a file or listing at origin/main or a PR head without checkout, what changed since main, a guarded `commit` that stages only named paths and refuses the wrong branch, worktrees, archive export, open PRs touching the same files |

`jx ls` prints every command on one line each. Dispatcher: `bin/jx`; tools: `tools/*.py`; notes: `docs/`.

## Install

```bash
ln -sfn "$PWD/bin/jx" /opt/homebrew/bin/jx        # or any PATH dir
ln -sfn "$PWD" ~/.claude/skills/jx                # one skill entry; SKILL.md at the repo root
```
