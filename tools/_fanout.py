"""`jx <tool> all <command>`: one call per environment, each TSV row prefixed with its env. Shared by k, obs and temporal."""
import contextlib, io, sys


def fan_out(envs, a, raw=False):
    """Run a.f(a) once per env. One env failing is reported and the rest still run, then exit 1; a termination signal
    (exit code 128+) stops the loop at once. raw (or --json) prints each env's output as it is, without the env column."""
    first, failed = True, False
    for env in envs:
        a.env = env; buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try: a.f(a)
            except SystemExit as e:
                if isinstance(e.code, int) and e.code >= 128: raise  # SIGTERM: the caller is being stopped, not this env failing
                if e.code not in (None, 0): sys.stderr.write(f"[{env}] {e}\n"); failed = True; continue
        if a.json or raw: print(buf.getvalue(), end=""); continue
        lines = buf.getvalue().splitlines()
        if first and lines: print("env\t" + lines[0]); first = False
        for ln in lines[1:]: print(f"{env}\t{ln}")
    if failed: sys.exit(1)
