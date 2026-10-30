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

- **Instruction re-read trigger (B8).** CC: re-reads instruction files on `--continue`/resume
  and puts the changed ones before the resumed prompt. pi: re-reads only on `/reload`. dsh:
  announces updated nested files ("Updated instructions from:"). noeta: re-reads the files shown
  so far with every new message and shows the changed ones after that message. Why: a durable
  task has no resume moment (it sleeps between messages and may wake on another worker), and a
  note for a message lands after it in the append-only log. Evidence: zero tokens when nothing
  changed (digest compare); `parity/deviations.md` row; test
  `test_a_changed_instruction_file_is_shown_again_with_the_next_message`.
- **List-change notes (T2, B7) are a subset of CC's.** CC's deferred-tools delta also covers
  restored/retracted tool definitions, a denied-tool lead and needs-auth servers, and compresses
  long name lists; noeta tells joined, returned and removed names, and MCP server instructions
  joined/changed/removed. Why: the rest belongs to CC features noeta does not have (definition
  retraction, the auth-stub listing is already a tool), per "no features beyond CC's". The MCP
  instructions cap of 2,048 characters is CC's truncation shape ("… [truncated]"); its exact
  number was not read from the bundle (the constant is imported across modules), so it is the
  value CC uses for MCP descriptions — check it when the next recording has a server with long
  instructions.
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

### Benchmark (Phase 4)
- Benchmarks: Terminal-Bench 4.0 (63 of 66 tasks; 3 need a GPU) and SWE-rebench 07-2026 (111).
  Why: TB4 is the current headline board with Claude Code as its reference agent and is not
  saturated; SWE-rebench is fresh (issues after the model's data) and cheap per task. TB 2.1 is
  the smoke loop only.
- Comparison: noeta against harbor's `claude-code` agent (CC 2.1.312) in one harbor job — same
  model (`opensource/deepseek_v4_1_flash`, gateway Anthropic endpoint), effort `high`, tasks,
  timeouts, interleaved trials, concurrency split evenly. Recorded requests agree on
  `max_tokens` 65536, `thinking` adaptive, `output_config.effort` high, no temperature; CC gets
  `CLAUDE_CODE_MAX_CONTEXT_TOKENS` = noeta's 1M window (2.1.312 honours it for non-Claude ids,
  else it assumes 200k). The one wrapper over CC: both agents' process trees are stopped at
  the agent timeout (harbor only cancels its `docker exec`; the agent kept editing during a
  same-container verify).
- Fairness of the environment: noeta installs into its own venv under `/opt/noeta-bench`
  from uploaded static uv, rg and a python-build-standalone CPython; the task's interpreter
  and packages are never touched.
- Nothing uploaded: harbor telemetry off; CC telemetry, error reporting, auto-updater,
  non-essential traffic and the WebFetch domain preflight off; local dataset copies (a
  registry run records a download at harbor's hub); LiteLLM's price-map download off; network
  logs of the smoke runs show only the gateway, the corp proxy (package and install
  downloads) and task-driven traffic. The gateway key is redacted from job dirs.
- Nothing else touched: each run builds every image through its own buildx builder, removed
  with all it pulled at the end; prebuilt task images come in via a run-local dataset copy
  whose Dockerfiles are `FROM <prebuilt>`, so they live in the trial's compose project;
  leftovers are removed only when compose labels or container mounts prove them the run's.
  Pulled images are never removed by name or digest: another program may hold the same one.
  Cost of that: disk peaks per task in flight (SWE-rebench median ~2 GB on docker's disk with
  the builder's state elsewhere), so concurrency is sized to free space.
- Measures: pass rate (setup failures counted, infrastructure errors shown apart), paired
  per-task difference with its standard error, tokens from each agent's own records
  (noeta: its store — sub-agents and compaction included; side calls such as titles are in
  neither agent's count), time, and cost at DeepSeek's own list price (read 2026-10-30).

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
- [x] Pick one or two current benchmarks, with reasons.
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
- 2026-10-29 B7 (agents): agent types that join or leave mid-task are told in a system note (`subtasks` plugin, types in the log).
- 2026-10-29 B7 (MCP): servers' `instructions` from `initialize` reach the model under `# MCP Server Instructions` and are withdrawn on disconnect (`noeta/plugins/mcp/instructions.py`; recorded in the server listing, told set in the log).
- 2026-10-29 ctx-skills-memory gates: `make check` 2446 passed, coverage 88.46 %; `make parity` 0 mismatches (11 accepted deviations). DeepSeek V4.1, 9 trials, 5 scenarios vs `ds41-main-37fd9997`: no_delegate_trivial_lookup, read_over_shell_cat, compaction_continuity 1.00 (=); edit_not_write 0.92 (-0.08: 3 trials renamed with `sed` through Bash, which the auto-mode guidance allows; correct result, no Write); delegate_parallel_review 0.00 (-0.11, 5 of 9 hit the 240 s trial timeout, as on main). No list-change or re-read note fired in any trial; the harness reports no regression.
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
- 2026-10-30 Phase 4 harness on branch `bench-2026` (`bench/compare.py`, `bench/harbor_adapter.py`): smoke 2+2 tasks both agents graded; gateway took 32 parallel calls with no 429; review fixes (own venv, CC context window, store usage, WebFetch preflight, run-owned builder and cleanup). Full run not started.
