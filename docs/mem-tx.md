# jx mem and jx tx

The agent's own bookkeeping as commands: the persistent memory files of a project, and the session
transcripts Claude Code keeps for it. Both resolve the project from the cwd (`~/.claude/projects/<slug>`,
every non-alphanumeric character of the path replaced by `-`) or from `--project PATH`. TSV by default,
`--json` in any position.

## Memory

```bash
jx mem ls --grep 'commit|branch'           # recall first: name, type, modified, description
jx mem show <name>                          # one file; name or file stem, hyphens or underscores
jx mem grep 'TZ=UTC'                        # search bodies
jx mem add <slug> --type feedback --desc "one line" --body-file - --link <other-name>
jx mem append <name> --body-file -          # dated addendum
jx mem edit <name> --replace OLD NEW        # OLD must match exactly once
jx mem edit <name> --desc "new one-liner"   # rewrites the frontmatter and its index hook
jx mem doctor [--fix-index]                 # files with no index line, index lines with no file, bad frontmatter
```

`add` refuses an existing slug and refuses a near-duplicate. Similarity is the overlap coefficient on
name-plus-description tokens (shared tokens over the smaller set), threshold 0.5, so a short new
description cannot slip past a long existing one that says the same thing. `--force` overrides. The
file follows the memory contract (frontmatter with name, description, metadata.type) and gets its
`- [Title](file.md) — hook` line in `MEMORY.md`.

`doctor --fix-index` appends an index line for every orphan. The index is loaded into every session's
prefill, so on a large memory directory prefer curating which orphans deserve a line.

## Transcripts

```bash
jx tx sessions --since 7d                   # newest first: id, started, last, messages, title
jx tx grep 'Build the Lever' --since 2d --role assistant --limit 20
jx tx grep 'timeout' --subagents            # include subagent transcripts, role `agent`
jx tx cmds --grep kubectl --since 7d        # the Bash commands past sessions ran, with their descriptions
jx tx cmds --full --sessions 3              # whole commands, newlines escaped
jx tx show <session-prefix> --limit 40      # one session's messages in order; --tools shows tool calls
```

Everything streams line by line and stops early; `sessions` counts messages with a substring check
before any JSON parsing, so 30 sessions over a ~900 MB project directory list in under half a second.
Thinking parts are never printed.

## Why these shapes

The mining that produced this arsenal was itself 343 transcript heredocs; `tx cmds` is that extractor
as a command. `mem add`'s duplicate check encodes the standing rule to look for an existing memory
before writing one. `mem doctor` found, on first run, that 128 of 330 memory files in the main project
had no index line and were therefore invisible to recall.
