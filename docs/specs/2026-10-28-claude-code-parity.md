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
- **Claude Code's files are first-class inputs.** noeta reads `CLAUDE.md` (with `@import` and
  the directory walk), `.claude/settings*.json`, `.claude/skills`, `.claude/agents`,
  `.claude/rules`, `.mcp.json` and `~/.claude/*` with Claude Code's meaning, alongside `.noeta/`,
  behind the same workspace trust gate. A Claude Code project works in noeta unchanged.
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
- [ ] Re-record the steps the 2.1.312 recording lost (see the 2026-10-28 Phase 0 log entry).
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
- [ ] Permission modes (default, acceptEdits, plan, dontAsk, auto, bypassPermissions) and one
  `Tool(spec)` allow/deny/ask rule syntax; read scope and sensitive-path list; protected paths.
- [ ] Auto mode classifier as Claude Code runs it for API-key users: a client-side two-stage
  call per tool use, the same classifier context, thresholds (consecutive/total denials fall
  back to asking); `auto` becomes the default mode.
- [ ] Hooks: all baseline events, command/prompt/agent/http types, `allow`/`updatedInput`/
  `PermissionRequest`, timeouts and matchers.
- [ ] Skills: every baseline frontmatter field (`allowed-tools` as pre-approval, `when_to_use`,
  `context: fork`, `` !`cmd` ``, `${CLAUDE_SKILL_DIR}`, `paths`), directory walk.
- [ ] Agent files: every baseline frontmatter field; unknown fields warn.
- [ ] Memory: `CLAUDE.md` layers, `@import`, `.claude/rules`, per-project auto memory.
- [ ] MCP: OAuth, headersHelper, SSE, streamable-http spelling, resources tools, elicitation via
  `noeta.core.questions`, deferred by default.
- [ ] Sandbox for local exec (bubblewrap on Linux) with Claude Code's settings shape and domain
  proxy.

Phase 4 — missing tools
- [ ] `SendMessage` (resume a finished sub-agent from its log) and fork sub-agents, background by
  default, `<task-notification>` delivery with usage, per-call denial instead of whole-task
  failure.
- [ ] `ScheduleWakeup`, `CronCreate/List/Delete`, `/loop` on the kernel's timers.
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
- A Claude Code project (CLAUDE.md, `.claude/`, `.mcp.json`) runs in noeta with no edits.
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
