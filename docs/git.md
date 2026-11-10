# jx git

The git idioms that get rewritten every session, as one command each, plus one guarded writer.
TSV where a table fits, `--json` in any position where a payload exists. No company or repo
names live in the tool; refs and paths come from the caller.

## Read without touching the checkout

```bash
jx git cat path/to/file                      # origin/main's copy, after a quiet fetch
jx git cat a.py b.py --pr 1234               # the PR head's copies; fetches pull/1234/head into refs/remotes/pr/1234
jx git cat docs/PLAN.md --outline            # markdown headings (def/class lines for .py) with line numbers
jx git cat docs/PLAN.md --section 'speed'    # one section, through to the next heading of its level or higher
jx git cat src/a.py --grep 'def main|add_argument' -C 2 -i   # numbered matches with context, `--` between hunks
jx git cat src/a.py --lines 150:185          # a numbered range; A: or :B leave an end open
jx git ls infra/environments -r --ref origin/release
jx git since                                 # branch, base, merge-base, ahead/behind, files changed, uncommitted
jx git since --log | --files | --stat
jx git export origin/main /tmp/main-tree     # git archive + a .git-sha file, for tests that must not see the working tree
jx git touching src/billing tests/billing    # open PRs whose files overlap (gh)
jx git doctor                                # toplevel, branch, upstream, ahead/behind, uncommitted, shallow, origin, local-main age
```

On a shallow clone `origin/main` can exist without the history that joins it to `HEAD`. `since` then
falls back to local `main` and says so, and says how long ago local `main` last moved relative to
`origin/main`, because numbers against a stale base mislead.

## The one writer

```bash
jx git commit -m "subject" --expect-branch my-branch -- path/one path/two
jx git commit -F msg.md --trailer "Co-Authored-By: Name <mail>" --push -- path
```

Guards run before any write, each a one-line refusal naming the tell and the fix:
on `main` or `master`; `HEAD` not the branch you expected (the checkout switched under you); no paths
named (this command never stages everything); a path that does not exist (shell cwd drift); a path
with nothing to commit. Other changed paths not in the commit print a warning naming them, since in a
shared checkout they are usually another agent's work; `--strict` makes that a refusal.

After the commit it compares the committed file list to the requested paths and fails on any
difference. With `--push` it pushes with `-u` and checks that `origin/<branch>` equals `HEAD`, so a
swallowed failure cannot be reported as "pushed".

## Worktrees for a dirty shared checkout

```bash
jx git wt fix-x --copy src/thing.py --copy tests/   # fresh branch off origin/main under $TMPDIR, your edits copied in
jx git wt ls
jx git wt rm "$TMPDIR/jx-wt-fix-x"                   # never the main worktree
```
