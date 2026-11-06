#!/usr/bin/env python3
"""jx review — prepare a GitHub PR once; recheck code and feedback without model polling.

  jx review <pr> [--since SHA] [--out DIR] [--paths P ...]
  jx review <pr> --check-only [--previous SNAPSHOT] [--local] [--out DIR]
  jx review --selftest

Run inside the repo checkout. Reads GitHub and fetches Git objects; never switches
branches, creates worktrees or posts comments. Writes a bundle to DIR
(default $TMPDIR/review-prep/OWNER/REPO/pr<N>, reused for the same PR):

  tree/             snapshot of the whole repo at the PR head (--paths limits it)
  diff-code.patch   merge-base..head with repository-configured exclusions
  diffstat.txt      every changed file; excluded files grouped per directory
  delta-code.patch  --since only: since..head (same exclusions; limited to the PR's own
                    files when the base branch was merged in since then)
  range-diff.txt    --since only, when the branch was rebased

Every call saves snapshot.json, body.md and feedback.md, including paginated inline
comments and reviews. --check-only skips fetching/exporting code. --previous compares
the saved review inputs, including edited/deleted feedback even when HEAD is unchanged.
--local also compares local HEAD and the remote branch with the PR head and requires
a clean checkout. It does not push. This is for the author's checkout before reporting.
Exit codes: 0 captured/changed; 1 local/published mismatch; 3 review inputs unchanged;
2 usage, git or gh failure.
Exit 3 says nothing about CI success, correctness, or whether findings were addressed.
Optional .jx/review.json at the checkout root sets {"exclude": ["GIT_GLOB", ...]}.
Without this file, all changed files are included. Effective exclusions are saved
in snapshot.json; changing them invalidates old patches even at the same PR head.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

CONFIG = Path(".jx/review.json")
STANDARDS = {"AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "CODING_STANDARDS.md", "CONTEXT.md", "README.md"}
MARKER = ".review-prep"
FIELDS = ("number,title,body,state,isDraft,url,author,headRefName,baseRefName,headRefOid,baseRefOid,"
          "mergeable,mergeStateStatus,mergeCommit,statusCheckRollup")


def feedback(owner, name, pr):
    result = {}
    for kind, endpoint in (("comment", f"issues/{pr}/comments"),
                           ("review", f"pulls/{pr}/reviews"), ("inline", f"pulls/{pr}/comments")):
        pages = json.loads(run("gh", "api", f"repos/{owner}/{name}/{endpoint}?per_page=100", "--paginate", "--slurp"))
        for page in pages:
            for item in page:
                record = {k: item.get(k) for k in ("body", "updated_at", "submitted_at", "state", "commit_id",
                          "path", "line", "original_line", "in_reply_to_id", "html_url")}
                record["author"] = (item.get("user") or {}).get("login")
                result[f"{kind}:{item['id']}"] = record
    return result


def changes(before, after):
    if before["pr"]["url"] != after["pr"]["url"]:
        die("--previous belongs to another PR")
    old, new = before["feedback"], after["feedback"]
    return {
        "head": before["pr"]["headRefOid"] != after["pr"]["headRefOid"],
        "base": before["pr"]["baseRefOid"] != after["pr"]["baseRefOid"],
        "description": any(before["pr"].get(k) != after["pr"].get(k) for k in ("title", "body")),
        "state": before["pr"]["state"] != after["pr"]["state"],
        "exclusions": before.get("exclude", []) != after["exclude"],
        "feedback": {"added": sorted(new.keys() - old.keys()), "removed": sorted(old.keys() - new.keys()),
                     "edited": sorted(k for k in old.keys() & new.keys() if old[k] != new[k])},
    }


def die(message, code=2):
    print(f"review-prep: {message}", file=sys.stderr)
    raise SystemExit(code)


def run(*cmd, ok=(0,)):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode not in ok:
        die(f"{' '.join(cmd[:4])} ... exited {p.returncode}: {p.stderr.strip()[:400]}")
    return p.stdout


def git(*args, ok=(0,)):
    return run("git", *args, ok=ok).strip()


def exclusions():
    if not CONFIG.exists():
        return []
    config = json.loads(CONFIG.read_text())
    if (not isinstance(config, dict) or set(config) != {"exclude"}
            or not isinstance(config["exclude"], list)
            or any(not isinstance(p, str) or not p.strip() for p in config["exclude"])):
        die(f'{CONFIG} must contain only {{"exclude": ["GIT_GLOB", ...]}}')
    return config["exclude"]


def exclude_specs(exclude):
    return [f":(top,exclude,glob){pattern}" for pattern in exclude]


def when(iso):
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def numstat(a, b, exclude=()):
    rows = []
    for line in git("diff", "--numstat", "--no-renames", a, b, "--", ".", *exclude_specs(exclude)).splitlines():
        add, dele, path = line.split("\t", 2)
        rows.append((0 if add == "-" else int(add), 0 if dele == "-" else int(dele), path))
    return rows


def stat_lines(rows, included=None):
    """Included files one per line; excluded files folded to one line per directory."""
    included = {r[2] for r in rows} if included is None else included
    code = [r for r in rows if r[2] in included]
    folded = defaultdict(lambda: [0, 0, 0])
    for add, dele, path in rows:
        if path not in included:
            key = "/".join(path.split("/")[:3]) if path.count("/") >= 3 else path.rsplit("/", 1)[0] or path
            f = folded[key]
            f[0] += add; f[1] += dele; f[2] += 1
    out = [f"+{a:<6} -{d:<6} {p}" for a, d, p in sorted(code, key=lambda r: -r[0])]
    out += [f"+{a:<6} -{d:<6} {k}/  ({n} files excluded from the patch)" for k, (a, d, n) in sorted(folded.items())]
    return code, out


def write_patch(path, a, b, paths=(".",), exclude=()):
    patch = run("git", "diff", a, b, "--", *paths, *exclude_specs(exclude))
    path.write_text(patch)
    return len(patch)


def selftest():
    assert when("2026-11-05T16:00:00-08:00") == when("2026-11-06T00:00:00Z")
    rows = [(5, 1, "a/b/c.py"), (9, 0, "build/out/x.json"), (1, 0, "build/out/y.json")]
    code, lines = stat_lines(rows, {"a/b/c.py"})
    assert [r[2] for r in code] == ["a/b/c.py"] and "2 files excluded" in lines[-1]
    assert stat_lines(rows)[0] == rows
    print("selftest ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr", nargs="?", type=int)
    ap.add_argument("--since", help="SHA you last reviewed; report only what changed after it")
    ap.add_argument("--check-only", action="store_true", help="capture current PR and feedback without exporting code")
    ap.add_argument("--previous", help="snapshot.json from the last review; detect edited/deleted feedback too")
    ap.add_argument("--local", action="store_true", help="verify clean local HEAD and remote branch both match PR head")
    ap.add_argument("--out", help="bundle directory")
    ap.add_argument("--paths", nargs="*", default=[], help="snapshot only these paths instead of the whole repo")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if args.pr is None:
        ap.error("pr number is required")

    args.out = str(Path(args.out).resolve()) if args.out else None
    args.previous = str(Path(args.previous).resolve()) if args.previous else None
    os.chdir(git("rev-parse", "--show-toplevel"))
    exclude = exclusions()

    pr = json.loads(run("gh", "pr", "view", str(args.pr), "--json", FIELDS))
    owner, name = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/", pr["url"]).groups()
    head, base = pr["headRefOid"], pr["baseRefName"]
    out = Path(args.out or Path(tempfile.gettempdir()) / "review-prep" / owner / name / f"pr{args.pr}").resolve()
    if out.exists() and any(out.iterdir()) and not (out / MARKER).exists():
        die(f"{out} exists, is not empty and was not made by review-prep; pass another --out")
    source = {"url": pr["url"], "head": head, "base": pr["baseRefOid"], "merge": pr.get("mergeCommit"),
              "exclude": exclude}
    old_source = json.loads((out / "snapshot.json").read_text()).get("source") if (out / "snapshot.json").exists() else None
    snapshot = {"captured_at": datetime.now(timezone.utc).isoformat(), "pr": pr,
                "feedback": feedback(owner, name, args.pr), "source": old_source, "exclude": exclude}
    snapshot["changes"] = changes(json.loads(Path(args.previous).read_text()), snapshot) if args.previous else None
    if old_source != source:
        if (out / "tree").exists():
            shutil.rmtree(out / "tree")
        for artifact in ("diff-code.patch", "diffstat.txt", "delta-code.patch", "range-diff.txt"):
            (out / artifact).unlink(missing_ok=True)
        snapshot["source"] = None
    if args.local:
        local_head = git("rev-parse", "HEAD")
        remote = git("ls-remote", "origin", f"refs/heads/{pr['headRefName']}").split("\t")[0]
        dirty = git("status", "--porcelain=v1")
        snapshot["local"] = {"head": local_head, "remote_head": remote, "status": dirty,
                             "matches_published": local_head == remote == head and not dirty}
    out.mkdir(parents=True, exist_ok=True)
    (out / MARKER).write_text(head + "\n")
    (out / "snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    (out / "body.md").write_text(pr.get("body") or "")
    (out / "feedback.md").write_text("\n\n".join(
        f"## {key} — {item['author'] or 'deleted user'}\n{item['html_url'] or ''}\n"
        f"{item['path'] or ''}:{item['line'] or ''}\n\n{item['body'] or ''}"
        for key, item in snapshot["feedback"].items()) + "\n")

    print(f"PR {pr['number']}: {pr['title']}")
    print(f"url: {pr['url']}")
    print(f"state: {pr['state']}{' draft' if pr['isDraft'] else ''}  mergeable: {pr['mergeable']} {pr['mergeStateStatus']}")
    print(f"head: {head}  ({pr['headRefName']})")
    print(f"Review-Key: github.com/{owner}/{name}@{head}")
    print(f"review exclusions: {json.dumps(exclude)}")
    checks = pr.get("statusCheckRollup") or []
    print("checks: " + ("; ".join(f"{c.get('workflowName') or c.get('context')}/{c.get('name', '')}="
                                   f"{c.get('conclusion') or c.get('status') or c.get('state')}" for c in checks) or "none"))
    print(f"feedback: {len(snapshot['feedback'])} records; full text: {out / 'feedback.md'}")
    unchanged = False
    if snapshot["changes"] is not None:
        delta = snapshot["changes"]
        print("changes: " + json.dumps(delta, sort_keys=True))
        unchanged = not any(delta[k] for k in ("head", "base", "description", "state", "exclusions")) and not any(delta["feedback"].values())
    print(f"snapshot: {out / 'snapshot.json'}")
    if args.local:
        print("local: " + json.dumps(snapshot["local"], sort_keys=True))
        if not snapshot["local"]["matches_published"]:
            return 1
    if args.check_only:
        if unchanged:
            print("Review inputs unchanged. CI and review completion are separate checks.")
        return 3 if unchanged else 0

    run("git", "fetch", "-q", "origin", base, f"pull/{args.pr}/head")
    git("cat-file", "-e", f"{head}^{{commit}}")
    merged = (pr.get("mergeCommit") or {}).get("oid")
    base_tip = f"{merged}^1" if merged else f"origin/{base}"
    mb = git("merge-base", base_tip, head)
    print(f"base: {base_tip}  merge-base: {mb}")

    since = None
    if args.since:
        since = git("rev-parse", "--verify", "-q", f"{args.since}^{{commit}}", ok=(0, 1))
        if not since:
            die(f"--since {args.since} is not a commit in this checkout")
        if since == head:
            print("No new code. Feedback was refreshed; use --previous to detect comment edits.")
            return 3 if unchanged else 0
    for stale_file in ("delta-code.patch", "range-diff.txt"):
        (out / stale_file).unlink(missing_ok=True)

    rows = numstat(mb, head)
    included = {r[2] for r in numstat(mb, head, exclude)} if exclude else None
    code, lines = stat_lines(rows, included)
    (out / "diffstat.txt").write_text("\n".join(lines) + "\n")
    size = write_patch(out / "diff-code.patch", mb, head, exclude=exclude)
    commits = git("log", "--oneline", f"{mb}..{head}").splitlines()
    print(f"commits: {len(commits)}  files: {len(rows)} ({len(code)} included, {len(rows) - len(code)} excluded)  "
          f"+{sum(r[0] for r in rows)} -{sum(r[1] for r in rows)}  diff-code.patch: {size} bytes")
    for line in lines[:30]:
        print("  " + line)
    if len(lines) > 30:
        print(f"  ... {len(lines) - 30} more in diffstat.txt")

    if since:
        if subprocess.run(["git", "merge-base", "--is-ancestor", since, head]).returncode == 0:
            delta = git("log", "--oneline", "--no-merges", f"{since}..{head}", f"^{base_tip}").splitlines()
            # If the base was merged in after `since`, a plain since..head diff is mostly the base's changes.
            base_moved = git("merge-base", base_tip, since) != mb
            own = [r[2] for r in code] if base_moved else ["."]
            dsize = write_patch(out / "delta-code.patch", since, head, own, exclude=exclude) if own else 0
            own_set = {r[2] for r in code}
            delta_rows = [r for r in numstat(since, head) if not base_moved or r[2] in own_set]
            delta_included = {r[2] for r in numstat(since, head, exclude)} if exclude else None
            _, dlines = stat_lines(delta_rows, delta_included)
            note = " (base merged in since then; delta limited to this PR's files)" if base_moved else ""
            print(f"since {since[:9]}: {len(delta)} new commits, delta-code.patch: {dsize} bytes{note}")
            for line in delta[:15] + ["--"] + dlines[:20]:
                print("  " + line)
        else:
            old_base = git("merge-base", base_tip, since)
            rd = git("range-diff", "--no-color", f"{old_base}..{since}", f"{mb}..{head}")
            (out / "range-diff.txt").write_text(rd + "\n")
            heads = [ln for ln in rd.splitlines() if re.match(r"\s*(\d+|-):", ln)]
            print(f"since {since[:9]}: REBASED. range-diff.txt has the per-commit comparison "
                  f"('=' same patch, '!' changed, '>' new, '<' dropped):")
            for line in heads[:20]:
                print("  " + line[:150])

    tree = out / "tree"
    if tree.exists():
        shutil.rmtree(tree)
    tree.mkdir()
    archive = subprocess.Popen(["git", "archive", head, "--", *args.paths], stdout=subprocess.PIPE)
    subprocess.run(["tar", "-x", "-C", str(tree)], stdin=archive.stdout, check=True)
    if archive.wait() != 0:
        die("git archive failed")
    snapshot["source"] = source
    (out / "snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    print(f"tree/ holds: {' '.join(args.paths) or 'whole repo at head'}")

    dirs = {""} | {"docs"}
    for _, _, path in code:
        parts = path.split("/")[:-1]
        dirs |= {"/".join(parts[:i]) for i in range(1, len(parts) + 1)}
    found = [p for p in git("ls-tree", "-r", "--name-only", head).splitlines()
             if p.rsplit("/", 1)[-1] in STANDARDS and (p.rsplit("/", 1)[0] if "/" in p else "") in dirs]
    print("standards docs at head: " + (" ".join(found) or "none found"))
    print(f"bundle: {out}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as error:
        die(str(error))
