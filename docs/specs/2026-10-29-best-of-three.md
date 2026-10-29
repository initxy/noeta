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

### @-mention placement (D2)
- Others: CC attaches the Read call and result as meta messages hoisted ahead of the person's text; pi's CLI inlines `<file>` text into the prompt; dsh attaches nothing and tells the model to read the path.
- noeta: CC's two meta blocks, read in the task through `Read`, placed after the person's text in the same user turn.
- Why: the attachment is made inside the task once the message is in its append-only log; moving it ahead would rewrite a logged message. The wire turn is the same; only the block order differs.
- Evidence: `tests/plugins/fs/test_mentions.py`; DeepSeek V4.1, edit_not_write with and without `@shop/pricing.py`, 9 trials each: Read calls on the file 0.89 -> 0, requests 4.22 -> 2.89, input tokens 44.8k -> 30.9k, score 0.92 -> 1.00.

### Changed-file snippet source (B6)
- Others: CC diffs against the content it keeps for every read file; pi and dsh send no such note.
- noeta: the changed lines are worked out against the model's last whole `Read` result still in the conversation; without one the note says they are not shown.
- Why: the log is the only state a task carries between workers; a stored copy of every read file would double the log for reads.
- Evidence: `tests/plugins/fs/test_changed_reminder.py` (a change shown after the step that made it; `Edit` with no new `Read`).

### Date without the user's zone (C4)
- Others: CC uses the client's zone, else a UTC-labelled date (`assumedUtc`); dsh stamps the requester's zone; pi gives no date.
- noeta: same as CC: the task config's `timezone` (front ends send it), else the UTC date marked as UTC's, in noeta's own words.
- Why: a server's own zone is a wrong fact about the user.
- Evidence: `tests/plugins/context/test_date_change.py`; the full DeepSeek V4.1 eval (which sends no zone, so every request carries the UTC form) scored 0.927 overall against 0.919 on main.

### Smaller recorded differences
- Mentioned directory listing capped at 1,000 entries; hook blocking feedback names no command; visible thinking does not count as speaking for the quiet nudge; a non-Claude model without `coauthor_email` gets no attribution note. Each has its `parity/deviations.md` row and reason.

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
- 2026-10-29 D1 (ctx-attach-env): every image a person sends is fitted once in `Client.create`/`send` (`noeta/core/images.py`, moved from toolkit so the kernel can use it), covering the TUI, ACP, stream-json and HTTP; an unfittable one becomes a one-line note. Tested over HTTP and stream-json.
- 2026-10-29 D2-D5 (ctx-attach-env): @-mentions moved from the TUI into the file tools plugin (`noeta/plugins/fs/mentions.py`): read in the task's exec env through `Read` (counts as read for `Edit`; image, notebook, PDF via Read; first 2,000 lines or a too-large reference past the cap; long PDFs named with a by-pages instruction), CC's Called/Result blocks as system-origin content, deny rules honoured, and a mention sent from a TUI attached to a server is attached on the server. Rules `rem.at-mention.1-6`; two deviations (placement after the text, directory cap).
- 2026-10-29 C3 (ctx-attach-env): `Platform:` comes from the exec env (`uname -s` for a non-local env; left out when it cannot be told), no longer the worker's `sys.platform`; the shell/OS omission is now a deviations row.
- 2026-10-29 C4 (ctx-attach-env): dates follow the task config's `timezone` (the person's IANA zone, sent by the TUI, ACP and `-p`; inherited by sub-agents; accepted over HTTP); without one the UTC date, marked as UTC's (CC's `assumedUtc` form, own wording), so a server never gives its own zone's date. Rule `main.env.13` updated.
- 2026-10-29 C5 (ctx-attach-env): the snapshot leaves out `Git user:` when unset and gives an empty commit list instead of `(none)`, as CC; noeta's line-boundary status truncation kept.
- 2026-10-29 C8 (ctx-attach-env): the co-author address comes from the catalog (`ModelSpec.coauthor_email`; Claude models default to `noreply@anthropic.com`); a non-Claude model with no address gets no attribution note instead of an `@anthropic.com` trailer. Deviation row updated.
- 2026-10-29 B3 (ctx-attach-env): the quiet nudge fires every 5 tool-only steps since the stretch began or its last nudge, at most 3 per stretch (CC 2.1.312 `silent_turn_reminder`, `jwn=3`/`$wn=5`); rule `rem.quiet-nudge.2-3` updated.
- 2026-10-29 B6 (ctx-attach-env): file-changed note checked before every model call (so right after a Bash/formatter step), as a system turn (order 70), CC's meaning (current state, don't revert, say so if wrong, otherwise no mention) in own words, with the changed lines numbered (diff against the model's last whole Read still in the conversation; then the file counts as read) or a not-shown pointer to Read. Removed the 'Bash does not note a changed file' row from Not yet matched; rule `rem.file-changed.2` extended.
- 2026-10-29 B9 (ctx-attach-env): hook text for the model is led by CC's labels: `<hook name> hook additional context: ` (UserPromptSubmit context had no label at all; SessionStart no longer names its source) and `<hook name> hook blocking error` for a tool hook's block; tool events name `<event>:<tool>`. Rule `rem.hook-context.1`.
- 2026-10-29 ctx-attach-env gates: `make check` green (2443 passed, coverage 88.40%), `make parity` 0 mismatches (11 accepted deviations). DeepSeek V4.1, 13 scenarios x 9 (`--out` in the session scratchpad, ds41-ctx-attach-env.json): overall 0.927 vs 0.919 on main (ds41-main-37fd9997); every scenario within one trial of main except delegate_parallel_review 0.22 vs 0.11 (8 of 9 hit the 240 s trial timeout, as on main); 494 requests, 6.2M input tokens. Against the committed baseline (another model) `make eval` reports delegate_parallel_review as regressed, as main does.
