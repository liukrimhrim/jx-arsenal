"""Dynamic checks for `jx preflight --full`: test collection, the fix-removal run and mutation of changed lines.
They run the repo's tests with `test_python` inside temporary `git worktree`s (tests that call git need a real
checkout), carry over uncommitted and untracked changes, and remove the worktrees afterwards."""
import ast, copy, functools, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, tempfile, threading, time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from preflight import Check, Skipped, run


def _env(ctx):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    env.update({k: str(v) for k, v in (ctx.config.get("test_env") or {}).items()})
    return env


def worktree(ctx):
    d = Path(tempfile.mkdtemp(prefix="jx-preflight-wt-"))
    run(["git", "-C", str(ctx.root), "worktree", "add", "--detach", "-q", "-f", str(d), "HEAD"], timeout=900)
    dirty = run(["git", "-C", str(ctx.root), "-c", "core.quotePath=false", "diff", "--name-only", "-z", "HEAD"]).stdout.split("\0")
    untracked = run(["git", "-C", str(ctx.root), "-c", "core.quotePath=false", "ls-files", "-z", "--others", "--exclude-standard"]).stdout.split("\0")
    for p in filter(None, dirty + untracked):
        src, dst = ctx.root / p, d / p
        if src.is_file():
            dst.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(src, dst)
        elif dst.exists():
            dst.unlink()
    return d


def drop(ctx, d):
    run(["git", "-C", str(ctx.root), "worktree", "remove", "--force", str(d)], check=False, timeout=300)
    shutil.rmtree(d, ignore_errors=True)
    run(["git", "-C", str(ctx.root), "worktree", "prune"], check=False)


# Mutants and reverted code run on the user's machine. One flipped `not in` in a leak scan SIGKILLed every process its
# owner had (ocria-inference PR 121). This macOS profile denies every signal whose target is outside the sandbox, which
# only the test run's own descendants share. Sandboxed processes cannot exec setuid binaries, so /bin/ps, which only
# reads, runs outside it.
SANDBOX = ('(version 1)(allow default)(deny signal)(allow signal (target same-sandbox))'
           '(allow process-exec (literal "/bin/ps") (with no-sandbox))')


@functools.cache
def sandbox_problem():
    """Why test runs cannot be confined to their own process tree here, or "" when sandbox-exec works."""
    try:
        p = subprocess.run(["sandbox-exec", "-p", SANDBOX, "/usr/bin/true"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"sandbox-exec did not run ({e}); the --full test runs need macOS"
    return f"sandbox-exec exited {p.returncode}: {p.stderr.strip()[:200]} (inside another sandbox? run jx preflight outside it)" if p.returncode else ""


def pytest(ctx, tree, files, timeout=None, extra=(), env=None, times=None):
    """Run pytest on files inside tree. Returns (status, {case: pass|fail|skip}, seconds); status is ok, failed or timeout."""
    if sandbox_problem(): raise Skipped(f"tests must not run where they can signal processes outside their own tree: {sandbox_problem()}")
    left = ctx.deadline - time.time() - 4  # the run's budget, less the time to remove worktrees and write the report
    if left <= 0: return "timeout", {}, 0.0, ""
    timeout = min(timeout or left, left)
    fd, junit = tempfile.mkstemp(suffix=".xml", prefix="jx-preflight-"); os.close(fd)
    argv = ["sandbox-exec", "-p", SANDBOX, ctx.test_python(), "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", *extra, *files]
    t0 = time.time()
    p = subprocess.Popen(argv, cwd=tree, env=env or _env(ctx), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        status = "ok" if p.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        try: os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError): pass
        out, _ = p.communicate()
        status = "timeout"
    cases = {}
    try:
        for case in ET.parse(junit).iter("testcase"):
            key = f"{case.get('classname', '')}::{case.get('name', '')}"
            cases[key] = "fail" if case.find("failure") is not None or case.find("error") is not None else "skip" if case.find("skipped") is not None else "pass"
            if times is not None: times[key] = float(case.get("time") or 0)
    except (ET.ParseError, OSError):
        pass
    finally:
        try: os.unlink(junit)
        except OSError: pass
    return status, cases, time.time() - t0, out


def _case_file(key, files):
    cls = key.split("::")[0]
    for f in files:
        dotted = f[:-3].replace("/", ".")
        if cls == dotted or cls.startswith(dotted + ".") or cls.split(".")[0] == f.rsplit("/", 1)[-1][:-3] or dotted.endswith(cls.split(".")[0]):
            return f
    return None


def tests_collect(ctx):
    for raw in ctx.config.get("test_commands") or []:
        cmd = raw.replace("{python}", shlex.quote(ctx.test_python()))
        toks, env = shlex.split(cmd), _env(ctx)
        while toks and re.match(r"^[A-Za-z_]\w*=", toks[0]):
            k, v = toks.pop(0).split("=", 1); env[k] = v.replace("$PWD", str(ctx.root))
        if "--collect-only" not in toks and "--co" not in toks: toks += ["--collect-only", "-q"]
        if "no:cacheprovider" not in toks: toks += ["-p", "no:cacheprovider"]
        try:
            p = subprocess.run(toks, cwd=ctx.root, env=env, text=True, capture_output=True, timeout=max(1, ctx.deadline - time.time() - 2))
        except FileNotFoundError:
            yield "", "", f"`{raw}`: {toks[0]} not found; set test_python or fix the command", "block"; continue
        except subprocess.TimeoutExpired:
            yield "", "", f"`{raw}` did not finish collecting within the run's {ctx.args.budget} s budget", "warn"; continue
        if p.returncode not in (0, 5):
            text = p.stdout + p.stderr
            errs = [l for l in text.splitlines() if re.match(r"^(ERROR|E\s|ImportError|ModuleNotFoundError)", l)]
            failing = set(re.findall(r"^ERROR (\S+)", text, re.M))
            missing = set(re.findall(r"No module named '([\w.]+)'", text))
            third_party = {m for m in missing if not _in_repo(ctx.root, m)}
            if failing and third_party and len(failing) <= len(re.findall(r"No module named '(?:" + "|".join(map(re.escape, third_party)) + r")'", text)):
                yield "", "", (f"`{raw}`: {len(failing)} test file(s) cannot be collected because the test python lacks "
                               f"{', '.join(sorted(third_party))} (not a module of this repo); install it into test_python"), "warn"
                continue
            yield "", "", f"`{raw}` fails to collect (exit {p.returncode}): {' | '.join(errs[:3]) or text.strip()[-300:]}", "block"


def _in_repo(root, module):
    """Whether a missing module is this repository's own (a broken import, not a missing dependency)."""
    top = module.split(".")[0]
    return (root / top).is_dir() or (root / f"{top}.py").is_file() or any(root.glob(f"*/{top}/__init__.py"))


def _changed_test_functions(ctx, f):
    tree, out = ctx.tree(f.path), {}
    for n in ast.walk(tree) if tree else []:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith("test") and \
                any(k in f.added for k in range(n.lineno, (n.end_lineno or n.lineno) + 1)):
            out[n.name] = n.lineno
    return out


def _is_test_file(p):
    b = p.rsplit("/", 1)[-1]
    return b.endswith(".py") and (b.startswith("test_") or b.endswith("_test.py"))


def tests_fix_removal(ctx):
    tests = [f for f in ctx.changed(".py", tests=True) if _is_test_file(f.path)]
    code = [f for f in ctx.changed(statuses="AMDR", live=False) if not ctx.is_test(f.path) and not f.excluded]
    fns = {f.path: _changed_test_functions(ctx, f) for f in tests}
    if not code or not any(fns.values()): return
    d = worktree(ctx)
    try:
        for f in code:
            if f.status in ("A", "R") and (d / f.path).exists(): (d / f.path).unlink()
            if f.status != "A":
                p = subprocess.run(["git", "-C", str(ctx.root), "show", f"{ctx.merge_base}:{f.old_path}"], capture_output=True)
                if p.returncode == 0:
                    (d / f.old_path).parent.mkdir(parents=True, exist_ok=True); (d / f.old_path).write_bytes(p.stdout)
        names = sorted({name for path in fns for name in fns[path]})  # -k keeps the run to the changed tests
        status, cases, _, _ = pytest(ctx, d, [f.path for f in tests if fns[f.path]], extra=("-k", " or ".join(names)))
        if status == "timeout":
            yield "", "", f"the changed tests did not finish on the base code within the run's {ctx.args.budget} s budget; fix-removal is incomplete", "info"
        states = {}  # (path, function) -> results of all its cases; a parametrized test is caught if any case fails
        for key, st in cases.items():
            path = _case_file(key, list(fns))
            name = key.split("::")[-1].split("[")[0]
            if path and name in fns[path]:
                states.setdefault((path, name), set()).add(st)
        for (path, name), seen in states.items():
            if "pass" in seen and "fail" not in seen:
                yield path, fns[path][name], f"`{name}` still passes with this diff's non-test changes reverted; it does not detect the change"
    finally:
        drop(ctx, d)


SWAP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq,
        ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}


def mutants(src, tree, lines, vocab):
    """(start, end, replacement, description) byte splices of single-node mutations on the given lines."""
    starts = [0] + [i + 1 for i, b in enumerate(src) if b == 10]
    par = {c: n for n in ast.walk(tree) for c in ast.iter_child_nodes(n)}

    def span(n):
        return starts[n.lineno - 1] + n.col_offset, starts[n.end_lineno - 1] + n.end_col_offset

    def seg(n):
        a, b = span(n); return src[a:b].decode("utf-8", "replace")

    def in_fstring(n):
        cur = n
        while cur in par:
            cur = par[cur]
            if isinstance(cur, (ast.JoinedStr, ast.FormattedValue)): return True
        return False

    seen = set()
    for n in ast.walk(tree):
        if getattr(n, "lineno", None) not in lines or getattr(n, "end_lineno", None) is None or in_fstring(n): continue
        rep = None
        if isinstance(n, ast.Compare) and len(n.ops) == 1 and type(n.ops[0]) in SWAP:
            m = copy.copy(n); m.ops = [SWAP[type(n.ops[0])]()]; rep = f"({ast.unparse(m)})"
        elif isinstance(n, ast.BoolOp):
            m = copy.copy(n); m.op = ast.Or() if isinstance(n.op, ast.And) else ast.And(); rep = f"({ast.unparse(m)})"
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not):
            rep = f"({seg(n.operand)})"
        elif isinstance(n, ast.Constant) and type(n.value) is int and isinstance(par.get(n), ast.Compare):
            rep = str(n.value + 1)  # a boundary; elsewhere (a timeout, a size, an index) the mutant is mostly noise
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value in vocab and not isinstance(par.get(n), ast.Expr):
            rep = repr(vocab[(vocab.index(n.value) + 1) % len(vocab)])
        elif isinstance(n, ast.Return) and n.value is not None and not (isinstance(n.value, ast.Constant) and n.value.value is None):
            a, _ = span(n.value); b = span(n.value)[1]
            yield a, b, "None", f"`return {seg(n.value)[:50]}` → `return None`"; continue
        elif isinstance(n, ast.Raise):
            rep = "pass"
        elif isinstance(n, (ast.If, ast.While)) and not isinstance(n.test, (ast.Compare, ast.BoolOp, ast.UnaryOp)):
            a, b = span(n.test); yield a, b, f"not ({seg(n.test)})", f"`{seg(n.test)[:50]}` → `not (...)`"; continue
        elif isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Tuple) and len(n.type.elts) >= 2:
            a, b = span(n.type); rest = ", ".join(seg(e) for e in n.type.elts[1:])
            yield a, b, f"({rest},)", f"drop `{seg(n.type.elts[0])}` from the except"; continue
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in ("min", "max"):
            a, b = span(n.func); yield a, b, "max" if n.func.id == "min" else "min", f"`{n.func.id}` → `{'max' if n.func.id == 'min' else 'min'}`"; continue
        elif isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Add, ast.Sub)) and not any(isinstance(x, (ast.Constant, ast.JoinedStr)) and isinstance(getattr(x, "value", None), str) for x in (n.left, n.right)):
            m = copy.copy(n); m.op = ast.Sub() if isinstance(n.op, ast.Add) else ast.Add(); rep = f"({ast.unparse(m)})"
        if rep is None: continue
        a, b = span(n)
        if (a, b, rep) in seen: continue
        seen.add((a, b, rep))
        yield a, b, rep, f"`{seg(n)[:50]}` → `{rep[:50]}`"


def _tests_for(ctx, path):
    dotted, stem = path[:-3].replace("/", "."), path.rsplit("/", 1)[-1][:-3]
    rx = re.compile(rf"\b{re.escape(dotted)}\b|\bimport\s+{re.escape(stem)}\b|\bfrom\s+[\w.]*\b{re.escape(stem)}\s+import\b|[\w.]+\.{re.escape(stem)}\b")
    hits = [p for p in ctx.live if _is_test_file(p) and rx.search(ctx.text(p) or "")]
    changed = [f.path for f in ctx.changed(".py", tests=True) if _is_test_file(f.path)]
    return list(dict.fromkeys(hits + [c for c in changed if re.search(rf"\b{re.escape(stem)}\b", ctx.text(c) or "")]))[:25]


# Loaded through PYTHONPATH into the baseline's pytest process and every Python process its tests start: records which
# test (PYTEST_CURRENT_TEST, inherited by child processes) executes which line of the file being mutated, and which
# tests start a process with an environment of their own, which the trace cannot follow.
TRACER = r"""
import os, sys, threading
_here = os.path.dirname(os.path.abspath(__file__))
for _d in sys.path:  # run the sitecustomize this one shadows (Homebrew's Python ships one)
    _f = os.path.join(_d or ".", "sitecustomize.py")
    if os.path.abspath(_d or ".") != _here and os.path.isfile(_f):
        with open(_f) as _fh:
            exec(compile(_fh.read(), _f, "exec"), {"__name__": "sitecustomize", "__file__": _f})
        break
_target, _out = os.environ.get("JX_TRACE_FILE"), os.environ.get("JX_TRACE_OUT")
if _target and _out:
    _seen, _files = set(), {}

    def _write(kind, value):
        test = os.environ.get("PYTEST_CURRENT_TEST", "").rsplit(" (", 1)[0]
        if (kind, test, value) not in _seen:
            _seen.add((kind, test, value))
            with open(os.path.join(_out, f"{os.getpid()}.tsv"), "a") as fh:  # written as it goes: a killed child keeps its lines
                fh.write(f"{kind}\t{test}\t{value}\n")

    def _local(frame, event, arg):
        if event == "line":
            _write("line", frame.f_lineno)
        return _local

    def _call(frame, event, arg):
        name = frame.f_code.co_filename
        if name not in _files:
            _files[name] = os.path.realpath(name) == _target
        return _local if _files[name] else None

    sys.settrace(_call)
    threading.settrace(_call)
    import subprocess
    _init = subprocess.Popen.__init__

    def _popen(self, *a, **k):
        if k.get("env") is not None and "JX_TRACE_OUT" not in k["env"]:
            _write("opaque", "")
        _init(self, *a, **k)
    subprocess.Popen.__init__ = _popen
"""


def traced(ctx, tree, files, path, timeout, times=None):
    """One traced run: (status, cases, seconds, output, {line: {test node ids}}, {node ids the trace cannot follow})."""
    out, site = Path(tempfile.mkdtemp(prefix="jx-trace-")), Path(tempfile.mkdtemp(prefix="jx-tracer-"))
    try:
        (site / "sitecustomize.py").write_text(TRACER)
        env = _env(ctx)
        env.update(JX_TRACE_FILE=os.path.realpath(tree / path), JX_TRACE_OUT=str(out),
                   PYTHONPATH=os.pathsep.join(filter(None, [str(site), env.get("PYTHONPATH")])))
        status, cases, secs, text = pytest(ctx, tree, files, timeout=timeout, env=env, times=times)
        lines, opaque = {}, set()
        for f in out.glob("*.tsv"):
            for row in f.read_text(errors="replace").splitlines():
                kind, test, value = (row.split("\t") + ["", ""])[:3]
                if not test or not (tree / test.split("::")[0]).is_file(): continue  # import time, or not a runnable node id
                if kind == "opaque": opaque.add(test)
                elif kind == "line" and value.isdigit(): lines.setdefault(int(value), set()).add(test)
        return status, cases, secs, text, lines, opaque
    finally:
        shutil.rmtree(out, ignore_errors=True); shutil.rmtree(site, ignore_errors=True)


def baseline(ctx, trees, tests, path, src, deadline):
    """The traced baseline of `tests` on the file with one comment appended (no line moves), split across every
    worktree so no worker idles: a dict of the passing cases, whether any failed, each case's seconds, which tests run
    which line and which the trace cannot follow; plus the status and output. A test that pins the file's hash fails
    here, so it is not among the tests whose failure counts as a kill."""
    listing = pytest(ctx, trees[0], tests, extra=("--collect-only",))[3]
    ids = [line for line in listing.splitlines() if re.match(r"\S+?\.py::\S", line)]
    chunks = [c for c in (ids[i::len(trees)] for i in range(len(trees))) if c] or [tests]  # no listing: one run of the files
    used = trees[:len(chunks)]
    times = {}
    for t in used: (t / path).write_bytes(src + b"\n# jx-preflight control mutant\n")
    try:
        with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
            parts = list(pool.map(lambda tc: traced(ctx, tc[0], tc[1], path, deadline - time.time(), times), zip(used, chunks)))
    finally:
        for t in used: (t / path).write_bytes(src)
    cases, covering, opaque = {}, {}, set()
    for _, part, _, _, lines, blind in parts:
        cases.update(part); opaque |= blind
        for ln, who in lines.items(): covering.setdefault(ln, set()).update(who)
    status = next((st for st in ("timeout", "failed") if any(part[0] == st for part in parts)), "ok")
    return dict(passing=sorted(k for k, v in cases.items() if v == "pass"), clean=all(v != "fail" for v in cases.values()),
                times=times, covering={str(k): sorted(v) for k, v in covering.items()}, opaque=sorted(opaque)), status, parts[-1][3]


def _junit_key(nodeid):
    """The `classname::name` this module's pytest() gives a node id, as pytest's junit report names it."""
    path, bracket, params = nodeid.partition("[")
    names = path.split("::")
    names[0] = re.sub(r"\.py$", "", names[0].replace("/", "."))
    return f"{'.'.join(names[:-1])}::{names[-1]}{bracket}{params}"


def earlier_verdicts(ctx):
    """(head, {mutant key: result}) from the latest earlier jx full run whose head is HEAD or an ancestor of it."""
    if getattr(ctx.args, "mutate_all", False): return None, {}, {}
    common = Path(run(["git", "-C", str(ctx.root), "rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip())
    best = None
    for p in (common / "preflight").glob("*.full.json"):
        try: rec = json.loads(p.read_text())
        except (OSError, ValueError, RecursionError): continue
        m = rec.get("mutation") if isinstance(rec, dict) else None
        if not (isinstance(m, dict) and isinstance(m.get("results"), list) and rec.get("runner") == "jx" and isinstance(rec.get("head"), str)): continue
        if run(["git", "-C", str(ctx.root), "merge-base", "--is-ancestor", rec["head"], "HEAD"], check=False).returncode: continue
        if best is None or str(rec.get("created", "")) > str(best.get("created", "")): best = rec
    if best is None: return None, {}, {}
    base = best["mutation"].get("baselines")
    return (best["head"], {tuple(r["key"]): r for r in best["mutation"]["results"] if isinstance(r, dict) and isinstance(r.get("key"), list)},
            base if isinstance(base, dict) else {})


def tests_mutate(ctx):
    vocab = list(ctx.config.get("status_vocab") or [])
    deadline = ctx.deadline - 6  # no mutant starts after this; the last ones are stopped 2 s later
    since, earlier, baselines = earlier_verdicts(ctx)
    kept = {}  # baselines to keep for the next run
    targets = [f for f in ctx.changed(".py", tests=False) if f.added and ctx.tree(f.path) is not None]
    total, results = dict(killed=0, survived=0, unknown=0, skipped=0, reused=0), []
    if not targets:
        ctx.mutation = dict(total, results=results, baselines={}); return
    workers = max(1, int(ctx.config.get("workers") or min(8, max(2, (os.cpu_count() or 4) - 2))))
    trees, free, lock = [], [], threading.Lock()  # worktrees are made on first need: a rerun that reuses every verdict makes none
    try:
        for f in targets:
            tests = _tests_for(ctx, f.path)
            src = (ctx.root / f.path).read_bytes()
            muts = list(mutants(src, ctx.tree(f.path), f.added, vocab))
            if not muts: continue
            if not tests:
                yield f.path, min(f.added), f"no test imports {f.path}; its {len(muts)} mutants of changed lines cannot be killed"; continue
            texts = src.decode("utf-8", "replace").splitlines()
            tests_sha = hashlib.sha256(b"".join((ctx.root / t).read_bytes() for t in tests if (ctx.root / t).is_file())).hexdigest()
            order, by_line = [], {}
            for m in muts: by_line.setdefault(src[:m[0]].count(b"\n") + 1, []).append(m)
            while any(by_line.values()):
                for ln in sorted(by_line):
                    if by_line[ln]: order.append((ln, by_line[ln].pop(0)))

            def key(ln, desc):  # what the verdict depends on: the line's text (nth of its copies) and the mutation
                text = texts[ln - 1] if ln <= len(texts) else ""
                return [f.path, text, texts[:ln - 1].count(text), desc]

            def held(ln, desc):  # an earlier verdict made with these same test files
                old = earlier.get(tuple(key(ln, desc)))
                return old if old and old.get("tests_sha") == tests_sha and old.get("verdict") in ("killed", "survived", "unknown") else None

            passing, covering, opaque, times, clean = set(), {}, set(), {}, True
            if not all(held(ln, m[3]) for ln, m in order):  # something must run: a baseline and worktrees
                if time.time() > deadline: total["skipped"] += len(muts); continue
                if not trees:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        trees.extend(pool.map(lambda _: worktree(ctx), range(workers)))
                    free.extend(trees)
                stamp = hashlib.sha256(src + tests_sha.encode()).hexdigest()  # this file's bytes and its test files'
                base = baselines.get(stamp)
                if not base:
                    base, status, out = baseline(ctx, trees, tests, f.path, src, deadline)
                    if status == "timeout":
                        total["skipped"] += len(muts)
                        yield f.path, "", (f"the tests for {f.path} did not finish once within the run's {ctx.args.budget} s budget, so none "
                                           f"of its {len(muts)} mutants ran; raise --budget for this change"), "info"; continue
                    if not base["passing"]:
                        yield f.path, "", f"baseline tests for {f.path} pass nothing ({status}); mutation skipped: {out.strip()[-200:]}"; continue
                kept[stamp] = base
                passing, clean, opaque = set(base["passing"]), base["clean"], set(base["opaque"])
                covering = {int(k): set(v) for k, v in base["covering"].items()}
                times = base["times"]

            def one(item):
                ln, (a, b, rep, desc) = item
                old = held(ln, desc) or earlier.get(tuple(key(ln, desc)))
                if old and (held(ln, desc) or (old.get("verdict") == "killed" and old.get("killer") in passing)):
                    return ln, desc, old["verdict"], old.get("killer"), old.get("ran"), True
                if time.time() > deadline: return ln, desc, "skipped", None, None, False
                new = src[:a] + rep.encode() + src[b:]
                try: ast.parse(new)
                except SyntaxError: return ln, desc, "invalid", None, None, False
                # ponytail: a test that reaches this line only in a process started with `python -I`, or one whose
                # environment drops PYTHONPATH without passing env= to Popen, is invisible to the trace, so its kill is
                # missed. Tests seen starting a process with their own env always run; lines no test was seen running
                # get every test, as before.
                # the tests that run this line, fastest first so -x reaches a kill soonest; none that fails anyway
                picked = sorted((t for t in covering.get(ln, set()) | opaque if _junit_key(t) in passing),
                                key=lambda t: (times.get(_junit_key(t), 0), t))
                run_on = picked if picked and covering.get(ln) else tests
                per = 3 * sum(times.get(_junit_key(t), 0) for t in picked) + 10 if run_on is picked else 3 * sum(times.values()) + 10
                with lock: t = free.pop()
                try:
                    (t / f.path).write_bytes(new)
                    st, res, _, _ = pytest(ctx, t, run_on, timeout=per, extra=("-x",) if clean or run_on is picked else ())
                finally:
                    (t / f.path).write_bytes(src)
                    with lock: free.append(t)
                killer = next((k for k in sorted(res) if res[k] == "fail" and k in passing), None)
                ran = len(picked) if run_on is picked else None
                if killer: return ln, desc, "killed", killer, ran, False
                if st == "timeout": return ln, desc, ("skipped" if time.time() > deadline else "unknown"), None, ran, False
                if st == "failed" and not res: return ln, desc, "killed", None, ran, False
                return ln, desc, "survived", None, ran, False

            with ThreadPoolExecutor(max_workers=workers) as pool:
                for ln, desc, verdict, killer, ran, reused in pool.map(one, order):
                    if verdict == "invalid": continue
                    total[verdict] += 1; total["reused"] += reused
                    if verdict in ("killed", "survived", "unknown"):
                        results.append(dict(key=key(ln, desc), verdict=verdict, killer=killer, ran=ran, tests_sha=tests_sha))
                    if verdict == "survived":
                        who = f"the {ran} test(s) that run line {ln}" if ran else f"{len(tests)} test files"
                        yield f.path, ln, f"mutant survived {who}: {desc}; no test notices this change in behaviour"
        ctx.mutation = dict(total, results=results, baselines=kept)
        reuse = f"; {total['reused']} verdicts reused from the full run of {since[:12]} (--mutate-all reruns them)" if total["reused"] else ""
        yield "", "", (f"mutation: {total['killed']} killed, {total['survived']} survived, {total['unknown']} unknown (timeout or "
                       f"hash-pinned), {total['skipped']} not run within the run's {ctx.args.budget} s budget{'; run again to go on from here' if total['skipped'] else ''}{reuse}"), "info"
    finally:
        with ThreadPoolExecutor(max_workers=max(1, len(trees))) as pool:
            list(pool.map(lambda t: drop(ctx, t), trees))


CHECKS = [
    Check("tests.collect", "full", "block", "PR 52 r2, 76 r2", "documented test commands that fail to collect", tests_collect),
    Check("tests.fix-removal", "full", "warn", "PR 76, 85", "changed tests that still pass with the non-test changes reverted", tests_fix_removal),
    Check("tests.mutate", "full", "warn", "PR 84 r2, 67, 85, 86, 80 r4", "mutants of changed lines that no test kills", tests_mutate),
]
