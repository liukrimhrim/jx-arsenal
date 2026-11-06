#!/usr/bin/env python3
"""jx tf — Terraform lever for agents. TSV by default, --json for the raw payload. Never runs apply.

  jx tf roots [DIR] [--changed [BASE]]        every root (dir with a backend block) under DIR; --changed keeps those touched vs BASE
  jx tf check <root|-> [--fix]                fmt -check + init -backend=false + validate -json, one row per root and per diagnostic
  jx tf plan <root> [--target ADDR ...] [--var k=v ...] [--var-file F] [--refresh-only] [--lock] [--out FILE]
                                              init (retrying -reconfigure when the backend moved), lock-free plan, one row per
                                              changed resource; exit 0 no changes, 2 changes, 1 error (terraform's own codes)
  jx tf diff <planfile> [ADDRESS ...] [--root R]   attribute-level before/after of a saved plan, one row per changed path
  jx tf state <root> [--grep REGEX]           `state list`, filtered
  jx tf doctor [root]                         local terraform version, the root's required_version, backend, init and git context

Roots are directories holding *.tf files with a backend block. The lever reads the backend profile from
the root at run time; nothing environment-specific lives here. `-` reads roots from a TSV on stdin
(column `root`, else the first column), so `jx tf roots infra --changed | jx tf check -` is one pipe.
"""
import argparse, glob, json, os, re, signal, subprocess, sys, tempfile
signal.signal(signal.SIGPIPE, signal.SIG_DFL)

ACTION = {("create",): "add", ("update",): "change", ("delete",): "destroy", ("delete", "create"): "replace", ("create", "delete"): "replace", ("no-op",): "no-op", ("read",): "read"}
TF = ["terraform"]
BACKEND = re.compile(r'backend\s+"([^"]+)"\s*\{(.*?)\n\s*\}', re.S)

def run(root, *args, env=None):
    """Run one terraform command in `root`, non-interactive and uncoloured. Returns (rc, stdout, stderr)."""
    p = subprocess.run([*TF, f"-chdir={root}", *args], text=True, capture_output=True, env={**os.environ, **env} if env else None)
    return p.returncode, p.stdout, p.stderr

def init(root, upgrade=False):
    """Returns True when the init had to take the -reconfigure retry."""
    rc, out, err = run(root, "init", "-input=false", "-no-color", *(["-upgrade"] if upgrade else []))
    reconf = bool(rc) and "reconfigure" in err + out
    if reconf:  # backend block changed since the last init; a fresh init with the new backend is the documented fix
        rc, out, err = run(root, "init", "-input=false", "-no-color", "-reconfigure")
    if rc: fail(root, "init", err + out)
    return reconf

def fail(root, what, text):
    t = re.sub(r"\n\s*\n", "\n", text.strip())[-1500:]
    hints = [(r"chdir .*no such file or directory", "that root directory does not exist; `jx tf roots <dir>` lists roots (paths are relative to the cwd)"),
             (r"no default value|No value for required variable", "the root needs variables: pass --var k=v or --var-file; CI's values are in the workflow that plans this root"),
             (r"Invalid value for variable", "a validation rule rejected the value; the message above says what is accepted"),
             (r"authentication_error|401|403|Unauthorized|invalid.*token|credentials", "a provider or the state backend rejected credentials: `jx tf doctor <root>` shows the backend profile; export the provider's token env var (see `terraform -chdir=<root> providers`)"),
             (r"Error acquiring the state lock", "state is locked (Atlantis or another plan): wait, or re-run without --lock"),
             (r"Required plugins are not installed|Inconsistent dependency lock", "run `jx tf plan <root> --upgrade` once to refresh .terraform")]
    hint = next((h for p, h in hints if re.search(p, t, re.I)), "")
    sys.exit(f"terraform {what} failed in {root}:\n{t}" + (f"\n→ {hint}" if hint else ""))

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, a, raw=None):
    if a.json: print(json.dumps(raw if raw is not None else rows, indent=1))
    else: tsv(rows, header)

def git(cwd, *args):
    p = subprocess.run(["git", "-C", cwd, *args], text=True, capture_output=True)
    return p.stdout.strip() if p.returncode == 0 else ""
def dirty(root): return [l for l in git(root, "status", "--porcelain", "--", ".").splitlines() if l.strip()]
def branch(root): return git(root, "rev-parse", "--abbrev-ref", "HEAD")

def backend(root):
    """(backend type, profile) from the root's backend block; ('', '') when the root has none."""
    for f in sorted(glob.glob(os.path.join(root, "*.tf"))):
        m = BACKEND.search(open(f).read())
        if m:
            p = re.search(r'^\s*profile\s*=\s*"([^"]*)"', m.group(2), re.M)
            return m.group(1), p.group(1) if p else ""
    return "", ""

def roots_arg(arg):
    """One root, or every root from a TSV on stdin when arg is `-` (column `root`, else the first column)."""
    if arg != "-": return [arg.rstrip("/")]
    lines = [l.rstrip("\n") for l in sys.stdin if l.strip()]
    if not lines: sys.exit("no roots on stdin; expected a TSV with a root column (e.g. from `jx tf roots DIR --changed`)")
    head = lines[0].split("\t"); i = head.index("root") if "root" in head else 0
    out = []
    for l in lines[1:] if "root" in head else lines:
        v = l.split("\t")[i].strip().rstrip("/")
        if v and not os.path.isdir(v): sys.exit(f"{v}: not a directory (stdin must list roots, e.g. from `jx tf roots`)")
        if v and v not in out: out.append(v)
    return out

def show_json(root, planfile):
    rc, out, err = run(root, "show", "-json", planfile)
    if rc: fail(root, "show", err)
    return json.loads(out)

def summarize(plan):
    rows = []
    for r in plan.get("resource_changes", []):
        act = ACTION.get(tuple(r["change"]["actions"]), "/".join(r["change"]["actions"]))
        if act == "no-op": continue
        rows.append({"action": act, "address": r["address"], "type": r["type"], "provider": (r.get("provider_name") or "").rsplit("/", 1)[-1],
                     "replace_because": ",".join(".".join(map(str, p)) for p in r["change"].get("replace_paths") or [])})
    order = {"destroy": 0, "replace": 1, "change": 2, "add": 3, "read": 4}
    rows.sort(key=lambda r: (order.get(r["action"], 9), r["address"]))
    outs = [{"action": ACTION.get(tuple(v["actions"]), "/".join(v["actions"])), "address": f"output.{k}", "type": "output", "provider": "", "replace_because": ""}
            for k, v in plan.get("output_changes", {}).items() if v["actions"] != ["no-op"]]
    return rows + outs

def cmd_plan(a):
    root = a.root.rstrip("/")
    warn = lambda s: sys.stderr.write(f"warning: {s}\n")
    if d := dirty(root): warn(f"{root} has {len(d)} uncommitted changes; this plan reflects the working tree, not a commit")
    if (b := branch(root)) and b not in ("main", "master"): warn(f"on branch {b}; a plan here is not what CI plans from main")
    if init(root, a.upgrade): warn("backend reconfigured (backend block differed from the cached init)")
    out = a.out or os.path.join(tempfile.mkdtemp(prefix="jx-tf-"), "plan.tfplan")
    args = ["plan", "-input=false", "-no-color", "-detailed-exitcode", f"-out={out}", "-lock=true" if a.lock else "-lock=false"]
    if a.lock: args.append("-lock-timeout=2m")
    if a.refresh_only: args.append("-refresh-only")
    for t in a.target: args += ["-target", t]
    for v in a.var: args += ["-var", v]
    if a.var_file: args += ["-var-file", a.var_file]
    rc, pout, perr = run(root, *args)
    if rc == 1: fail(root, "plan", perr + pout)
    plan = show_json(root, out)
    if plan.get("errored"): fail(root, "plan", perr + pout or "plan file marked errored")
    rows = summarize(plan)
    emit(rows, ["action", "address", "type", "provider", "replace_because"], a, plan if a.json else None)
    counts = {k: sum(r["action"] == k for r in rows) for k in ("add", "change", "destroy", "replace")}
    sys.stderr.write(" ".join(f"{k}={v}" for k, v in counts.items()) + f"  plan={out}" + ("  (targeted: not the full root)" if a.target else "") + "\n")
    sys.exit(2 if rows else 0)

def module_dirs(root):
    """Local module directories a root's *.tf files reference, resolved relative to the root."""
    out = set()
    for f in glob.glob(os.path.join(root, "*.tf")):
        for m in re.finditer(r'source\s*=\s*"(\.{1,2}/[^"]+)"', open(f).read()):
            out.add(os.path.normpath(os.path.join(root, m.group(1))))
    return out

def changed_files(cwd, base):
    """Repo-relative paths changed vs merge-base(BASE, HEAD) plus the working tree."""
    top = git(cwd, "rev-parse", "--show-toplevel")
    if not top: sys.exit(f"{cwd} is not inside a git checkout; --changed needs one")
    mb = git(cwd, "merge-base", base, "HEAD")
    if not mb and base == "origin/main":  # a shallow clone can hold origin/main without the history that joins it to HEAD
        base = "main"; mb = git(cwd, "merge-base", base, "HEAD")
        if mb: sys.stderr.write("--changed: origin/main shares no history with HEAD here; using local main\n")
    if not mb: sys.exit(f"no merge-base between `{base}` and HEAD (unknown ref, or a shallow clone cut the history); `git fetch --unshallow`, or pass --changed <ref> that shares history")
    files = set(git(cwd, "diff", "--name-only", mb).splitlines())
    files |= {l[3:].split(" -> ")[-1] for l in git(cwd, "status", "--porcelain").splitlines() if l.strip()}
    return top, {os.path.normpath(f) for f in files}

def cmd_roots(a):
    d = a.dir.rstrip("/") or "."
    found = []
    for cur, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if not x.startswith(".") and x != "modules")
        if any(f.endswith(".tf") for f in files) and backend(cur)[0]: found.append(cur)
    if a.changed is not None:
        top, files = changed_files(d, a.changed or "origin/main")
        touched = lambda p: any(f == p or f.startswith(p + "/") for f in files)
        rel = lambda p: os.path.normpath(os.path.relpath(os.path.abspath(p), top))
        found = [r for r in found if touched(rel(r)) or any(touched(rel(m)) for m in module_dirs(r))]
    rows = [{"root": r, "backend": backend(r)[0], "profile": backend(r)[1], "initialized": os.path.isdir(os.path.join(r, ".terraform"))} for r in found]
    emit(rows, ["root", "backend", "profile", "initialized"], a)

def check_env(root):
    """`init -backend=false` still loads a backend cached in .terraform/ (and resolves its credentials), so validate
    would need AWS creds. A private TF_DATA_DIR hides that cache; providers already installed in the root are
    shared through a symlink so nothing is downloaded twice."""
    d = tempfile.mkdtemp(prefix="jx-tf-check-")
    prov = os.path.join(root, ".terraform", "providers")
    if os.path.isdir(prov): os.symlink(os.path.abspath(prov), os.path.join(d, "providers"))
    return {"TF_DATA_DIR": d}

def cmd_check(a):
    roots, rows, bad = roots_arg(a.root), [], False
    if not roots: sys.stderr.write("no roots on stdin; nothing to check\n")
    for root in roots:
        if a.fix:
            rc, out, err = run(root, "fmt", "-recursive", "-no-color")
            sys.stderr.write(f"fmt -recursive rewrote {len(out.split())} files in {root}\n")
        rc, out, err = run(root, "fmt", "-check", "-recursive", "-no-color")
        files = out.split()
        if rc not in (0, 3): rows.append({"root": root, "kind": "fmt", "status": "error", "detail": err.strip()}); bad = True
        else: rows.append({"root": root, "kind": "fmt", "status": "ok" if not files else f"{len(files)} files", "detail": " ".join(files)})
        bad |= bool(files)
        env = check_env(root)
        rc, out, err = run(root, "init", "-backend=false", "-input=false", "-no-color", env=env)
        if rc:
            rows.append({"root": root, "kind": "validate", "status": "init failed", "detail": re.sub(r"\s+", " ", (err + out).strip())[:300]}); bad = True; continue
        rc, out, err = run(root, "validate", "-json", env=env)
        try: v = json.loads(out)
        except ValueError: rows.append({"root": root, "kind": "validate", "status": "error", "detail": (err + out).strip()[:300]}); bad = True; continue
        diags = v.get("diagnostics", [])
        rows.append({"root": root, "kind": "validate", "status": "ok" if v.get("valid") else f"{v.get('error_count', 0)} errors", "detail": f"{v.get('warning_count', 0)} warnings"})
        bad |= not v.get("valid")
        for dg in diags:
            if dg.get("severity") == "warning" and not a.warnings: continue
            rg = dg.get("range") or {}
            where = f"{rg.get('filename', '')}:{(rg.get('start') or {}).get('line', '')}" if rg else ""
            rows.append({"root": root, "kind": "diag", "status": dg.get("severity", ""), "detail": f"{dg.get('summary', '')}: {dg.get('detail', '')} ({where})".replace(":  (", " (")})
    emit(rows, ["root", "kind", "status", "detail"], a)
    if bad: sys.stderr.write("→ `jx tf check <root> --fix` rewrites formatting; validate errors name file:line\n"); sys.exit(1)

def flatten(v, path="", out=None):
    """Nested JSON → {dotted.path: leaf}; list items by index; empty containers stay leaves so `{}`→`{a:1}` shows."""
    out = {} if out is None else out
    if isinstance(v, dict) and v:
        for k, x in v.items(): flatten(x, f"{path}.{k}" if path else str(k), out)
    elif isinstance(v, list) and v:
        for i, x in enumerate(v): flatten(x, f"{path}.{i}" if path else str(i), out)
    else: out[path] = v
    return out

def marked(tree, path):
    """True when `path` or any ancestor is flagged true in an after_unknown / *_sensitive tree."""
    for p, v in flatten(tree or {}).items():
        if v is True and (path == p or path.startswith(p + ".")): return True
    return False

def cmd_diff(a):
    root = a.root or (os.path.dirname(a.planfile) if os.path.isdir(os.path.join(os.path.dirname(a.planfile) or ".", ".terraform")) else None)
    if not root: sys.exit("pass --root <dir>: `show -json` needs the plan's root (the plan file is not next to a .terraform dir)")
    plan = show_json(root.rstrip("/"), os.path.abspath(a.planfile))
    changes = [r for r in plan.get("resource_changes", []) if r["change"]["actions"] != ["no-op"]]
    if a.address:
        missing = set(a.address) - {r["address"] for r in changes}
        if missing: sys.exit(f"not in this plan's changes: {', '.join(sorted(missing))}\n→ `jx tf diff {a.planfile}` with no address lists every changed resource")
        changes = [r for r in changes if r["address"] in a.address]
    show = lambda v: ("" if v is None else v if isinstance(v, str) else json.dumps(v))[:120]
    rows = []
    for r in changes:
        c = r["change"]; before, after = flatten(c.get("before")), flatten(c.get("after"))
        unknown = {p for p, v in flatten(c.get("after_unknown") or {}).items() if v is True}
        for p in sorted(set(before) | set(after) | unknown):
            b, af = before.get(p), after.get(p)
            is_unknown = marked(c.get("after_unknown"), p)
            if b == af and not is_unknown: continue
            rows.append({"address": r["address"], "path": p,
                         "before": "(sensitive)" if b is not None and marked(c.get("before_sensitive"), p) else show(b),
                         "after": "(known after apply)" if is_unknown else "(sensitive)" if af is not None and marked(c.get("after_sensitive"), p) else show(af)})
    emit(rows, ["address", "path", "before", "after"], a, plan if a.json else None)

def cmd_state(a):
    root = a.root.rstrip("/"); init(root)
    rc, out, err = run(root, "state", "list")
    if rc: fail(root, "state list", err + out)
    rows = [{"address": l} for l in out.splitlines() if l.strip() and (not a.grep or re.search(a.grep, l))]
    emit(rows, ["address"], a)

def cmd_doctor(a):
    rc, out, err = run(".", "version", "-json")
    ver = json.loads(out) if rc == 0 else {}
    rows = [{"item": "terraform", "value": ver.get("terraform_version", (err or out).strip() or "not found")},
            {"item": "AWS_PROFILE", "value": os.environ.get("AWS_PROFILE", "unset")}]
    if a.root:
        root = a.root.rstrip("/")
        req = next((m.group(1) for f in sorted(glob.glob(os.path.join(root, "*.tf"))) for m in [re.search(r'required_version\s*=\s*"([^"]+)"', open(f).read())] if m), "")
        bt, prof = backend(root)
        rows += [{"item": "root", "value": root}, {"item": "required_version", "value": req or "(none)"},
                 {"item": "backend", "value": bt or "(no backend block: not a root)"},
                 {"item": "profile", "value": prof or f"ambient AWS_PROFILE={os.environ.get('AWS_PROFILE', 'unset')}"},
                 {"item": "initialized", "value": os.path.isdir(os.path.join(root, ".terraform"))},
                 {"item": "branch", "value": branch(root) or "(not a git checkout)"}, {"item": "dirty", "value": f"{len(dirty(root))} files"}]
    emit(rows, ["item", "value"], a)

def main():
    ap = argparse.ArgumentParser(prog="jx tf", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="raw terraform JSON instead of TSV")
    g = argparse.ArgumentParser(add_help=False); g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="raw terraform JSON instead of TSV")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sp.add_parser("roots", parents=[g], help="list terraform roots under a directory")
    p.add_argument("dir", nargs="?", default=".", help="directory to walk (.)"); p.add_argument("--changed", nargs="?", const="", metavar="BASE", help="only roots with files changed vs BASE's merge-base or in the working tree, counting referenced modules (origin/main)")
    p.set_defaults(f=cmd_roots)
    p = sp.add_parser("check", parents=[g], help="fmt -check + validate per root, no backend; exit 1 if any root fails")
    p.add_argument("root", help="root directory, or - for roots from TSV on stdin"); p.add_argument("--warnings", action="store_true", help="also list warning diagnostics (hidden; the validate row still counts them)"); p.add_argument("--fix", action="store_true", help="run `fmt -recursive` (writes files) before checking (off)")
    p.set_defaults(f=cmd_check)
    p = sp.add_parser("plan", parents=[g], help="init + lock-free plan, one row per changed resource; exit 2 when there are changes")
    p.add_argument("root", help="root directory"); p.add_argument("--target", action="append", default=[], metavar="ADDR", help="plan only this address, repeatable (whole root)")
    p.add_argument("--var", action="append", default=[], metavar="k=v", help="terraform -var, repeatable"); p.add_argument("--var-file", help="terraform -var-file")
    p.add_argument("--refresh-only", action="store_true", help="refresh-only plan: drift between state and reality"); p.add_argument("--lock", action="store_true", help="take the state lock with a 2m timeout (off: read-only plan, no lock)")
    p.add_argument("--out", help="keep the plan file here for `jx tf diff` (a temp dir)"); p.add_argument("--upgrade", action="store_true", help="init -upgrade first"); p.set_defaults(f=cmd_plan)
    p = sp.add_parser("diff", parents=[g], help="attribute-level before/after of a saved plan")
    p.add_argument("planfile", help="plan file from `jx tf plan --out`"); p.add_argument("address", nargs="*", help="only these resource addresses (all changed)")
    p.add_argument("--root", help="root the plan was made in (the plan file's directory when it holds .terraform)"); p.set_defaults(f=cmd_diff)
    p = sp.add_parser("state", parents=[g], help="state list, filtered")
    p.add_argument("root", help="root directory"); p.add_argument("--grep", metavar="REGEX", help="keep addresses matching this regex (all)"); p.set_defaults(f=cmd_state)
    p = sp.add_parser("doctor", parents=[g], help="local terraform, root's required_version/backend/init state, git context; no remote calls")
    p.add_argument("root", nargs="?", help="root directory (none: terraform version and AWS_PROFILE only)"); p.set_defaults(f=cmd_doctor)
    a = ap.parse_args(); a.f(a)

if __name__ == "__main__": main()
