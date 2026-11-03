# jx oncall

incident.io and PagerDuty, read-only, through one stdlib Python CLI. TSV by default, `--json` in any
position for the raw payload. GET only: nothing here acknowledges, resolves, pauses, or creates.

## Tokens

`INCIDENTIO_API_KEY` and `PAGERDUTY_API_KEY` in the environment win. Otherwise
`~/.config/jx-arsenal/oncall.json` names a command per provider whose stdout is the token:

```json
{"incidentio": {"token_cmd": "op read op://vault/item/field"},
 "pagerduty":  {"token_cmd": "some-command-that-prints-the-key"}}
```

Tokens are held in memory for one call and never printed. `jx oncall doctor` reports which source
each provider used and whether a one-item read succeeded.

## Questions this answers in one call

```bash
jx oncall who                                   # every schedule of both providers: who, until when, which layer
jx oncall who --at 2026-12-25T09:00:00Z
jx oncall incidents --status open               # open on either provider, newest first, with owner and url
jx oncall incidents --since 7d --provider pagerduty
jx oncall alerts --since 1d --group             # alert volume by dedup-key prefix, how many still firing
jx oncall alerts --source my-alert-source --status firing
jx oncall show <id>                             # provider detected from the id shape; alerts and the trigger/ack/notify/resolve timeline
jx oncall escalations --since 7d                # what paged, on which path, who acknowledged
jx oncall users <name-or-email-fragment>
jx oncall routes                                # alert routes (enabled?) and alert sources
jx oncall reconcile --since 2d                  # do the two providers agree on volume in the window
jx oncall api incidentio /v2/alerts --param 'status[one_of]=firing' --all   # any endpoint, paginated, raw JSON
```

## API facts baked in

incident.io ignores `status=` on `/v2/alerts`; the working filter is `status[one_of]=`. Its
`created_at[gte]` on `/v2/incidents` accepts a date, not a timestamp, so the exact cut is applied
client-side. Page-size ceilings differ per endpoint, so each call pins its own. PagerDuty
sub-resources carry a local UTC offset; every timestamp is printed in UTC. Pagination is followed
end to end: incident.io by `pagination_meta.after`, PagerDuty by `offset` while `more` is true.

## Why these shapes

`who` across both providers exists because the two rotations have disjoint rosters and the
question "who gets paged for this" has two answers. `reconcile` is the volume parity check that was
done by hand after every routing change. `show` detects the provider so an id pasted from a Slack
message needs no flag. `api` is the escape hatch that replaces a raw `curl` with the auth and
pagination already handled.
