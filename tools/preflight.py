#!/usr/bin/env python3
"""jx preflight — the checks reviewers kept running after the PR opened, run by the author before it does. Each check
names the past review miss it came from; `jx preflight ls` prints the catalogue.

  jx preflight run [--base origin/main] [--body DRAFT.md | --pr N] [--receipts DIR ...] [--topic T ...] [--full]
      [--budget 60] [--mutate-all] [--owned GLOB ...] [--readonly GLOB ...] [--ledger FILE] [--only ID,..] [--skip ID,..]
      [--strict] [--out DIR] [--dry-run]                    findings TSV: id, check, severity, path, line, message, origin,
                                                            disposition; exit 1 while a block (or, with --strict, an ask) is open
                                                            --only and --skip take ids or globs, of jx's checks and the repo pack's alike
  jx preflight ls                                           every check: id, tier, severity, origin, what it finds
  jx preflight doctor                                       git, base ref, repo config, pack scripts, test python
  jx preflight vendor [--dry-run] [--allow-dirty]           copy this engine into the repo as .jx/preflight/engine/, so
                                                            the checks run there with python and git alone

Severity: block = definite defect; warn = likely defect, look at it; ask = needs a written disposition; info = summary.
Scope: the diff is merge-base(--base, HEAD) against the working tree, plus untracked files. Searches run over the "live
set": tracked and untracked files minus `frozen_lists` (sha256 manifests whose files never change) and `excludes`.
The fast tier is static. --full adds test collection, a fix-removal run (changed tests against base code) and mutation
of changed lines (--budget seconds), each in temporary `git worktree`s that are removed afterwards, under a macOS
sandbox that denies signals to any process outside the test run's own tree (elsewhere those checks are skipped).
Mutation traces which tests run each changed line and runs each mutant against only those. A later full run reuses a
mutant's verdict while its line is unchanged (--mutate-all reruns them).
Budget: the whole run ends within --budget seconds (default 60). Checks run side by side; one still running when the
budget ends is stopped and says what it left undone, and mutation keeps its verdicts so the next run goes on from there.
Receipts: every run writes <git-common-dir>/preflight/<HEAD>.<fast|full>.json (head, dirty, partial, the open blocks
and asks with their ids), so a hook or a reviewer can ask whether this exact commit passed without rerunning it.
Dispositions: a ledger line `- <id> <reason>` (the run writes a template to OUT/ledger.md; pass it back with --ledger),
or a standing line `- <check-glob> <path-glob>: <reason>` in the repo's dispositions file. A disposed finding never fails
the run. Repo config: `.jx/preflight.json` (frozen_lists, excludes, governing_docs, test_commands, test_python,
ci_only_dirs, status_vocab, secret_allow, dispositions, checks). `checks` lists repo-pack scripts run as
`<cmd> OUT/change.json`; each prints rows `check<TAB>severity<TAB>path<TAB>line<TAB>message<TAB>origin`.
A repository's copy: `vendor` writes this file, the two check modules and ENGINE.json (the jx commit and each file's
sha256) to .jx/preflight/engine/. `python3 .jx/preflight/engine/preflight.py run` then works without jx. Inside such a
repository `jx preflight` hands every command but `vendor` to that copy, so everyone runs one version;
JX_PREFLIGHT_OWN=1 runs jx's own instead. A copy that no longer matches its ENGINE.json refuses to run: it is replaced
as a whole with `vendor`, never edited.
"""
import argparse, fnmatch, hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, tempfile, threading, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

signal.signal(signal.SIGPIPE, signal.SIG_DFL)
sys.path.insert(0, str(Path(__file__).resolve().parent))
if __name__ == "__main__": sys.modules["preflight"] = sys.modules[__name__]  # else the checks' `from preflight import` makes a second copy whose Missing/Skipped escape main's except clauses
SEVERITIES = ("block", "warn", "ask", "info")
HEADER = ["id", "check", "severity", "path", "line", "message", "origin", "disposition"]
ENGINE = ("preflight.py", "_preflight_static.py", "_preflight_dynamic.py")  # what `vendor` copies; ENGINE.json beside them marks a copy
PROG = os.environ.get("PREFLIGHT_PROG") or "jx preflight"  # the command the hints name; a repository's wrapper around its copy sets it
DEFAULTS = {"frozen_lists": [], "excludes": [".claude/worktrees/**", "node_modules/**", ".venv*/**"], "governing_docs": [],
            "test_commands": [], "test_python": "", "test_python_fallback": "", "ci_only_dirs": [], "status_vocab": ["pass", "fail", "pending"],
            "secret_allow": [], "dispositions": ".jx/preflight/DISPOSITIONS.md", "checks": [], "workers": 2}


class Missing(Exception):
    """A required executable is not on PATH."""


class Skipped(Exception):
    """A check cannot run safely here; it reports why as a warning."""


def run(argv, cwd=None, ok=(0,), timeout=None, env=None, check=True):
    try:
        p = subprocess.run(argv, cwd=cwd, text=True, errors="replace", capture_output=True, timeout=timeout, env=env)  # a latin-1 file in the diff must not end the run
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
    files, cur, old_n, new_n, header = [], None, 0, 0, True
    for line in text.splitlines():
        if line.startswith("diff --git "):
            header = True  # until the file's first hunk; after it `--- x` / `+++ x` are a removed `-- x` / an added `++ x`
            m = re.match(r'diff --git "?a/(.*?)"? "?b/(.*?)"?$', line)
            cur = FileChange(m.group(2), "M", m.group(1)) if m else FileChange(line.split()[-1][2:], "M")
            files.append(cur); continue
        if cur is None: continue
        if line.startswith("new file mode"): cur.status = "A"
        elif line.startswith("deleted file mode"): cur.status = "D"
        elif line.startswith("rename from "): cur.status, cur.old_path = "R", line[12:]
        elif line.startswith("rename to "): cur.path = line[10:]
        elif line.startswith("Binary files") or line.startswith("GIT binary patch"): cur.binary = True
        elif header and line.startswith("+++ ") and line[4:] != "/dev/null": cur.path = line[6:] if line[4:6] == "b/" else line[4:]
        elif header and (line.startswith("--- ") or line.startswith("+++ ")): pass
        elif (m := HUNK.match(line)):
            header = False
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
            try:
                failed = run(["git", "-C", str(root), "fetch", "-q", "origin", self.base[7:]], check=False, timeout=60).returncode
            except RuntimeError:  # timed out: offline, or the remote is unreachable
                failed = True
            if failed: print(f"preflight: fetch of {self.base} failed; using the local ref", file=sys.stderr)
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
        for f in self.files.values():  # a deleted file is never live: judge it by the rules that would have excluded it
            f.excluded = (f.path in frozen or excluded(f.path)) if f.status == "D" else f.path not in self.live_set
        self.body = self._body()

    def _body(self):
        a = self.args
        if getattr(a, "body", None):
            try: return Path(a.body).read_text(errors="replace")
            except OSError as e: sys.exit(f"preflight: --body: {e}")
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
        """The python that runs the repo's tests: $PREFLIGHT_TEST_PYTHON; else `test_python`, a command printing the
        path; else, where that command's program is not installed, `test_python_fallback`, a path in the repo; else
        this interpreter. The command gets what is left of the run's budget: a cold one that builds an environment is
        stopped there, and the checks that need it say so."""
        with self.__dict__.setdefault("_test_python_lock", threading.Lock()):  # the checks that run tests start together: resolve once
            return self._resolve_test_python()

    def _resolve_test_python(self):
        if isinstance(getattr(self, "_test_python", None), Skipped): raise self._test_python
        if not hasattr(self, "_test_python"):
            cmd, spare = self.config.get("test_python"), self.config.get("test_python_fallback")
            argv = shlex.split(cmd) if cmd else []
            if os.environ.get("PREFLIGHT_TEST_PYTHON"):
                self._test_python = os.environ["PREFLIGHT_TEST_PYTHON"]
            elif argv and spare and not shutil.which(argv[0]):
                if not (self.root / spare).is_file():
                    raise Skipped(f"no test python: `{argv[0]}` is not on PATH and {spare} does not exist; build it, or "
                                  "set PREFLIGHT_TEST_PYTHON")
                self._test_python = str(self.root / spare)
            elif cmd:
                left = max(0.1, self.deadline - time.time()) if getattr(self, "deadline", None) else 1800
                try:
                    p = run(argv, cwd=self.root, check=False, timeout=left)
                except RuntimeError:  # it did not finish in what was left of the budget
                    self._test_python = Skipped(f"the test python command did not finish within the run's {self.args.budget} s budget: "
                                                f"`{cmd}`. Run it once yourself, which builds the interpreter, then run this again")
                    raise self._test_python
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
                    deadline=getattr(self, "deadline", None),  # epoch seconds by which a pack script must have printed its rows
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
        except (ValueError, RecursionError) as e: sys.exit(f"{p}: invalid JSON ({e}); fix it, then `{PROG} doctor`")
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
        if when and not any(map(matcher(when), changed)): continue
        out.append(spec)
    return out


def pack_cmd(spec):
    """A pack check's argv; [] when the spec has no `cmd`."""
    cmd = spec.get("cmd") or []
    return cmd if isinstance(cmd, list) else shlex.split(cmd)


def run_pack(ctx, spec, change_json):
    cmd = pack_cmd(spec)
    if not cmd: return [Finding(spec.get("id", "pack"), "block", "", "", "pack check has no `cmd`", "preflight")], ""
    if cmd[0] in ("python", "python3"): cmd = [sys.executable, *cmd[1:]]
    try:
        p = subprocess.Popen([*cmd, str(change_json)], cwd=ctx.root, text=True, errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    except OSError as e:  # not found, not executable, …
        return [Finding(spec.get("id", "pack"), "block", "", "", f"pack check did not run: {cmd[0]} not found" if isinstance(e, FileNotFoundError)
                        else f"pack check did not run: {cmd[0]}: {e.strerror or e}", "preflight")], ""
    late = []
    try:
        out, err = p.communicate(timeout=max(1, min(spec.get("timeout", 1800), ctx.deadline - time.time())))
    except subprocess.TimeoutExpired:  # stop the script and what it started; rows it already printed still count
        try: os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError): pass
        out, err = p.communicate()
        late = [Finding(spec.get("id", "pack"), "warn", "", "", f"did not finish within the run's {ctx.args.budget} s budget, so "
                        "findings are missing; a pack script reads `deadline` from change.json to stop in time", "preflight")]
    rows = []
    for line in out.splitlines():
        cols = line.split("\t")
        if len(cols) >= 6 and cols[0] != "check":
            rows.append(Finding(cols[0], cols[1], cols[2], cols[3], "\t".join(cols[4:-1]), cols[-1]))
    if late:
        return rows + late, err
    if p.returncode:
        rows.append(Finding(spec.get("id", "pack"), "block", "", "", f"pack script exited {p.returncode}: "
                            f"{(err.strip().splitlines() or [''])[-1][:300]}", "preflight"))
    return rows, err


def picker(a):
    """Whether --only and --skip leave a check id in the run; jx's checks and the repository's pack go by the same rule."""
    only = {x for s in (a.only or []) for x in s.split(",") if x}
    skip = {x for s in (a.skip or []) for x in s.split(",") if x}
    return lambda cid: (not only or any(fnmatch.fnmatch(cid, o) for o in only)) and not any(fnmatch.fnmatch(cid, s) for s in skip)


def selected(checks, a):
    pick = picker(a)
    return [c for c in checks if pick(c.id) and (c.tier == "fast" or a.full)]


def toplevel():
    p = run(["git", "rev-parse", "--show-toplevel"], check=False)
    if p.returncode: sys.exit(f"preflight: not inside a git repository; cd into the checkout, then `{PROG} run`")
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
    ctx.deadline = t0 + a.budget  # the whole run ends by then: every test run and pack script is cut to what is left
    pick = picker(a); packs = [s for s in pack_checks(ctx) if pick(s.get("id", ""))]
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

    def pack(spec):
        s = time.time()
        rows, err = run_pack(ctx, spec, out / "change.json")
        return spec, rows, err, round(time.time() - s, 3)
    def own(c):
        s = time.time()
        try:
            rows = c(ctx)
        except Missing as e:
            rows = [Finding(c.id, "warn", "", "", f"{e}: not found on PATH; check skipped", "preflight")]
        except Skipped as e:
            rows = [Finding(c.id, "warn", "", "", f"check skipped: {e}", "preflight")]
        except Exception as e:  # a crashing check must not hide the others; it reports itself as a block
            import traceback
            (logs / f"{c.id}.err").write_text(traceback.format_exc())
            rows = [Finding(c.id, "block", "", "", f"check crashed: {type(e).__name__}: {e} (see {logs / (c.id + '.err')})", "preflight")]
        return c, rows, round(time.time() - s, 3)
    # pack checks are subprocesses and the full-tier checks run tests: all start now, beside the static checks, so
    # the run takes the longest check, not the sum
    pool = ThreadPoolExecutor(max_workers=max(1, len(packs) + len(checks)))
    pending = [pool.submit(pack, spec) for spec in packs]
    slow = [pool.submit(own, c) for c in checks if c.tier == "full"]
    for c, rows, secs in [own(c) for c in checks if c.tier != "full"] + [job.result() for job in slow]:
        findings += rows
        timings[c.id] = secs
    for job in pending:
        spec, rows, err, secs = job.result()
        if err: (logs / f"{spec.get('id', 'pack')}.err").write_text(err)
        findings += rows
        timings[spec.get("id", "pack")] = secs
    pool.shutdown()
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
    code = 1 if open_blocks or (a.strict and open_asks) else 0
    write_receipt(ctx, a, unique, code, out)
    return code


def write_receipt(ctx, a, unique, code, out):
    """Record this run for HEAD so a gate (a PR-creation hook) can check it without rerunning; return the path."""
    common = Path(git(ctx.root, "rev-parse", "--path-format=absolute", "--git-common-dir").strip())
    d = common / "preflight"; d.mkdir(parents=True, exist_ok=True)
    tier = "full" if a.full else "fast"
    rec = dict(head=ctx.head, dirty=ctx.dirty, tier=tier, runner="jx", partial=bool(a.only or a.skip), base=ctx.base,
               merge_base=ctx.merge_base, created=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), exit=code, out=str(out),
               open=[dict(id=f.id, check=f.check, severity=f.severity, path=f.path, line=f.line, message=f.message)
                     for f in unique if f.severity in ("block", "ask") and not f.disposition],
               counts={s: sum(f.severity == s for f in unique) for s in SEVERITIES},
               mutation=getattr(ctx, "mutation", None))  # each mutant's verdict, which a later full run reuses
    path = d / f"{ctx.head}.{tier}.json"
    path.write_text(json.dumps(rec, indent=1))
    return path


def cmd_ls(a):
    rows = [dict(check=c.id, tier=c.tier, severity=c.severity, origin=c.origin, finds=c.finds) for c in catalogue()]
    if a.json: print(json.dumps(rows, indent=1)); return 0
    print("check\ttier\tseverity\torigin\tfinds")
    for r in rows: print("\t".join(r[k] for k in ("check", "tier", "severity", "origin", "finds")))
    return 0


def cmd_doctor(a):
    rows, bad = [], False
    if not shutil.which("git"):
        print(f"git: not found on PATH; install git, then `{PROG} doctor`", file=sys.stderr); return 1
    p = run(["git", "rev-parse", "--show-toplevel"], check=False)
    if p.returncode:
        print(f"preflight: not inside a git repository; cd into the checkout, then `{PROG} doctor`", file=sys.stderr); return 1
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
        cmd = pack_cmd(spec)
        script = next((c for c in cmd if c.endswith(".py")), None)
        ok = bool(cmd) and (script is None or (Path(root) / script).is_file()); bad |= not ok
        rows.append(dict(item=f"pack {spec.get('id', '?')}", state="ok" if ok else "missing", detail=" ".join(cmd)))
    tp, spare = config.get("test_python"), config.get("test_python_fallback")
    exe = shlex.split(tp)[0] if tp else sys.executable
    found = shutil.which(exe) or (Path(exe).is_file() and exe)
    detail = tp or f"{sys.executable} (default)"
    if not found and spare:  # a machine without the command's program uses the fallback path, if it was built
        found = (Path(root) / spare).is_file()
        detail = f"{spare} (`{exe}` is not on PATH)" if found else f"`{exe}` is not on PATH and {spare} does not exist; build it, or set PREFLIGHT_TEST_PYTHON"
    rows.append(dict(item="test_python", state="ok" if found else "missing", detail=detail))
    import _preflight_dynamic
    why = _preflight_dynamic.sandbox_problem()
    rows.append(dict(item="sandbox", state="missing" if why else "ok", detail=why or "--full test runs cannot signal outside their own tree"))
    if a.json: print(json.dumps(rows, indent=1))
    else:
        print("item\tstate\tdetail")
        for r in rows: print(f"{r['item']}\t{r['state']}\t{r['detail']}")
    return 1 if bad else 0


def pinned():
    """Refuse to run as a repository's copy that no longer matches its ENGINE.json."""
    here = Path(__file__).resolve().parent
    try:
        want = json.loads((here / "ENGINE.json").read_text())["files"]
        changed = [n for n in ENGINE if hashlib.sha256((here / n).read_bytes()).hexdigest() != want[n]]
    except FileNotFoundError:
        return  # jx's own engine, or a copy with a file missing, which fails on its import
    except (ValueError, KeyError, TypeError, RecursionError) as e:
        sys.exit(f"preflight: {here / 'ENGINE.json'} does not read ({type(e).__name__}: {e}); take a new copy with `jx preflight vendor`")
    if changed:
        sys.exit(f"preflight: {', '.join(changed)} in {here} no longer match ENGINE.json. This copy of jx's preflight engine is "
                 f"replaced as a whole, never edited: restore it with `git checkout -- {here}`, or take a new one with "
                 "`jx preflight vendor` from a jx checkout")


def repo_copy():
    """The engine copy this repository pins, unless this file is it or JX_PREFLIGHT_OWN is set."""
    try:
        p = run(["git", "rev-parse", "--show-toplevel"], check=False)
    except Missing:
        return None
    copy = Path(p.stdout.strip()) / ".jx" / "preflight" / "engine" / "preflight.py"
    if p.returncode or os.environ.get("JX_PREFLIGHT_OWN") or not copy.is_file() or copy.resolve() == Path(__file__).resolve():
        return None
    return copy


def cmd_vendor(a):
    """Copy the engine into this repository as .jx/preflight/engine/, with ENGINE.json: the jx commit and each file's sha256."""
    src, root = Path(__file__).resolve().parent, Path(toplevel())
    if (src / "ENGINE.json").is_file():
        sys.exit("preflight: this is a repository's copy of the engine; run `jx preflight vendor` from a jx checkout")
    state = run(["git", "-C", str(src), "status", "--porcelain", "--", *ENGINE], check=False)
    dirty = state.returncode == 0 and bool(state.stdout.strip())
    if dirty and not a.allow_dirty:
        sys.exit(f"preflight: the engine in {src} has uncommitted changes, and ENGINE.json names the commit a copy came "
                 "from. Commit them, then `jx preflight vendor` (or --allow-dirty to try a copy first)")
    commit = git(src, "rev-parse", "HEAD").strip() + ("+dirty" if dirty else "") if state.returncode == 0 else None
    origin = run(["git", "-C", str(src), "remote", "get-url", "origin"], check=False).stdout.strip() if commit else ""
    files = {n: (src / n).read_bytes() for n in ENGINE}
    manifest = dict(_doc="A copy of jx's preflight engine. Never edit these files: replace them with `jx preflight vendor`.",
                    source=re.sub(r"^git@([^:]+):(.+?)(?:\.git)?$", r"https://\1/\2", origin), commit=commit,
                    files={n: hashlib.sha256(b).hexdigest() for n, b in files.items()})
    files["ENGINE.json"] = (json.dumps(manifest, indent=1) + "\n").encode()
    dest, rows = root / ".jx" / "preflight" / "engine", []
    for name, data in files.items():
        old = (dest / name).read_bytes() if (dest / name).is_file() else None
        rows.append(dict(file=str((dest / name).relative_to(root)), state="same" if old == data else "new" if old is None else "update"))
        if old != data and not a.dry_run:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / name).write_bytes(data)
    if a.json: print(json.dumps(rows, indent=1))
    else:
        print("file\tstate")
        for r in rows: print(f"{r['file']}\t{r['state']}")
    todo = sum(r["state"] != "same" for r in rows)
    print(f"preflight: engine at {commit or 'an unknown commit'}; {todo} file(s) {'would change' if a.dry_run else 'written'} in {dest}"
          + ("" if a.dry_run or not todo else f"; run it there with `python3 {dest.relative_to(root)}/preflight.py run`"), file=sys.stderr)
    return 0


def main():
    pinned()
    copy = repo_copy()
    if copy and sys.argv[1:2] != ["vendor"]:  # the repository pins its engine: everyone there runs that one
        os.execv(sys.executable, [sys.executable, str(copy), *sys.argv[1:]])
    parser = argparse.ArgumentParser(prog=PROG, description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
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
    r.add_argument("--budget", type=int, default=60, help="seconds the whole run may take (default 60)")
    r.add_argument("--mutate-all", action="store_true",
                   help="rerun every mutant instead of reusing verdicts from an earlier full run of this branch")
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
    d = sub.add_parser("doctor", parents=[common], help="git, base ref, config, pack scripts, test python, sandbox")
    d.add_argument("--base", default="origin/main", help="base ref to resolve (default origin/main)")
    d.add_argument("--config", help="config file to check instead of the repo's .jx/preflight.json")
    v = sub.add_parser("vendor", parents=[common], help="copy this engine into the repository as .jx/preflight/engine/")
    v.add_argument("--dry-run", action="store_true", help="say which files would change, write nothing")
    v.add_argument("--allow-dirty", action="store_true", help="copy an engine with uncommitted changes; ENGINE.json says so")
    a = parser.parse_args()
    a.json = getattr(a, "json", False)
    try:
        sys.exit({"run": cmd_run, "ls": cmd_ls, "doctor": cmd_doctor, "vendor": cmd_vendor}[a.cmd](a))
    except Missing as e:
        sys.exit(f"{e}: not found on PATH; install it, then `{PROG} doctor`")
    except RuntimeError as e:
        sys.exit(f"preflight: {e}")


if __name__ == "__main__":
    main()
