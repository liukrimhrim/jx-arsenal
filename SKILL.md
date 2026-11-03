---
name: jx
description: One CLI for the loops agents otherwise glue by hand — PR review threads and verified replies, CI run/job/log triage, kubectl across environments, AWS CloudWatch/ECR/cost/Logs Insights/EKS, Prometheus (AMP) and Loki queries by environment, Terraform check/plan/diff per root. Run `jx ls` once to see every command. Use it instead of gh/kubectl/aws piped into python heredocs; chain with stdin `-`, and prefer `--wait` and `jx pr triage` so one call finishes a loop.
---

# jx

`jx ls` lists every command on one line each. `jx <tool> --help` is the deep reference for one tool.
`jx doctor` checks gh auth, aws identity and SSO token, kubectl contexts. Tools: `pr`, `ci`, `k`, `aws`, `obs`, `tf`.

Output is TSV with a header row; `--json` in any position gives the raw payload. Error messages
name the command that lists valid ids. Anything that posts or triggers has `--dry-run`.

## Keep the model out of the loop

```bash
jx pr triage 1234 --wait                       # wait for checks, then failed-job log lines for every failing run
jx pr ci 1234 --failed | jx ci failed -        # same thing as two composable steps; `-` reads run_id from stdin
jx ci runs deploy.yml --branch main --limit 3 | jx ci wait -
jx ci dispatch deploy.yml -f env=stage --wait  # one call: trigger, resolve run id, block until done
K_ENVS=dev,stage,prod jx k all images my-api -n default   # what is running where, one table
jx obs all fresh 'up{job="my-api"}' --max-age 120      # are samples landing in every environment, exit 1 if not
jx tf roots infra --changed | jx tf check -              # fmt + validate only the roots this branch touched
```

Column names are argument names (`run_id`, `job_id`, `comment_id`), so `cut`/`awk` are rarely
needed and `-` usually is enough. Intermediate results never need to pass through the model.

## Rules

- Reach for `jx` before writing `gh api ... | python3 -c`, `kubectl -o json | python3`, `aws ... | jq`, a SigV4 `curl` against AMP, or `terraform show -json | python3`.
- `jx pr reply` / `jx pr comment` post to GitHub: `--dry-run` first, post only with the user's approval of that exact text.
- Deeper notes per tool: `docs/gh.md`, `docs/k.md`, `docs/aws.md` in this directory.
