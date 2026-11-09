#!/usr/bin/env python3
"""jx preflight — the checks reviewers kept running after the PR opened, run by the author before it does. Each check
names the past review miss it came from; `jx preflight ls` prints the catalogue.

  jx preflight run [--base origin/main] [--body DRAFT.md | --pr N] [--receipts DIR ...] [--topic T ...] [--full]
      [--budget 600] [--owned GLOB ...] [--readonly GLOB ...] [--ledger FILE] [--only ID,..] [--skip ID,..]
      [--strict] [--out DIR] [--dry-run]                    findings TSV: id, check, severity, path, line, message, origin,
                                                            disposition; exit 1 while a block (or, with --strict, an ask) is open
  jx preflight ls                                           every check: id, tier, severity, origin, what it finds
  jx preflight doctor                                       git, base ref, repo config, pack scripts, test python

Severity: block = definite defect; warn = likely defect, look at it; ask = needs a written disposition; info = summary.
Scope: the diff is merge-base(--base, HEAD) against the working tree, plus untracked files. Searches run over the "live
set": tracked and untracked files minus `frozen_lists` (sha256 manifests whose files never change) and `excludes`.
The fast tier is static. --full adds test collection, a fix-removal run (changed tests against base code) and mutation
of changed lines (--budget seconds), each in temporary `git worktree`s that are removed afterwards.
Dispositions: a ledger line `- <id> <reason>` (the run writes a template to OUT/ledger.md; pass it back with --ledger),
or a standing line `- <check-glob> <path-glob>: <reason>` in the repo's dispositions file. A disposed finding never fails
the run. Repo config: `.jx/preflight.json` (frozen_lists, excludes, governing_docs, test_commands, test_python,
ci_only_dirs, status_vocab, secret_allow, dispositions, checks). `checks` lists repo-pack scripts run as
`<cmd> OUT/change.json`; each prints rows `check<TAB>severity<TAB>path<TAB>line<TAB>message<TAB>origin`.
"""
import argparse, fnmatch, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, tempfile, time
from pathlib import Path

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
sys.path.insert(0, str(Path(__file__).resolve().parent))
SEVERITIES = ("block", "warn", "ask", "info")
HEADER = ["id", "check", "severity", "path", "line", "message", "origin", "disposition"]
DEFAULTS = {"frozen_lists": [], "excludes": [".claude/worktrees/**", "node_modules/**", ".venv*/**"], "governing_docs": [],
            "test_commands": [], "test_python": "", "ci_only_dirs": [], "status_vocab": ["pass", "fail", "pending"],
            "secret_allow": [], "dispositions": ".jx/preflight/DISPOSITIONS.md", "checks": [], "workers": 2}


class Missing(Exception):
    """A required executable is not on PATH."""


def run(argv, cwd=None, ok=(0,), timeout=None, env=None, check=True):
    try:
        p = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=timeout, env=env)
    except FileNotFoundError:
        raise Missing(argv[0])
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"{shlex.join(map(str, argv))} did not finish in {timeout}s")
    if check and p.returncode not in ok:
        raise RuntimeError(f"{shlex.join(map(str, argv))} exited {p.returncode}: {(p.stderr or p.stdout).strip()[:500]}")
    return p


def git(root, *args, ok=(0,)):
    return run(["git", "-C", str(root), *args], ok=ok).stdout


def glob_re(pattern):
    """Compile a git-style glob: `**` spans directories, `*` and `?` stay inside one; a slash-free pattern matches a basename."""
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i): out += "(?:.*/)?"; i += 3
        elif pattern.startswith("**", i): out += ".*"; i += 2
        elif pattern[i] == "*": out += "[^/]*"; i += 1
        elif pattern[i] == "?": out += "[^/]"; i += 1
        else: out += re.escape(pattern[i]); i += 1
    return re.compile(out + r"\Z")


def matcher(patterns):
    compiled = [(glob_re(p), "/" not in p) for p in patterns]
    return lambda path: any(rx.match(path) or (base and rx.match(path.rsplit("/", 1)[-1])) for rx, base in compiled)


class FileChange:
    def __init__(self, path, status, old_path=None):
        self.path, self.status, self.old_path = path, status, old_path or path
        self.added, self.removed_old, self.hunks, self.binary, self.excluded = set(), set(), [], False, False

    def map_line(self, n):
        """New line number of old line n, or None when this diff removed or rewrote it."""
        shift = 0
        for os_, ol, _ns, nl in self.hunks:
            if ol == 0:
                if n <= os_: break
                shift += nl; continue
            if n < os_: break
            if n < os_ + ol: return None
            shift += nl - ol
        return n + shift

    def as_json(self):
        return dict(path=self.path, status=self.status, old_path=self.old_path, excluded=self.excluded, binary=self.binary,
                    added=sorted(self.added), removed_old=sorted(self.removed_old), hunks=self.hunks)


HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_diff(text):
    """Files of a `git diff -U0 --find-renames` patch: status, renames, added (new side) and removed (old side) lines."""
    files, cur, old_n, new_n = [], None, 0, 0
    for line in text.splitlines():
        if line.startswith("diff --git "):
            m = re.match(r'diff --git "?a/(.*?)"? "?b/(.*?)"?$', line)
            cur = FileChange(m.group(2), "M", m.group(1)) if m else FileChange(line.split()[-1][2:], "M")
            files.append(cur); continue
        if cur is None: continue
        if line.startswith("new file mode"): cur.status = "A"
        elif line.startswith("deleted file mode"): cur.status = "D"
        elif line.startswith("rename from "): cur.status, cur.old_path = "R", line[12:]
        elif line.startswith("rename to "): cur.path = line[10:]
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"): cur.binary = True
        elif line.startswith("+++ ") and line[4:] != "/dev/null": cur.path = line[6:] if line[4:6] == "b/" else line[4:]
        elif line.startswith("--- ") or line.startswith("+++ "): pass
        elif (m := HUNK.match(line)):
            os_, ol, ns, nl = int(m[1]), int(m[2] or 1) if m[2] != "0" else 0, int(m[3]), int(m[4] or 1) if m[4] != "0" else 0
            cur.hunks.append([os_, ol, ns, nl]); old_n, new_n = os_, ns
        elif line.startswith("+"): cur.added.add(new_n); new_n += 1
        elif line.startswith("-"): cur.removed_old.add(old_n); old_n += 1
    return files


class Change:
    """Everything a check reads: the diff, file texts at both ends, ASTs and the live set, loaded lazily and cached."""

    def __init__(self, root, args, config):
        self.root, self.args, self.config = Path(root), args, config
        self.base = args.base
        self._text, self._base, self._ast, self._base_ast = {}, {}, {}, {}
        if self.base.startswith("origin/"):
            p = run(["git", "-C", str(root), "fetch", "-q", "origin", self.base[7:]], check=False, timeout=60)
            if p.returncode: print(f"preflight: fetch of {self.base} failed; using the local ref", file=sys.stderr)
        self.base_tip = git(root, "rev-parse", "--verify", f"{self.base}^{{commit}}").strip()
        self.head = git(root, "rev-parse", "HEAD").strip()
        self.merge_base = git(root, "merge-base", self.base_tip, "HEAD").strip()
        diff = git(root, "-c", "core.quotePath=false", "diff", "-U0", "--find-renames", "--no-color", "--no-ext-diff",
                   "--src-prefix=a/", "--dst-prefix=b/", self.merge_base)
        self.files = {f.path: f for f in parse_diff(diff)}
        tracked = git(root, "-c", "core.quotePath=false", "ls-files", "-z").split("\0")
        untracked = git(root, "-c", "core.quotePath=false", "ls-files", "-z", "--others", "--exclude-standard").split("\0")
        for p in filter(None, untracked):
            if p in self.files: continue
            f = FileChange(p, "A"); n = len(self.lines(p)); f.added = set(range(1, n + 1)); f.hunks = [[0, 0, 1, n]]
            self.files[p] = f
        self.dirty = bool(git(root, "status", "--porcelain").strip())
        excluded = matcher(config["excludes"])
        frozen = set()
        for lst in config["frozen_lists"]:
            for line in (self.text(lst) or "").splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2 and re.fullmatch(r"[0-9a-fA-F]{32,128}", parts[0]): frozen.add(parts[1].lstrip("*").strip())
        self.frozen = frozen
        allp = [p for p in dict.fromkeys(tracked + untracked) if p]
        self.live = [p for p in allp if p not in frozen and not excluded(p) and (self.root / p).is_file()]
        self.live_set = set(self.live)
        self.tracked = set(filter(None, tracked))
        for f in self.files.values(): f.excluded = f.path not in self.live_set
        self.body = self._body()

    def _body(self):
        a = self.args
        if getattr(a, "body", None): return Path(a.body).read_text(errors="replace")
        if getattr(a, "pr", None):
            return run(["gh", "pr", "view", str(a.pr), "--json", "body", "--jq", ".body"], cwd=self.root).stdout
        return None

    # --- texts and trees
    def text(self, path):
        if path not in self._text:
            p = self.root / path
            try:
                data = p.read_bytes() if p.is_file() and p.stat().st_size <= 4_000_000 else None
            except OSError:
                data = None
            self._text[path] = None if data is None or b"\0" in data[:8192] else data.decode("utf-8", "replace")
        return self._text[path]

    def lines(self, path):
        t = self.text(path)
        return t.splitlines() if t is not None else []

    def base_text(self, path, ref=None):
        key = (path, ref)
        if key not in self._base:
            f = self.files.get(path)
            old = f.old_path if f else path
            if f and f.status == "A" and ref is None: self._base[key] = None
            else:
                p = run(["git", "-C", str(self.root), "show", f"{ref or self.merge_base}:{old}"], check=False)
                self._base[key] = p.stdout if p.returncode == 0 and "\0" not in p.stdout[:8192] else None
        return self._base[key]

    @staticmethod
    def _parse(cache, path, text_of):
        if path not in cache:
            import ast
            t = text_of(path)
            try: cache[path] = ast.parse(t) if t is not None else None
            except (SyntaxError, ValueError, RecursionError): cache[path] = None
        return cache[path]

    def tree(self, path): return self._parse(self._ast, path, self.text)
    def base_tree(self, path): return self._parse(self._base_ast, path, self.base_text)

    # --- selections
    @staticmethod
    def is_test(path):
        name = path.rsplit("/", 1)[-1]
        return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in f"/{path}" or name == "conftest.py"

    def changed(self, suffix=None, tests=None, statuses="AMR", live=True):
        out = []
        for f in self.files.values():
            if f.status not in statuses or f.binary or (live and f.excluded): continue
            if suffix and not f.path.endswith(suffix): continue
            if tests is not None and self.is_test(f.path) != tests: continue
            out.append(f)
        return sorted(out, key=lambda f: f.path)

    def live_files(self, suffix=None, tests=None):
        return [p for p in self.live if (not suffix or p.endswith(suffix)) and (tests is None or self.is_test(p) == tests)]

    def added_text(self, path):
        f, lines = self.files.get(path), self.lines(path)
        return [(n, lines[n - 1]) for n in sorted(f.added) if 0 < n <= len(lines)] if f else []

    def test_python(self):
        """The python that runs the repo's tests: `test_python` (a command printing the path) or this interpreter."""
        if not hasattr(self, "_test_python"):
            cmd = self.config.get("test_python")
            if cmd:
                p = run(shlex.split(cmd), cwd=self.root, check=False, timeout=1800)
                path = (p.stdout.strip().splitlines() or [""])[-1]
                if p.returncode or not path: raise RuntimeError(f"test_python command failed: {cmd}: {p.stderr.strip()[:300]}")
                self._test_python = path
            else:
                self._test_python = sys.executable
        return self._test_python

    def as_json(self, out):
        return dict(repo_root=str(self.root), base=self.base, base_tip=self.base_tip, merge_base=self.merge_base,
                    head=self.head, dirty=self.dirty, files=[f.as_json() for f in self.files.values()], live=self.live,
                    body_path=str(out / "body.md") if self.body is not None else None,
                    receipts=self.args.receipts or [], topics=self.args.topic or [], owned=self.args.owned or [],
                    readonly=self.args.readonly or [], full=self.args.full, budget=self.args.budget, out_dir=str(out),
                    test_python=self.config.get("test_python") or None, config=self.config)


class Finding:
    __slots__ = ("check", "severity", "path", "line", "message", "origin", "disposition")

    def __init__(self, check, severity, path, line, message, origin=""):
        self.check, self.severity, self.path, self.line = check, severity if severity in SEVERITIES else "warn", path or "", line or ""
        self.message, self.origin, self.disposition = " ".join(str(message).split()), origin or "", ""

    @property
    def id(self):
        key = f"{self.check}|{self.path}|{re.sub(r'[0-9]+', '#', self.message)}"
        return hashlib.sha1(key.encode()).hexdigest()[:10]

    def row(self):
        return dict(id=self.id, check=self.check, severity=self.severity, path=self.path, line=self.line,
                    message=self.message, origin=self.origin, disposition=self.disposition)


class Check:
    def __init__(self, id, tier, severity, origin, finds, fn):
        self.id, self.tier, self.severity, self.origin, self.finds, self.fn = id, tier, severity, origin, finds, fn

    def __call__(self, ctx):
        out = []
        for item in self.fn(ctx) or []:
            path, line, message, *rest = item
            out.append(Finding(self.id, rest[0] if rest else self.severity, path, line, message, self.origin))
        return out


def catalogue():
    import _preflight_static, _preflight_dynamic
    return _preflight_static.CHECKS + _preflight_dynamic.CHECKS


def load_config(root, path=None):
    cfg = dict(DEFAULTS)
    p = Path(path) if path else Path(root) / ".jx" / "preflight.json"
    if path and not p.is_file(): sys.exit(f"{p}: no such config file")
    if p.is_file():
        try: user = json.loads(p.read_text())
        except (ValueError, RecursionError) as e: sys.exit(f"{p}: invalid JSON ({e}); fix it, then `jx preflight doctor`")
        if not isinstance(user, dict): sys.exit(f"{p}: expected a JSON object")
        cfg.update(user)
        if "excludes" in user: cfg["excludes"] = list(dict.fromkeys(DEFAULTS["excludes"] + list(user["excludes"])))
    return cfg


def dispositions(root, config, ledger):
    """Return (per-id dispositions, standing [(check_rx, path_match, text)])."""
    by_id, standing = {}, []
    if ledger and Path(ledger).is_file():
        for line in Path(ledger).read_text().splitlines():
            m = re.match(r"\s*[-*]\s+([0-9a-f]{10})\s+(.+)", line)
            if m and not m.group(2).strip().upper().startswith("TODO"): by_id[m.group(1)] = m.group(2).strip()
    p = Path(root) / config["dispositions"]
    if p.is_file():
        for line in p.read_text().splitlines():
            m = re.match(r"\s*[-*]\s+`?([\w.*-]+)`?\s+`?([^\s:`]+)`?\s*:\s*(.+)", line)
            if m: standing.append((glob_re(m.group(1)), matcher([m.group(2)]), m.group(3).strip()))
    return by_id, standing


def pack_checks(ctx):
    """Repo-pack scripts from config whose `when` globs match a changed path, honouring tier."""
    out, changed = [], [f.path for f in ctx.files.values()]
    for spec in ctx.config["checks"]:
        if spec.get("tier", "fast") == "full" and not ctx.args.full: continue
        when = spec.get("when") or []
        if when and not any(matcher(when)(p) for p in changed): continue
        out.append(spec)
    return out


def run_pack(ctx, spec, change_json):
    cmd = spec["cmd"] if isinstance(spec["cmd"], list) else shlex.split(spec["cmd"])
    if cmd and cmd[0] in ("python", "python3"): cmd = [sys.executable, *cmd[1:]]
    try:
        p = run([*cmd, str(change_json)], cwd=ctx.root, check=False, timeout=spec.get("timeout", 1800))
    except (Missing, subprocess.TimeoutExpired) as e:
        return [Finding(spec.get("id", "pack"), "block", "", "", f"pack check did not run: {e}", "preflight")], ""
    rows = []
    for line in p.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) >= 6 and cols[0] != "check":
            rows.append(Finding(cols[0], cols[1], cols[2], cols[3], "\t".join(cols[4:-1]), cols[-1]))
    if p.returncode:
        rows.append(Finding(spec.get("id", "pack"), "block", "", "", f"pack script exited {p.returncode}: "
                            f"{(p.stderr.strip().splitlines() or [''])[-1][:300]}", "preflight"))
    return rows, p.stderr


def selected(checks, a):
    only = {x for s in (a.only or []) for x in s.split(",") if x}
    skip = {x for s in (a.skip or []) for x in s.split(",") if x}
    pick = lambda cid: (not only or any(fnmatch.fnmatch(cid, o) for o in only)) and not any(fnmatch.fnmatch(cid, s) for s in skip)
    return [c for c in checks if pick(c.id) and (c.tier == "fast" or a.full)]


def toplevel():
    p = run(["git", "rev-parse", "--show-toplevel"], check=False)
    if p.returncode: sys.exit("preflight: not inside a git repository; cd into the checkout, then `jx preflight run`")
    return p.stdout.strip()


def emit(rows, a):
    if getattr(a, "json", False): print(json.dumps(rows, indent=1)); return
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n")
    print("\t".join(HEADER))
    for r in rows: print("\t".join(esc(r.get(h, "")) for h in HEADER))


def cmd_run(a):
    root = toplevel()
    config = load_config(root, a.config)
    checks = selected(catalogue(), a)
    t0 = time.time()
    ctx = Change(root, a, config)
    packs = [s for s in pack_checks(ctx) if not a.only or any(fnmatch.fnmatch(s.get("id", ""), o) for x in a.only for o in x.split(","))]
    if a.dry_run:
        rows = [dict(check=c.id, tier=c.tier, kind="jx", reason=c.finds) for c in checks]
        rows += [dict(check=s.get("id", "pack"), tier=s.get("tier", "fast"), kind="pack", reason=str(s.get("cmd"))) for s in packs]
        print(f"preflight: {len(ctx.files)} changed files vs {ctx.merge_base[:12]} ({len(ctx.live)} live files)", file=sys.stderr)
        if a.json: print(json.dumps(rows, indent=1))
        else:
            print("check\ttier\tkind\treason")
            for r in rows: print(f"{r['check']}\t{r['tier']}\t{r['kind']}\t{r['reason']}")
        return 0
    out = Path(a.out) if a.out else Path(tempfile.mkdtemp(prefix="jx-preflight-"))
    out.mkdir(parents=True, exist_ok=True)
    if ctx.body is not None: (out / "body.md").write_text(ctx.body)
    (out / "change.json").write_text(json.dumps(ctx.as_json(out), indent=1))
    findings, timings, logs = [], {}, out / "logs"
    logs.mkdir(exist_ok=True)
    for c in checks:
        s = time.time()
        try:
            findings += c(ctx)
        except Missing as e:
            findings.append(Finding(c.id, "warn", "", "", f"{e}: not found on PATH; check skipped", "preflight"))
        except Exception as e:  # a crashing check must not hide the others; it reports itself as a block
            import traceback
            (logs / f"{c.id}.err").write_text(traceback.format_exc())
            findings.append(Finding(c.id, "block", "", "", f"check crashed: {type(e).__name__}: {e} (see {logs / (c.id + '.err')})", "preflight"))
        timings[c.id] = round(time.time() - s, 3)
    for spec in packs:
        s = time.time()
        rows, err = run_pack(ctx, spec, out / "change.json")
        if err: (logs / f"{spec.get('id', 'pack')}.err").write_text(err)
        findings += rows
        timings[spec.get("id", "pack")] = round(time.time() - s, 3)
    by_id, standing = dispositions(root, config, a.ledger)
    seen, unique = set(), []
    for f in findings:
        k = (f.check, f.severity, f.path, f.line, f.message)
        if k in seen: continue
        seen.add(k); unique.append(f)
        f.disposition = by_id.get(f.id) or next((t for crx, pm, t in standing if crx.match(f.check) and pm(f.path or "-")), "")
    order = {s: i for i, s in enumerate(SEVERITIES)}
    unique.sort(key=lambda f: (order[f.severity], f.check, f.path, int(f.line) if str(f.line).isdigit() else 0))
    rows = [f.row() for f in unique]
    (out / "findings.tsv").write_text("\t".join(HEADER) + "\n" + "".join("\t".join(str(r[h]).replace("\t", " ") for h in HEADER) + "\n" for r in rows))
    (out / "timings.json").write_text(json.dumps(dict(total=round(time.time() - t0, 3), checks=timings), indent=1))
    todo = [f for f in unique if f.severity in ("block", "ask") and not f.disposition]
    (out / "ledger.md").write_text("# preflight ledger: replace TODO with a reason (done: <path:line>, n/a: <why>, decision: <who/where>)\n"
                                   + "".join(f"- {f.id} TODO  <!-- {f.check} {f.path}:{f.line} {f.message[:160]} -->\n" for f in todo))
    emit(rows, a)
    open_blocks = [f for f in unique if f.severity == "block" and not f.disposition]
    open_asks = [f for f in unique if f.severity == "ask" and not f.disposition]
    counts = {s: sum(f.severity == s for f in unique) for s in SEVERITIES}
    print(f"preflight: {counts['block']} block, {counts['warn']} warn, {counts['ask']} ask ({len(open_blocks)} blocks and "
          f"{len(open_asks)} asks open) in {time.time() - t0:.1f}s; {len(ctx.files)} changed files vs {ctx.merge_base[:12]}; "
          f"report in {out}", file=sys.stderr)
    if open_blocks or open_asks:
        print(f"next: fix each block, then dispose the rest in {out / 'ledger.md'} and rerun with --ledger {out / 'ledger.md'}", file=sys.stderr)
    return 1 if open_blocks or (a.strict and open_asks) else 0


def cmd_ls(a):
    rows = [dict(check=c.id, tier=c.tier, severity=c.severity, origin=c.origin, finds=c.finds) for c in catalogue()]
    if a.json: print(json.dumps(rows, indent=1)); return 0
    print("check\ttier\tseverity\torigin\tfinds")
    for r in rows: print("\t".join(r[k] for k in ("check", "tier", "severity", "origin", "finds")))
    return 0


def cmd_doctor(a):
    rows, bad = [], False
    if not shutil.which("git"):
        print("git: not found on PATH; install git, then `jx preflight doctor`", file=sys.stderr); return 1
    p = run(["git", "rev-parse", "--show-toplevel"], check=False)
    if p.returncode:
        print("preflight: not inside a git repository; cd into the checkout, then `jx preflight doctor`", file=sys.stderr); return 1
    root = p.stdout.strip()
    rows.append(dict(item="repo", state="ok", detail=root))
    base = run(["git", "-C", root, "rev-parse", "--verify", f"{a.base}^{{commit}}"], check=False)
    rows.append(dict(item="base", state="ok" if base.returncode == 0 else "missing",
                     detail=base.stdout.strip()[:12] if base.returncode == 0 else f"{a.base} does not resolve; `git fetch origin` or pass --base"))
    bad |= base.returncode != 0
    cfg_path = Path(a.config) if a.config else Path(root) / ".jx" / "preflight.json"
    config = load_config(root, a.config)
    rows.append(dict(item="config", state="ok" if cfg_path.is_file() else "none", detail=str(cfg_path) if cfg_path.is_file() else "defaults (no .jx/preflight.json)"))
    for lst in config["frozen_lists"]:
        ok = (Path(root) / lst).is_file(); bad |= not ok
        rows.append(dict(item="frozen_list", state="ok" if ok else "missing", detail=lst))
    for spec in config["checks"]:
        cmd = spec["cmd"] if isinstance(spec["cmd"], list) else shlex.split(spec["cmd"])
        script = next((c for c in cmd if c.endswith(".py")), None)
        ok = script is None or (Path(root) / script).is_file(); bad |= not ok
        rows.append(dict(item=f"pack {spec.get('id', '?')}", state="ok" if ok else "missing", detail=" ".join(cmd)))
    tp = config.get("test_python")
    exe = shlex.split(tp)[0] if tp else sys.executable
    found = shutil.which(exe) or (Path(exe).is_file() and exe)
    rows.append(dict(item="test_python", state="ok" if found else "missing", detail=tp or f"{sys.executable} (default)"))
    if a.json: print(json.dumps(rows, indent=1))
    else:
        print("item\tstate\tdetail")
        for r in rows: print(f"{r['item']}\t{r['state']}\t{r['detail']}")
    return 1 if bad else 0


def main():
    parser = argparse.ArgumentParser(prog="jx preflight", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw JSON instead of TSV")
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="<command>")
    r = sub.add_parser("run", parents=[common], help="run the checks on this branch")
    r.add_argument("--base", default="origin/main", help="base ref; the diff starts at merge-base(BASE, HEAD) (default origin/main)")
    g = r.add_mutually_exclusive_group()
    g.add_argument("--body", help="draft PR description to check claims against")
    g.add_argument("--pr", type=int, help="read the PR description of this number with gh")
    r.add_argument("--receipts", nargs="*", default=[], help="`jx check run` receipt dirs backing test-count claims")
    r.add_argument("--topic", nargs="*", default=[], help="spec topics; governing-doc sections that match become asks")
    r.add_argument("--full", action="store_true", help="also collect tests, rerun changed tests on base code, and mutate changed lines")
    r.add_argument("--budget", type=int, default=600, help="seconds the mutation run may take (default 600)")
    r.add_argument("--owned", nargs="*", default=[], help="globs this change may touch; anything else is a block")
    r.add_argument("--readonly", nargs="*", default=[], help="globs this change must not touch")
    r.add_argument("--ledger", help="ledger file with `- <id> <reason>` dispositions from an earlier run")
    r.add_argument("--only", action="append", help="comma list of check ids or globs to run")
    r.add_argument("--skip", action="append", help="comma list of check ids or globs to skip")
    r.add_argument("--strict", action="store_true", help="an ask without a disposition also fails the run")
    r.add_argument("--out", help="report directory (default a new temp dir)")
    r.add_argument("--config", help="config file to use instead of the repo's .jx/preflight.json (e.g. today's config on an old commit)")
    r.add_argument("--dry-run", action="store_true", help="list the checks that would run, run none")
    sub.add_parser("ls", parents=[common], help="the check catalogue")
    d = sub.add_parser("doctor", parents=[common], help="git, base ref, config, pack scripts, test python")
    d.add_argument("--base", default="origin/main", help="base ref to resolve (default origin/main)")
    d.add_argument("--config", help="config file to check instead of the repo's .jx/preflight.json")
    a = parser.parse_args()
    a.json = getattr(a, "json", False)
    try:
        sys.exit({"run": cmd_run, "ls": cmd_ls, "doctor": cmd_doctor}[a.cmd](a))
    except Missing as e:
        sys.exit(f"{e}: not found on PATH; install it, then `jx preflight doctor`")
    except RuntimeError as e:
        sys.exit(f"preflight: {e}")


if __name__ == "__main__":
    main()
