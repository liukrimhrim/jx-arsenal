# `jx venv`: pinned interpreters by path, shared across sessions

```bash
PY=$(jx venv path -p 3.12 -r tests/requirements-cpu.txt pytest==9.2.0)   # stdout is only the python path
MODAL_TEST_PYTHON=$(jx venv path -p 3.12 modal==1.5.2) \
AIPERF_TEST_PYTHON=$(jx venv path -p 3.12 aiperf==0.10.0) "$PY" -m pytest tests -q
jx venv ls                       # key, python, built, last used, build seconds, size, inputs
jx venv rm --unused-since 14d --dry-run
```

The key hashes the `-p` request, the package specs, and the bytes of every `-r` file plus
its `-r`/`-c` includes. Edit a requirements file and the next call builds a new venv; the old
one stays until `jx venv rm`. A build that fails or is interrupted leaves nothing cached, and
two sessions asking for the same key build it once (the second waits on a lock).
Local paths (`-e .`, `./pkg`) are hashed by name only: `jx venv rm <key>` after editing one.

## When to use it, and when `uv run` is enough

Measured 2026-11-07 with a warm uv cache: a 96-pin CPU test venv builds in 1.0 s, a 1 GB
torch venv in 1.2 s, a cached lookup takes 0.05 s. The saving is model calls, not minutes:
past sessions spent several calls per venv finding an interpreter, assembling `uv venv` +
`uv pip install` lines in a per-session scratchpad, and adding one missing pin at a time.

- One command needing one environment: `uv run --no-project -p 3.12 --with-requirements F --with pytest python -m pytest ...`
  is a single line and needs no jx. Its environment is temporary.
- Tests that take other interpreters by path (`MODAL_TEST_PYTHON`, `AIPERF_TEST_PYTHON`), or a
  venv several sessions reuse: `jx venv path`, because the path persists.

Cache: `$JX_VENV_CACHE`, default `~/.cache/jx/venvs`. Sizes in `ls` are apparent sizes;
uv clones files from its own cache, so real disk use is lower. Needs `uv` on PATH (`jx venv doctor`).
