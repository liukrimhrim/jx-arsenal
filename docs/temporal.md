# jx temporal

Temporal by environment. One stdlib Python CLI that port-forwards the frontend Service for the duration of
one call, runs the local `temporal` CLI with JSON output, and prints TSV. `--json` in any position for the
raw payload. Read-only: no terminate, cancel, signal, reset, schedule pause, or batch commands exist here.

## Access

`<env>` is a kubectl context. `~/.config/jx-arsenal/temporal.json` can map names and override the
Kubernetes namespace, the frontend Service suffix, and the Temporal namespace; defaults are `temporal`,
`frontend-headless`, and `default`. Each call opens its own forward on a free local port and closes it,
so two calls never share a port and nothing is left running. `jx temporal <env> doctor` proves the chain
in about five seconds.

## Questions this answers in one call

```bash
jx temporal prod count --since 1d --by-status                  # executions by status in the window
jx temporal prod list --type my-workflow --status Failed --since 6h --limit 20
jx temporal prod failed --since 6h --group                      # failure message, count, example id: the triage table
jx temporal prod failed --type my-workflow --since 1d           # one row per failed run with its innermost failure
jx temporal prod describe <workflow-id>                          # status, times, innermost failure, pending activities
jx temporal prod history <workflow-id> --failures                # only the failing events, with the message
jx temporal prod history <workflow-id> --grep 'child|marker'
jx temporal prod queue my-task-queue                             # who is polling, how long ago, backlog
jx temporal prod schedules                                       # paused, type, next run, last run, last result
T_ENVS=dev,stage,prod jx temporal all count --since 1h --by-status
```

Filters compose into a visibility query: `--type`, `--status`, `--queue`, `--since`, and `--query` for
anything else, ANDed verbatim. A bad query prints the syntax and a command that shows real values.

## Why these shapes

`failed --group` replaces the loop of list, describe each, parse the failure chain, count by message that
was written by hand for every incident. `describe` reads the innermost `cause.message` from the close
event, so a failed run needs no history fetch. `queue` exits 1 when nothing is polling, which is the
stuck-queue tell. `history --failures` keeps the first event and every failed, timed-out, terminated, or
canceled event, which is usually the whole story in under ten rows.
