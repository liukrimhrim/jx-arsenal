"""Dynamic checks for `jx preflight --full`: test collection, the fix-removal run and mutation of changed lines.
They run the repo's tests with `test_python` inside temporary `git worktree`s (tests that call git need a real
checkout), carry over uncommitted and untracked changes, and remove the worktrees afterwards."""
import ast, copy, os, re, shlex, shutil, signal, subprocess, sys, tempfile, threading, time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from preflight import Check, run


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


def pytest(ctx, tree, files, timeout=None, extra=()):
    """Run pytest on files inside tree. Returns (status, {case: pass|fail|skip}, seconds); status is ok, failed or timeout."""
    fd, junit = tempfile.mkstemp(suffix=".xml", prefix="jx-preflight-"); os.close(fd)
    argv = [ctx.test_python(), "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={junit}", *extra, *files]
    t0 = time.time()
    p = subprocess.Popen(argv, cwd=tree, env=_env(ctx), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, start_new_session=True)
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
            p = subprocess.run(toks, cwd=ctx.root, env=env, text=True, capture_output=True, timeout=900)
        except FileNotFoundError:
            yield "", "", f"`{raw}`: {toks[0]} not found; set test_python or fix the command", "block"; continue
        except subprocess.TimeoutExpired:
            yield "", "", f"`{raw}` did not finish collecting in 900 s", "warn"; continue
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
        status, cases, _, _ = pytest(ctx, d, [f.path for f in tests if fns[f.path]], timeout=1800)
        for key, st in cases.items():
            if st != "pass": continue
            path = _case_file(key, list(fns))
            name = key.split("::")[-1].split("[")[0]
            if path and name in fns[path]:
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
        elif isinstance(n, ast.Constant) and type(n.value) is int and not isinstance(par.get(n), ast.UnaryOp):
            rep = str(n.value + 1)
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


def tests_mutate(ctx):
    vocab = list(ctx.config.get("status_vocab") or [])
    deadline = time.time() + ctx.args.budget
    targets = [f for f in ctx.changed(".py", tests=False) if f.added and ctx.tree(f.path) is not None]
    if not targets: return
    workers = max(1, int(ctx.config.get("workers") or 2))
    trees = [worktree(ctx) for _ in range(workers)]
    free = list(trees); lock = threading.Lock(); total = dict(killed=0, survived=0, unknown=0, skipped=0)
    try:
        for f in targets:
            tests = _tests_for(ctx, f.path)
            src = (ctx.root / f.path).read_bytes()
            muts = list(mutants(src, ctx.tree(f.path), f.added, vocab))
            if not muts: continue
            if not tests:
                yield f.path, min(f.added), f"no test imports {f.path}; its {len(muts)} mutants of changed lines cannot be killed"; continue
            if time.time() > deadline: total["skipped"] += len(muts); continue
            status, base, secs, out = pytest(ctx, trees[0], tests, timeout=max(60, deadline - time.time()))
            passing = {k for k, v in base.items() if v == "pass"}
            if not passing:
                yield f.path, "", f"baseline tests for {f.path} pass nothing ({status}); mutation skipped: {out.strip()[-200:]}"; continue
            (trees[0] / f.path).write_bytes(src + b"\n# jx-preflight control mutant\n")
            _, ctrl, _, _ = pytest(ctx, trees[0], tests, timeout=3 * secs + 30)
            (trees[0] / f.path).write_bytes(src)
            pinned = {k for k in passing if ctrl.get(k) != "pass"}
            per = 3 * secs + 5
            order, by_line = [], {}
            for m in muts: by_line.setdefault(src[:m[0]].count(b"\n") + 1, []).append(m)
            while any(by_line.values()):
                for ln in sorted(by_line):
                    if by_line[ln]: order.append((ln, by_line[ln].pop(0)))

            def one(item):
                ln, (a, b, rep, desc) = item
                if time.time() > deadline: return ln, desc, "skipped"
                new = src[:a] + rep.encode() + src[b:]
                try: ast.parse(new)
                except SyntaxError: return ln, desc, "invalid"
                with lock: t = free.pop()
                try:
                    (t / f.path).write_bytes(new)
                    st, res, _, _ = pytest(ctx, t, tests, timeout=per, extra=("-x",) if not pinned else ())
                finally:
                    (t / f.path).write_bytes(src)
                    with lock: free.append(t)
                if st == "timeout": return ln, desc, "unknown"
                if any(res.get(k) == "fail" for k in passing - pinned): return ln, desc, "killed"
                if any(res.get(k) == "fail" for k in pinned): return ln, desc, "unknown"
                if st == "failed" and not res: return ln, desc, "killed"
                return ln, desc, "survived"

            with ThreadPoolExecutor(max_workers=workers) as pool:
                for ln, desc, verdict in pool.map(one, order):
                    if verdict == "invalid": continue
                    total[verdict] = total.get(verdict, 0) + 1
                    if verdict == "survived":
                        yield f.path, ln, f"mutant survived {len(tests)} test files: {desc}; no test notices this change in behaviour"
        yield "", "", f"mutation: {total['killed']} killed, {total['survived']} survived, {total['unknown']} unknown (timeout or hash-pinned), {total['skipped']} not run (budget {ctx.args.budget}s)", "info"
    finally:
        for t in trees: drop(ctx, t)


CHECKS = [
    Check("tests.collect", "full", "block", "PR 52 r2, 76 r2", "documented test commands that fail to collect", tests_collect),
    Check("tests.fix-removal", "full", "warn", "PR 76, 85", "changed tests that still pass with the non-test changes reverted", tests_fix_removal),
    Check("tests.mutate", "full", "warn", "PR 84 r2, 67, 85, 86, 80 r4", "mutants of changed lines that no test kills", tests_mutate),
]
