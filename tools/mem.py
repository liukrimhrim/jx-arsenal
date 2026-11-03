#!/usr/bin/env python3
"""jx mem — the agent's persistent memory files for the current project. TSV by default, --json for raw.

  jx mem ls [--type T] [--grep REGEX]          every memory: name, type, modified, description (from frontmatter)
  jx mem show <name>                           print one memory file (name or file stem)
  jx mem grep <REGEX>                          search bodies: name, line number, line
  jx mem doctor [--fix-index]                  files reachable from neither MEMORY.md nor a hub it links, dangling index lines, bad frontmatter; --fix-index appends index lines
  jx mem add <slug> --type T --desc "..." --body-file F|- [--link NAME ...] [--force]   new memory file + index line; refuses existing slugs and near-duplicates
  jx mem append <name> --body-file F|-         append a dated addendum to one memory
  jx mem edit <name> [--replace OLD NEW] [--desc "..."]   exact single-occurrence replace; --desc rewrites the description and its index hook

Memory lives in ~/.claude/projects/<slug>/memory/ where <slug> is the cwd with every non-alphanumeric character replaced by `-`.
--project PATH picks another checkout. Nothing machine-specific lives in this script.
"""
import argparse, json, os, re, signal, sys
from datetime import datetime, timezone
signal.signal(signal.SIGPIPE, signal.SIG_DFL)

def mem_dir(a):
    root = os.path.abspath(a.project or os.getcwd())
    d = os.path.join(os.path.expanduser("~/.claude/projects"), re.sub(r"[^A-Za-z0-9_-]", "-", root), "memory")
    if not os.path.isdir(d):
        have = sorted(x for x in os.listdir(os.path.expanduser("~/.claude/projects")) if os.path.isdir(os.path.join(os.path.expanduser("~/.claude/projects"), x, "memory")))
        sys.exit(f"no memory dir for {root} ({d}). Projects with memory: {', '.join(have)}. Pass --project PATH.")
    return d

def frontmatter(text):
    """Returns (dict, body). Flat `key: value` plus one level of indented metadata keys; good enough for these files."""
    m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not m: return {}, text
    fm, fm_meta, cur = {}, {}, None
    for line in m.group(1).splitlines():
        if re.match(r"^\s+\w+:", line) and cur: fm_meta[line.split(":", 1)[0].strip()] = line.split(":", 1)[1].strip().strip('"')
        elif ":" in line: k, v = line.split(":", 1); cur = k.strip(); fm[cur] = v.strip().strip('"')
    fm.update({k: v for k, v in fm_meta.items() if k not in fm}); return fm, m.group(2)

def load(d):
    out = []
    for f in sorted(os.listdir(d)):
        if not f.endswith(".md") or f == "MEMORY.md": continue
        text = open(os.path.join(d, f)).read(); fm, body = frontmatter(text)
        out.append({"file": f, "name": fm.get("name", f[:-3]), "type": fm.get("type", f.split("_", 1)[0] if "_" in f else ""), "description": fm.get("description", ""),
                    "modified": datetime.fromtimestamp(os.path.getmtime(os.path.join(d, f)), timezone.utc).strftime("%Y-%m-%d"), "body": body, "fm_ok": bool(fm)})
    return out

def tsv(rows, header):
    esc = lambda v: str(v).replace("\t", " ").replace("\n", "\\n") if v is not None else ""
    print("\t".join(header))
    for r in rows: print("\t".join(esc(r.get(h)) for h in header))
def emit(rows, header, a): print(json.dumps(rows, indent=1)) if a.json else tsv(rows, header)

def find(d, name):
    ms = [m for m in load(d) if m["name"] == name or m["file"][:-3] == name or m["file"] == name]
    if not ms: sys.exit(f"no memory named {name!r}; `jx mem ls --grep {name}` searches names and descriptions")
    return ms[0]

def cmd_ls(a):
    rows = load(mem_dir(a))
    if a.type: rows = [r for r in rows if r["type"] == a.type]
    if a.grep: rx = re.compile(a.grep, re.I); rows = [r for r in rows if rx.search(r["name"] + " " + r["description"])]
    rows.sort(key=lambda r: r["modified"], reverse=True)
    emit([{k: r[k] for k in ("name", "type", "modified", "description")} for r in rows], ["name", "type", "modified", "description"], a)
    sys.stderr.write(f"{len(rows)} memories\n")

def cmd_show(a): print(open(os.path.join(mem_dir(a), find(mem_dir(a), a.name)["file"])).read(), end="")

def cmd_grep(a):
    rx = re.compile(a.regex, re.I); rows = []
    for m in load(mem_dir(a)):
        for i, line in enumerate(m["body"].splitlines(), 1):
            if rx.search(line): rows.append({"name": m["name"], "line": i, "text": line.strip()[:200]})
    emit(rows, ["name", "line", "text"], a); sys.stderr.write(f"{len(rows)} hits\n")

def cmd_doctor(a):
    d = mem_dir(a); mems = load(d); idx = open(os.path.join(d, "MEMORY.md")).read() if os.path.exists(os.path.join(d, "MEMORY.md")) else ""
    links = lambda text: set(re.findall(r"\]\(([^)]+\.md)\)", text))
    direct = links(idx); files = {m["file"] for m in mems}
    # a file the index links that itself links three or more memories is a hub (sub-index); what it links counts as indexed
    hubs = sorted(f for f in direct & files if len(links(open(os.path.join(d, f)).read())) >= 3)
    via_hub = {t for f in hubs for t in links(open(os.path.join(d, f)).read())} - direct
    linked = direct | via_hub
    if a.fix_index:
        missing = [m for m in mems if m["file"] not in linked]
        append_index(d, [index_line(m["file"], m["name"], m["description"]) for m in missing]); linked |= {m["file"] for m in missing}
        idx = open(os.path.join(d, "MEMORY.md")).read(); sys.stderr.write(f"added {len(missing)} index lines\n")
    rows = [{"issue": "unreachable: not in MEMORY.md nor in a hub it links", "file": f} for f in sorted(files - linked)]
    rows += [{"issue": "index points at missing file", "file": f} for f in sorted(linked - files)]
    rows += [{"issue": "no frontmatter", "file": m["file"]} for m in mems if not m["fm_ok"]]
    rows += [{"issue": "frontmatter name differs from file stem (fine, but search by either)", "file": m["file"]} for m in mems if m["name"].replace("-", "_") != m["file"][:-3].replace("-", "_")]
    emit(rows, ["issue", "file"], a)
    sys.stderr.write(f"{len(mems)} memories: {len(direct & files)} linked from MEMORY.md, {len(via_hub & files)} via {len(hubs)} hubs ({', '.join(hubs)}); {len(idx.splitlines())} index lines; {len(rows)} issues\n")

STOP = {"that", "this", "with", "from", "when", "what", "then", "than", "have", "into", "over", "only", "your", "them", "they", "also", "been", "does", "must", "never",
        "always", "before", "after", "should", "about", "which", "their", "there", "where", "while", "will", "would", "could", "more", "some", "such", "each", "very", "just", "like", "memory"}
def tokens(*s): return {w for w in re.findall(r"[a-z0-9]+", " ".join(s).lower()) if len(w) >= 4 and w not in STOP}
def overlap(x, y): return len(x & y) / min(len(x), len(y)) if x and y else 0.0  # smaller-set denominator: a short new description cannot hide behind a long existing one
def body_arg(a): return sys.stdin.read() if a.body_file == "-" else open(a.body_file).read()
def today(): return datetime.now(timezone.utc).strftime("%Y-%m-%d")
def title(slug): return slug.replace("-", " ").replace("_", " ").capitalize()
def index_line(file, slug, desc): return f"- [{title(slug)}]({file}) — {desc[:80]}"
def append_index(d, lines):
    p = os.path.join(d, "MEMORY.md"); old = open(p).read() if os.path.exists(p) else ""
    with open(p, "a") as fh: fh.write(("" if not old or old.endswith("\n") else "\n") + "".join(l + "\n" for l in lines))

def cmd_add(a):
    d = mem_dir(a); mems = load(d); slug = a.slug.strip().lower().replace("_", "-"); file = f"{a.type}_{slug.replace('-', '_')}.md"
    norm = lambda s: s.replace("-", "_")
    for m in mems:
        if norm(slug) in (norm(m["name"]), norm(m["file"][:-3])) or norm(m["file"][:-3]) == file[:-3]:
            sys.exit(f"memory {m['name']!r} already exists ({m['file']}); `jx mem append {m['name']}` extends it")
    if not a.force:
        new = tokens(slug, a.desc); scored = sorted(((overlap(new, tokens(m["name"], m["description"])), m["name"]) for m in mems), reverse=True)[:3]
        if scored and scored[0][0] >= 0.5:
            sys.exit("near-duplicates: " + ", ".join(f"{n} ({s:.2f})" for s, n in scored) + f"; `jx mem append {scored[0][1]}` to extend it, or --force to add anyway")
    body = body_arg(a).strip()
    if a.link: body += "\n\nRelated: " + ", ".join(f"[[{l}]]" for l in a.link)
    text = f"---\nname: {slug}\ndescription: {a.desc}\nmetadata:\n  type: {a.type}\n---\n\n{body}\n"
    line = index_line(file, slug, a.desc); open(os.path.join(d, file), "w").write(text); append_index(d, [line])
    emit([{"file": file, "index_line": line}], ["file", "index_line"], a)

def cmd_append(a):
    d = mem_dir(a); p = os.path.join(d, find(d, a.name)["file"])
    with open(p, "a") as fh: fh.write(f"\n\n**{today()} addendum:** {body_arg(a).strip()}\n")
    emit([{"file": os.path.basename(p), "lines": len(open(p).read().splitlines())}], ["file", "lines"], a)

def cmd_edit(a):
    if not a.replace and a.desc is None: sys.exit("nothing to do: pass --replace OLD NEW and/or --desc TEXT")
    d = mem_dir(a); file = find(d, a.name)["file"]; p = os.path.join(d, file); text = open(p).read(); changed = []
    if a.replace:
        old, new = a.replace; n = text.count(old)
        if n != 1: sys.exit(f"--replace OLD matches {n} times in {file}; it must match exactly once")
        text = text.replace(old, new); changed.append("body")
    if a.desc is not None:
        text, n = re.subn(r"^description: .*$", f"description: {a.desc}", text, count=1, flags=re.M)
        if not n: sys.exit(f"{file} has no `description:` frontmatter line")
        changed.append("description"); ip = os.path.join(d, "MEMORY.md"); idx = open(ip).read()
        idx, n = re.subn(r"(\]\(" + re.escape(file) + r"\)\s*—\s*)[^·\n]*", lambda m: m.group(1) + a.desc[:80], idx, count=1)
        if n: open(ip, "w").write(idx); changed.append("index hook")
        else: sys.stderr.write(f"no `]({file}) — hook` index line to rewrite; `jx mem doctor` lists index issues\n")
    open(p, "w").write(text); emit([{"file": file, "changed": ", ".join(changed)}], ["file", "changed"], a)

def main():
    ap = argparse.ArgumentParser(prog="jx mem", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="JSON rows instead of TSV"); ap.add_argument("--project", help="checkout path whose memory to use (cwd)")
    g = argparse.ArgumentParser(add_help=False); g.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="JSON rows instead of TSV"); g.add_argument("--project", default=argparse.SUPPRESS, help="checkout path whose memory to use (cwd)")
    sp = ap.add_subparsers(dest="cmd", required=True, metavar="<command>")
    p = sp.add_parser("ls", parents=[g], help="list memories with type, modified date and description"); p.add_argument("--type", help="feedback | project | reference | user"); p.add_argument("--grep", help="regex over name and description"); p.set_defaults(f=cmd_ls)
    p = sp.add_parser("show", parents=[g], help="print one memory file"); p.add_argument("name", help="frontmatter name or file stem"); p.set_defaults(f=cmd_show)
    p = sp.add_parser("grep", parents=[g], help="search memory bodies"); p.add_argument("regex"); p.set_defaults(f=cmd_grep)
    p = sp.add_parser("doctor", parents=[g], help="index/file mismatches and bad frontmatter"); p.add_argument("--fix-index", action="store_true", help="append an index line for every memory file that has none (off)"); p.set_defaults(f=cmd_doctor)
    p = sp.add_parser("append", parents=[g], help="append a dated addendum to one memory"); p.add_argument("name", help="frontmatter name or file stem"); p.add_argument("--body-file", required=True, help="file holding the addendum, or - for stdin (required)"); p.set_defaults(f=cmd_append)
    p = sp.add_parser("edit", parents=[g], help="replace one exact string and/or rewrite the description"); p.add_argument("name", help="frontmatter name or file stem")
    p.add_argument("--replace", nargs=2, metavar=("OLD", "NEW"), help="exact string to replace; OLD must occur exactly once (none)"); p.add_argument("--desc", help="new frontmatter description; the index line's hook follows (unchanged)"); p.set_defaults(f=cmd_edit)
    p = sp.add_parser("add", parents=[g], help="write a new memory file and its MEMORY.md index line; refuses existing slugs and near-duplicates")
    p.add_argument("slug", help="hyphenated name, e.g. prefer-x-over-y; file becomes <type>_<slug_with_underscores>.md")
    p.add_argument("--type", required=True, choices=["user", "feedback", "project", "reference"], help="memory type (required)")
    p.add_argument("--desc", required=True, help="one-line description; also the index hook, truncated to 80 chars (required)")
    p.add_argument("--body-file", required=True, help="file holding the body, or - for stdin (required)")
    p.add_argument("--link", action="append", metavar="NAME", help="related memory name; repeatable; written as a final `Related: [[a]], [[b]]` line (none)")
    p.add_argument("--force", action="store_true", help="add even when a near-duplicate scores >= 0.5 (off)")
    p.set_defaults(f=cmd_add)
    a = ap.parse_args(); a.f(a)

if __name__ == "__main__": main()
