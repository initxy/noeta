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
