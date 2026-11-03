# jx tf

Terraform by root. One stdlib Python CLI over the `terraform` binary. TSV by default, `--json` in any
position for the raw payload. It never runs `apply` or any state-mutating command.

A root is a directory with `*.tf` files and a `backend` block. The lever reads the backend profile from
the root itself at run time, so nothing environment-specific lives in the repo.

## The loop, one command per step

```bash
jx tf roots infra --changed                     # roots this branch touched, modules included; falls back to local main on a shallow clone
jx tf roots infra --changed | jx tf check -     # fmt -check + validate for exactly those roots; exit 1 on any failure
jx tf check infra/environments/dev --fix        # rewrite formatting, then check
jx tf plan infra/environments/dev               # init (retrying -reconfigure), lock-free plan, one row per change; exit 2 = changes
jx tf plan infra/environments/dev --target module.eks --out /tmp/dev.tfplan
jx tf diff /tmp/dev.tfplan --root infra/environments/dev                 # every changed attribute, before and after
jx tf diff /tmp/dev.tfplan module.eks.aws_eks_cluster.this --root ...     # one resource
jx tf state infra/environments/dev --grep nodegroup
jx tf doctor infra/environments/dev             # terraform version, required_version, backend profile, init state, branch, dirty files
```

## What the plan output tells you that terraform does not

Two stderr warnings precede every plan. `has N uncommitted changes` means the plan reflects the working
tree, not a commit. `on branch X` means it is not what CI plans from main. A plan taken on a dirty branch
once showed 203 destroys that were the branch, not the infrastructure; these lines exist so an agent
never reads that number without the context.

Exit codes mirror terraform's `-detailed-exitcode`: 0 no changes, 2 changes, 1 error. `errored: true`
in the plan file is treated as an error even when terraform wrote the file.

Plans are lock-free by default so they never contend with Atlantis or a colleague. `--lock` takes the
state lock with a two minute timeout for the rare case the plan must see in-flight state.

## check runs without credentials

`terraform init -backend=false` on an already-initialized root still loads the backend cached in
`.terraform/` and resolves its credentials. `check` runs init and validate under a private `TF_DATA_DIR`
with the root's installed providers symlinked in, so it needs no AWS session and downloads nothing twice.
Provider deprecation warnings are counted on the validate row and hidden; `--warnings` lists them.

## Error hints

Missing variables point at `--var` and the CI workflow that plans the root. A validation-rule rejection
says to read the message above. Credential errors point at `jx tf doctor <root>` for the backend profile
and at `terraform providers` for the token a provider needs. A held state lock says to wait or drop `--lock`.
