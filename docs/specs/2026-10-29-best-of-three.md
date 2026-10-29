---
status: active
created: 2026-10-29
branch: best-of-three
---

# Best of three: Claude Code, pi and dsh

## Goal

Every part of what the model sees (the context) follows the best of Claude Code (CC),
pi (pi-mono) and DeepSeek Harness (dsh), with CC as the default; every place noeta differs
is written down here with how the others do it and why noeta's way is better, backed by an
eval or a measured token count. `make parity` shows no difference that is not marked as an
intentional deviation. Every extension point of pi and dsh is compared in
`docs/extending.md`, and writing a plugin takes no more steps than in pi. The eval covers
every feature area with DeepSeek V4.1, and a current public coding-agent benchmark is run
and reported in the README.

## Non-goals

- Features outside the three kinds noeta builds: what CC has, what running on several
  machines needs, and the extension mechanism itself. Anything else is left to plugins;
  unclear cases are asked, not guessed.
- Pushing, releasing, or touching another program's data (owner sign-off).
- Reading `~/.claude`, `.claude` or `CLAUDE.md`; uploading anything.

## Context

- CC baseline: 2.1.312, recorded in `parity/2.1.312/` (`scripts/parity/README.md`); accepted
  deviations in `parity/deviations.md` (ADR 003).
- pi: `badlogic/pi-mono` @ e7a0df2 (2026-10-29). dsh: `deepseek-ai/DeepSeek-Harness` @ f3803c96
  (0.2.2-alpha.2, 2026-10-28). Both read from shallow clones, never vendored.
- Eval model: DeepSeek V4.1, gateway id `opensource/deepseek_v4_1_flash` (the gateway's only
  V4.1 model; it answers as `deepseek-v4.1-flash`), Anthropic wire.
- Starting point: local main @37fd9997, `make check` 2424 passed.

## Decisions

## Plan

### Phase 0: baseline
- [x] DeepSeek V4.1 eval of the existing 13 scenarios on main, 9 trials, archived.

### Phase 1: context audit (branch `ctx-*`)
- [ ] Per-area comparison CC / pi / dsh / noeta: system prompt, reminders, environment and
      git snapshot, attachments and images, skills, memory, project files, tool definitions
      and deferred tools, compaction, sub-agents, plan mode, todos.
- [ ] Revert every unexplained difference to CC; adopt pi/dsh where better, each with numbers.
- [ ] Every remaining difference recorded below (others / noeta / why better / evidence) and,
      when the replay can see it, marked in `parity/deviations.md`.

### Phase 2: extension points (branch `ext-*`)
- [ ] Item-by-item table of pi and dsh extension points in `docs/extending.md`, each with a
      verdict and reason.
- [ ] Plugin authoring steps no more than pi's.

### Phase 3: eval coverage (branch `eval-coverage`)
- [ ] A scenario for each area: skills, reminders, memory, compaction continuation,
      sub-agents and parallel, plan mode, todos, permissions and approval, hooks, MCP,
      prompt injection, tool selection, ask vs act.
- [ ] 9 trials each on DeepSeek V4.1; results and baseline archived.

### Phase 4: benchmark
- [ ] Pick one or two current benchmarks, with reasons.
- [ ] Run noeta and, where possible, CC on DeepSeek V4.1; version, samples, runs and cost in
      the README.

### Phase 5: wrap-up
- [ ] `make check`, `make parity`, `make cluster`, `make e2e` green on main.

## Acceptance criteria

1. Each context area follows the best of the three, or this spec argues with evidence why
   noeta's way is better; `make parity` shows zero differences besides marked deviations.
2. The extension-point table is complete, each row with a verdict and reason; a plugin takes
   no more steps than in pi.
3. Every feature area has an eval scenario; 9 trials each on DeepSeek V4.1, results and
   baseline archived.
4. The benchmark result with version, samples and cost is in the README.
5. `make check`, `make parity`, `make cluster` and `make e2e` are green on main.

## Risks

- The gateway rate-limits (HTTP 429) under parallel eval load; a 0 from a 429 is a harness
  failure, not a regression.
- DeepSeek V4.1 is weaker at tool use than the model the baseline was recorded on; a new
  baseline on DeepSeek is recorded before any comparison.

## Progress log
- 2026-10-29 Started from local main @37fd9997; `make check` 2424 passed.
- 2026-10-29 Baseline on main: `make parity` 0 mismatches (11 accepted deviations); `make cluster` 13/13; DeepSeek V4.1 eval 9 trials overall 0.919 (`evals/results/ds41-main-37fd9997.json`): every scenario 1.00 except delegate_parallel_review 0.11 (5 of 9 hit the 240 s trial timeout), honest_test_failure 0.89, concise_direct_answer 0.94. 522 requests, 6.7M input tokens (5.3M cached), 125k output.
- 2026-10-29 T3: ToolSearch `+word` also matches a whole word of the description, as Claude Code filters (`noeta/plugins/tool_search/search.py`).
- 2026-10-29 M5: the recall selector picks by subject and is sparing with [user]/[project] memories (`memory/recall`); `side.recall` rules added.
- 2026-10-29 M3: a recalled memory shows its first 200 lines / 4,096 bytes with a note naming the file to Read (`noeta/plugins/memory/extension.py`).
- 2026-10-29 M4: a recalled memory older than a day carries an age-and-verify note ahead of its path.
- 2026-10-29 M6: recall skips sub-agents and one-word (non-CJK) messages, and never offers a memory file the task already Read.
- 2026-10-29 S3: a skill listing line is capped at 1,536 characters + `…` (was 384 estimated tokens, which cut CJK descriptions at 384 characters).
- 2026-10-29 S12: the noeta-only `priority:` skill key is gone; over-budget keep order is rank, tier, name.
- 2026-10-29 S7/S9: a loaded skill is the base-directory line and its body (no "Activated skill" header or description, one blank line); the Skill result says the skill starts now and its text follows; the post-compaction block follows.
- 2026-10-29 S6: a skill whose listed text changed is listed again (digest kept in the listing record), under the same header as the first listing.
- 2026-10-29 S10: a skill named mid-message (`/<name>` not as the leading command) gets a skill-mention note (`reminder/skill_mention`).
- 2026-10-29 S11: a `.noeta/skills` / `.agents/skills` below the working directory joins the task when a file under it is touched, announced by name (directories recorded in the log).
- 2026-10-29 I3: an `@import` whose extension is not on Claude Code's text list is skipped (`TEXT_EXTENSIONS`, `noeta/plugins/instructions/files.py`).
- 2026-10-29 I2: a nested instruction file over max(40,000, 5 % of the window in characters) is cut at a line end with a Read-the-rest note (`noeta/plugins/instructions/extension.py`, `chars_per_token`).
- 2026-10-29 B8: instruction files shown so far are read again with each new message; changed ones are shown again under a re-read note (digests in the log; deviation row for the trigger and placement).
- 2026-10-29 T2: deferred tools that join, return or leave mid-task are told in a system note (`deferred_delta`, `noeta/plugins/environment.py`; shared `noeta/toolkit/deltas.py`); the last list named is in the log.
