# Measuring the arsenal: jx tx usage and jx bench

Two commands answer "did the tools reduce model calls, tokens and time without reducing quality".

## What a session cost: `jx tx usage`

```bash
jx tx usage --since 7d                   # one row per session
jx tx usage --since 8w --by week         # aggregated
```

Columns per session: `calls` (distinct model requests), `out_tok` and `think_tok` (output and the thinking
share of it), `in_tok` (uncached input, including cache creation), `cache_read`, `wall_min` (first to last
message; includes idle time), `model_min` (time from a tool result or user message to the next model
record, summed), `tool_min` (tool_use to tool_result, summed), and the Bash mix: `jx`, `heredoc`
(`python3 - <<EOF` or `python3 -c`), `raw` (everything else).

Read `model_min` and `calls` as the latency lever and `out_tok` as the spend lever. Compare sessions of the
same task class on the same model; `wall_min` spans days for a session left open and says little on its own.

## The paired benchmark: `jx bench`

```bash
jx bench run                              # every question, both arms, fresh session each
jx bench run --only oncall-who,pd-open    # a subset
jx bench report <results-dir>             # paired table, medians, on/off ratios
```

Each question is a factual one with a ground truth the harness computes by command right before the pair
runs. Two fresh `claude -p` sessions answer it in the configured checkout: the `on` arm with the arsenal
first on PATH and the skill symlink pointing at this checkout; the `off` arm with a `jx` shim that exits
127 first on PATH, the skill symlink moved aside, and one appended system-prompt line saying the command
and the checkout do not exist. The first run showed why the third fence is needed: with only the shim and
the hidden skill, an off-arm agent found the checkout by path from its memory and ran `python3 bin/jx`.
The report marks any off session that still touched the checkout as `(touched arsenal)`.
Arms alternate order per question so drift does not favour one. The symlink is restored after every
session, including on error. The prompt asks for a final `ANSWER:` line; grading compares it to the truth
by numbers, names, required tokens or a word-overlap threshold, and the raw answer is kept for a human read.

The question file lives in `~/.config/jx-arsenal/bench/questions.json` because it names real systems.

## Reading the result honestly

- `calls`, `out_tok`, `wall_s` and `cost_usd` come from the session's result JSON; `jx`, `heredoc`, `raw`
  from its transcript. Both arms pay the same environment prefill, which can be hundreds of thousands of
  cached tokens, so compare output tokens and calls first and input tokens last.
- A `FAIL` with an empty answer usually means the turn cap was hit. That is a real outcome, not a grading
  error, but read the transcript before counting it.
- Ten questions is enough to see an order-of-magnitude difference, not a 20 percent one. Each session
  costs real money; run subsets while iterating.
- Quality on the benchmark is correctness of a factual answer. It says nothing about the harder failure
  this arsenal could cause on real work, where a clean table ends verification early. Watch for that in
  `jx tx usage` as a rising `jx` count with a falling share of confirming commands, and in review findings.

## First measurement, 2026-11-06

Ten factual questions drawn from the same usage mining that produced the arsenal, one fresh session per
arm per question, same model in both arms, 20 sessions, about $18. Medians per question:

| | arsenal on | arsenal off | on/off |
|---|---|---|---|
| correct answers | 10/10 | 9/10 | |
| model calls | 4.0 | 5.5 | 0.73 |
| output tokens | 648 | 1,514 | 0.43 |
| thinking tokens | 179 | 489 | 0.37 |
| wall seconds | 22 | 42 | 0.53 |
| cost (USD) | 0.80 | 0.90 | 0.89 |

The off arm's one miss counted one open incident where there were two. Cost barely moves because both
arms pay the same environment prefill on every call; output tokens and calls are where the difference
is. Three questions (a run's failed jobs, pods not ready, a PR's review threads) were solved by both
arms with one raw `gh` or `kubectl` call and show no difference, which is the honest shape: the arsenal
pays off where a question needs auth, pagination, a port-forward, or a reshape, and does nothing where a
one-liner already exists.

What the first run taught, and what changed because of it: the off arm found the checkout by path and
ran `bin/jx` directly (now fenced and flagged); `jx temporal failed --limit 200` ran 200 serial describes
and blew the Bash timeout (now concurrent, 23 seconds); the Temporal question asked for "the most common
failure" over 12,000 failures while the tool samples 200, and the agent with the tool spent 25 turns
trying to be more rigorous than the grader (question now names the sample); two grader misses were the
grader's (`T` versus space in a timestamp, an off-by-one on days).

Limits: n=10, one run per arm, so the per-question numbers have run-to-run variance (the same off-arm
question took 9, 10 and 25 calls across three runs) and only the medians and the ratios above are worth
quoting. Correctness here is factual; it says nothing about the harder failure on real work, where a
clean table ends verification early.
