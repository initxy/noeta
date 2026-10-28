---
status: active
created: 2026-10-28
branch: parity
---

# Claude Code parity

## Goal

noeta is an open-source Claude Code that is friendly to servers. Everything the model sees and
every behavior a user can observe matches Claude Code: the system prompt's rules, the tool set
and each tool's schema and behavior, reminders, compaction, permissions, hooks, skills, agents,
MCP and the CLI/headless surface. A difference there is a bug. noeta differs only below that
line: the event log, crash recovery, multi-machine scheduling, the plugin structure, and the
server/remote surfaces, which stay noeta's own.

## Non-goals

- Copying Claude Code's text. Claude Code is closed source (© Anthropic, all rights reserved);
  prompts, tool descriptions, reminder and compaction wording are written by us. Parity is in
  behavior: the same rules, parameters, numbers, triggers and ordering, checked by tests.
- Per-model variants. The baseline is one configuration (below); what Claude Code sends to other
  models is not tracked.
- Claude Code's cloud features (remote sessions, remote control, claude.ai artifacts, teleport)
  and its telemetry backend.
- Changing the kernel to match Claude Code where Claude Code's design is weaker (see Deviations).

## Context

- The 2026-10-28 five-way comparison of noeta @633e0e9 against Claude Code 2.1.312 found the
  kernel sounder than Claude Code's and the model-facing layer short of it: about 8 P0s (two of
  them security holes), ~40 P1s and many P2s across loop/context, tools, sub-agents,
  permissions/hooks and extensions. Most gaps predate 2.1.301; Claude Code changed little in
  these areas between 2.1.301 and 2.1.312.
- Today's prompts and tool descriptions were written "aligned to Claude Code" by hand, rule by
  rule, with no check that keeps them aligned; drift is found only by audits like the one above.
- Claude Code reads `ANTHROPIC_BASE_URL`, so a local recording proxy sees the exact requests it
  sends (system blocks, tool list, reminders, compaction requests, cache breakpoints). Verified
  2026-10-28: 2.1.312 recorded through a proxy in front of an internal gateway, the proxy
  swapping the model for a cheap one on the way out (Claude Code still believes it talks to
  Opus 5.5, so the request is the baseline's). Recordings live outside the repo (they contain
  Claude Code's text): `~/cc-recordings/<version>/`, one directory per session, `INDEX.md`.
- What the recordings showed that the bundle did not: the default permission mode is `auto`;
  for an API-key user the auto-mode check is a client-side classifier call per tool use
  (two stages, `max_tokens` 64 then 2112, a ~140K-char system prompt); the environment, agent
  list and skill list ride a `role: "system"` message inside `messages`, not the system prompt;
  deferred tools appear as a `DeferredToolPlaceholder` with `defer_loading`; side requests
  name the session and the plan, digest WebFetch pages and suggest the next prompt; Claude
  Code calls `count_tokens`; compaction reuses the main prefix and appends its instruction as
  the last user message.

## Decisions

- **Baseline.** Claude Code latest stable at the time of a sync, default settings, Opus 5.5,
  native install, **API-key user** (noeta's users bring their own key; a claude.ai subscription
  adds Artifact/SendFeedback, a longer main prompt, 1-hour cache and server-side safeguards,
  none of which noeta can use). First sync: 2.1.312. Every Claude Code release gets a diff against the
  recorded contract; the diff is the work list.
- **Behavior parity, our wording.** For every model-visible text there is a rule checklist
  extracted from Claude Code (each rule, number, limit, trigger), and noeta's text must cover the
  checklist and add nothing the baseline lacks. Tests check the checklist, not the bytes.
- **Replay, not simulation.** `make parity` starts a local fake Anthropic endpoint that answers
  each step with the response Claude Code got at the same step of the same scenario; noeta's real
  Anthropic provider talks to it and noeta runs the tools for real in a scratch copy of the
  scenario project. The endpoint keeps noeta's requests, so step N of noeta is compared with step
  N of Claude Code on the wire. The per-scenario model turns (text and tool calls) are
  model output, not Claude Code text, and are checked in as the scenario script.
- **The contract is data.** A recording of Claude Code is distilled into a checked-in
  `parity/` contract of facts, not text: tool names and order, schema fields/types/required/
  enums, deferred-tool set, numeric constants (thresholds, caps, timeouts), reminder kinds and
  their triggers, compaction prompt sections and what is re-attached after compaction,
  cache-breakpoint positions, permission modes and rule syntax, hook events and their
  input/output fields, skill/agent frontmatter fields. noeta's request for the same scenario is
  checked against it.
- **noeta's own files, Claude Code's mechanisms (owner, 2026-10-28; replaces "Claude Code's files
  are first-class inputs").** Running a Claude Code project unchanged is not a goal. noeta reads
  only its own places — `~/.noeta/` and the project's `.noeta/`, instructions in `NOETA.md`
  (plus the cross-tool `AGENTS.md`) — never `~/.claude/`, `.claude/`, `CLAUDE.md` or `.mcp.json`
  implicitly, so the two tools never pick up each other's hooks, env or instructions. The
  mechanisms and formats inside are Claude Code's: instruction layering, `@import`,
  path-scoped rules, settings with allow/deny/ask/hooks/env, skill and agent directories,
  saved workflows.
- **Contract vs implementation (owner, 2026-10-28).** What the model or the user sees (tool
  interfaces, script language, file formats, behaviour) matches Claude Code; how it is executed
  may differ wherever noeta's way is better.
- **Headless protocol parity.** `noeta -p` speaks Claude Code's `stream-json` input/output and
  control-request protocol, so clients written against Claude Code's SDK drive noeta (locally or
  through the server). This is the server-friendly surface's main door; noeta's own HTTP API
  stays.
- **No noeta-only surface in the default agent (owner, 2026-10-28).** Tools the baseline lacks
  go: `Grep`/`Glob` (search through Bash, as the native build), `TodoWrite`, `BashOutput`/
  `KillShell` (Monitor/TaskStop), `RecallHistory`, `memory_*` (memory becomes Claude Code's: a
  directory of files written with `Write`, indexed by `MEMORY.md`). The main prompt's rules
  with no baseline counterpart are deleted. WebFetch refuses what Claude Code refuses
  (localhost, dotless hosts), reversing the 2026-09-18 "refuse nothing" choice. Every baseline
  tool noeta lacks is built (`Workflow`, `ReportFindings`, `ListAgents`/`SendMessage`,
  `ScheduleWakeup`/`Cron*`, plan mode, `SubagentHandback`).
- **Workflow scripts run in an embedded V8 (mini-racer), no system Node (owner, 2026-10-28;
  replaces the Node.js proposal).** The model writes JavaScript (Claude Code's contract), and
  JavaScript in a capability-free engine is the safe choice. `mini-racer` is a regular
  dependency (prebuilt wheels for linux/mac/win, x86_64 and arm64, musl included): every check
  and replay gets a fresh V8 context holding only the workflow API, with no module loader,
  process, network or files, code generation from strings off, the clock and randomness
  throwing, a time limit and a heap limit. The script is replayed against the agent results
  recorded in the task's log (finished `agent()` calls answer from the log), which makes a run
  durable like any other wait. Where mini-racer cannot load, the tool says so and starts
  nothing.
- **Deviations are listed, not implied.** A model-visible difference is allowed only when the
  kernel forces it, and each one is a row in `parity/deviations.md` with its reason. Starting
  list:
  - Tools run after the assistant message is logged, not while it streams (log-first crash
    safety).
  - A tool started before a crash answers "result unknown" instead of re-running.
  - Waits (approval, sub-agent, timer) survive restarts and move between machines.

## Plan

Phase 0 — measure
- [x] Recording proxy: forwards to an upstream (optionally rewriting the model), stores each
  request and response as JSON; Claude Code 2.1.312 honors `ANTHROPIC_BASE_URL` with it.
- [x] Record 2.1.312 interactively (tmux): edit/test, search, background Bash, MCP tool and
  resource, WebFetch, sub-agent, skill with arguments, task list, AskUserQuestion, interrupt,
  plan mode, `/compact`, default-mode approvals, auto-compaction (window 100K × 30 %),
  `--continue`. Stored in `~/cc-recordings/2.1.312/`.
- [x] Move the recording tools into `scripts/parity/` (proxy, tmux driver, scenario list).
- [x] Distill recordings into `parity/` (contract facts + per-scenario model scripts), never
  Claude Code's text.
- [x] Replay endpoint + runner + comparator; `make parity` prints every mismatch per scenario and
  step; record the first mismatch report in this spec.
- [x] Re-record the steps the 2.1.312 recording lost (see the 2026-10-28 Phase 0 log entry).
- [x] Rule checklists `parity/rules/*.md` (our words) for the main prompt, each tool description,
  each reminder, the compaction prompt and the auto-mode classifier.

Phase 1 — P0 fixes (needed regardless of parity)
- [x] Remembered shell approvals keep the full argv (or a Claude Code-style explicit prefix
  shown on the card), never program + first arg.
- [x] Workspace trust gates `.noeta/agents`, `.noeta/prompts`, `.noeta/skills` like
  `.noeta/plugins`.
- [x] Agent files default to one-shot as sub-agents.
- [x] Image size checked on the base64 size, downscaled to Claude Code's limits.
- [x] Every built-in host sets a spill directory.
- [x] MCP result cap and per-server timeout as Claude Code; `$ARGUMENTS` substitution.
- [x] Model catalog: Claude 5.5 family and Fable; unknown models announced, not silently 128K.

Phase 2 — what the model sees
- [ ] Tool set and order as the baseline: `Agent` (alias `Task`), deferred tools + `ToolSearch`
  (`select:` syntax), retired tools removed, Glob/Grep handled as the native build does.
- [ ] Each tool's schema and behavior per contract (Read paging on token cap, file-unchanged
  dedupe, Edit on externally changed files, notebook, WebFetch paging/PDF/4xx, Bash
  timeout-to-background, read-only command table, Monitor modes, TaskStop).
- [ ] System prompt sections per checklist, including the `<system-reminder>` explanation.
- [ ] Reminder catalog: every baseline reminder kind that applies, with Claude Code's triggers
  (background-task status after compaction, mid-turn user message wording, todo/plan nudges,
  file-changed, token/budget where the baseline shows them).
- [ ] Compaction per contract: trigger formula, clearing (keep count), summary prompt sections,
  post-compaction re-attachments and their caps, idle compaction, failure breaker.
- [ ] Prompt cache: breakpoint positions and TTL choices per contract (1-hour where the baseline
  uses it).
- [ ] Loop edge cases: empty reply and tool-intent-without-call retried once, interrupt leaves
  `[Request interrupted by user]` and keeps the partial reply, `Retry-After` capped, max_tokens
  shrink on window overflow, mid-turn injection on by default in every front end.

Phase 3 — behaviors around the model
- [x] Permission modes (default, acceptEdits, plan, dontAsk, auto, bypassPermissions) and one
  `Tool(spec)` allow/deny/ask rule syntax; read scope and sensitive-path list; protected paths.
- [x] Auto mode classifier as Claude Code runs it for API-key users: a client-side two-stage
  call per tool use, the same classifier context, thresholds (consecutive/total denials fall
  back to asking); `auto` becomes the default mode.
- [ ] Hooks: all baseline events, command/prompt/agent/http types, `allow`/`updatedInput`/
  `PermissionRequest`, timeouts and matchers.
- [ ] Skills: every baseline frontmatter field (`allowed-tools` as pre-approval, `when_to_use`,
  `context: fork`, `` !`cmd` ``, `${CLAUDE_SKILL_DIR}`, `paths`), directory walk.
- [ ] Agent files: every baseline frontmatter field; unknown fields warn.
- [x] Memory: `CLAUDE.md` layers, `@import`, `.claude/rules`, per-project auto memory.
- [x] MCP: OAuth, headersHelper, SSE, streamable-http spelling, resources tools, elicitation via
  `noeta.core.questions`, deferred by default.
- [x] Sandbox for local exec (bubblewrap on Linux) with Claude Code's settings shape and domain
  proxy.

Phase 4 — missing tools
- [ ] `SendMessage` (resume a finished sub-agent from its log) and fork sub-agents, background by
  default, `<task-notification>` delivery with usage, per-call denial instead of whole-task
  failure.
- [x] `ScheduleWakeup`, `CronCreate/List/Delete`, `/loop` on the kernel's timers.
- [ ] Worktree isolation (`EnterWorktree`/`ExitWorktree`, `isolation: worktree`).
- [ ] Plan mode tools, `WebSearch`, `NotebookEdit`, `LSP`.

Phase 5 — front ends
- [ ] `noeta -p`: `stream-json` in/out, control requests, `--resume`, `--max-turns`, permission
  prompt tool, structured output.
- [ ] TUI slash commands with mechanisms (`/compact`, `/model`, `/rewind`, `/context`, `/cost`,
  `/memory`, `/config`), `@` mentions, pasted images; plugin commands may run locally.
- [ ] ACP: real diffs, mode and model switching, plan updates. HTTP: command list, pending
  questions, agent/model list.
- [ ] Rewind with file checkpoints; session fork.

## Acceptance criteria

- `make parity` passes on the baseline scenarios: no mismatch outside `parity/deviations.md`.
- A client written for Claude Code's headless `stream-json` protocol drives `noeta -p`.
- `make eval` shows no regression; the benchmark is run once at the end against Claude Code on
  the same sample.
- Bumping the baseline to a new Claude Code release is: record, diff, fix the listed mismatches.

## Risks

- Recording needs real API calls through Claude Code (cost, and the account's terms); scenarios
  are kept small and recorded once per release.
- Some Claude Code behavior is server-side or flag-gated and invisible in recordings (auto-mode
  classifier details, remote model catalog); those rows are marked "observed" with what was seen.
- Writing our own wording that covers a checklist exactly is review-heavy; each prompt change
  states its byte delta.
- The scope is large; Phases 1 and 0 come first and are independently useful.

## Progress log

- 2026-10-28: owner set the goal (parity with Claude Code; kernel/extensibility/distribution
  are noeta's own), behavior parity with our own wording, baseline = latest Claude Code default
  config.
- 2026-10-28: recorded 2.1.312 on the owner's Mac through the internal gateway (no quota);
  baseline narrowed to the API-key user. Owner: start. Phase 0 tooling and Phase 1 run in
  parallel.
- 2026-10-28: Phase 0 tooling in `scripts/parity/` (README there): `record.py` (now also keeps
  requests the client abandons), `drive.py` + `scenarios.json` (the one step list record,
  distill and replay share), `project.py` (the scenario project as data — a checked-in
  `CLAUDE.md`/`.claude/`/`.mcp.json` would be picked up by agents working on this repo),
  `distill.py` → `parity/2.1.312/` (per-request facts, tool sets, text hashes, model scripts
  with paths as `{PROJECT}`/`{HOME}`), `replay.py` + `compare.py` → `parity/report.md`.
  Choices: the report and the raw capture (`parity/out/`) are gitignored — the report is
  regenerated in ~20 s and its summary lives here; text kinds are named in
  `scripts/parity/names.json` (Claude Code's by opening-words hash, noeta's by pattern);
  replay registers Opus 5.5 through a models file (noeta's catalog lacks it, a Phase 1 item);
  noeta runs in its default permission mode with every approval allowed; m3 gives noeta
  `auto_compact_window=63000` because noeta has no percent override (same ~30K trigger).
- 2026-10-28: first `make parity` report (deterministic across runs): 153 mismatches —
  tools 46, context 52, layout 11, cache 7, params 4, system 3, headers 5, side requests 5,
  compaction 1, run 19. The main ones: tools noeta lacks (`ToolSearch`, `Skill`,
  `ScheduleWakeup`, `ListAgents`, `ReportFindings`, `Workflow`, deferred MCP resource tools,
  `ExitPlanMode`, sub-agent `SubagentHandback`), tools only noeta offers (`RecallHistory`,
  `BashOutput`, `KillShell`, `Grep`, `Glob`, `memory_*`, `TodoWrite`), `Agent` named `Task`
  and missing `model`/`effort`/`isolation`, no tool is deferred, schemas lack `$schema`/
  `additionalProperties: false`; no `thinking`, `context_management`, `output_config`,
  `metadata` (nor `safeguards` in auto mode) and no beta headers; one system block instead
  of Claude Code's three; no `role: system` messages, so the environment, agent list, skill
  list, deferred-tool list, date and token budget never reach the model in Claude Code's
  place (noeta puts the environment in the first user turn); different cache-mark positions;
  side requests Claude Code makes and noeta does not (auto-mode classifier ×10, session
  title, plan name, count_tokens, sub-agent activity summary); `/compact`, `/plan` and
  `/release-notes` are not commands noeta offers (the project's `.claude/skills` skill is not
  one), so the manual compaction never happens; noeta asks approval for every
  Bash/Write/Edit/MCP call where auto mode classifies.
- 2026-10-28: recording gaps in 2.1.312 (need a re-record): m1 `search` and m3 `r2` prompts
  never reached the model (typed while Claude Code was still busy); m3 `after` arrived
  mid-turn because `r4` was still running; m1 `interrupt` lost its interrupted request (the
  old proxy dropped abandoned requests) and Escape before any output put the prompt back in
  the input box, so `after-interrupt`'s text was appended to it and no interrupted turn
  exists; Escape after m1 `plan` lost a request and left plan mode on into `after-compact`;
  m2's approval prompts were not logged; every auto-mode classifier stage-1 call
  (`max_tokens` 64) got a 400 from the gateway, so stage 1 is unobserved; the sessions shared
  one project directory, so m2–m4 started from m1's end state.
- 2026-10-28: Phase 1 done in 9 commits (one per item plus the retry comment); `make check`
  1429 passed, 87.31 %. Open: Claude Code's per-model image limits (2576 px / 4784 tokens on
  the 5.5 family) not applied, the 2000 px default is; skill `arguments` names (`$name`) not
  read; malformed tool calls still retried by the transient policy, not once.
- 2026-10-28: Phase 1 merged (8f52fc5, make check 1444 passed). Owner decisions: drop noeta-only
  tools and prompt rules, WebFetch as Claude Code, build every missing baseline tool.
- 2026-10-28: sub-agents (Phase 2/4 slice): `Agent` with Claude Code's schema, background only;
  `SubagentHandback`, agent-message and task-notification placed as m1 0036/0037 show;
  `SendMessage` (resume = a new task with `TaskCreated.history_from`), `ListAgents`, forks,
  worktree isolation, progress label; built-ins Explore/Plan/general-purpose; Claude Code's
  agent-file fields. Kernel: `ToolOutput.end_turn`, one-shot tasks wait for live children,
  `history_from`, `Agent.denied_tools`, origin `agent`, `ChildEnded` usage. Sub-agent lines
  in the report 39 -> 24 (all 156 -> 145); what is left there belongs to the request-shape, tool-surface and
  deferred-tool work. Open: `SendMessage.notify_when_idle`, the transcript `output_file`,
  agent-file `skills`/`mcpServers`/`hooks`/`memory`/`initialPrompt` (kept, not applied).
- 2026-10-28: Phase 2 first wave merged (request shape + main prompt, tool surface with deferred
  tools/ToolSearch, sub-agents with Agent/SubagentHandback/SendMessage/ListAgents, compaction +
  Claude Code-style memory). `make parity` 156 → 65; make check 1374 passed; make e2e 7 passed;
  evals: `grep_over_shell_search` became `search_through_shell` (the baseline has no Grep),
  new baseline 0.987 (old 0.952). Open: deferred-tool names in the environment message, skill
  list from `.claude/skills` (Phase 3), background-event wording, side requests (session title,
  plan name, count_tokens), m3 no longer compacts under replay (noeta's token estimate below
  Claude Code's count), ReportFindings/ScheduleWakeup/Workflow/ExitPlanMode, auto mode.
  Background sub-agents' approvals wait at the root — and that matches Claude Code: the
  re-recording (r2, m2 card 004) shows the card raised to the user, titled with the agent
  ("· from the general-purpose agent"); the earlier "Claude Code denies the call" was an
  unverified guess. To match: say which agent asks on the card.
- 2026-10-28: re-recorded 2.1.312 as `~/cc-recordings/2.1.312-r2/` (the first recording kept)
  and re-distilled `parity/2.1.312` from it. `drive.py session` now gives each session a fresh
  project and config, types only when Claude Code is idle (no request in flight per the
  proxy's `.live`, no spinner, the turn's "done" line), saves and answers each card (keys per
  step in `scenarios.json`), dismisses Claude Code's own notices, interrupts only once reply
  text shows, clears text left in the input box, and marks every step that went wrong
  (`!!!` in `<session>.marks`, also checked by distill). Root cause of the stage-1 400s: the
  classifier's first stage sends `thinking: {type: disabled}`, which the gateway turns into a
  DeepSeek reasoning setting DeepSeek rejects; DeepSeek was also taking minutes per call, so
  r2 used `gateway/seed-evolving`, which accepts the request as sent (`record.py
  --drop-disabled-thinking` stays for DeepSeek). New: m2 (approve, "don't ask again" for
  `python3 *`, deny via Escape, a sub-agent's approval), m5 (a project `PreToolUse` hook,
  a failing Read, background Bash + Monitor, TaskStop, `rm -rf .git` in the project).
  Classifier block path, as recorded: Claude Code's model first asked for confirmation; once
  confirmed, stage 1 (`max_tokens` 64) answered block, stage 2 (`max_tokens` 8192 here, 2112
  in the first recording) allowed it, and the command ran — a stage-1 block escalates to stage
  2, and a block at both stages is still unrecorded. An Escape while a tool call's input is
  still streaming withdraws the prompt into the input box instead of interrupting (kept
  outside the contract in `2.1.312-r2-attempts/`). Report after re-recording: 169 mismatches
  (tools 55, context 50, run 22, layout 15, cache 8, headers 6, side requests 5, params 4,
  system 3, compaction 1); the larger count comes from the new steps (sub-agent tool sets,
  approvals, Monitor/TaskStop, interrupt kinds), not from regressions.
  Background sub-agents' approvals wait at the root (Claude Code denies the one call) — owner to
  decide.
- 2026-10-28: Claude Code's files (Phase 2/3 slice): the instructions plugin reads Claude Code's
  layers in its order (managed, `~/.claude`, every directory from the outermost ancestor down:
  `CLAUDE.md`, `.claude/CLAUDE.md`, `.claude/rules`, `CLAUDE.local.md`; `@path` imports five
  deep; `paths:` rules and nested files when a tool touches a file) and ends with `MEMORY.md`;
  decision: a directory with no `CLAUDE.md` reads its `NOETA.md`, else `AGENTS.md`
  (`parity/deviations.md`). The memory section moves before `# Environment`
  (`ContextPart.before`). `~/.claude/settings.json`, `~/.noeta/settings.json`, trusted
  `.claude/settings*.json` and `managed-settings.json` merge in Claude Code's precedence
  (`noeta/cli/claude_settings.py`): env, model, hooks/MCP file lists, and the permissions
  plugin's `mode`/`allow`/`deny`/`ask`/`additional_directories` as plain data (only arguments
  it takes; the rule-syntax worker adds them). `.claude/skills` and `~/.claude/skills` are
  read, workspace first. The environment turn carries the deferred tools (order 30) and the
  agent list (order 40, moved from the first user turn); sub-agents get the deferred list
  first and no budget notes, Explore/Plan no git snapshot. `make parity` 65 -> 51 (context
  33/279 -> 24/147, layout 8/68 -> 4/14). Open: the skill command wrapper
  (`<command-message>` block, plain-text skill body), instructions re-read on resume (needs a
  resume signal from front ends), instructions/git context after compaction (compaction
  worker), `outputStyle` read but not applied.
- 2026-10-28: ScheduleWakeup / Cron* / `/loop`, ReportFindings, Workflow, session title and
  `count_tokens` (Read's token ceiling). Kernel: timers (`set_timer` records; a waiting task's
  `ready_at` is its earliest timer, an idle task fires due ones), `TaskView.spawn` for children
  outside a tool call, notices with `kind`/`data`, a config `output_schema`, the provider's
  `count_tokens`. The m3 replay compacts again: the summarize instruction was appended to the
  trailing token-count system turn instead of the user message before it (not the token
  estimate). `make parity` 65 -> 63 (gone: the three missing tools, session title,
  count_tokens, "never compacted"; new: post-compaction layout lines m3 now reaches). Open: a
  `/workflows` view and TaskStop for runs, nested `workflow()`, built-in workflows, the
  `workflow-authoring` skill, worktree isolation for workflow agents, folding `noop` loop ticks
  in front ends, `/loop` as a Skill-tool skill (it is a front-end command), Claude Code's
  "fire only when idle" also for one-shot tasks (they end before a timer fires).
- 2026-10-28: Phase 3 permissions (branch of the permissions worker, merged parity @c5b58fe).
  Kernel: `Allow(approve)` / firm `Ask` / `Extension.resolve_ask` (may also return an `Ask`),
  a declined call stops the turn with the `[Request interrupted by user for tool use]` marker
  (r2 m2), `ModelRequest` stop sequences, per-block breakpoints, betas, vendor fields,
  `thinking=None`, `Client.signal`. Permissions: six modes, `auto` by default; deny > ask (firm)
  > allow rules (`noeta/toolkit/permission_rules.py`, shared with hook `if`); working
  directories + `additional_directories`, reads outside ask, writes outside refused (fs fence,
  deviation row); Claude Code's protected paths, `.noeta` protected as `.claude`; read-only
  shell commands run without asking; acceptEdits file commands; cards with Claude Code's
  choices (don't ask again for rules / a directory, switch to auto or accept edits), "always"
  to `.noeta/settings.local.json` in trusted projects. Auto mode as recorded for an API-key
  user (r2): `claude-sonnet-5` first (64 tokens, thinking off, `</block>`), stage 2 (8192)
  only when stage 1 does not allow, fallback to the task's model with +2048 and no thinking;
  transcript chunks and caching as recorded; block → error with reason; no verdict → held
  back, not a judgment; 3 consecutive / 20 total blocks fall back to asking; safeguards on the
  first main request per process, dangerous-tool-use beta until the first compaction,
  afk-mode on every auto request. Classifier prompt in our words: 96,537 bytes
  (`texts/auto_mode_classifier.md`; Claude Code's 140,239 chars), every rule of the checklist.
  Plan mode: `/plan`, `ExitPlanMode` (deferred, main agent) as a question with Claude Code's
  choices, plan file in `<noeta home>/plans`, plan-mode / plan-exited notes, plan-name side
  request. Hooks (sub-worker): 14 events, command/http/prompt types, Claude Code's fields,
  600 s / 30 s timeouts, PreToolUse allow/deny/ask/updatedInput, PermissionRequest via
  `resolve_ask`, PermissionDenied on classifier blocks, `Extension.before_compact`. Front ends:
  shift+tab mode cycle and choices in the TUI, ACP session modes, HTTP `pending` and
  `signals`; sub-agent cards say which agent asks (r2 m2 card 004). `make parity` on the
  merged contract 66 → 42 (run 63 → 14 occurrences: no more approvals Claude Code did not
  ask; side requests 14 → 0; the 5 auto-mode deviation ids removed). Open: compaction requests
  lack the dangerous-tool-use / fallback-credit betas; Claude Code re-sends the auto-mode note
  standalone after compaction (noeta's rides in the rebuilt environment message); hook
  `agent` / `mcp_tool` types; sensitive-file read list (unverified for Read in the bundle);
  `make eval` not run (needs credentials) — the main request now carries the auto-mode note.
- 2026-10-28: Workflow scripts moved from the worker's `node` to an embedded V8 (`mini-racer`)
  per the owner: a fresh context per replay, 30 s and 256 MiB per replay, evaluated in a
  thread. Friction: mini-racer queues a Python-side function call behind a script that never
  yields (an endless loop after an `await`) with no way to stop it, so once script code can run
  the engine only evaluates; such a loop ends when the context closes. Saved workflows now come
  only from `.noeta/workflows/` and `~/.noeta/workflows/`.
- 2026-10-28: Own places, per the owner's decision: instructions are `NOETA.md` in Claude
  Code's layering (managed `/etc/noeta`, user `~/.noeta`, per directory `NOETA.md`,
  `.noeta/NOETA.md`, `.noeta/rules`, `NOETA.local.md`, `AGENTS.md` as the fallback); settings
  `~/.noeta/settings.json`, trusted `.noeta/settings*.json`, managed `managed-settings.json`;
  skills, agents, MCP (`.noeta/mcp.json`) and worktrees under `.noeta/`; the env knobs
  `NOETA_`-prefixed. Nothing under `~/.claude`, `.claude/`, `CLAUDE.md` or `.mcp.json` is
  read. The parity replay writes noeta's copy of the scenario project in noeta's places and
  maps recorded paths to them; `CLAUDE_CONFIG_DIR` isolation dropped. `make parity` 42 → 42;
  the model-visible names are rows in `parity/deviations.md`.
- 2026-10-28: Phase 3 sandbox. `LocalExecEnv(sandbox=...)` with Claude Code's `sandbox`
  object (merged across settings files: lists add up, a project tightens but never loosens,
  `bwrapPath` managed-only; `Edit` allow / `Read` deny / `WebFetch(domain:)` allow rules and
  `additionalDirectories` feed it). Linux: bwrap (own net/pid/ipc/uts namespaces,
  `--ro-bind / /`, writable working dir + `allowWrite` + private `$TMPDIR`, `.noeta` config and
  `.git/hooks`/`.git/config` read-only, credential stores and noeta's home hidden) behind a
  stdlib bridge that relays loopback proxy ports to per-command Unix sockets and execs the
  command under seccomp (no `AF_UNIX`, no new user namespaces). macOS: a generated Seatbelt
  profile (unit-tested only — no Mac here). Network: a per-command HTTP+SOCKS5 filter in a
  daemon thread; refusals reach the model as `<sandbox_violations>`. Bash/background/Monitor
  confined unless excluded or `dangerouslyDisableSandbox` (asked about whatever the allow
  rules say); `autoAllowBashIfSandboxed`; `failIfUnavailable` refuses; Bash's description
  gains a sandbox section only while the sandbox is on and can start
  (`parity/rules/tools/Bash.md` `tool.Bash.s.*`). Containers: `start_container` locked down
  by default (no network, read-only root, reduced caps, no-new-privileges, pids limit);
  `sandbox=` maps `allowedDomains` onto an internal Docker network whose only exit is the
  filter on its gateway. Tested on Linux (bwrap 0.6.1, Docker): writes outside fail, a
  non-allowed host is refused, an allowed one is reached via the proxy, direct connections
  fail, Unix sockets and `unshare -U` refused. Deviations: no mid-command prompt for unknown
  hosts, `mask` credentials act as `deny`, no `/sandbox` command. Open: `allowed_domains`
  Bash parameter (auto mode), the platform notes of Claude Code's sandbox section, violation
  notes for background jobs, git commits from a sandboxed linked worktree (its `.git` lives
  in the main repository, outside the writable set), macOS run on a real Mac.
- 2026-10-28: Phase 3 MCP. Transports `stdio` / `http` (`streamable-http` too) / `sse` / `ws`
  (asyncio WebSocket, no new dependency), multiplexed by request id (a timed-out call is
  cancelled, the connection kept). OAuth 2.1 (RFC 9728 / 8414 / 7591, PKCE S256, loopback
  callback on Claude Code's port choice, tokens in `$NOETA_HOME/mcp-auth` 0700/0600,
  refresh); the sign-in never blocks a tool call: as in Claude Code, a server needing it
  offers `mcp__<server>__authenticate` / `__complete_authentication` (model-visible, our
  wording, `noeta/plugins/mcp/descriptions/`). `headersHelper` (`NOETA_MCP_SERVER_NAME` /
  `_URL`). Client capabilities `roots` + `elicitation` (protocol 2025-11-25); elicitation
  parks the call on `noeta.core.questions.ask`; `roots/list` answers the task's cwd;
  `tools/list_changed` re-lists after the call. `ReadMcpResourceDirTool` is build-flagged
  off in 2.1.312 and absent from the recording: not added (tool.mcp.11 n/a). `noeta mcp
  add|add-json|list|get|remove|login|logout` (`noeta/cli/mcp_command.py`, through
  `Mcp.admin` by registry name). For the owner: Claude Code's `local` scope writes
  `.noeta/settings.local.json`'s `mcpServers`, read only once the project is trusted. Open:
  a second elicitation in one call is declined (a resumed call cannot park again), a worker
  restart loses an open elicitation (result unknown), no workspace-trust gate on a project's
  `headersHelper` beyond the consent keys, the Streamable HTTP server-push GET stream,
  `roots/list_changed`, sampling.
