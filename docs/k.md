
# k lever

One stdlib Python CLI over `kubectl`. TSV by default, `--json` for the raw kubectl payload.
`<env>` is the kubectl context name; `~/.config/jx-arsenal/k.json` can map names to contexts and
defines the list `k all` iterates (or set `K_ENVS=a,b,c`). Run `k <env> doctor` first in a new shell.

## Read the cluster

```bash
jx k dev pods -n default                  # unhealthy first: ready, status or waiting reason, restarts, age, node
jx k dev pods -A -l app.kubernetes.io/name=traefik
jx k dev images -n default                   # image per container + GIT_SHA env var; --full for registry/digest, --env-var NAME
jx k dev images my-deploy -n default
jx k dev nodes                            # instance type, zone, pool, capacity type, kubelet version, age
jx k dev nodes --pools                    # Karpenter nodeclaims by nodepool, pool counts on stderr
jx k dev events --warn -A --limit 30      # newest first
jx k dev logs deploy/my-api -n default --since 15m --grep '(?i)error' -C 3
jx k dev rollout deploy/my-api -n default --timeout 120s
jx k dev secret my-secret SOME_KEY -n default   # decoded value to stdout; wrong key lists the real keys
```

## Every environment at once

```bash
K_ENVS=dev,stg,prd jx k all nodes | awk -F'\t' '$3!="True"'    # env column prepended; one unreachable env is reported, not fatal
K_ENVS=dev,stg,prd jx k all images my-deploy -n default           # compare images / GIT_SHA across envs
```

`secret`, `logs`, `rollout` are one env at a time. An env that does not answer in 45s
(`K_TIMEOUT` to change) fails fast with a VPN hint instead of kubectl's multi-minute retry.

## Why these columns

Waiting reason (ImagePullBackOff, CrashLoopBackOff, CreateContainerConfigError) is what a triage
needs and `kubectl get pods` hides it behind STATUS. `img` exists because "what is actually running
versus what was promoted" is the most common deploy question. `nodes --pools` is the Karpenter
census: pool, instance type, capacity type, age versus expireAfter.
