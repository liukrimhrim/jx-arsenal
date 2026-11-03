# jx-arsenal

Small, agent-friendly CLIs distilled from recurring command sequences in my coding-agent
sessions. Each lever replaces a hand-rolled `tool --json | python3 -c '...'` chain with one
command that already emits the answer shape (TSV by default, `--json` for raw payloads).

Design rules: stdlib only where possible · machine-readable output · `--dry-run` on anything
that posts or mutates · `--help` and a `doctor` subcommand per tool · errors name the next command.

| Lever | What it covers |
|---|---|
| [`gh_levers/`](gh_levers/) | `pr`: review threads, reply/comment with verify, commits since last review, check rollup. `ci`: Actions runs, jobs by attempt, failed-job log triage, dispatch, wait. |

## Install as Claude Code skills

```bash
ln -s "$PWD/gh_levers" ~/.claude/skills/gh_levers
```

Each lever directory carries a `SKILL.md` so the agent discovers it.
