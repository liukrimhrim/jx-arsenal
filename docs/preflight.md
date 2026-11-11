# jx preflight

Before a PR opens, the author runs the checks that reviewers kept running after it opened. The catalogue
comes from about 30 reviewed PRs in one repository, where most fixes needed four to seven review rounds.
Reviewers rarely needed insight the author lacked. They did things the author skipped:

- grepped for what references the change;
- opened the real artifact;
- read the governing spec;
- re-checked every claim in the PR body;
- broke each guard to see whether a test noticed.

Each check names the miss it came from (`jx preflight ls`, column `origin`). A `⇢` marks an extrapolation of
the same failure to similar code.

```bash
jx preflight run                                     # fast tier: static checks against merge-base(origin/main, HEAD)
jx preflight run --full --body draft.md --receipts /tmp/jx-check-abc   # + collection, fix-removal, mutation; claims vs receipts
jx preflight run --topic speed "step time"            # also turn matching governing-doc requirements into asks
jx preflight run --ledger /tmp/jx-preflight-x/ledger.md   # carry dispositions from the last run forward
jx preflight run --config today.json                 # e.g. today's config on an old commit (backtests)
jx preflight run --dry-run                           # which checks would run, and why
jx preflight ls | jx preflight doctor
```

## Reading the output

**Severities:**

- **block**: a definite defect. A call that no longer matches a changed signature, a Modal image that Modal
  refuses, a credential, a test file CI stopped running, a citation past the end of a file.
- **warn**: a likely defect, so look at it.
- **ask**: a question only the author can answer. A checklist for a lifecycle, credential, parser, command-gate, path-or-reference or SDK change.
  A governing-doc requirement. A workflow change the body never mentions.
- **info**: a summary, such as the mutation score.

**Exit codes:** 1 while a block is undisposed, or with `--strict` while an ask is undisposed. 2 on usage
errors. Otherwise 0.

**Reports.** `OUT` is `--out`, or a new directory under `<git-dir>/preflight-out/` (per worktree, never tracked); only the newest 10 stay, and the report holding the `--ledger` a run is given is never the one removed.

**Dispositions.** Every run writes `OUT/ledger.md` with one `- <id> TODO` line per open block or ask. Replace
`TODO` with a reason (`done: path:line`, `n/a: why`, `decision: who/where`) and rerun with `--ledger`. Decisions
that hold across PRs go in the repo's dispositions file (default `.jx/preflight/DISPOSITIONS.md`), one line per
rule: `- <check-glob> <path-glob>: <reason>`. A disposed finding never fails the run.

## Scope and cost

- **The diff** is merge-base(`--base`, HEAD) against the working tree, plus untracked files.
- **The live set** is where searches, ASTs and citation scans run: tracked and untracked files minus `excludes`,
  minus every file listed in `frozen_lists` (sha256 manifests of files that never change). The fast tier
  takes about 8 s on a 37-file, 7,000-line PR in a 10,000-file repository whose manifests freeze all but
  425 files. Without a config it scans every tracked file, which can take minutes.
- **`--full`** runs the repo's tests with `test_python`, a command that prints a python path, for example
  `jx venv path -p 3.12 -r tests/requirements.txt`. Each run happens in temporary `git worktree`s that carry
  uncommitted and untracked changes and are removed afterwards.
  - **Sandbox.** Mutants and reverted code run on your machine, so each test run starts under `sandbox-exec`
    with a profile that denies every signal to a process outside the run's own tree. A mutant of a leak
    scan's filter once SIGKILLed every process its owner had. The run can still kill its own children, and
    `/bin/ps` (setuid, read-only) runs outside the sandbox so process scans still work. Where `sandbox-exec`
    cannot start (Linux, or already inside a sandbox), fix-removal and mutation are skipped with a warning;
    `jx preflight doctor` shows which.
  - **Mutation** splices one node's exact byte range per mutant, so file-hash tests stay meaningful. It first
    runs a no-op control mutant: tests that fail on the control (hash-pinned files) don't count as kills.
    Timeouts count as unknown. `--budget` bounds the run.
  - **Per-line test selection.** The baseline run is traced: a `sitecustomize` hook, loaded through `PYTHONPATH`
    into pytest and every Python process its tests start, records which test (`PYTEST_CURRENT_TEST`) executes which
    line of the file being mutated. Each mutant then runs only the tests that execute its line, plus any test seen
    starting a process with an environment of its own. A line no test was seen running gets every test. A test
    that reaches a line only through `python -I`, or through a child whose environment drops `PYTHONPATH`, is
    invisible to the trace, so a kill it would make is missed. Workers default to the CPUs less two, 2 to 8 (each
    is a worktree, made in parallel). The baseline is split across all of them, and a mutant's tests run fastest
    first, so `-x` reaches a kill soonest. An integer constant is mutated only where it is a comparison's boundary.
  - **Budget.** The whole run ends within `--budget` seconds (default 60). The full-tier checks and the pack scripts
    start together. The `test_python` command gets what is left of it: a cold one that builds an environment is
    stopped there, and the checks that need it say to run it once first. One still running when the budget ends is stopped and says what it left undone; a pack script
    reads `deadline` (epoch seconds) from `change.json` to stop in time. Mutation keeps its verdicts and its
    baseline, so the next run goes on from where this one stopped.
  - **Reused verdicts.** Each full run records every mutant's verdict in the receipt (killed, survived, or unknown
    for a timeout), keyed by its line's text and the mutation. A later full run whose HEAD descends from that run's
    head reuses any verdict made with the same test files. When the test files changed, it reuses a kill while the
    killing test still passes. Everything else runs again, including mutants a budget cut left unrun. When every
    mutant of a file holds, nothing runs for it: no baseline, no control mutant, no worktree. `--mutate-all` reruns
    every mutant.
  - **Fix-removal** reverts the non-test changes and reports changed tests that still pass. A parametrized test counts as one test: it is reported only when none of its cases fails.

## Repo config: `.jx/preflight.json`

| key | meaning |
|---|---|
| `frozen_lists` | sha256 manifests (`<hex>  <path>` lines); the listed files leave the live set |
| `excludes` | globs that leave the live set (`.claude/worktrees/**` and `.venv*/**` are always excluded) |
| `governing_docs` | spec documents `spec.ledger` turns into requirement asks |
| `test_commands` | documented test commands `tests.collect` must be able to collect; `{python}` is replaced by the test python |
| `test_python`, `test_env` | the command that prints the test interpreter, and extra environment for test runs |
| `test_python_fallback` | a path in the repo used as the test interpreter where `test_python`'s program is not installed; `$PREFLIGHT_TEST_PYTHON` overrides both |
| `ci_only_dirs` | test directories only CI can run (they are still searched for callers) |
| `status_vocab` | string literals mutation swaps (default `pass`, `fail`, `pending`) |
| `secret_allow` | globs where long digests are expected (locks, manifests) |
| `undisclosed_watch` | more globs whose changes the PR body must mention |
| `dispositions` | the standing dispositions file |
| `workers` | parallel mutation worktrees (default 2) |
| `checks` | repo-pack scripts: `{id, cmd, tier: fast|full, when: [globs], timeout}` |

## Repo-pack contract

Pack scripts run side by side, and beside jx's own checks, so a run takes about as long as its slowest check. A pack script receives `OUT/change.json` as its last argument:

- `repo_root`, `base`, `base_tip`, `merge_base`, `head`, `dirty`;
- `files[]`, each with `path`, `status`, `old_path`, `excluded`, `binary`, `added`, `removed_old` and `hunks`;
- `live`, `body_path`, `receipts`, `topics`, `owned`, `readonly`, `full`, `budget`, `out_dir`, `test_python`
  and `config`.

It prints findings as `check⇥severity⇥path⇥line⇥message⇥origin` and exits 0. A non-zero exit becomes a
`block` naming the script. Repository knowledge lives in the pack: which locks to recheck, which readers to
fuzz, which local citation rule applies. Generic checks live here.

## A copy in the repository

A repository whose contributors may not have jx can hold the engine itself:

```
jx preflight vendor            # writes .jx/preflight/engine/: preflight.py, the two check modules, ENGINE.json
python3 .jx/preflight/engine/preflight.py run --full --body draft.md     # the same run, with python and git alone
```

- **Pinned.** `ENGINE.json` names the jx commit and each file's sha256. `vendor` refuses an engine with
  uncommitted changes (`--allow-dirty` to try a copy first; the commit is then marked `+dirty`), and
  `--dry-run` says which files would change.
- **One version for everyone.** Inside a repository with a copy, `jx preflight` hands every command but
  `vendor` to that copy. `JX_PREFLIGHT_OWN=1` runs jx's own engine there, to try a new check before vendoring it.
- **Never edited.** A copy whose files no longer match `ENGINE.json` refuses to run and names the two ways out:
  restore it, or take a new copy.
- **Its own name in hints.** A wrapper script sets `PREFLIGHT_PROG` to the command people type in that repository,
  and the hints name it in place of `jx preflight`.
- **Without jx's other tools.** Set `test_python_fallback` for machines where the `test_python` command cannot run.
  Add the copy's folder to `excludes`, or the run checks and mutates the engine as if it were the repository's code.

## Limits

- The checks are heuristics with known blind spots. A runtime-only defect that no static shape or
  local test can reach (a provider quota, a host memory limit) still needs a real run.
- Workflow parsing covers the YAML GitHub workflows use (block mappings and sequences, block scalars, flow
  lists) and `matrix`, `if:`, `env` substitution. It does not follow reusable workflows or composite actions.
- The sandbox only stops signals. A test can still ask launchd (`launchctl kill`) or an app (`osascript`) to stop
  something, or delete files outside its worktree.
- Name resolution for `refs.callers` follows `from X import f`, `import X as m` and same-module calls. It does
  not follow calls through instances.
