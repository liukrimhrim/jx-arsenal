# jx obs

Prometheus (Amazon Managed Prometheus) and Loki by environment. One stdlib Python CLI; AMP calls are
SigV4-signed by hand with credentials from `aws configure export-credentials`, Loki is plain HTTP over
the tailnet. TSV by default, `--json` in any position for the raw API payload.

## Config

`~/.config/jx-arsenal/obs.json` holds everything environment-specific. Nothing in the repo does.

```json
{"envs": {
  "dev":  {"profile": "dev",  "region": "us-east-1", "workspace": "ws-...", "loki": "http://dev-loki:3100"},
  "prod": {"profile": "prod", "region": "us-east-1", "workspace": "ws-...", "loki": "http://prod-loki:80"}
}}
```

`aws amp list-workspaces --profile <p>` gives the workspace id. `jx obs <env> doctor` proves the whole chain.

## Questions this answers in one call

```bash
jx obs prod q 'sum by (job) (rate(http_requests_total[5m]))'          # instant vector, one row per series
jx obs prod q 'sum(rate(container_cpu_usage_seconds_total[5m]))' --since 6h --step 15m   # range: points, first/last, min/max
jx obs prod metrics container_                                         # what metric names exist
jx obs prod series '{job="api"}'                                       # per metric: series count and label names
jx obs prod labels 'up{job="api"}'                                     # per label: distinct values and samples
jx obs all fresh 'up{job="api"}' --max-age 120                         # are samples landing everywhere; exit 1 if not
jx obs prod rules --firing                                             # which alert rules are firing or pending
jx obs prod alerts                                                     # active alerts with labels and summary
jx obs prod logs '{kubernetes_namespace_name="default"} |= "error"' --since 30m --limit 50
jx obs prod logcount '{kubernetes_namespace_name="default"} |= "error"' --since 6h --step 1h --by kubernetes_container_name
jx obs prod loki-labels kubernetes_namespace_name                      # stream label values
```

## Why these shapes

`fresh` exists because "are the new samples landing in every environment" was asked by hand dozens of
times and answered with a heredoc each time. `series` and `labels` replace the label census heredoc
("which metrics lack a container label", "discover the real label names"). `logcount` is the per-bucket
error count that otherwise becomes a `query_range` plus a python reshape. `--json` keeps the raw
Prometheus or Loki payload for the rare case the table is not the right shape.

Instant results drop `__name__` for `timestamp()`, so `fresh` recovers the metric name from the selector.
`q` prints the scalar directly when the result is a scalar.
