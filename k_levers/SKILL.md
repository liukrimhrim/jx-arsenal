---
name: k_levers
description: kubectl lever for agents — pods with waiting reasons, deployment images and GIT_SHA, Karpenter node/nodeclaim census, warning events, grepped logs, rollout, secret keys, across one or every environment. Use instead of kubectl -o json | python3 chains.
---

# k lever

One stdlib Python CLI over `kubectl`. TSV by default, `--json` for the raw kubectl payload.
`<env>` is the kubectl context name; `~/.config/jx-arsenal/k.json` can map names to contexts and
defines the list `k all` iterates (or set `K_ENVS=a,b,c`). Run `k <env> doctor` first in a new shell.

```bash
K=~/.claude/skills/k_levers/k.py   # symlink to this repo's k_levers/
```

## Read the cluster

```bash
$K dev pods -n default                  # unhealthy first: ready, status or waiting reason, restarts, age, node
$K dev pods -A -l app.kubernetes.io/name=traefik
$K dev img -n default                   # image per container + GIT_SHA env var; --full for registry/digest, --env-var NAME
$K dev img my-deploy -n default
$K dev nodes                            # instance type, zone, pool, capacity type, kubelet version, age
$K dev nodes --pools                    # Karpenter nodeclaims by nodepool, pool counts on stderr
$K dev events --warn -A --limit 30      # newest first
$K dev logs deploy/my-api -n default --since 15m --grep '(?i)error' -C 3
$K dev rollout deploy/my-api -n default --timeout 120s
$K dev secret my-secret SOME_KEY -n default   # decoded value to stdout; wrong key lists the real keys
```

## Every environment at once

```bash
K_ENVS=dev,stg,prd $K all nodes | awk -F'\t' '$3!="True"'    # env column prepended; one unreachable env is reported, not fatal
K_ENVS=dev,stg,prd $K all img my-deploy -n default           # compare images / GIT_SHA across envs
```

`secret`, `logs`, `rollout` are one env at a time. An env that does not answer in 45s
(`K_TIMEOUT` to change) fails fast with a VPN hint instead of kubectl's multi-minute retry.

## Why these columns

Waiting reason (ImagePullBackOff, CrashLoopBackOff, CreateContainerConfigError) is what a triage
needs and `kubectl get pods` hides it behind STATUS. `img` exists because "what is actually running
versus what was promoted" is the most common deploy question. `nodes --pools` is the Karpenter
census: pool, instance type, capacity type, age versus expireAfter.
