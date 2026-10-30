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
  retraction, the auth-stub listing is already a tool), per "no features beyond CC's". MCP
  server instructions are cut at CC's 4,096 characters plus "… [truncated]" (bundle: the
  instructions go through the MCP description cut, `u$()` =
  `CLAUDE_CODE_MAX_MCP_DESCRIPTION_LENGTH ?? 4096`; no cut for first-party HTTP servers, which
  noeta does not have).
### @-mention placement (D2)
- Others: CC attaches the Read call and result as meta messages hoisted ahead of the person's text; pi's CLI inlines `<file>` text into the prompt; dsh attaches nothing and tells the model to read the path.
- noeta: CC's two meta blocks, read in the task through `Read`, placed after the person's text in the same user turn.
- Why: the attachment is made inside the task once the message is in its append-only log; moving it ahead would rewrite a logged message. The wire turn is the same; only the block order differs.
- Evidence: `tests/plugins/fs/test_mentions.py`; DeepSeek V4.1, edit_not_write with and without `@shop/pricing.py`, 9 trials each: Read calls on the file 0.89 -> 0, requests 4.22 -> 2.89, input tokens 44.8k -> 30.9k, score 0.92 -> 1.00.

### Changed-file snippet source (B6)
- Others: CC diffs against the content it keeps for every read file; pi and dsh send no such note.
- noeta: the changed lines are worked out against the model's last whole `Read` result still in the conversation; without one the note says they are not shown. The snippet itself is CC's (`oKs`): 8 context lines, blocks one after another, cut at 8,192 characters with `... [N lines truncated] ...`; a file last read with an offset or a limit gets no note, as in CC.
- Line numbers: those of the file now; CC numbers each block from its start in the old file, which is off after an earlier block that changed the line count. A number the model cannot find in the file is the one thing numbering must avoid.
- Why: the log is the only state a task carries between workers; a stored copy of every read file would double the log for reads.
- Evidence: `tests/plugins/fs/test_changed_reminder.py` (a change shown after the step that made it; `Edit` with no new `Read`; no note for a windowed read; 8 lines; the cut).

### Date without the user's zone (C4)
- Others: CC uses the client's zone, else a UTC-labelled date (`assumedUtc`); dsh stamps the requester's zone; pi gives no date.
- noeta: same as CC: the task config's `timezone` (front ends send it), else the UTC date marked as UTC's, in noeta's own words.
- Why: a server's own zone is a wrong fact about the user.
- Evidence: `tests/plugins/context/test_date_change.py`; the full DeepSeek V4.1 eval (which sends no zone, so every request carries the UTC form) scored 0.927 overall against 0.919 on main.

### Smaller recorded differences
- Mentioned directory listing capped at 1,000 entries (left to ctx-fix-safety, which owns @-mentions); visible thinking does not count as speaking for the quiet nudge; a non-Claude model without `coauthor_email` gets no attribution note. Each has its `parity/deviations.md` row and reason.

### Extension points (Phase 2)
- Scope: the item-by-item review (pi e7a0df2, dsh f3803c96) found nine points to add (CC has them, multi-machine needs them, or they are the mechanism); the owner answered the open five on 2026-10-29: userConfig/`settings.json`, Python plugins shipping `skills/` `commands/` `agents/`, `InstructionsLoaded`, open model families yes; `statusLine` no (left to plugins). Every other row is have or won't, with its reason, in `docs/extending.md`.
- `--plugin-dir`: the person's own flag, so it loads for this run regardless of project trust (the machine policy still applies); refused with `--server`, whose plugins are the server's. A plugin file that joins no agent gets a notice, not an implicit `add_to`: one rule for every plugin place.
- Side-call headers (ADD-2). Others: pi and dsh hand plugins a model registry; CC's WebFetch calls a small model. noeta: `ToolContext.complete` is `TaskView.complete`, so `WebFetch`'s digest no longer carries the headers a hook put on the main request, as title, recall and summary calls never did. Why: those headers exist only in the process that built the main request; a durable copy would put credentials in the log. A host that needs them on every call sets them on the provider or passes `digest=`. Verified before removing: WebFetch's cache fell back to the configured model with no headers on a worker that had not built the request (after an approval or a crash), and had no call at all without an earlier hook there; compaction's did not break across workers (overflow follows its request in the same step) but held the request as seen in its own `before_model`, before later extensions changed it (test `test_overflow_summarizes_the_request_as_it_was_sent`).
- Tool progress (ADD-3) fits the live path: three `Delta` fields (`call_id`, `tool`, `elapsed`), one `ToolContext` field and a closure in the runner; nothing recorded. Across Postgres a progress text crosses as its tail (it replaces, so cutting it into pieces would show only the last). stream-json sends CC's `tool_progress` (no output in it, as CC's SDK) always, not only with `--include-partial-messages`. First user: foreground `Bash` (after 2 s, then every second, its output's last 4 KB), CC's live Bash output.
- userConfig (5a). Others: CC asks when a plugin is enabled, keeps secrets in its credential store (`pluginSecrets`), fills non-sensitive values into skill text. noeta: asks on `noeta plugin add` / `noeta plugin configure` (or `--config KEY=VALUE`); values in `~/.noeta/settings.json` `pluginConfigs` (CC's key in noeta's copy of the file, ADR-004), a sensitive one in the OS keyring with only a `{keyring:}` reference stored; filled into commands, agent prompts, MCP servers and hook `args`, not skill bodies. Why: a reference keeps secrets out of the task's plugin record; skill bodies get neither this nor `${CLAUDE_PLUGIN_ROOT}` yet, so both wait for one change. A plugin's `settings.json` `agent` (and the person's own `agent`) picks the agent front ends start with; `statusLine` keys are skipped.
- `InstructionsLoaded` (5c). A file shown again because it changed fires `session_start` (CC's reason when it reloads on a resume; noeta re-reads with each message). `compact` fires from `Compacted` and the instructions plugin's recorded files, so it fires even with re-attaching turned off. The hooks plugin reads the instructions plugin's records (CONTEXT "Layers").
- Model families (5d): any lowercase identifier, not only registered provider names (those are wire names, `openai_compat`); a near-miss of a known family is still refused.
- Python plugin parts (5b): only `skills/`, `commands/` (`/<directory>:<command>`) and `agents/`; hooks and MCP servers stay Claude Code plugins' (asked for three).
- Maintenance: `Agent.inherit_model` / `permission_mode` / `isolation` move to `Agent.extra` in CC's spelling (`model: inherit`, `permissionMode`, `isolation`); only the subtasks plugin read them.
- Review fixes (2026-10-29, review-ext-points): a plugin's option references are checked per MCP server and per hook when the plugin is read, so a secret one machine cannot read (keyring lacking it or no backend, a variable unset) leaves out only what uses it, with a notice naming the plugin, logged on a worker; `PreToolUse` / `PermissionRequest` / `UserPromptSubmit` hooks refuse (exit 2, `onFailure: block`) instead of disappearing; `Stop`-like events are left out, not refused, because refusing a stop would loop the turn. Every hook of a plugin gets each option this machine can read as `CLAUDE_PLUGIN_OPTION_<KEY>`, as CC; one it cannot read is left unset with a notice, and only a hook that names it is left out or refuses. Plugin-authored hooks and MCP servers may name only `{keyring:noeta-plugin/<plugin>/…}`; `{env:}` stays allowed (CC expands environment variables in `.mcp.json`). `plugin add` removes what it copied when configuring fails. Readers skip unknown delta kinds; versions before this one cannot, so the CHANGELOG gives the upgrade order. The digest model stays the task's (`ctx.model`), not the main request's after `before_model`: that model is not in the fold, and adding it is a fold-version change for one edge case.
### Reply result budget order (S3)
- Others: CC persists the largest fresh results of one user message once they pass 200,000 characters together (`Udr`), freezing each choice by `tool_use_id`; pi and dsh have no per-message budget.
- noeta: the same budget and `<persisted-output>` form, decided per result as each call finishes (a running total shared by calls that run together), so the results that arrive past the budget are saved, not necessarily the largest; nothing is saved for the budget without a spill directory.
- Why: each call's result is logged as it finishes (crash safety); a largest-first choice would have to hold every result of the reply unlogged, or rewrite logged ones.
- Evidence: `tests/core/test_result_limits.py` (six 45K results: four inline, two saved, total ≤ 200K); `parity/deviations.md` row.

### Fallback when a large result cannot be saved (S4)
- Others: CC keeps only the first part ("only the first X are shown"); pi keeps the tail of a command's output; dsh keeps head and tail.
- noeta: keeps the head and the tail around an elision (`<truncated-output>`).
- Why: a build or test log's error is at its end; two of the three keep the end. Only a host without a spill directory reaches it (every built-in host sets one), so no eval was run for it.
- Evidence: `parity/deviations.md` row; `tests/plugins/fs/test_output_limit.py`.

### Sub-agent plan file (A6)
- Others: CC gives a sub-agent of a planning conversation the plan-mode text in its sub-agent form with the plan file `<session plan name>-agent-<agent id>.md` (`nT`); pi and dsh have no plan mode in sub-agents.
- noeta: as CC. The parent writes its plan name into the child's config with its mode (`plan_name`, `Permissions.inherit`; a grandchild keeps the root's name), and the child plans in `<plans dir>/<name>-agent-<task id>.md` with the sub-agent text; no plan tools. (The earlier "needs the parent's state" reason did not hold: the parent writes the child's config.)
- Evidence: `test_a_sub_agent_of_a_planning_task_plans_too`.

### Pending A/B (an A/B run decides these next; behaviour unchanged until then)
- **Skill listing budget ceiling (S2).** CC: window × chars per token × 1 %, in characters, no ceiling (30,000 characters on a 1M window); pi: no budget, every skill in full; dsh: no total budget, 500 characters a skill. noeta: 1 % of the window in estimated tokens, capped at 4,096 tokens (about 16,000 characters; `RESIDENT_BUDGET_CEILING_TOKENS`). A/B: 80 real skills (about 40,000 characters), 40 prompts each aimed at one skill; arm A the ceiling, arm B CC's budget; first-step `Skill` routing accuracy and tokens per task. Keep the ceiling only if A is within 1 point of B.
- **Over-budget short-summary step (S4).** CC: each skill gets its full line or its name only, by usage rank; pi and dsh never degrade. noeta: every skill first gets its first sentence (24 tokens at most), full lines only if all fit (`short_summary`, `fit_menu_to_budget`). A/B: the same fixture with the budget forced to half the roster and a synthetic usage table; routing accuracy on prompts aimed at skills A shortens and B names only. Keep the step only if A beats B by 3 points or more.
- **Plan-mode full text wording (B5).** CC: 4,908 characters. noeta: its own wording, 2,832 bytes, now with the rules it had dropped restored (2026-10-29, ctx-fix-fidelity); the remaining difference is wording and emphasis. A/B: plan-mode scenarios (the plan file written, `ExitPlanMode` instead of a text question, Explore agents used), CC's text against noeta's, 9 trials each; keep noeta's only if it is not worse.
- **Recall off by default (M7).** CC: behind a server flag the recordings cannot show; pi: no memory. noeta: off unless `recall_model` is set (the CLI never sets it). A/B: record one CC session with two memory files and see whether a selector request goes out; if it does, run memory_recall with recall on against off and default the CLI's `recall_model` to the small model if on is not worse.
- **Git snapshot refresh on resume (C7).** CC: re-announces the session context when it changed on a resume ("these values replace the earlier ones"). noeta: the snapshot is fixed at task start. A/B: a task resumed after commits and a branch switch; does the model act on the stale branch (for example in PR text), refresh against frozen.
- **Pasted-image path note (D6).** CC and dsh save a pasted image and tell the model its path; noeta keeps no file. A/B: "put this pasted logo into assets/" tasks with and without the path line (about 60 tokens an image); add it if it lifts success.

### Skill usage ranking wiring (owner, 2026-10-29)
- Others: CC ranks the over-budget skill listing by decayed use across sessions, always on; pi and dsh do not rank.
- noeta: on by default in the `noeta` command (every front end and server): the skills plugin gets `usage_from`, the store's client, and ranks by `UsageRank`; an SDK host that builds agents itself opts in with `usage_from` or `rank`.
- Why: a plugin reads no other task by itself (`CONTEXT.md` Layers), and front ends import no plugin, so the client is passed as the plugin's argument where the store is opened.
- Evidence: `tests/cli/test_skill_usage_rank.py`.

### Parity replay usage
- The replay's fake model reported zero input tokens; with the threshold now anchored on reported usage (C2) it would never compact. It reports noeta's own estimate of each request's text, as a real model reports its count; `make parity` stays at 0 mismatches.
- The comparator names the opening of CC's too-large-file note (`names.json`), so it is split as its own kind instead of merging into the token-counter part, which is why the missing note (C9) went unseen; m3's contract re-distilled from the r2 recording changes only those layouts. A general "kind CC sends and noeta never does" check already existed (`diff_layout`); it could not fire on a part merged into another.

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
- [x] Item-by-item table of pi and dsh extension points in `docs/extending.md`, each with a
      verdict and reason.
- [x] Plugin authoring steps no more than pi's.

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
- 2026-10-29 Phase 2 started on `ext-points` from `best-of-three` @b9c7ca7a; `make check` 2465 passed.
- 2026-10-29 ADD-1: `--plugin-dir` (file, plugin directory, Claude Code plugin or a directory of them; TUI and `-p`) loads for this run; a plugin file that joins no agent is a notice; hello-plugin README fixed.
- 2026-10-29 5d: `register_models` accepts any lowercase family; near-misses of anthropic/openai refused.
- 2026-10-29 ADD-2: `ToolContext.complete`; `on_context_overflow(view, request)`; WebFetch's and compaction's per-process caches removed (what each broke: Decisions).
- 2026-10-29 5c: `InstructionsLoaded` hooks fire per instruction file shown (five load reasons, CC's fields).
- 2026-10-29 ADD-4: `Allow`, `Deny`, `Ask`, `Rewrite`, `ToolChanges`, `ToolUse`, `Recorded`, `TaskState` exported from `noeta`; the examples import from it.
- 2026-10-29 ADD-3: `ToolContext.progress` on the live path; TUI, ACP `tool_call_update`, stream-json `tool_progress`; foreground Bash is the first user. A timing race in its test fixed in a follow-up commit.
- 2026-10-29 5a: CC plugin `userConfig` (asked on `noeta plugin add`, `configure`, `--config`) and `settings.json` `agent`.
- 2026-10-29 5b: a Python plugin directory ships `skills/`, `commands/`, `agents/`.
- 2026-10-29 Maintenance: sub-agent fields into `Agent.extra`, `ToolContext.extras` dropped, the permissions/checkpoints record contract written down (CONTEXT "Layers", extending.md).
- 2026-10-29 docs/extending.md: full item-by-item table (have / added / won't, each with a reason), new hook rows, step count next to pi's (4 and 4; 1 command for a file kept elsewhere).
- 2026-10-29 Friction: an outside writer duplicated a block of `tests/plugins/context/test_date_change.py` in this worktree mid-run (F811 in `make lint`); not ours, restored, its diff kept in the session scratchpad.
- 2026-10-29 ext-points gates: `make check` 2488 passed, coverage 88.53 %; `make parity` 0 mismatches (11 accepted deviations); `make cluster` 13/13. No model-facing prompt text changed, so no eval run.
- 2026-10-29 Review fixes on `ext-points` (review-ext-points, P1 + all P2 + cheap P3): secrets checked per plugin part with notices and fail-closed guard hooks; keyring errors are messages, not tracebacks; `plugin add` rolls back; tolerant delta readers and the rolling-upgrade note; ACP ignores progress after a result; API changes under "Changed"; plugin files name only their own keyring secrets; old one-argument `on_context_overflow` still runs; Bash progress errors never fail a call; `__init__.py` in a plugin place hides nothing; settings file written atomically; `<plugin>:<agent>` resolved exactly; settings-file hooks set no `env`; dsh `--patch` cell corrected. Probe test `review/probe` scenarios now build with notices.
- 2026-10-29 Re-review fix: hooks get every option this machine can read as `CLAUDE_PLUGIN_OPTION_<KEY>` again (CC; a script reading its secret from the environment works, tested), an unreadable one is unset with a notice; a failed `plugin add` keeps options kept before; agent-file hooks set no `env`; `parity/deviations.md` Hooks rows for the fail-closed guard hook and plugin-own keyring references.
- 2026-10-29 C9 (ctx-compact-plan): a restored file that `Read` refuses whole for its size gets the too-large note (CC `compact_file_reference`) instead of nothing; candidates follow CC's read-file state, which a compaction resets to the files it restored in full (`restored_paths`, `seen_through` in the compaction record), so a noted file is not noted again. Comparator: `names.json` names the note's opening; m3 re-distilled from r2. Rules `compact.reattach.12-13`.
- 2026-10-29 C3 (ctx-compact-plan): rapid-refill breaker (CC `J9t=3`): an auto-compaction needed again within 3 model calls of the last in the same turn is a refill; the third in a row ends the turn (`autocompact_thrashing`). Rule `compact.trigger.7`.
- 2026-10-29 C2 (ctx-compact-plan): the threshold check is the newest reported usage plus the estimate since; the full estimate only before any usage. The parity replay's fake model now reports noeta's estimate of each request as its input tokens (it reported 0).
- 2026-10-29 C11 (ctx-compact-plan): skills given again after a compaction: newest invoked first, each cut to 5,000 tokens with a note, 25,000 in all (CC `Far`, `O9e`). Rule `compact.reattach.14`.
- 2026-10-29 S2/S3 (ctx-compact-plan): the runner caps every tool's text result at min(its `max_result_chars`, 50,000) and a reply's results at 200,000 together, saving the rest with `<persisted-output>` before logging; `Read` is exempt; WebSearch 100K -> 50K. Spill helpers moved to `noeta/core/large_output.py` (toolkit re-exports). S4 head+tail fallback kept with a deviations row. Rules `compact.results.1-2`.
- 2026-10-29 Memory saving bug (ctx-compact-plan): permissions refused every write to the memory directory (outside the working dirs) and the file tools' fence would too; now CC's auto-memory carve-out: `.md` writes and any read under the memory plugin's `dir` are allowed in every mode, and the fs write roots include it. Rule `main.mem.20`.
- 2026-10-29 Plan mode (ctx-compact-plan): a task created in plan mode now enters it on start with a plan file (it never got plan state, so no text and the model edited); a mode switch arriving with the prompt is told at once (only a typed `/plan <request>` waits a call, as m1); P1 cadence (CC `hTn` 5/5: full, none for 4 prompts, sparse, full every 5th); P2 re-entry note when planning again with the old plan file; A6 children inherit plan mode with the sub-agent form of the text. Rules `rem.plan-active.10-12`, `rem.plan-reentry.1`.
- 2026-10-29 A5 (ctx-compact-plan): the 16-Agent-calls-per-reply cap is gone (no such cap in CC; no kernel reason: each call stages its child in its own append). Rule `tool.Agent.23`.
- 2026-10-29 UsageRank (ctx-compact-plan, owner decision): the `noeta` command passes its store's client to every skills plugin (`usage_from`), so the listing ranks by use by default. Rule `main.env.17`.
- 2026-10-29 ctx-compact-plan gates: `make check` green (2481 passed, coverage 88.51 %), `make parity` 0 mismatches (11 accepted deviations). DeepSeek V4.1, 9 trials (eval-coverage scenarios run from a local copy of that branch's `evals/`, not committed), this branch vs `best-of-three` @b9c7ca7a: memory_save 1.00 vs 0.07; plan_mode_started (local variant of plan_mode_read_only created with `permission_mode: plan`) 0.78 vs 0.00; plan_mode_read_only 1.00 vs 0.89; background_subagent_result, skill_activation 1.00 both; compaction_resume_edits 0.93 vs 1.00 (every trial ends on `autocompact_thrashing`: the scenario's 24K usable window refills within 3 calls of each summary, which is where CC's breaker stops too; at a 40K window the scenario never compacts, 0.00 by its rule, so the scenario needs a window between the two). Against `ds41-main-37fd9997`: compaction_continuity, parallel_independent_reads 1.00 (=); delegate_parallel_review 0.11 (= main, 600 s trial timeouts).
- 2026-10-29 ctx-fix-fidelity (review-p1-fidelity fixes, from `best-of-three` @0d96a993): changed-file snippet with CC's 8 context lines and 8,192-character cut, no note after a windowed read; new, edited and nested-found skills listed with descriptions before the next model call (one checksum command over the SKILL.md files per call; nested ones no longer recorded as listed); sub-agent plan file `<parent plan name>-agent-<task id>.md`; hook blocking feedback names the command (deviation row removed); Bash 30,000, Monitor and ListAgents 10,000, ScheduleWakeup 1,000, ReportFindings 256 declared on the tools; plan-mode cadence checked per model call; MCP server instructions cut at 4,096 (read from the bundle); the memory index read again with each message; the plan-mode rules cut from the full text restored. Pending A/B section added to Decisions for S2, S4, B5, M7, C7, D6. The directory @-listing cap is left to ctx-fix-safety.
- 2026-10-29 ctx-fix-fidelity gates: `make check` green (2523 passed, coverage 88.67 %), `make parity` 0 mismatches (11 accepted deviations, the hook-feedback row removed). DeepSeek V4.1, 9 trials, 9 scenarios (eval-coverage `evals/` run from a local copy, not committed), this branch vs `best-of-three` @0d96a993 (session scratchpad `fid/ds41-fix-fidelity*.json`): overall 0.919 vs 0.900; skill_activation, skill_follow, memory_save, memory_recall, plan_mode_read_only, plan_mode_started, mcp_tool_use 1.00 both; changed_file_reminder 0.73 vs 0.78 (every trial got the note and answered with the new value; the score differs only in whether the model also re-read the file, 3 vs 4 of 9); hook_block_adapts 0.53 vs 0.32 (PreToolUse text unchanged; base had more 420 s timeouts). 458 vs 515 requests, 5.6M vs 6.3M input tokens.
