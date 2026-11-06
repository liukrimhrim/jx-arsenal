# jx modal

Modal through one stdlib Python CLI over the `modal` command. TSV by default, `--json` in any position
for the raw CLI payload. Read-only: no deploy, stop, rollback, or serve. `-p PROFILE` picks a workspace
profile and `-e ENV` an environment; both work before or after the subcommand.

```bash
jx modal whoami                                  # active profile, environments, default environment
jx modal apps                                    # deployed apps, busiest first: id, name, tasks, created
jx modal apps --state all -p other-workspace
jx modal containers                              # every running container with its age
jx modal containers --count                      # containers per app
jx modal containers my-app --watch 300 --interval 5   # the count sampled every 5s for 5 minutes, as rows
jx modal logs my-app --since 30m --grep 'error|Traceback' -C 3
jx modal logs ta-01ABC... --tail 50              # one container's last lines, however old
jx modal logs my-app --for 30                    # follow live for 30 seconds, then stop
jx modal history my-app                          # versions with when, who, commit
jx modal cost --since 7d --top 10                # spend per app with share of total
jx modal cost --for yesterday --hourly --top 3   # the time axis for the top spenders
jx modal ls secrets | envs | volumes
```

## What it fixes

`containers --watch` replaces the hand-written loop that polled `modal container list` every few seconds
through an autoscaling test. `logs --for N` replaces attaching to a stream and killing it by hand.
`cost` sums the billing report per app and prints the share, which is the question the raw report makes
you compute.

Every timestamp is UTC. The Modal CLI prints local time with an offset; a value with no offset, such as
a billing interval, is already UTC. `--tail` without `--since` means the last N lines whenever they were
written, so an idle container still shows its log. Modal draws errors in a box; the tool keeps the text
and adds the command that lists valid names.
