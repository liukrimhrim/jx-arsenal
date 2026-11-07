#!/usr/bin/env python3
"""jx venv — virtualenvs keyed by their inputs, built once with uv and reused by every session.

  jx venv path [-p 3.12] [-r FILE ...] [PKG ...] [--dry-run]   python of the venv for exactly these inputs; builds it on first use
  jx venv ls                                                     cached venvs: key, python, built, last used, build time, size, inputs
  jx venv rm <key ...> | --unused-since 14d [--dry-run]          delete cached venvs
  jx venv doctor                                                 uv on PATH, cache dir, count and size

The key hashes the -p request, every package spec, and the bytes of every -r file plus the files
it includes (-r/-c lines), so editing a requirements file yields a new venv, never a stale one.
stdout of `path` is only the python path, so it composes:
  PY=$(jx venv path -p 3.12 -r tests/requirements.txt pytest==9.2.0) && "$PY" -m pytest tests
Cache: $JX_VENV_CACHE, default ~/.cache/jx/venvs. A second session asking for the same key while
it builds waits on a lock and then reuses it. A failed or killed build leaves nothing cached.
"""
import argparse, fcntl, hashlib, json, os, re, shutil, subprocess, sys, time, signal
signal.signal(signal.SIGPIPE, signal.SIG_DFL)  # `| head` must not traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

META = ".jx-venv.json"  # written last: its presence means the build finished
INCLUDE = re.compile(r"^\s*(?:-r|-c|--requirement|--constraint)(?:\s*=\s*|\s+)(\S+)")

def cache(): return Path(os.environ.get("JX_VENV_CACHE") or Path.home() / ".cache" / "jx" / "venvs")

def since(s):
    """30m, 6h, 7d, 2w: that long ago, in UTC. Anything else is parsed as an ISO time (a naive one is UTC)."""
    m = re.fullmatch(r"(\d+)([smhdw])", s or "")
    if m: return datetime.now(timezone.utc) - timedelta(**{{"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[m.group(2)]: int(m.group(1))})
    try: dt = datetime.fromisoformat((s or "").replace("Z", "+00:00")); return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError: sys.exit(f"bad time {s!r}; use 30m, 6h, 7d, 2w, or an ISO time like 2026-01-31T00:00:00Z")

def uv():
    p = shutil.which("uv")
    if not p: sys.exit("`uv` not found on PATH; install it (`brew install uv`), then `jx venv doctor`")
    return p

def req_files(reqs):
    """The -r files named plus every file they pull in with -r/-c, resolved relative to the including file as pip does."""
    seen, todo = [], [Path(r).resolve() for r in reqs]
    while todo:
        f = todo.pop(0)
        if f in seen: continue
        if not f.is_file(): sys.exit(f"requirements file not found: {f}")
        seen.append(f)
        todo += [(f.parent / m.group(1)).resolve() for m in map(INCLUDE.match, f.read_text().splitlines()) if m]
    return seen

def key_of(args):
    # ponytail: local paths (`-e .`, `./pkg`) are hashed by name, not content; rebuild with `jx venv rm` after editing one
    h = hashlib.sha256(json.dumps({"python": args.python, "pkgs": sorted(args.pkgs)}).encode())
    for b in sorted(f.read_bytes() for f in req_files(args.requirement)): h.update(hashlib.sha256(b).digest())
    return h.hexdigest()[:16]

def run(argv):
    p = subprocess.run(argv, text=True, capture_output=True)
    if p.returncode:
        tail = "\n".join((p.stderr or p.stdout).strip().splitlines()[-30:])
        raise RuntimeError(f"{' '.join(argv)}\n{tail}")
    return p.stdout

def build_cmds(args, d):
    py = d / "bin" / "python"
    cmds = [[uv(), "venv", "-q", *(["-p", args.python] if args.python else []), str(d)]]
    if args.requirement or args.pkgs:
        cmds.append([uv(), "pip", "install", "-q", "-p", str(py), *[x for f in args.requirement for x in ("-r", str(Path(f).resolve()))], *args.pkgs])
    return py, cmds

def cmd_path(args):
    key = key_of(args); d = cache() / key; py = d / "bin" / "python"
    if (d / META).exists():
        os.utime(d / META); return done(args, key, d, py, True)
    py, cmds = build_cmds(args, d)
    if args.dry_run:
        print(f"DRY RUN: key {key} not cached; would build {d}"); [print("  " + " ".join(c)) for c in cmds]; return
    cache().mkdir(parents=True, exist_ok=True)
    with open(cache() / f"{key}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # another session building this key: wait, then reuse its result
        if (d / META).exists(): os.utime(d / META); return done(args, key, d, py, True)
        shutil.rmtree(d, ignore_errors=True)  # a killed build left a directory without META
        sys.stderr.write(f"building {key} …\n"); t0 = time.time()
        try:
            for c in cmds: run(c)
            version = run([str(py), "-c", "import platform; print(platform.python_version())"]).strip()
        except BaseException as e:  # Ctrl-C included: never leave a half-built venv behind
            shutil.rmtree(d, ignore_errors=True)
            if isinstance(e, RuntimeError): sys.exit(f"{e}\n→ build failed; nothing was cached. Fix the inputs, then rerun `jx venv path`")
            raise
        meta = {"key": key, "python": version, "request": args.python or "", "requirements": [str(f) for f in req_files(args.requirement)],
                "pkgs": args.pkgs, "built": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "build_secs": round(time.time() - t0, 1)}
        (d / META).write_text(json.dumps(meta, indent=1))
    done(args, key, d, py, False, meta["build_secs"])

def done(args, key, d, py, cached, secs=None):
    if args.json: print(json.dumps({"python": str(py), "dir": str(d), "key": key, "cached": cached, "build_secs": secs})); return
    print(py); sys.stderr.write(f"cached {key}\n" if cached else f"built {key} in {secs}s\n")

def entries():
    out = []
    for m in sorted(cache().glob(f"*/{META}")):
        try: meta = json.loads(m.read_text())
        except ValueError: continue
        size = sum(f.lstat().st_size for f in m.parent.rglob("*") if not f.is_symlink() and f.is_file())
        used = datetime.fromtimestamp(m.stat().st_mtime, timezone.utc)
        out.append({**meta, "dir": str(m.parent), "last_used": used.strftime("%Y-%m-%dT%H:%M:%SZ"), "used_dt": used, "size_mb": round(size / 2**20),
                    "inputs": " ".join([*("-r " + r for r in meta.get("requirements", [])), *meta.get("pkgs", [])])})
    return sorted(out, key=lambda e: e["used_dt"], reverse=True)

def cmd_ls(args):
    rows = [{k: v for k, v in e.items() if k != "used_dt"} for e in entries()]
    if args.json: print(json.dumps(rows, indent=1)); return
    tsv(rows, ["key", "python", "built", "last_used", "build_secs", "size_mb", "inputs"])
    sys.stderr.write(f"{len(rows)} venvs in {cache()}\n")

def cmd_rm(args):
    es = entries()
    if args.unused_since:
        cut = since(args.unused_since); pick = [e for e in es if e["used_dt"] < cut]
    elif args.keys:
        pick = []
        for k in args.keys:
            hit = [e for e in es if e["key"].startswith(k)]
            if len(hit) != 1: sys.exit(f"{k!r} matches {len(hit)} venvs; `jx venv ls` lists keys")
            pick += hit
    else: sys.exit("name keys to remove, or --unused-since 14d; `jx venv ls` lists keys")
    for e in pick:
        if args.dry_run: print(f"DRY RUN: would remove {e['dir']} ({e['size_mb']} MB, last used {e['last_used']})"); continue
        with open(cache() / f"{e['key']}.lock", "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX); shutil.rmtree(e["dir"])
        (cache() / f"{e['key']}.lock").unlink(missing_ok=True); print(f"removed {e['dir']}")
    if not pick: sys.stderr.write("nothing to remove\n")

def cmd_doctor(args):
    p = uv(); print(run([p, "--version"]).strip(), f"({p})")
    es = entries(); print(f"cache: {cache()} — {len(es)} venvs, {sum(e['size_mb'] for e in es)} MB"); print("ok")

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))

def add_globals(p, suppress=False):
    p.add_argument("--json", action="store_true", help="raw JSON instead of TSV", **({"default": argparse.SUPPRESS} if suppress else {}))

def main():
    ap = argparse.ArgumentParser(prog="jx venv", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter); add_globals(ap)
    g = argparse.ArgumentParser(add_help=False); add_globals(g, suppress=True)  # same flags accepted after the subcommand
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sp.add_parser("path", parents=[g], help="python of the venv for these inputs; builds it once, then reuses it")
    p.add_argument("-p", "--python", help="interpreter request passed to `uv venv -p` (3.12); part of the key")
    p.add_argument("-r", "--requirement", action="append", default=[], metavar="FILE", help="requirements file, repeatable; its bytes and its -r/-c includes are part of the key")
    p.add_argument("pkgs", nargs="*", metavar="PKG", help="extra package specs such as pytest==9.2.0")
    p.add_argument("--dry-run", action="store_true", help="print the key and the uv commands a build would run; build nothing"); p.set_defaults(f=cmd_path)
    sp.add_parser("ls", parents=[g], help="cached venvs, most recently used first").set_defaults(f=cmd_ls)
    p = sp.add_parser("rm", parents=[g], help="delete cached venvs by key prefix or by last use")
    p.add_argument("keys", nargs="*", help="key or unique key prefix from `jx venv ls`"); p.add_argument("--unused-since", help="remove venvs not used since 14d / 2w / an ISO time")
    p.add_argument("--dry-run", action="store_true", help="list what would be removed; remove nothing"); p.set_defaults(f=cmd_rm)
    sp.add_parser("doctor", parents=[g], help="uv on PATH, cache dir, count and size").set_defaults(f=cmd_doctor)
    args = ap.parse_args(); args.f(args)

if __name__ == "__main__": main()
