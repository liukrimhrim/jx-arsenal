"""Static checks for `jx preflight`. Each check yields (path, line, message[, severity]); `Check` adds id and origin.
Origins name the past review miss (PR number, review round) a check comes from; ⇢ marks an extrapolation to the same
failure shape elsewhere."""
import ast, difflib, fnmatch, hashlib, itertools, json, os, posixpath, re, shlex, subprocess
from collections import Counter, defaultdict
from pathlib import Path

from preflight import Check, FileChange, glob_re, matcher, parse_diff, run

DOC = (".md", ".rst", ".txt", ".adoc")
TEXTY = DOC + (".yml", ".yaml", ".toml", ".cfg", ".ini", ".json", ".sh", ".env", ".py", ".html")
REGISTRY_SKIP = matcher(["*.sha256", "**/locks/**", "*.lock", "*LOCK*.json", "*lock*.json", "SHA256SUMS"])


def is_doc(p): return p.endswith(DOC)
def stem(p): return p.rsplit("/", 1)[-1].rsplit(".", 1)[0]
def base(p): return p.rsplit("/", 1)[-1]
def modname(p): return p[:-3].replace("/", ".") if p.endswith(".py") else p


def lines_matching(ctx, rx, paths=None, skip=()):
    """(path, line_no, line) over live text files whose text matches rx at least once."""
    for p in paths if paths is not None else ctx.live:
        if p in skip: continue
        t = ctx.text(p)
        if t is None or not rx.search(t): continue
        for i, line in enumerate(t.splitlines(), 1):
            if rx.search(line): yield p, i, line


def top_names(tree, imports=True):
    """Names a module exposes: top-level defs, classes, assignments and (with imports) imported aliases, which are importable too."""
    out = set()
    for n in tree.body if tree else []:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): out.add(n.name)
        elif isinstance(n, ast.Assign): out |= {t.id for t in n.targets if isinstance(t, ast.Name)}
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name): out.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)) and imports: out |= {(a.asname or a.name).split(".")[0] for a in n.names}
        elif isinstance(n, (ast.If, ast.Try)):
            for sub in [n.body, getattr(n, "orelse", []), *[h.body for h in getattr(n, "handlers", [])]]:
                out |= top_names(ast.Module(body=sub, type_ignores=[]), imports)
    return out


def names_elsewhere(ctx, skip):
    """Top-level names of every live module except `skip`, cached per path."""
    cache = ctx.__dict__.setdefault("_top_names", {})
    out = set()
    for p in ctx.live_files(".py"):
        if p == skip: continue
        if p not in cache: cache[p] = top_names(ctx.tree(p), imports=False)
        out |= cache[p]
    return out


def parents(tree):
    par = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node): par[child] = node
    return par


def call_name(call):
    f = call.func
    if isinstance(f, ast.Name): return f.id
    if isinstance(f, ast.Attribute):
        v = f.value
        return f"{v.id}.{f.attr}" if isinstance(v, ast.Name) else f"?.{f.attr}"
    return ""


def handler_names(try_node):
    out = set()
    for h in try_node.handlers:
        if h.type is None: out.add("*"); continue
        for t in h.type.elts if isinstance(h.type, ast.Tuple) else [h.type]:
            out.add(t.attr if isinstance(t, ast.Attribute) else getattr(t, "id", "?"))
    return out


def enclosing_try_handlers(node, par):
    """Union of handler names of every try whose body contains node, up to the enclosing function."""
    names, cur = set(), node
    while cur in par:
        p = par[cur]
        if isinstance(p, ast.Try) and cur in p.body: names |= handler_names(p)
        if isinstance(p, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)): break
        cur = p
    return names


def _env_reads(ctx, path):
    """env_reads of a live file, walked once per run."""
    cache = ctx.__dict__.setdefault("_env_reads", {})
    if path not in cache: cache[path] = env_reads(ctx.tree(path))
    return cache[path]


def env_reads(tree):
    """Env var names this module reads: os.environ[...], .get/.pop/.setdefault on an environ, os.getenv."""
    out = {}
    for n in ast.walk(tree) if tree else []:
        name = None
        if isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            f, cn = n.func, call_name(n)
            if cn in ("os.getenv", "getenv") or (isinstance(f, ast.Attribute) and f.attr in ("get", "pop", "setdefault")
                                                 and re.search(r"environ|\benv\b", ast.unparse(f.value))):
                name = n.args[0].value
        elif (isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load)  # `os.environ["X"] = ...` sets X for a child: no read
              and re.search(r"environ", ast.unparse(n.value)) and isinstance(n.slice, ast.Constant)):
            name = n.slice.value if isinstance(n.slice.value, str) else None
        if name and re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", name): out.setdefault(name, n.lineno)
    return out


def cli_flags(tree):
    out = {}
    for n in ast.walk(tree) if tree else []:
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "add_argument":
            for a in n.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str) and a.value.startswith("--") and len(a.value) > 4:
                    out.setdefault(a.value, n.lineno)
    return out


# ---------------------------------------------------------------- A. the change's reach outside the diff

def binds_module(ctx, p, path):
    """Whether file p imports the module at `path` under its own name, so that `stem.name` in p reads that module and
    not a file called `stem.json` or another object's attribute."""
    mod, cache = stem(path), ctx.__dict__.setdefault("_binds_module", {})
    if (p, path) not in cache:
        found = False
        for n in ast.walk(ctx.tree(p) or ast.Module(body=[], type_ignores=[])):
            if isinstance(n, ast.Import):
                found |= any(a.name.rsplit(".", 1)[-1] == mod and a.asname in (None, mod) for a in n.names)
            elif isinstance(n, ast.ImportFrom) and any(a.name == mod and a.asname in (None, mod) for a in n.names):
                tail = [*filter(None, [(n.module or "").replace(".", "/")]), mod + ".py"]
                if n.level:  # relative: from the importing file's package, `level - 1` folders up
                    here = p.split("/")[:-1]
                    found |= path == "/".join([*here[:len(here) - (n.level - 1)], *tail])
                else:
                    found |= ("/" + path).endswith("/" + "/".join(tail))
        cache[(p, path)] = found
    return cache[(p, path)]


def refs_removed(ctx):
    for f in ctx.changed(".py", statuses="MDR"):
        bt = ctx.base_tree(f.path)
        if bt is None: continue
        ht = ctx.tree(f.path) if f.status != "D" else None
        mod = stem(f.old_path)
        defined_elsewhere = names_elsewhere(ctx, f.path)
        # A name the module only imported is importable from it, so a use through the module still breaks. A bare
        # mention elsewhere is that file's own import of the same thing: dropping `import json` here is not its concern.
        passed_on = top_names(bt) - top_names(bt, imports=False)
        for name in sorted(top_names(bt) - top_names(ht) - defined_elsewhere):
            if len(name) < 4 or name.startswith("__"): continue
            rx = re.compile(rf"\b{re.escape(name)}\b")
            attr = re.compile(rf"\b{re.escape(mod)}\.{re.escape(name)}\b")
            imported = re.compile(rf"\bfrom\s+[\w.]*\b{re.escape(mod)}\s+import\b.*\b{re.escape(name)}\b")
            def through(p, line):  # `from mod import name`, or `mod.name` in a file that imports mod
                return p.endswith(".py") and bool(imported.search(line) or attr.search(line) and binds_module(ctx, p, f.old_path))
            hits = ((p, i, "block" if through(p, line) else "warn") for p, i, line in lines_matching(ctx, rx, skip={f.path}))
            for p, i, sev in itertools.islice((h for h in hits if h[2] == "block" or name not in passed_on), 8):
                yield p, i, f"`{name}` was removed from {f.old_path} but is still referenced here", sev
        for kind, old, new in (("env var", env_reads(bt), env_reads(ht)), ("CLI flag", cli_flags(bt), cli_flags(ht))):
            for name in sorted(set(old) - set(new)):
                if kind == "env var" and any(name in _env_reads(ctx, p) for p in ctx.live_files(".py") if p != f.path): continue
                rx = re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])")
                for p, i, _ in itertools.islice(lines_matching(ctx, rx, skip={f.path}), 6):
                    yield p, i, f"{kind} `{name}` is no longer read/defined by {f.path} but is still named here"
    names = Counter(base(p) for p in ctx.tracked)
    for f in ctx.changed(statuses="DR", live=False):
        targets = [f.old_path] + ([base(f.old_path)] if len(base(f.old_path)) >= 8 and names[base(f.old_path)] <= 1 else [])
        rx = re.compile("|".join(rf"(?<![\w./-]){re.escape(t)}(?![\w-])" for t in targets))
        for p, i, _ in itertools.islice(lines_matching(ctx, rx, skip={f.path}), 8):
            what = "deletes" if f.status == "D" else f"renames to {f.path}"
            yield p, i, f"names {f.old_path}, which this diff {what}"


def refs_old_literal(ctx):
    for f in ctx.changed(".py", statuses="MR"):
        old, new = {}, {}
        for tree, into in ((ctx.base_tree(f.path), old), (ctx.tree(f.path), new)):
            for n in tree.body if tree else []:
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str):
                    for t in n.targets:
                        if isinstance(t, ast.Name) and t.id.isupper(): into[t.id] = n.value.value
        for name, value in old.items():
            if name not in new or new[name] == value or len(value) < 6: continue
            needles = {value} | ({base(value)} if "/" in value and len(base(value)) >= 8 else set())
            rx = re.compile("|".join(re.escape(x) for x in needles))
            role = re.compile(rf"\b{re.escape(name)}\b|(?i:\b(default|current|latest|now)\b)")
            for p, i, line in lines_matching(ctx, rx, skip={f.path}):
                if role.search(line):
                    yield p, i, f"still names the old {name} `{base(value)}`; it is now `{base(new[name])}`"


VERSIONED = re.compile(r".*[_-]v?r?\d+$")


def refs_registry(ctx):
    bydir = defaultdict(list)
    for p in ctx.live: bydir[p.rsplit("/", 1)[0] if "/" in p else ""].append(p)
    registries = [p for p in ctx.live if p.endswith(TEXTY) and not p.endswith(".py") and not REGISTRY_SKIP(p)]
    for f in ctx.changed(statuses="A"):
        name = base(f.path); s, ext = (name.rsplit(".", 1) + [""])[:2]
        if VERSIONED.match(s) or not ext: continue
        d = f.path.rsplit("/", 1)[0] if "/" in f.path else ""
        tokens = re.split(r"([_-])", s)
        best = None
        for k in range(1, (len(tokens) + 1) // 2):
            pre, suf = "".join(tokens[:2 * k - 1]), "".join(tokens[-(2 * k - 1):])
            for affix, test in ((pre, lambda n, a=pre: n.startswith(a) and n[len(a):len(a) + 1] in "_-"),
                                (suf, lambda n, a=suf: n.endswith(a) and n[-len(a) - 1:-len(a)] in "_-")):
                sibs = [p for p in bydir[d] if p != f.path and p.endswith("." + ext) and test(stem(p))]
                if len(sibs) >= 2 and (best is None or len(affix) > len(best[0])): best = (affix, sibs)
        if not best: continue
        affix, sibs = best
        in_ci = f.path in ci_head(ctx)
        for r in registries:
            if r == f.path or (in_ci and r.startswith(".github/workflows/")): continue
            t = ctx.text(r) or ""
            listed = [b for b in (base(x) for x in sibs) if b in t]
            if len(listed) >= 2 and len(listed) * 3 >= len(sibs) and name not in t:
                line = next((i for i, l in enumerate(t.splitlines(), 1) if listed[0] in l), 1)
                yield r, line, f"lists {len(listed)} of the {len(sibs)} `{affix}` files in {d or '.'}/ but not {name}"


def _norm(line): return " ".join(line.split())


def line_index(ctx):
    if not hasattr(ctx, "_line_index"):
        idx = defaultdict(list)
        for p in ctx.live:
            for i, l in enumerate(ctx.lines(p), 1):
                s = _norm(l)
                if len(s) >= 15: idx[s].append((p, i))
        ctx._line_index = idx
    return ctx._line_index


SKIPLINE = re.compile(r"^(#|//|<!--|\*|import |from \S+ import |@|\"\"\"|'''|[)\]}>,;:'\"\s]*$|(else|try|finally|pass|break|continue)\b)")


def rewritten(f, old, new):
    """Old line numbers this diff rewrote in place: removed lines whose hunk adds a line at least 60% similar (a fix, not a move)."""
    out = set()
    for os_, ol, ns, nl in f.hunks:
        if not ol or not nl: continue
        adds = [_norm(new[k - 1]) for k in range(ns, ns + nl) if 0 < k <= len(new)]
        for k in range(os_, os_ + ol):
            if 0 < k <= len(old):
                o = _norm(old[k - 1])
                if any(difflib.SequenceMatcher(None, o, a, autojunk=False).ratio() >= 0.6 for a in adds if a != o): out.add(k)
    return out


def refs_old_pattern(ctx):
    added = {_norm(l) for f in ctx.changed() for _, l in ctx.added_text(f.path)}
    idx = line_index(ctx)
    for f in ctx.changed(statuses="MR"):
        old, new = (ctx.base_text(f.path) or "").splitlines(), ctx.lines(f.path)
        for n in sorted(rewritten(f, old, new)):
            s = _norm(old[n - 1])
            if len(s) < 15 or SKIPLINE.match(s) or s in added: continue
            hits = idx.get(s, [])
            if not hits or len(hits) > 10: continue
            for p, i in hits[:5]:
                yield p, i, f"this diff removed `{s[:90]}` from {f.old_path}:{n}; the same line is still here"


def signature(fn, method):
    a = fn.args
    pos = [x.arg for x in a.posonlyargs + a.args]
    static = any(getattr(d, "id", getattr(d, "attr", "")) == "staticmethod" for d in fn.decorator_list)
    if method and not static and pos and pos[0] in ("self", "cls"): pos = pos[1:]
    posonly = {x.arg for x in a.posonlyargs}
    return dict(pos=pos, required=len(pos) - len(a.defaults), posonly=posonly,
                kwonly={x.arg: d is not None for x, d in zip(a.kwonlyargs, a.kw_defaults)}, vararg=bool(a.vararg), kwarg=bool(a.kwarg))


def signatures(tree):
    out = {}
    for n in tree.body if tree else []:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)): out[n.name] = signature(n, False)
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)): out[f"{n.name}.{m.name}"] = signature(m, True)
    return out


def bad_call(call, s):
    if any(isinstance(x, ast.Starred) for x in call.args) or any(k.arg is None for k in call.keywords): return None
    npos, kws = len(call.args), {k.arg for k in call.keywords}
    if npos > len(s["pos"]) and not s["vararg"]: return f"passes {npos} positional arguments; it takes {len(s['pos'])}"
    allowed = (set(s["pos"]) - s["posonly"]) | set(s["kwonly"])
    unknown = sorted(k for k in kws if k not in allowed)
    if unknown and not s["kwarg"]: return f"passes unknown keyword {', '.join(unknown)}"
    missing = [p for p in s["pos"][npos:s["required"]] if p not in kws] + [k for k, d in s["kwonly"].items() if not d and k not in kws]
    return f"is missing {', '.join(missing)}" if missing else None


def refs_callers(ctx):
    for f in ctx.changed(".py", statuses="MR"):
        old, new = signatures(ctx.base_tree(f.path)), signatures(ctx.tree(f.path))
        changed = {q: s for q, s in new.items() if q in old and old[q] != s}
        if not changed: continue
        mod, dotted = stem(f.path), modname(f.path)
        names = {q.split(".")[0] for q in changed}
        for p in ctx.live_files(".py"):
            t = ctx.text(p) or ""
            if not any(n in t for n in names): continue
            tree = ctx.tree(p)
            if tree is None: continue
            funcs, mods = {}, set()
            if p == f.path: funcs = {q: q for q in changed if "." not in q} | {q.split(".")[0]: q.split(".")[0] for q in changed if "." in q}
            for n in ast.walk(tree):
                if isinstance(n, ast.ImportFrom) and n.module and (n.module == dotted or n.module.split(".")[-1] == mod):
                    for al in n.names: funcs[al.asname or al.name] = al.name
                elif isinstance(n, ast.ImportFrom) and n.module:
                    for al in n.names:
                        if al.name == mod and (n.module + "." + mod == dotted or dotted.endswith(n.module.split(".")[-1] + "." + mod)):
                            mods.add(al.asname or al.name)
                elif isinstance(n, ast.Import):
                    for al in n.names:
                        if al.name == dotted or al.name.split(".")[-1] == mod: mods.add(al.asname or al.name.split(".")[0] if not al.asname else al.asname)
            for n in ast.walk(tree):
                if not isinstance(n, ast.Call): continue
                q = None
                if isinstance(n.func, ast.Name) and n.func.id in funcs and funcs[n.func.id] in changed: q = funcs[n.func.id]
                elif isinstance(n.func, ast.Attribute):
                    v = n.func.value
                    if isinstance(v, ast.Name) and v.id in mods and n.func.attr in changed: q = n.func.attr
                    elif isinstance(v, ast.Name) and v.id in funcs and f"{funcs[v.id]}.{n.func.attr}" in changed: q = f"{funcs[v.id]}.{n.func.attr}"
                    elif isinstance(v, ast.Name) and v.id == "self" and p == f.path:
                        q = next((c for c in changed if c.endswith("." + n.func.attr)), None)
                if q and (problem := bad_call(n, changed[q])):
                    yield p, n.lineno, f"call to `{q}` {problem} after its signature changed in {f.path}", "block"


def refs_orphan(ctx):
    for f in ctx.changed(statuses="A", live=False):
        p = f.path
        lockish = "/locks/" in f"/{p}" or re.search(r"lock", base(p), re.I) and p.endswith(".json")
        if lockish:
            if not any(base(p) in (ctx.text(q) or "") for q in ctx.live if q != p):
                yield p, 1, f"nothing in the live tree names {base(p)}; an unreferenced lock is dead weight or a missed DEFAULT bump"
            continue
        if f.excluded or not p.endswith(".py") or ctx.is_test(p) or base(p) in ("__init__.py", "conftest.py", "setup.py"): continue
        rx = re.compile(rf"\b{re.escape(stem(p))}\b")
        if not any(rx.search(ctx.text(q) or "") for q in ctx.live if q != p):
            yield p, 1, f"nothing imports or names {p}"
    test_text = None
    for f in ctx.changed(".py", tests=False):
        tree, old = ctx.tree(f.path), top_names(ctx.base_tree(f.path))
        for n in tree.body if tree else []:
            if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) or n.name in old or n.decorator_list or n.name == "main": continue
            if n.lineno not in f.added: continue
            rx = re.compile(rf"\b{re.escape(n.name)}\b")
            own = sum(1 for l in ctx.lines(f.path) if rx.search(l))
            in_code = own > 1 or any(rx.search(ctx.text(q) or "") for q in ctx.live_files(".py", tests=False) if q != f.path)
            if test_text is None: test_text = "\n".join(ctx.text(q) or "" for q in ctx.live_files(".py", tests=True))
            if not in_code and rx.search(test_text):
                yield f.path, n.lineno, f"`{n.name}` is new non-test code that only tests call"


def docs_cochange(ctx):
    p = run(["git", "-C", str(ctx.root), "log", "--no-merges", "--format=@%H", "--name-only", "-n", "3000", ctx.merge_base], check=False)
    commits, cur = [], set()
    for line in p.stdout.splitlines():
        if line.startswith("@"):
            if cur: commits.append(cur)
            cur = set()
        elif line.strip(): cur.add(line.strip())
    if cur: commits.append(cur)
    changed = {f.path for f in ctx.files.values()} | {f.old_path for f in ctx.files.values()}
    for x in sorted(f.path for f in ctx.changed() if not is_doc(f.path)):
        with_x = [c for c in commits if x in c and len(c) <= 40]
        if len(with_x) < 5: continue
        docs = Counter(y for c in with_x for y in c if is_doc(y) and y != x and y in ctx.live_set)
        for y, k in docs.most_common(3):
            if k >= 5 and k / len(with_x) >= 0.6 and y not in changed:
                yield y, 1, f"changed together with {x} in {k} of its last {len(with_x)} commits; does it need an update?"


WORD = re.compile(r"[A-Za-z_][\w.-]*[\d_-][\w.-]*|[A-Z][a-z]+[A-Z]\w*")  # snake, kebab, digits or CamelCase


def docs_namesake(ctx):
    """A changed module's namesake doc beside it (engine_smoke.py and ENGINE_SMOKE.md) that names something the
    diff changed, and did not change itself."""
    changed = {f.path for f in ctx.files.values()} | {f.old_path for f in ctx.files.values()}
    docs = defaultdict(list)
    for p in ctx.live_files(".md"):
        docs[(posixpath.dirname(p), posixpath.basename(p)[:-3].lower().replace("-", "_"))].append(p)
    for f in ctx.changed(".py", tests=False):
        key = (posixpath.dirname(f.path), posixpath.basename(f.path)[:-3].lower().replace("-", "_"))
        base = (ctx.base_text(f.path) or "").splitlines()
        touched = [l for _, l in ctx.added_text(f.path)] + [base[n - 1] for n in f.removed_old if 0 < n <= len(base)]
        words = list(dict.fromkeys(m.group().strip(".-") for l in touched for m in WORD.finditer(l)
                                   if len(m.group().strip(".-")) >= 5 and "/" not in l[max(m.start() - 1, 0):m.end() + 1]))
        for doc in docs.get(key, []):
            if doc in changed: continue
            lines = ctx.lines(doc)
            hits = [(w, next((i for i, l in enumerate(lines, 1) if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", l)), 1))
                    for w in words if re.search(rf"(?<![\w-]){re.escape(w)}(?![\w-])", ctx.text(doc) or "")]
            if hits:
                named = ", ".join(f"`{w}` (:{n})" for w, n in hits[:3])
                yield doc, hits[0][1], f"documents {f.path}, which this diff changes where it names {named}; {doc} did not change. Does it still hold?"


def env_undocumented(ctx):
    docs = "\n".join(ctx.text(p) or "" for p in ctx.live if is_doc(p))
    for f in ctx.changed(".py", tests=False):
        old = env_reads(ctx.base_tree(f.path))
        for name, line in env_reads(ctx.tree(f.path)).items():
            if name not in old and line in f.added and name not in docs:
                yield f.path, line, f"reads env var `{name}`, which no doc names"


PIN = re.compile(r"(?<![\w.-])([A-Za-z][A-Za-z0-9_.-]*)(?:\[[^\]]*\])?==([0-9][A-Za-z0-9_.+!-]*)")


def pins(ctx):
    out = defaultdict(list)
    for p in ctx.live:
        name = base(p)
        if p.endswith(".py"):
            tree = ctx.tree(p)
            for n in ast.walk(tree) if tree else []:
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and "pip_install" in n.func.attr:
                    for c in ast.walk(n):
                        if isinstance(c, ast.Constant) and isinstance(c.value, str):
                            for m in PIN.finditer(c.value): out[m[1].lower().replace("_", "-").replace(".", "-")].append((p, c.lineno, m[2]))
            continue
        reqs = "requirements" in name or name.endswith((".in", "constraints.txt"))
        if not (reqs or p.endswith((".yml", ".yaml", ".sh", ".md", ".toml", "Dockerfile"))): continue
        for i, line in enumerate(ctx.lines(p), 1):
            if not reqs and not re.search(r"\b(pip|uv)\b", line): continue
            for m in PIN.finditer(line.split("#")[0] if reqs else line):
                out[m[1].lower().replace("_", "-").replace(".", "-")].append((p, i, m[2]))
    return out


def pins_drift(ctx):
    added = {(f.path, n) for f in ctx.changed() for n in f.added}
    for pkg, locs in pins(ctx).items():
        versions = {v for _, _, v in locs}
        if len(versions) < 2: continue
        for p, i, v in locs:
            if (p, i) in added:
                others = sorted({f"{v2} at {p2}:{i2}" for p2, i2, v2 in locs if v2 != v})[:3]
                yield p, i, f"pins {pkg}=={v}, while {'; '.join(others)}"


def fn_tokens(fn):
    body = fn.body[1:] if fn.body and isinstance(fn.body[0], ast.Expr) and isinstance(getattr(fn.body[0], "value", None), ast.Constant) else fn.body
    return re.findall(r"\w+|[^\w\s]", ast.dump(ast.Module(body=body, type_ignores=[]), annotate_fields=False))


def functions(tree):
    for n in ast.walk(tree) if tree else []:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and sum(isinstance(x, ast.stmt) for x in ast.walk(n)) - 1 >= 5:
            yield n


def dup_helpers(ctx):
    new = []
    for f in ctx.changed(".py"):
        old = {getattr(n, "name", None) for n in ast.walk(ctx.base_tree(f.path)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))} if ctx.base_tree(f.path) else set()
        for fn in functions(ctx.tree(f.path)):
            if fn.name not in old and fn.lineno in f.added and not fn.name.startswith("test"): new.append((f.path, fn, fn_tokens(fn)))
            attrs = lambda node: {c.func.attr for c in ast.walk(node) if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)}
            hashes = any(isinstance(c, ast.Call) and call_name(c).startswith("hashlib.") for c in ast.walk(fn))
            if fn.lineno in f.added and hashes and any({"update", "read"} <= attrs(b) for b in ast.walk(fn) if isinstance(b, (ast.For, ast.While))):
                yield f.path, fn.lineno, f"`{fn.name}` hashes a file chunk by chunk; `hashlib.file_digest(f, 'sha256')` does it"
    if not new: return
    if not hasattr(ctx, "_fn_corpus"):
        ctx._fn_corpus = [(p, fn, sum(isinstance(x, ast.stmt) for x in ast.walk(fn))) for p in ctx.live_files(".py") for fn in functions(ctx.tree(p))]
    reported = set()
    for path, fn, toks in new:
        best, size = None, sum(isinstance(x, ast.stmt) for x in ast.walk(fn))
        for p, other, osize in ctx._fn_corpus:
            if other is fn or other.name == fn.name or not (0.8 * size <= osize <= 1.25 * size): continue
            ot = getattr(other, "_jx_tokens", None)
            if ot is None: ot = other._jx_tokens = fn_tokens(other)
            if not (0.85 * len(toks) <= len(ot) <= 1.15 * len(toks)): continue
            sm = difflib.SequenceMatcher(None, toks, ot, autojunk=False)
            if sm.real_quick_ratio() < 0.9 or sm.quick_ratio() < 0.9: continue
            r = sm.ratio()
            if r >= 0.9 and (best is None or r > best[0]): best = (r, p, other)
        if best and (id(best[2]), id(fn)) not in reported:
            reported.add((id(fn), id(best[2])))
            yield path, fn.lineno, f"`{fn.name}` is a {best[0]:.0%} copy of `{best[2].name}` at {best[1]}:{best[2].lineno}; reuse it or say why not"


PATHLIKE = re.compile(r"^[\w.-]+(?:/[\w.-]+)+\.\w{1,8}$")


def path_constants(tree):
    """(path, line) of each repo path a module-level UPPER constant holds directly or as a dict value; tuples and
    lists of inputs are left out (tools list the files they hash on purpose)."""
    for s in tree.body if tree else []:
        if isinstance(s, (ast.Assign, ast.AnnAssign)) and any(isinstance(t, ast.Name) and t.id.isupper()
                                                               for t in (s.targets if isinstance(s, ast.Assign) else [s.target])):
            vals = [s.value] if isinstance(s.value, ast.Constant) else list(s.value.values) if isinstance(s.value, ast.Dict) else []
            for v in vals:
                if isinstance(v, ast.Constant) and isinstance(v.value, str) and len(v.value) >= 15 and PATHLIKE.match(v.value):
                    yield v.value, v.lineno


def dup_constants(ctx):
    """A path constant this diff adds that another non-test module already defines: two definitions drift apart."""
    new = [(f.path, v, line) for f in ctx.changed(".py", tests=False) for v, line in path_constants(ctx.tree(f.path)) if line in f.added]
    if not new: return
    wanted = {v for _, v, _ in new}
    defined = defaultdict(list)
    for p in ctx.live_files(".py", tests=False):
        if not any(v in (ctx.text(p) or "") for v in wanted): continue
        for v, line in path_constants(ctx.tree(p)):
            if v in wanted: defined[v].append((p, line))
    for path, v, line in new:
        other = next(((p, n) for p, n in defined[v] if p != path), None)
        if other:
            yield path, line, f"`{v}` is also defined at {other[0]}:{other[1]}; define it once and import it, so the two cannot drift"


def imports_private(ctx):
    for f in ctx.changed(".py"):
        tree = ctx.tree(f.path)
        for n in ast.walk(tree) if tree else []:
            if not isinstance(n, ast.ImportFrom) or n.lineno not in f.added or n.module in (None, "__future__"): continue
            priv = [a.name for a in n.names if a.name.startswith("_") and not a.name.startswith("__")]
            if priv and n.level == 0:
                known = re.compile(rf"from\s+{re.escape(n.module)}\s+import\b[^\n]*\b({'|'.join(map(re.escape, priv))})\b")
                if any(known.search(l) and not (p in ctx.files and i in ctx.files[p].added)
                       for p in ctx.live_files(".py") if p != f.path and known.search(ctx.text(p) or "")
                       for i, l in enumerate(ctx.lines(p), 1)):
                    continue  # an existing coupling, not a new one
                yield f.path, n.lineno, f"imports private {', '.join(priv)} from {n.module}; make it public or move it to a shared helper"
            last = n.module.split(".")[-1]
            if ctx.is_test(f.path) and last.startswith("test_"):
                yield f.path, n.lineno, f"imports from test module {n.module}; shared test code belongs in a helper module"


# ---------------------------------------------------------------- B. citations and claims

CITE = re.compile(r"(?<![\w/.:@-])((?:\.\.?/)*(?:[\w.@+-]+/)*[\w@+-][\w.@+-]*\.[A-Za-z][A-Za-z0-9]{0,7}):(\d+)(?:\s?[-–]\s?(\d+))?(?![\w:])")
LINK = re.compile(r"\[([^\]]*)\]\(([^)\s#]+)#L(\d+)(?:-L?(\d+))?\)")


def citations(text):
    """(line_no, ref, a, b, link_text, start_col) for path:line and [..](path#Ln) citations."""
    for i, line in enumerate(text.splitlines(), 1):
        for m in LINK.finditer(line):
            if "://" not in m[2]: yield i, m[2], int(m[3]), int(m[4] or m[3]), m[1], m.start()
        for m in CITE.finditer(line):
            if "://" in line[max(0, m.start() - 12):m.start()]: continue
            yield i, m[1], int(m[2]), int(m[3] or m[2]), None, m.start()


def basenames(ctx):
    if not hasattr(ctx, "_basenames"):
        idx = defaultdict(list)
        for p in set(ctx.tracked) | ctx.live_set: idx[base(p)].append(p)
        ctx._basenames = idx
    return ctx._basenames


def resolve(ctx, ref, citing):
    d = citing.rsplit("/", 1)[0] if "/" in citing else ""
    for c in ([os.path.normpath(os.path.join(d, ref))] if d else []) + [os.path.normpath(ref)]:
        if (ctx.root / c).is_file(): return c, "ok"
    if "/" not in ref:
        hits = basenames(ctx).get(ref, [])
        live = [h for h in hits if h in ctx.live_set]
        if len(hits) == 1: return hits[0], "ok"
        if len(live) == 1: return live[0], "ok"
        near = [h for h in hits if h.startswith(d + "/")] if d else []
        if len(near) == 1: return near[0], "ok"
        return (hits, "ambiguous") if hits else (None, "external")
    first = ref.split("/")[0]
    return (None, "missing") if first in ("..", ".") or (ctx.root / first).exists() else (None, "external")


def changed_text(ctx):
    """(path, line_no, line) of added lines in changed text files, plus the PR body as `(body)`."""
    for f in ctx.changed():
        if not f.path.endswith(TEXTY) and "." in base(f.path): continue
        for n, l in ctx.added_text(f.path): yield f.path, n, l
    if ctx.body:
        for i, l in enumerate(ctx.body.splitlines(), 1): yield "(body)", i, l


def cite_resolve(ctx):
    for path, n, line in changed_text(ctx):
        for _, ref, a, b, _, _ in citations(line):
            target, state = resolve(ctx, ref, path if path != "(body)" else "")
            if state == "missing": yield path, n, f"cites {ref}:{a}, but no such file exists"
            elif state == "ambiguous": yield path, n, f"cites {ref}:{a}, but {len(target)} files are named {ref} ({', '.join(target[:3])}); give the path"
            elif state == "ok":
                size = len(ctx.lines(target))
                if size and b > size: yield path, n, f"cites {ref}:{a}{'-' + str(b) if b != a else ''}, but {target} has {size} lines", "block"


BACKTICK = re.compile(r"`([^`]{3,80})`")
IDENT = re.compile(r"[A-Za-z_][\w.]*(\(\))?|\[[\w.-]+\][^`]*|[A-Z][A-Z0-9_ ]{3,}")


def cite_content(ctx):
    for path, n, line in changed_text(ctx):
        for _, ref, a, b, link_text, col in citations(line):
            target, state = resolve(ctx, ref, path if path != "(body)" else "")
            if state != "ok": continue
            lines = ctx.lines(target)
            if not lines or a > len(lines): continue
            cands = []
            if link_text: cands = BACKTICK.findall(link_text) or ([link_text] if IDENT.fullmatch(link_text) else [])
            else: cands = [m[1] for m in BACKTICK.finditer(line) if abs(m.start() - col) <= 90]
            for c in cands:
                c = c.strip().rstrip("()")
                if not IDENT.fullmatch(c) or len(c) < 4 or c in ref or c == stem(ref) or c.endswith(base(ref)): continue
                if c in lines[a - 1]: break
                near = [i for i in range(max(1, a - 10), min(len(lines), a + 10) + 1) if c in lines[i - 1]]
                if near:  # a near miss is drift or an off-by-N; a far one usually cites a use site on purpose
                    where = min(near, key=lambda i: abs(i - a))
                    yield path, n, f"cites {ref}:{a} for `{c}`, but `{c}` is on line {where}, not {a}"
                    break


def cite_drift(ctx):
    changed = {f.path: f for f in ctx.changed(statuses="MRD", live=False)}
    by_old = {f.old_path: f for f in changed.values()}
    side = None
    if ctx.base_tip != ctx.merge_base:
        diff = run(["git", "-C", str(ctx.root), "-c", "core.quotePath=false", "diff", "-U0", "--find-renames", "--no-color", "--no-ext-diff",
                    "--src-prefix=a/", "--dst-prefix=b/", ctx.merge_base, ctx.base_tip], check=False).stdout  # same shape as the main diff: parse_diff reads a/ b/
        side = {f.old_path: f for f in parse_diff(diff)}
    groups = defaultdict(list)
    for item in _drift(ctx, changed, by_old, side):
        if item[3:] not in groups[item[:3]]: groups[item[:3]].append(item[3:])
    for (p, target, kind), rows in groups.items():
        first = rows[0][0]
        lines = ", ".join(f"{a}→{m}" if m else str(a) for _, a, m in rows[:6]) + (", …" if len(rows) > 6 else "")
        msg = {"moved": f"cites {len(rows)} line(s) of {target} that this diff moved ({lines}); update the citations",
               "rewritten": f"cites {len(rows)} line(s) of {target} that this diff rewrote ({lines}); re-check what they point at",
               "deleted": f"cites {target}, which this diff deletes",
               "merge": f"cites {len(rows)} line(s) of {target} that {ctx.base} has since moved ({lines}); they drift on merge"}[kind]
        yield p, first, msg


def _drift(ctx, changed, by_old, side):
    for p in ctx.live:
        if not p.endswith(TEXTY): continue
        t = ctx.text(p)
        if not t or not re.search(r":\d|#L\d", t): continue
        own = ctx.files.get(p)
        for i, ref, a, b, _, _ in citations(t):
            target, state = resolve(ctx, ref, p)
            new_line = own is not None and i in own.added
            if state == "ok" and not new_line:
                f = changed.get(target) or by_old.get(target)
                if f is None: continue
                if f.status == "D": yield p, target, "deleted", i, a, None; continue
                m = f.map_line(a)
                if m is None: yield p, target, "rewritten", i, a, None
                elif m != a: yield p, target, "moved", i, a, m
            elif state == "ok" and new_line and side and target in side:
                m = side[target].map_line(a)
                if m is None or m != a: yield p, target, "merge", i, a, m


PATHISH = re.compile(r"^(?:\./)?[\w.@+-]+(?:/[\w.@+-]+)+/?(?::\d+(?:-\d+)?)?$")


def cite_paths(ctx):
    tops = {p.split("/")[0] for p in ctx.tracked}
    for path, n, line in changed_text(ctx):
        for m in BACKTICK.finditer(line):
            tok = m[1].strip()
            if not PATHISH.match(tok) or tok.startswith(("http", "--")): continue
            rel = re.sub(r":\d+(?:-\d+)?$", "", tok).rstrip("/")
            if rel.split("/")[0] not in tops: continue
            d = path.rsplit("/", 1)[0] if "/" in path and path != "(body)" else ""
            hits = [c for c in {rel, os.path.normpath(os.path.join(d, rel))} if (ctx.root / c).exists()]
            if not hits: yield path, n, f"names `{rel}`, which does not exist in this tree"
            elif all((ctx.root / h).is_file() and h not in ctx.tracked and h not in ctx.live_set for h in hits):
                yield path, n, f"names `{rel}`, which exists here but is not tracked (ignored?), so reviewers cannot see it"


NUM = r"(\d{1,3}(?:,\d{3})+|\d+)"
COUNT = re.compile(rf"{NUM}\s+(?:\w+\s+)?(tests?|cases|passed|failed|failures|errors?|skipped|mutants?|killed|survived|survivors)\b", re.I)


def receipt_state(ctx):
    p = subprocess.run(["git", "-C", str(ctx.root), "diff", "--binary", "HEAD"], capture_output=True)
    return ctx.head, hashlib.sha256(p.stdout).hexdigest()


def claims_counts(ctx):
    if not ctx.body: return
    claims = [(i, m) for i, l in enumerate(ctx.body.splitlines(), 1) for m in COUNT.finditer(l)]
    if not claims: return
    if not ctx.args.receipts:
        i, m = claims[0]
        yield "(body)", i, f"states test counts ('{m[0]}', {len(claims)} in all) but no --receipts were given to back them"; return
    head, diff_sha = receipt_state(ctx)
    numbers, failed = set(), []
    for d in ctx.args.receipts:
        try: r = json.loads((Path(d) / "result.json").read_text())
        except (OSError, ValueError, RecursionError): yield "(body)", "", f"{d}: no readable result.json; is it a `jx check run` directory?"; continue
        before = r.get("before") or {}
        if before.get("head") != head or before.get("tracked_diff_sha256") != diff_sha:
            yield "(body)", "", f"receipt {d} ran on {str(before.get('head'))[:12]} with a different tree; rerun it on this HEAD"; continue
        t = r.get("tests") or {}
        numbers |= {v for v in t.values() if isinstance(v, int)} | {t.get("passed", 0) + t.get("skipped", 0)}
        if r.get("exit_code") or t.get("failures") or t.get("errors"): failed.append((d, t, r.get("exit_code")))
    for i, m in claims:
        n = int(m[1].replace(",", ""))
        if numbers and n not in numbers and n > 3:
            yield "(body)", i, f"'{m[0]}' matches no count in the fresh receipts ({', '.join(map(str, sorted(numbers)))})"
    if failed and re.search(r"(?i)\b(pass(es|ed)?|green|all .* pass)\b", ctx.body):
        for d, t, code in failed: yield "(body)", "", f"receipt {d} failed (exit {code}, {t}) while the body reports passing tests", "block"


SHA = re.compile(r"(?<![\w/])([0-9a-f]{7,40})(?![\w/])")


def claims_shas(ctx):
    if not ctx.body: return
    root = str(ctx.root)
    for i, line in enumerate(ctx.body.splitlines(), 1):
        for m in SHA.finditer(line):
            s = m[1]
            if not (re.search(r"[a-f]", s) and re.search(r"\d", s)): continue
            before, ticked = line[max(0, m.start() - 24):m.start()], line[m.start() - 1:m.start()] == "`"
            if re.search(r"(?i)digest|sha-?256|hash|blob|tree|checksum", before) or not (ticked or re.search(r"(?i)\b(commit|at|head|tip|merge|rebase|on|from)\b\s*$", before)):
                continue
            if run(["git", "-C", root, "cat-file", "-e", f"{s}^{{commit}}"], check=False).returncode:
                yield "(body)", i, f"names commit {s}, which this clone does not have"; continue
            on = any(run(["git", "-C", root, "merge-base", "--is-ancestor", s, ref], check=False).returncode == 0 for ref in ("HEAD", ctx.base_tip))
            if not on: yield "(body)", i, f"names commit {s}, which is on neither this branch nor {ctx.base}"
            elif re.search(r"(?i)\b(at|head|tip|on)\s*`?$", line[:m.start()]) and not ctx.head.startswith(s) and not re.search(r"(?i)\b(was|earlier|before|previous|last review|round)\b", line):
                yield "(body)", i, f"names {s} as the tested head, but HEAD is {ctx.head[:7]}"


VERIFY_HEAD = re.compile(r"(?i)verif|test|evidence|result|check|validation")
PAST = re.compile(r"(?i)\b(verified|confirmed|checked|reproduced|tested|ran|passed|matched|proved|ensured|validated)\b")
ABSOLUTE = re.compile(r"(?i)\b(every|all|each|always|never|byte[- ]identical|exactly|independent(ly)?|real|end[- ]to[- ]end|no regressions?|fully)\b")
POINTER = re.compile(r"[\w./-]+\.\w{1,6}(:\d+)?|\b[0-9a-f]{7,40}\b|https?://|UNVERIFIED|receipt|run[- ]?id|\d{8}T\d{6}|`[^`]+`")


def claims_absolute(ctx):
    if not ctx.body: return
    section, n = "", 0
    for i, line in enumerate(ctx.body.splitlines(), 1):
        if re.match(r"\s*(#{1,6}\s|\*\*[^*]+\*\*\s*$)", line): section = line; continue
        if not VERIFY_HEAD.search(section): continue
        for sentence in re.split(r"(?<=[.;])\s+", line):
            if PAST.search(sentence) and ABSOLUTE.search(sentence) and not POINTER.search(sentence):
                n += 1
                if n <= 12: yield "(body)", i, f"claim without evidence: \"{sentence.strip()[:140]}\"; cite the receipt, path or run, or mark it UNVERIFIED"


def claims_undisclosed(ctx):
    if ctx.body is None: return
    body = ctx.body
    watch = matcher([".github/**", ".jx/**", *ctx.config.get("governing_docs", []), *ctx.config.get("undisclosed_watch", [])])
    told = lambda p: p in body or base(p) in body or (len(stem(p)) >= 6 and stem(p) in body)
    for f in ctx.changed(statuses="AMDR", live=False):
        default = any(re.match(r"\s*DEFAULT_\w+\s*=", l) for _, l in ctx.added_text(f.path)) if f.path.endswith(".py") else False
        if (watch(f.path) or default) and not told(f.path):
            yield f.path, "", f"this diff changes {f.path}{' (a DEFAULT_ constant)' if default else ''}, which the PR body never mentions"
    tops, said = defaultdict(list), set()
    for f in ctx.files.values(): tops[f.path.split("/")[0] if "/" in f.path else "."].append(f.path)
    if len(tops) > 1:
        for top, paths in sorted(tops.items()):
            if top in (".github", ".jx"): continue  # reported file by file above
            if top != "." and top not in body and not any(told(p) for p in paths):
                yield paths[0], "", f"this diff touches {top}/ ({len(paths)} files), which the PR body never mentions"


# ---------------------------------------------------------------- C. tests

def parse_yaml(text):
    """The YAML subset GitHub workflows use: block mappings and sequences, block scalars, flow lists, plain and quoted
    scalars. Anchors, tags and multi-document streams are not supported (a workflow that uses them parses partially)."""
    lines, pos = text.split("\n"), [0]
    ind = lambda l: len(l) - len(l.lstrip(" "))
    blank = lambda l: not l.strip() or l.lstrip().startswith("#")

    def skip():
        while pos[0] < len(lines) and blank(lines[pos[0]]): pos[0] += 1

    def uncomment(s):
        q = None
        for i, ch in enumerate(s):
            if q: q = None if ch == q else q
            elif ch in "'\"": q = ch
            elif ch == "#" and (i == 0 or s[i - 1] in " \t"): return s[:i].rstrip()
        return s.strip()

    def scalar(s):
        s = uncomment(s)
        if s.startswith("[") and s.endswith("]"): return [scalar(x) for x in s[1:-1].split(",") if x.strip()]
        if len(s) >= 2 and s[0] == s[-1] and s[0] in "'\"": return s[1:-1]
        return {"true": True, "false": False, "null": None, "~": None}.get(s, s)

    def keyval(s):
        m = re.match(r"""^("[^"]*"|'[^']*'|[^\s:'"][^:]*?)\s*:(?:\s+(.*)|\s*)$""", s)
        return (m[1].strip("'\""), m[2] or "") if m else None

    def block_scalar(parent, style):
        out = []
        while pos[0] < len(lines) and (not lines[pos[0]].strip() or ind(lines[pos[0]]) > parent):
            out.append(lines[pos[0]]); pos[0] += 1
        while out and not out[-1].strip(): out.pop()
        cut = min((ind(l) for l in out if l.strip()), default=0)
        body = [l[cut:] for l in out]
        return "\n".join(body) if style == "|" else re.sub(r"(?<!\n)\n(?!\n)", " ", "\n".join(body))

    def value(rest, parent):
        r = uncomment(rest)
        if r in ("|", "|-", "|+", ">", ">-", ">+"): return block_scalar(parent, r[0])
        if r == "":
            skip()
            if pos[0] < len(lines) and ind(lines[pos[0]]) > parent: return node(ind(lines[pos[0]]))
            if pos[0] < len(lines) and ind(lines[pos[0]]) == parent and lines[pos[0]].lstrip().startswith("- "): return node(parent)
            return None
        return scalar(r)

    def mapping(level, first=None):
        d = {}
        if first: k, rest = first; d[k] = value(rest, level)
        while True:
            skip()
            if pos[0] >= len(lines): return d
            l = lines[pos[0]]
            if ind(l) > level: pos[0] += 1; continue  # continuation of a multi-line plain scalar
            if ind(l) != level or l.lstrip().startswith("- ") or l.strip() == "-": return d
            kv = keyval(l.strip()); pos[0] += 1
            if kv: d[kv[0]] = value(kv[1], level)

    def sequence(level):
        out = []
        while True:
            skip()
            if pos[0] >= len(lines): return out
            l = lines[pos[0]]
            if ind(l) != level or not (l.lstrip().startswith("- ") or l.strip() == "-"): return out
            item = l.lstrip()[1:]; col = level + 1 + (len(item) - len(item.lstrip())); item = item.strip(); pos[0] += 1
            if not item: out.append(node(level + 1) if pos[0] < len(lines) else None); continue
            kv = keyval(item)
            out.append(mapping(col, kv) if kv and not item.startswith(("'", '"', "[")) else scalar(item))

    def node(_level=0):  # the level is read from the next non-blank line itself
        skip()
        if pos[0] >= len(lines): return None
        l = lines[pos[0]]
        return sequence(ind(l)) if l.lstrip().startswith("- ") or l.strip() == "-" else mapping(ind(l))

    try:
        return node(0)
    except (RecursionError, IndexError):
        return None


def _eval_if(expr, entry):
    if expr is None or expr is True: return True
    s = str(expr).strip()
    s = re.sub(r"^\$\{\{\s*(.*?)\s*\}\}$", r"\1", s)
    if re.fullmatch(r"(always|success)\(\)", s): return True
    if re.fullmatch(r"(failure|cancelled)\(\)", s): return False
    py = re.sub(r"\bmatrix\.([\w-]+)", lambda m: repr(entry.get(m[1])), s)
    py = py.replace("&&", " and ").replace("||", " or ").replace("!=", " __NE__ ").replace("!", " not ").replace(" __NE__ ", " != ")
    py = re.sub(r"\bstartsWith\(", "_sw(", py); py = re.sub(r"\bcontains\(", "_ct(", py); py = re.sub(r"\bendsWith\(", "_ew(", py)
    py = re.sub(r"\b(success|always)\(\)", "True", py); py = re.sub(r"\b(failure|cancelled)\(\)", "False", py)
    try: tree = ast.parse(py, mode="eval")
    except SyntaxError: return True
    fns = {"_sw": lambda a, b: str(a or "").startswith(str(b)), "_ew": lambda a, b: str(a or "").endswith(str(b)), "_ct": lambda a, b: str(b) in str(a or "")}

    def ev(n):
        if isinstance(n, ast.Expression): return ev(n.body)
        if isinstance(n, ast.Constant): return n.value
        if isinstance(n, ast.BoolOp): return all(ev(v) for v in n.values) if isinstance(n.op, ast.And) else any(ev(v) for v in n.values)
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not): return not ev(n.operand)
        if isinstance(n, ast.Compare) and len(n.ops) == 1:
            l, r = ev(n.left), ev(n.comparators[0])
            return (l == r) if isinstance(n.ops[0], ast.Eq) else (l != r) if isinstance(n.ops[0], ast.NotEq) else True
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in fns: return fns[n.func.id](*[ev(a) for a in n.args])
        raise ValueError
    try: return bool(ev(tree))
    except (ValueError, TypeError): return True


def _matrix(m):
    if not isinstance(m, dict): return [{}]
    keys = {k: v for k, v in m.items() if k not in ("include", "exclude") and isinstance(v, list)}
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*keys.values())] if keys else []
    inc = [e for e in m.get("include") or [] if isinstance(e, dict)]
    return combos + inc if combos or inc else [{}]


def _subst(s, entry, env):
    s = re.sub(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}", lambda m: " ".join(map(str, entry.get(m[1]))) if isinstance(entry.get(m[1]), list) else str(entry.get(m[1], "")), s)
    s = re.sub(r"\$\{\{\s*env\.([\w-]+)\s*\}\}", lambda m: str(env.get(m[1], "")), s)
    return re.sub(r"\$\{?([A-Za-z_]\w*)\}?", lambda m: str(env[m[1]]) if m[1] in env else m[0], s)


VALUE_OPTS = {"-k", "-m", "-p", "-c", "-o", "--basetemp", "--junitxml", "--rootdir", "--deselect", "--confcutdir", "-n", "--maxfail", "--durations", "--tb", "-W", "--timeout", "--log-level", "--cov", "--cov-report", "--cov-config"}


def pytest_invocations(cmd):
    """(mode, paths, ignores) for each test command in a shell script: pytest (run, partial or collect), and a test
    file run directly or through `-m unittest`, which runs it whole."""
    text = re.sub(r"\\\n\s*", " ", cmd)
    for seg in re.split(r"\n|;|&&|\|\|?", text):  # a pipe ends the command too: `pytest -q | tee log`
        direct = re.findall(r"(?:^|\s)python[\d.]*\s+(?:-\S+\s+)*([\w./-]*test_\w+\.py)\b", seg)
        unit = re.search(r"-m\s+unittest\b(.*)", seg)
        if direct or unit:
            yield "run", direct + ([t for t in unit.group(1).split() if not t.startswith("-")] if unit else []), []
            continue
        if not re.search(r"(^|[\s/])pytest(\s|$)", seg) or re.search(r"\binstall\b.*\bpytest\b", seg): continue  # `pip install pytest` runs nothing
        try: toks = shlex.split(seg)  # `-m "not slow"` is one value, not a path named slow
        except ValueError: toks = seg.replace("'", " ").replace('"', " ").split()
        i = next(k for k, t in enumerate(toks) if t.endswith("pytest"))
        args, paths, ignores, mode, skipnext = toks[i + 1:], [], [], "run", False
        for k, t in enumerate(args):
            if skipnext: skipnext = False; continue
            if t in ("--collect-only", "--co"): mode = "collect"
            elif t == "-k" or t.startswith("-k"): mode = "partial" if mode != "collect" else mode
            if t.startswith("--ignore="): ignores.append(t.split("=", 1)[1]); continue
            if t == "--ignore": ignores.append(args[k + 1] if k + 1 < len(args) else ""); skipnext = True; continue
            if re.fullmatch(r"\d*[<>]+", t): skipnext = True; continue  # `> log`, `2> err`
            if re.match(r"\d*[<>]", t): continue  # `2>&1`, `>log`
            if t in VALUE_OPTS: skipnext = True; continue
            if t.startswith("-") or t.startswith("$") or "=" in t: continue
            if "::" in t: mode = "partial" if mode == "run" else mode; t = t.split("::")[0]
            paths.append(t)
        yield mode, paths, ignores


RANK = {"collect": 1, "partial": 2, "run": 3}


def collect_ignores(ctx, tree_text):
    out = []
    for p in ctx.live:
        if base(p) != "conftest.py": continue
        try: t = ast.parse(tree_text(p) or "")
        except SyntaxError: continue
        for n in t.body:
            if isinstance(n, ast.Assign) and any(getattr(x, "id", "") == "collect_ignore" for x in n.targets) and isinstance(n.value, (ast.List, ast.Tuple)):
                d = p.rsplit("/", 1)[0] if "/" in p else ""
                out += [f"{d}/{e.value}".lstrip("/") for e in n.value.elts if isinstance(e, ast.Constant)]
    return out


def ci_selection(workflows, universe, ignores):
    sel, ignores = {}, [g.rstrip("/") for g in ignores]
    for wtext in workflows:
        wf = parse_yaml(wtext) or {}
        jobs = wf.get("jobs") if isinstance(wf, dict) else None
        for job in (jobs or {}).values():
            if not isinstance(job, dict): continue
            for entry in _matrix((job.get("strategy") or {}).get("matrix") if isinstance(job.get("strategy"), dict) else None):
                for step in job.get("steps") or []:
                    if not isinstance(step, dict) or "run" not in step or not _eval_if(step.get("if"), entry): continue
                    env = {k: _subst(str(v), entry, {}) for k, v in {**(job.get("env") or {}), **(step.get("env") or {})}.items()}
                    for mode, paths, extra in pytest_invocations(_subst(str(step["run"]), entry, env)):
                        drop = ignores + [g.rstrip("/") for g in extra]
                        targets = paths or [""]
                        for t in targets:
                            t = t.strip("/")
                            if any(ch in t for ch in "*?["): hit = [u for u in universe if fnmatch.fnmatch(u, t)]
                            elif t in universe: hit = [t]
                            else: hit = [u for u in universe if (not t or u.startswith(t + "/")) and not any((u == g or u.startswith(g + "/")) and not t.startswith(g) for g in drop)]
                            for u in hit:
                                if RANK[mode] > RANK.get(sel.get(u), 0): sel[u] = mode
    return sel


def _workflows(ctx, ref=None):
    if ref is None:
        return [ctx.text(p) or "" for p in sorted(ctx.tracked | ctx.live_set) if re.match(r"\.github/workflows/[^/]+\.ya?ml$", p) and (ctx.root / p).is_file()]
    names = run(["git", "-C", str(ctx.root), "ls-tree", "--name-only", ref, ".github/workflows/"], check=False).stdout.split()
    return [run(["git", "-C", str(ctx.root), "show", f"{ref}:{n}"], check=False).stdout for n in names if re.search(r"\.ya?ml$", n)]


def test_universe(ctx):
    return [p for p in ctx.live_files(".py") if base(p).startswith("test_") or base(p).endswith("_test.py")]


def ci_head(ctx):
    if not hasattr(ctx, "_ci_head"):
        wf = _workflows(ctx)
        ctx._ci_head = ci_selection(wf, test_universe(ctx), collect_ignores(ctx, ctx.text)) if wf else {}
    return ctx._ci_head


def tests_ci(ctx):
    uni = set(test_universe(ctx))
    added = [f.path for f in ctx.changed(".py", statuses="A") if f.path in uni]
    if not added or not _workflows(ctx): return
    sel = ci_head(ctx)
    for p in added:
        mode = sel.get(p)
        if mode is None: yield p, 1, "no CI workflow runs this new test file; add it to a job or say why it runs only locally"
        elif mode == "collect": yield p, 1, "CI only collects this new test file (--collect-only); its tests never execute there"


def tests_ci_diff(ctx):
    if not any(f.path.startswith(".github/workflows/") or base(f.path) == "conftest.py" for f in ctx.files.values()): return
    uni = test_universe(ctx)
    def old_text(p):  # a conftest this diff adds ignored nothing before; an unreadable base falls back to the live text
        if p in ctx.files and ctx.files[p].status == "A": return ""
        b = ctx.base_text(p)
        return ctx.text(p) if b is None else b
    old = ci_selection(_workflows(ctx, ctx.merge_base), uni, collect_ignores(ctx, old_text))
    new = ci_head(ctx)
    for p in sorted(old):
        if RANK[old[p]] >= 2 and RANK.get(new.get(p), 0) < RANK[old[p]]:
            yield p, 1, f"CI ran this file ({old[p]}) before this diff and now {'never runs it' if p not in new else 'only ' + new[p] + 's it'}", "block"


def _test_fn_changed(fn, f):
    return any(n in f.added for n in range(fn.lineno, (fn.end_lineno or fn.lineno) + 1))


def code_loads(ctx):
    if not hasattr(ctx, "_code_loads"):
        names, strings = set(), set()
        for p in ctx.live_files(".py", tests=False):
            tree = ctx.tree(p)
            for n in ast.walk(tree) if tree else []:
                if isinstance(n, ast.Name): names.add(n.id)
                elif isinstance(n, ast.Attribute): names.add(n.attr)
                elif isinstance(n, ast.Constant) and isinstance(n.value, str): strings.add(n.value)
                elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)): names.add(n.name)
        for p in ctx.live:
            if p.endswith((".sh", ".yml", ".yaml", ".env")): strings |= set(re.findall(r"[A-Z][A-Z0-9_]{3,}", ctx.text(p) or ""))
        ctx._code_loads = (names, strings)
    return ctx._code_loads


def tests_patch_targets(ctx):
    for f in ctx.changed(".py", tests=True):
        tree = ctx.tree(f.path)
        for n in ast.walk(tree) if tree else []:
            if not isinstance(n, ast.Call) or n.lineno not in f.added: continue
            cn, target, env = call_name(n), None, False
            strs = [a.value for a in n.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            if cn.endswith((".setattr", ".delattr")) and "monkeypatch" in cn:
                if len(n.args) >= 3 and len(strs) >= 1 and isinstance(n.args[1], ast.Constant): target = n.args[1].value
                elif n.args and isinstance(n.args[0], ast.Constant) and "." in str(n.args[0].value): target = n.args[0].value.rsplit(".", 1)[1]
            elif cn.endswith((".setenv", ".delenv")) and strs: target, env = strs[0], True
            elif cn in ("patch", "mock.patch", "unittest.mock.patch") and strs and "." in strs[0]: target = strs[0].rsplit(".", 1)[1]
            elif cn.endswith("patch.object") or cn == "?.object" and "patch" in ast.unparse(n.func):
                if len(n.args) >= 2 and isinstance(n.args[1], ast.Constant): target = n.args[1].value
            elif cn in ("patch.dict", "mock.patch.dict") and n.args and "environ" in ast.unparse(n.args[0]):
                for k in (n.args[1].keys if len(n.args) > 1 and isinstance(n.args[1], ast.Dict) else []):
                    if isinstance(k, ast.Constant) and isinstance(k.value, str):
                        names, strings = code_loads(ctx)
                        if k.value not in strings: yield f.path, n.lineno, f"patches env var `{k.value}`, which no non-test code reads"
            if not target or not isinstance(target, str): continue
            names, strings = code_loads(ctx)
            if env and target not in strings: yield f.path, n.lineno, f"sets env var `{target}`, which no non-test code reads"
            elif not env and target not in names: yield f.path, n.lineno, f"patches `{target}`, which no non-test code reads; the test may not exercise what it claims"


def junit_cases(d):
    import xml.etree.ElementTree as ET
    for x in Path(d).glob("**/*.xml"):
        try:
            for case in ET.parse(x).iter("testcase"): yield case
        except ET.ParseError: continue


def tests_skips(ctx):
    for f in ctx.changed(".py", tests=True):
        tree = ctx.tree(f.path)
        for n in ast.walk(tree) if tree else []:
            if isinstance(n, ast.Call) and n.lineno in f.added:
                cn, kws = call_name(n), {k.arg for k in n.keywords}
                if cn == "pytest.importorskip" and "reason" not in kws:
                    yield f.path, n.lineno, "importorskip without reason=: a missing dependency silently skips; say why, or fail in strict mode"
                elif cn == "pytest.skip" and not n.args and "reason" not in kws:
                    yield f.path, n.lineno, "pytest.skip() without a reason"
                elif re.fullmatch(r"(pytest\.)?mark\.(skip|skipif)|\?\.(skip|skipif)", cn) and "mark" in ast.unparse(n.func) and "reason" not in kws and not (cn.endswith("skip") and n.args):
                    yield f.path, n.lineno, f"{ast.unparse(n.func)} without reason="
    changed = {modname(f.path): f.path for f in ctx.changed(".py", tests=True)}
    if not changed: return
    for d in ctx.args.receipts or []:
        skipped = Counter()
        for case in junit_cases(d):
            if case.find("skipped") is None: continue
            cls = case.get("classname", "")
            hit = next((p for m, p in changed.items() if cls == m or cls.startswith(m + ".") or case.get("file", "") == p), None)
            if hit: skipped[hit] += 1
        for p, k in skipped.items(): yield p, 1, f"{k} tests in this changed file were skipped in receipt {d}; say which and why"


ASSERTISH = re.compile(r"assert|raises|fail|check|expect|verify|require|xfail|must|ensure", re.I)


def assert_helpers(ctx):
    """Names of functions in live test code that assert, raise-check or fail: calling one counts as asserting."""
    if not hasattr(ctx, "_assert_helpers"):
        out = set()
        for p in ctx.live_files(".py", tests=True):
            tree = ctx.tree(p)
            for fn in ast.walk(tree) if tree else []:
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) and not fn.name.startswith("test") and any(
                        isinstance(n, (ast.Assert, ast.Raise)) or (isinstance(n, ast.Call) and ASSERTISH.search(call_name(n))) for n in ast.walk(fn)):
                    out.add(fn.name)
        ctx._assert_helpers = out
    return ctx._assert_helpers


def tests_vacuous(ctx):
    helpers = assert_helpers(ctx)
    for f in ctx.changed(".py", tests=True):
        tree = ctx.tree(f.path)
        for fn in ast.walk(tree) if tree else []:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.name.startswith("test") or not _test_fn_changed(fn, f): continue
            asserted = False
            for n in ast.walk(fn):
                if isinstance(n, ast.Assert):
                    asserted = True
                    t = n.test
                    if isinstance(t, ast.Constant) and t.value: yield f.path, n.lineno, "asserts a constant; it can never fail"
                    elif isinstance(t, ast.Compare) and len(t.ops) == 1 and ast.dump(t.left) == ast.dump(t.comparators[0]):
                        yield f.path, n.lineno, f"compares `{ast.unparse(t.left)[:60]}` with itself; it can never fail"
                elif isinstance(n, ast.Call) and (ASSERTISH.search(call_name(n)) or call_name(n).split(".")[-1] in helpers): asserted = True
                elif isinstance(n, ast.With) and any(ASSERTISH.search(ast.unparse(i.context_expr)) for i in n.items): asserted = True
            if not asserted: yield f.path, fn.lineno, f"`{fn.name}` has no assertion, raises-check or assert helper"


# ---------------------------------------------------------------- D. python lints on changed code

BUILD_STEPS = {"env", "run_commands", "pip_install", "uv_pip_install", "apt_install", "run_function", "workdir", "dockerfile_commands",
               "pip_install_from_requirements", "pip_install_from_pyproject", "uv_sync", "poetry_install_from_file", "micromamba_install",
               "pip_install_private_repos", "entrypoint", "shell", "cmd"}
DEFERRED = {"add_local_file", "add_local_dir", "add_local_python_source"}
CTORS = {"debian_slim", "from_registry", "from_dockerfile", "micromamba", "from_id", "from_aws_ecr", "from_gcp_artifact_registry", "from_name"}


def _chain(call):
    seq, node = [], call
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        seq.append((node.func.attr, node)); node = node.func.value
    seq.reverse()
    return node, seq


def modal_order_findings(tree):
    """(line, message) for build steps after a deferred add_local_* in modal.Image chains and their reassignments."""
    out, states, consumed = [], {}, set()

    def walk_chain(call, start):
        root, seq = _chain(call)
        deferred = start
        for name, node in seq:
            consumed.add(id(node))
            copy = any(k.arg == "copy" and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords)
            if name in DEFERRED and not copy: deferred = True
            elif deferred and (name in BUILD_STEPS or (name in DEFERRED and copy)):
                out.append((node.lineno, f"`.{name}(...)` runs after a deferred `add_local_*`; Modal refuses this image at build time. "
                                         "Move it before the local mounts, or pass copy=True to the add_local_* call"))
        return deferred

    def image_root(call):
        root, seq = _chain(call)
        if isinstance(root, ast.Name) and root.id in states: return states[root.id]
        txt = ast.unparse(root) if root is not None else ""
        if re.search(r"(^|\.)Image$", txt) and seq and seq[0][0] in CTORS | DEFERRED | BUILD_STEPS: return False
        return None

    def visit_expr(e, target=None):
        for n in ast.walk(e):
            if isinstance(n, ast.Call) and id(n) not in consumed:
                start = image_root(n)
                if start is not None:
                    state = walk_chain(n, start)
                    if target is not None and n is e: states[target] = state

    def block(stmts):
        for s in stmts:
            if isinstance(s, ast.Assign) and len(s.targets) == 1 and isinstance(s.targets[0], ast.Name):
                visit_expr(s.value, s.targets[0].id)
            elif isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for d in s.decorator_list: visit_expr(d)
                block(s.body)
            else:
                for field in ("body", "orelse", "finalbody", "handlers"):
                    sub = getattr(s, field, None)
                    if isinstance(sub, list) and sub and isinstance(sub[0], (ast.stmt, ast.ExceptHandler)):
                        if field == "handlers": [block(h.body) for h in sub]
                        else: block(sub)
                for field in ("value", "test", "iter", "items", "targets"):
                    v = getattr(s, field, None)
                    for x in (v if isinstance(v, list) else [v]):
                        if isinstance(x, ast.withitem): visit_expr(x.context_expr)
                        elif isinstance(x, ast.AST): visit_expr(x)
    block(tree.body)
    return out


def py_modal_image_order(ctx):
    changed = {f.path for f in ctx.changed(".py")}
    for p in ctx.live_files(".py"):
        t = ctx.text(p) or ""
        if "add_local_" not in t: continue
        tree = ctx.tree(p)
        if tree is None: continue
        for line, msg in modal_order_findings(tree):
            yield p, line, msg if p in changed else msg + " (in a file this diff does not touch)", "block" if p in changed else "warn"


def _lint_files(ctx):
    for f in ctx.changed(".py", tests=False):
        tree = ctx.tree(f.path)
        if tree is not None: yield f, tree, parents(tree)


def _json_calls(tree):
    return (n for n in ast.walk(tree) if isinstance(n, ast.Call) and call_name(n) in ("json.loads", "json.load"))


def lint_json_recursion(tree, par):
    for n in _json_calls(tree):
        names = enclosing_try_handlers(n, par)
        if names and not names & {"RecursionError", "Exception", "BaseException", "*"} and names & {"ValueError", "JSONDecodeError", "TypeError"}:
            yield n.lineno, "json parse guarded only by ValueError: deeply nested input raises RecursionError and escapes; catch it too"


def lint_kill_eperm(tree, par):
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and call_name(n) in ("os.killpg", "os.kill", "killpg"):
            names = enclosing_try_handlers(n, par)
            if "ProcessLookupError" in names and not names & {"PermissionError", "OSError", "Exception", "BaseException", "*"}:
                yield n.lineno, "kill guarded only by ProcessLookupError: macOS raises PermissionError for a zombie group leader; handle both"


def lint_importerror(tree, _par):
    for n in ast.walk(tree):
        if isinstance(n, ast.Try) and any(isinstance(s, (ast.Import, ast.ImportFrom)) for s in n.body):
            for h in n.handlers:
                names = handler_names(ast.Try(body=[], handlers=[h], orelse=[], finalbody=[]))
                if "ImportError" in names and "ModuleNotFoundError" not in names:
                    yield h.lineno, "`except ImportError` also treats a broken install as missing; catch ModuleNotFoundError for an optional dependency"


def lint_timeout(tree, par):
    uses_popen = any(isinstance(n, ast.Attribute) and n.attr == "Popen" for n in ast.walk(tree))
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not any(k.arg == "timeout" for k in n.keywords): continue
        cn = call_name(n)
        sub = cn in ("subprocess.run", "subprocess.check_output", "subprocess.check_call", "subprocess.call") or cn.endswith(".communicate") \
            or (cn.endswith(".wait") and uses_popen and not re.search(r"event|thread|cond|barrier|lock", cn, re.I))
        if not sub: continue
        names = enclosing_try_handlers(n, par)
        if not names & {"TimeoutExpired", "SubprocessError", "Exception", "BaseException", "*"}:
            yield n.lineno, f"`{cn}(timeout=...)` raises TimeoutExpired; handle it (and kill and reap the child) or let the caller know it can"


SWEEPABLE = {"py.json-recursion": lint_json_recursion, "py.kill-eperm": lint_kill_eperm, "py.importerror": lint_importerror,
             "py.timeout-unhandled": lint_timeout}


def _sweep(ctx, lint):
    """Findings of `lint` on added lines; and, when this diff fixed an instance in a file, the same pattern elsewhere."""
    fixed_in = []
    for f, tree, par in _lint_files(ctx):
        for line, msg in lint(tree, par):
            if line in f.added: yield f.path, line, msg
        bt = ctx.base_tree(f.path)
        if bt is not None and len(list(lint(bt, parents(bt)))) > len(list(lint(tree, par))): fixed_in.append(f.path)
    if not fixed_in: return
    for p in ctx.live_files(".py", tests=False):
        if p in fixed_in: continue
        tree = ctx.tree(p)
        if tree is None: continue
        for line, msg in lint(tree, parents(tree)):
            yield p, line, f"{msg} (this diff fixed the same pattern in {fixed_in[0]})"


def py_json_recursion(ctx): yield from _sweep(ctx, lint_json_recursion)
def py_kill_eperm(ctx): yield from _sweep(ctx, lint_kill_eperm)
def py_importerror(ctx): yield from _sweep(ctx, lint_importerror)
def py_timeout(ctx): yield from _sweep(ctx, lint_timeout)


def py_isfinite(ctx):
    for f, tree, par in _lint_files(ctx):
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and call_name(n) in ("math.isfinite", "math.isnan", "math.isinf", "isfinite", "isnan", "isinf") and n.lineno in f.added:
                line = ctx.lines(f.path)[n.lineno - 1]
                if not re.search(r"isinstance\([^)]*float|type\([^)]*\)\s+is\s+float", line):
                    yield f.path, n.lineno, f"`{call_name(n)}` converts ints to float and raises OverflowError past ~1e308 (10**400 in JSON); check `type(x) is float` first"


def _fractional(v):
    return type(v) is float and v == v and abs(v) != float("inf") and v != int(v)


def py_float_threshold(ctx):
    """An ordered comparison of a float ratio with a fractional threshold: the boundary case can land either side."""
    for f, tree, par in _lint_files(ctx):
        consts = {}
        for s in tree.body:  # NAME = 0.97 and NAME, OTHER = 0.97, 31
            for tgt in s.targets if isinstance(s, ast.Assign) else []:
                pairs = zip(tgt.elts, s.value.elts) if isinstance(tgt, ast.Tuple) and isinstance(s.value, ast.Tuple) else [(tgt, s.value)]
                consts.update({n.id: v.value for n, v in pairs if isinstance(n, ast.Name) and isinstance(v, ast.Constant)})
        frac = lambda x: _fractional(x.value) if isinstance(x, ast.Constant) else isinstance(x, ast.Name) and _fractional(consts.get(x.id))
        ratio = lambda x: (isinstance(x, ast.BinOp) and isinstance(x.op, ast.Div)) or bool(re.search(r"ratio|frac|share", ast.unparse(x), re.I))
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Compare) and n.lineno in f.added and len(n.ops) == 1
                    and isinstance(n.ops[0], (ast.Lt, ast.LtE, ast.Gt, ast.GtE))): continue
            a, b = n.left, n.comparators[0]
            if (frac(a) and ratio(b)) or (frac(b) and ratio(a)):
                yield f.path, n.lineno, (f"`{ast.unparse(n)[:80]}` compares a float ratio with a threshold; (97/1016)/(100/1016) "
                                         "is 0.96999…, so a ratio exactly on the boundary can fall on the wrong side. Compare "
                                         "integer counts (Fraction(c * n_a, r * n_p) < Fraction(97, 100)) or state the tolerance")


def py_sigalrm(ctx):
    for f, tree, par in _lint_files(ctx):
        threads = any((isinstance(n, ast.Attribute) and n.attr in ("Thread", "ThreadPoolExecutor")) or
                      (isinstance(n, ast.Name) and n.id in ("Thread", "ThreadPoolExecutor")) or
                      (isinstance(n, (ast.Import, ast.ImportFrom)) and re.search(r"threading|concurrent", ast.unparse(n))) for n in ast.walk(tree))
        if not threads: continue
        for n in ast.walk(tree):
            alarm = (isinstance(n, ast.Call) and call_name(n) in ("signal.alarm", "signal.setitimer")) or (isinstance(n, ast.Attribute) and n.attr == "SIGALRM")
            if alarm and n.lineno in f.added:
                yield f.path, n.lineno, "SIGALRM timers work only on the main thread, and this module also runs threads or executors; work in a thread is not bounded by it"
                break


SHM = "/dev/" + "shm"


def py_dev_shm(ctx):
    for f, tree, par in _lint_files(ctx):
        for n in ast.walk(tree):
            if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith(SHM) and n.lineno in f.added:
                yield f.path, n.lineno, SHM + " is memory-backed and its size is host-dependent (often small in containers); put large data on disk"


VALIDATOR = re.compile(r"^_?(require|check|validate|verify|assert|ensure)\w*$")


def py_broad_except(ctx):
    for f, tree, par in _lint_files(ctx):
        for n in ast.walk(tree):
            if not isinstance(n, ast.Try) or not (n.lineno in f.added or any(h.lineno in f.added for h in n.handlers)): continue
            if not handler_names(n) & {"Exception", "ValueError", "*", "BaseException"}: continue
            calls = [c for s in n.body for c in ast.walk(s) if isinstance(c, ast.Call) and VALIDATOR.match(call_name(c).split(".")[-1])]
            if calls:
                yield f.path, n.lineno, f"this broad except also swallows `{call_name(calls[0])}(...)` failing inside the try; narrow the try to the call that can fail"


def _json_files(ctx, pattern):
    rx = fnmatch.translate(pattern)
    hits = [p for p in sorted(ctx.tracked | ctx.live_set) if re.match(rx, base(p))]
    return hits[:5]


def py_json_subscript(ctx):
    for f, tree, par in _lint_files(ctx):
        bound = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name) and isinstance(n.value, ast.Call) and call_name(n.value) in ("json.loads", "json.load"):
                bound[n.targets[0].id] = n
        for n in ast.walk(tree):
            if not isinstance(n, ast.Subscript) or n.lineno not in f.added or not isinstance(n.slice, ast.Constant) or not isinstance(n.slice.value, str): continue
            v = n.value
            if not ((isinstance(v, ast.Call) and call_name(v) in ("json.loads", "json.load")) or (isinstance(v, ast.Name) and v.id in bound)): continue
            key = n.slice.value
            scope, cur = tree, n
            while cur in par:
                cur = par[cur]
                if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)): scope = cur; break
            pats = set()
            for c in ast.walk(scope):
                if isinstance(c, ast.Constant) and isinstance(c.value, str) and c.value.endswith(".json"): pats.add(base(c.value))
                elif isinstance(c, ast.JoinedStr):
                    parts = ["*" if not isinstance(x, ast.Constant) else str(x.value) for x in c.values]
                    s = re.sub(r"\*+", "*", "".join(parts))
                    if s.endswith(".json"): pats.add(base(s))
            files = [h for p in sorted(pats) for h in _json_files(ctx, p)][:5]
            verdict = None
            for p in files:
                try: data = json.loads((ctx.root / p).read_text())
                except (OSError, ValueError, RecursionError): continue
                if isinstance(data, list): verdict = f"{p} (a real file this code may read) is a JSON list, so `[{key!r}]` raises TypeError"; break
                if isinstance(data, dict) and key not in data: verdict = f"{p} (a real file this code may read) has no key {key!r}"; break
            if verdict: yield f.path, n.lineno, verdict
            elif not files and isinstance(v, ast.Call):
                yield f.path, n.lineno, f"`[{key!r}]` assumes a JSON shape; which producer writes it? Test against a real file, not a hand-made one", "ask"


def py_unused(ctx):
    for f, tree, par in _lint_files(ctx):
        old = {getattr(n, "name", None) for n in ast.walk(ctx.base_tree(f.path))} if ctx.base_tree(f.path) else set()
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name in old or fn.lineno not in f.added or fn.decorator_list: continue
            body = [s for s in fn.body if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))]
            if not body or all(isinstance(s, (ast.Pass, ast.Raise)) or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant)) for s in body): continue
            if isinstance(par.get(fn), ast.ClassDef) and par[fn].bases: continue
            used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            for a in fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs:
                if a.arg not in used and a.arg not in ("self", "cls") and not a.arg.startswith("_"):
                    yield f.path, fn.lineno, f"`{fn.name}` never reads its parameter `{a.arg}`"


# ---------------------------------------------------------------- E. hygiene, scope, commits

def hygiene_diff_check(ctx):
    p = run(["git", "-C", str(ctx.root), "-c", "core.quotePath=false", "diff", "--check", ctx.merge_base], check=False)
    for line in p.stdout.splitlines():
        m = re.match(r"^(.+?):(\d+): (.+)$", line)
        if m and m[1] in ctx.files and not ctx.files[m[1]].excluded: yield m[1], int(m[2]), m[3]


SECRET_BLOCK = [re.compile(x) for x in (
    r"\bBearer\s+[A-Za-z0-9._~+/=-]{24,}", r"\bsk-(?:proj-|live-|ant-)?[A-Za-z0-9_-]{20,}", r"\bgh[pousr]_[A-Za-z0-9]{30,}",
    r"\bAKIA[0-9A-Z]{16}\b", r"\bxox[baprs]-[A-Za-z0-9-]{10,}", r"\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"(?i)\b(api[_-]?key|secret|token|password|passwd)\b\s*[:=]\s*['\"][A-Za-z0-9/+_.=-]{16,}['\"]")]
SECRET_WARN = re.compile(r"(?<![A-Za-z0-9/+=_-])(?=[A-Za-z0-9+/_-]*[A-Z])(?=[A-Za-z0-9+/_-]*[a-z])(?=[A-Za-z0-9+/_-]*\d)[A-Za-z0-9+/_-]{40,}={0,2}(?![A-Za-z0-9/+=_-])")
def entropic(tok):
    """A random-looking token: not a path of words, and at least ~4.2 bits of entropy per character."""
    import math
    if len(re.findall(r"[a-z]{4,}", tok)) >= 2 or re.search(r"[A-Za-z_]{3,}/[A-Za-z_]{3,}", tok): return False
    counts = Counter(tok)
    return -sum(c / len(tok) * math.log2(c / len(tok)) for c in counts.values()) >= 4.2


FAKE = re.compile(r"(?i)synthetic|example|dummy|fake|placeholder|redacted|xxxx|<[^>]+>|\$\{|test[-_]?(key|token|secret)|changeme")


def hygiene_secrets(ctx):
    allow = matcher(ctx.config.get("secret_allow") or [])
    for f in ctx.changed(live=False):
        if allow(f.path): continue
        for n, l in ctx.added_text(f.path):
            if FAKE.search(l): continue
            hit = next((rx for rx in SECRET_BLOCK if rx.search(l)), None)
            if hit: yield f.path, n, f"looks like a credential ({hit.pattern[:40]}…); remove it and rotate it if it is real", "block"
            elif any(entropic(m[0]) for m in SECRET_WARN.finditer(l)) and not re.search(r"sha|digest|hash|integrity|base64|checksum", l, re.I):
                yield f.path, n, "a long high-entropy token; if it is a credential, remove and rotate it, else add the path to secret_allow"


def scope_owned(ctx):
    owned, ro = matcher(ctx.args.owned or []), matcher(ctx.args.readonly or [])
    for f in ctx.files.values():
        if ctx.args.owned and not owned(f.path): yield f.path, "", "outside the --owned paths this change may touch"
        if ctx.args.readonly and ro(f.path): yield f.path, "", "inside a --readonly path this change must not touch"


CONVENTIONAL = re.compile(r"^\w+(\([^)]*\))?!?: ")


def git_commits(ctx):
    log = run(["git", "-C", str(ctx.root), "log", "--no-merges", "--format=%h%x09%s", f"{ctx.merge_base}..HEAD"], check=False).stdout
    rows = [l.split("\t", 1) for l in log.splitlines() if "\t" in l]
    if len(rows) < 3: return
    pref = [r for r in rows if CONVENTIONAL.match(r[1])]
    if len(pref) * 3 >= len(rows) * 2 and len(pref) < len(rows):
        for h, s in rows:
            if not CONVENTIONAL.match(s): yield "", "", f"commit {h} '{s[:70]}' has no type(scope): prefix, unlike {len(pref)} of the {len(rows)} commits on this branch"


def hygiene_big_files(ctx):
    for f in ctx.changed(statuses="A", live=False):
        p = ctx.root / f.path
        if p.is_file() and p.stat().st_size > 1_000_000:
            yield f.path, "", f"adds {p.stat().st_size / 1e6:.1f} MB; keep large artifacts out of git or say why they belong"


# ---------------------------------------------------------------- F. asks

PROMPTS = [
    ("prompt.lifecycle", r"subprocess|Popen|killpg|os\.kill\(|signal\.|\.terminate\(|timeout=|atexit|app stop|Thread\(|ThreadPoolExecutor",
     "process or remote-app lifecycle changed: is evidence saved before terminate? is there a cleanup reserve inside the deadline? "
     "are stops retried and idempotent ('already stopped' counts)? do timeouts tolerate a slow control plane? can a watchdog race the main loop?"),
    ("prompt.credentials", r"(?i)redact|secret|token|authorization|api[_-]?key|jwt|bearer|credential|password|modal-key",
     "credential handling changed: can the secret reach argv, logs, wrapped console output, saved artifacts or receipts? "
     "is redaction tested with a realistic token length and with line wrapping?"),
    ("prompt.parser", r"json\.loads?\(|splitlines\(|re\.(match|search|finditer|fullmatch)\(|\.split\(",
     "parser of external input changed: what do deep nesting, huge ints, NaN/Infinity, BOM, CRLF, invalid UTF-8, truncation and duplicates do? "
     "does a contradiction still fail when another part is missing (fail outranks pending)?"),
    ("prompt.command-gate", r"PreToolUse|permissionDecision|tool_input|shlex\.(split|shlex)\(",
     "code that allows or refuses shell commands changed: is every command of a call checked (`;`, `&&`, `||`, `|`, "
     "subshells, `sh -c`, each call of a wrapper), not only the first match? does a quoted argument stay whole, a "
     "multi-line one included? is the directory the command runs in used (the tool's workdir field, `cd`, `git -C`)? "
     "do explicit selectors (a PR number, --repo, --head, a push refspec) choose what is checked, not the current "
     "branch? can an earlier command of the same call (a commit, a checkout) change what gets sent?"),
    ("prompt.references", r"normpath|relpath|\.resolve\(\)|PurePosixPath|#L\d|\bcit(?:e|es|ation)\b|line_?refs?|\branges\b|\binterval",
     "code that resolves paths or line references changed: does a relative mention (`./x`, `../x`) resolve the same as "
     "its normalized form, looked up in the same set of files? is a range (`3-8`, `#L3-L8`) checked over every line "
     "inside it, not only its ends? do comma lists stay separate points?"),
    ("prompt.external-sdk", r"\bmodal\.|boto3|httpx|requests\.|\[\s*['\"](modal|gh|kubectl|aws|terraform)['\"]",
     "external SDK or CLI use changed: was it exercised against the real library offline (its resolver, the argv the real tool produces), "
     "not only a recording fake that accepts any call order?"),
]


def prompts(ctx):
    for pid, rx, text in PROMPTS:
        r = re.compile(rx)
        hit = next(((f.path, n) for f in ctx.changed(".py", tests=False) for n, l in ctx.added_text(f.path) if r.search(l)), None)
        if hit:
            yield hit[0], hit[1], f"[{pid}] {text}"
    tests = "\n".join(ctx.text(p) or "" for p in ctx.live_files(".py", tests=True))
    for f in ctx.changed(".py", tests=False):
        old = {}
        bt = ctx.base_tree(f.path)
        for n in bt.body if bt else []:
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, (int, float)) and not isinstance(n.value.value, bool):
                for t in n.targets:
                    if isinstance(t, ast.Name): old[t.id] = n.value.value
        tree = ctx.tree(f.path)
        for n in tree.body if tree else []:
            if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant) and isinstance(n.value.value, (int, float)) and not isinstance(n.value.value, bool) and n.lineno in f.added:
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id.isupper() and old.get(t.id) != n.value.value and not re.search(rf"\b{t.id}\b", tests):
                        yield f.path, n.lineno, f"[prompt.constants] {t.id} = {n.value.value!r} is new or changed and no test names it; would a test notice a wrong value?"


REQ = re.compile(r"(?i)\b(must|requires?|required|shall|need(s|ed)?|asks? for|at least|at most|never|only|no \w+|every|each|both|exactly)\b")
GENERIC = {"test", "tests", "evidence", "check", "checks", "utils", "util", "helpers", "helper", "main", "init", "py", "md", "docs", "port", "the", "and", "for"}


def spec_ledger(ctx):
    docs = [d for d in ctx.config.get("governing_docs") or [] if (ctx.root / d).is_file()]
    if not docs: return
    topics = {t.lower() for t in ctx.args.topic or []}
    explicit = set(topics)
    for f in ctx.changed(statuses="A"):
        if f.path.endswith((".py", ".md")) and not ctx.is_test(f.path):
            topics |= {t for t in re.split(r"[_\-.]", stem(f.path).lower()) if len(t) >= 4 and t not in GENERIC}
    auto = {t for t in topics if t not in {x.lower() for x in ctx.args.topic or []}}
    if auto:
        text = "\n".join(ctx.text(d) or "" for d in docs).lower()
        topics -= {t for t in auto if len(re.findall(rf"\b{re.escape(t)}\b", text)) > 12}  # too common to point at one requirement
    if not topics: return
    trx = re.compile(r"(?i)\b(" + "|".join(re.escape(t) for t in sorted(topics, key=len, reverse=True)) + r")\b")
    erx = re.compile(r"(?i)\b(" + "|".join(re.escape(t) for t in sorted(explicit, key=len, reverse=True)) + r")\b") if explicit else None
    out = []
    for d in docs:
        if d in {f.path for f in ctx.files.values()}: continue
        heading_hit, level = False, 0
        for i, line in enumerate(ctx.lines(d), 1):
            m = re.match(r"^(#{1,6})\s+(.*)", line) or re.match(r"^\s*-\s+\*\*\(([a-z])\)\s+(.*)", line)
            if m and line.startswith("#"):
                lv = len(m[1])
                if heading_hit and lv <= level: heading_hit = False
                if trx.search(m[2]): heading_hit, level = True, lv
                continue
            item = re.match(r"^\s*(?:[-*+]|\d+[.)]|\|)\s+\S", line)
            if (heading_hit and item) or (erx and erx.search(line) and REQ.search(line)):
                out.append((0 if heading_hit else 1, d, i, line.strip()))
    out.sort()
    for _, d, i, line in out[:25]:
        yield d, i, f"requirement to account for: {line[:220]}"


CHECKS = [
    Check("refs.removed", "fast", "warn", "PR 59, 48 ⇢", "removed or renamed defs, env vars, CLI flags and files still referenced elsewhere", refs_removed),
    Check("refs.old-literal", "fast", "warn", "PR 59, 31 ⇢", "the old value of a changed constant still named as the default/current one", refs_old_literal),
    Check("refs.registry", "fast", "warn", "PR 31, 52, 54, 55, 67, 69, 72, 73", "a new file missing from a doc or workflow that lists its siblings", refs_registry),
    Check("refs.old-pattern", "fast", "warn", "PR 75, 80 r4", "a line this diff removed (a fixed pattern) that still exists elsewhere", refs_old_pattern),
    Check("refs.callers", "fast", "block", "PR 80 r3", "call sites that no longer match a changed signature, ci-only dirs included", refs_callers),
    Check("refs.orphan", "fast", "warn", "PR 59, 62, 76", "a new lock or module nothing references; new code only tests call", refs_orphan),
    Check("docs.cochange", "fast", "ask", "PR 52, 54, 55, 76 ⇢", "a doc that historically changes with a changed file but did not change", docs_cochange),
    Check("docs.namesake", "fast", "ask", "PR 116", "a module's namesake doc beside it that names what the diff changed and did not change", docs_namesake),
    Check("env.undocumented", "fast", "warn", "PR 51, 52", "a newly read env var that no doc names", env_undocumented),
    Check("pins.drift", "fast", "warn", "PR 59, 76 ⇢", "a package pinned at different versions across requirements, workflows, images and docs", pins_drift),
    Check("dup.helpers", "fast", "warn", "PR 31, 54, 56, 59, 69", "a new function that near-copies an existing one under another name; hand-rolled hashing", dup_helpers),
    Check("dup.constants", "fast", "warn", "PR 116", "a new path constant another non-test module already defines", dup_constants),
    Check("imports.private", "fast", "warn", "PR 62, 67, 73, 76", "private names imported across modules; tests importing other test modules", imports_private),
    Check("cite.resolve", "fast", "warn", "PR 84, 67, 74", "path:line citations in changed text that do not resolve, are ambiguous or out of range", cite_resolve),
    Check("cite.drift", "fast", "warn", "PR 31, 85 r1", "citations into changed files whose lines moved or changed, here or when the base merges", cite_drift),
    Check("cite.content", "fast", "warn", "PR 67, 74", "a cited line that does not contain the identifier the citation is about", cite_content),
    Check("cite.paths", "fast", "warn", "PR 31, 65, 70", "paths named in changed docs or the body that do not exist or are not tracked", cite_paths),
    Check("claims.counts", "fast", "warn", "PR 56, 70, 54", "test counts in the body that no fresh `jx check` receipt backs", claims_counts),
    Check("claims.shas", "fast", "warn", "PR 54, 56, 87", "commits in the body that are missing, off-branch or not the current head", claims_shas),
    Check("claims.absolute", "fast", "ask", "PR 52, 58, 67, 73, 84", "absolute verification claims without an evidence pointer", claims_absolute),
    Check("claims.undisclosed", "fast", "ask", "PR 51, 52, 80 r2, 87", "workflow, config, governing-doc or DEFAULT_ changes the body never mentions", claims_undisclosed),
    Check("tests.ci", "fast", "warn", "PR 52, 54, 80 r2", "a new test file no CI workflow runs (or only collects)", tests_ci),
    Check("tests.ci-diff", "fast", "block", "PR 87", "test files CI ran before this diff and no longer runs", tests_ci_diff),
    Check("tests.patch-targets", "fast", "warn", "PR 51", "patch/setattr/setenv targets that no non-test code reads", tests_patch_targets),
    Check("tests.skips", "fast", "warn", "PR 52, 54", "skips without a reason; skips of changed tests in the receipts", tests_skips),
    Check("tests.vacuous", "fast", "warn", "PR 52", "self-comparisons, constant asserts and tests without an assertion", tests_vacuous),
    Check("py.modal-image-order", "fast", "block", "PR 88", "a modal.Image build step after a deferred add_local_* (Modal refuses the image)", py_modal_image_order),
    Check("py.json-recursion", "fast", "warn", "PR 68, 69", "json parsing guarded only by ValueError (RecursionError escapes)", py_json_recursion),
    Check("py.isfinite-int", "fast", "warn", "PR 84, 85", "math.isfinite/isnan on values that may be huge ints (OverflowError)", py_isfinite),
    Check("py.float-threshold", "fast", "warn", "PR 99", "a float ratio compared with a fractional threshold (boundary cases flip)", py_float_threshold),
    Check("py.kill-eperm", "fast", "warn", "PR 75 ⇢", "kill/killpg guarded only by ProcessLookupError (PermissionError on a zombie leader)", py_kill_eperm),
    Check("py.importerror", "fast", "warn", "PR 54", "`except ImportError` around an optional import (hides broken installs)", py_importerror),
    Check("py.sigalrm-threads", "fast", "warn", "PR 80 r2", "SIGALRM timers in a module that runs threads or executors", py_sigalrm),
    Check("py.dev-shm", "fast", "warn", "PR 80 r5", "data written to memory-backed /dev/shm", py_dev_shm),
    Check("py.timeout-unhandled", "fast", "warn", "PR 59, 75", "subprocess timeouts without TimeoutExpired handling", py_timeout),
    Check("py.broad-except-validation", "fast", "warn", "PR 80 r4", "a broad except that also swallows the code's own require/check failures", py_broad_except),
    Check("py.json-subscript", "fast", "warn", "PR 89", "a JSON subscript whose real files have another shape (or no known producer)", py_json_subscript),
    Check("py.unused", "fast", "warn", "PR 51, 85", "parameters of new functions that are never read", py_unused),
    Check("hygiene.diff-check", "fast", "warn", "PR 31, 76", "whitespace errors (`git diff --check`)", hygiene_diff_check),
    Check("hygiene.secrets", "fast", "warn", "CONTEXT rules", "credential-shaped strings in added lines", hygiene_secrets),
    Check("scope.owned", "fast", "block", "briefs of PR 84-86", "changes outside --owned or inside --readonly", scope_owned),
    Check("git.commits", "fast", "warn", "PR 68", "a commit subject that breaks the branch's own prefix convention", git_commits),
    Check("hygiene.big-files", "fast", "warn", "-", "added files over 1 MB", hygiene_big_files),
    Check("prompts", "fast", "ask", "PR 59, 75, 76, 80, 68, 69, 84-86, 88", "topic checklists triggered by what the diff touches; untested constants", prompts),
    Check("spec.ledger", "fast", "ask", "PR 84, 85, 86, 48, 72, 73, 32, 59", "governing-doc requirements matching --topic or new file names", spec_ledger),
]
