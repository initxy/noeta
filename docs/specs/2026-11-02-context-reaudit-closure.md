---
status: active
created: 2026-11-02
branch: ctx-reaudit-closure
---

# Context re-audit closure

## Goal

Every model-visible difference from Claude Code (CC) 2.1.312 that a second, independent audit
of local main @c5d3647a found without a written reason is closed: matched to CC (preferred),
or kept as a deviation written here as others (CC, pi, dsh) / noeta / why, with numbers where
they can be measured, plus a row in `parity/deviations.md`. Every uncertain item is resolved
from the recorded CC data and bundle, or names the evidence that is missing. Every wrong rule
row is corrected and every missing `noeta today` cell filled. The replay (`make parity`) sees
the per-request tool order it missed.

## Non-goals

- Features CC does not have, beyond what multi-machine operation or the extension mechanism
  needs.
- A whole CC feature noeta lacks, or a second main prompt for older Claude models: main's
  behaviour stays, a deviation row says why, and the item is an owner question below.
- Undoing a decision of `docs/specs/archive/2026-11-02-context-audit-closure.md` (CLO).
- Pushing, releasing, touching another program's data, uploading anything.

## Context

- The audit report (outside the repo) classes each difference J (written reason), U (none),
  ? (uncertain). Ids below are the report's: P prompt/envelope, E environment, R reminders,
  S skills, M memory/instructions, T tools, C compaction, A sub-agents/plan/classifier;
  K rule catalogue, L leftovers are this spec's.
- CC evidence: the recordings (`~/cc-recordings/2.1.312*`, derived data in `parity/2.1.312/`)
  and the 2.1.312 bundle's strings (read locally, never committed). No-copy rule
  (`parity/rules/README.md`): noeta's texts say the same thing in noeta's words; protocol
  literals (tags, names, formats) are kept exact.
- pi `badlogic/pi-mono` @ e7a0df27, dsh `deepseek-ai/DeepSeek-Harness` @ f3803c96 (the
  columns in `parity/deviations.md`).
- Starting point: local main @c5d3647a; `make parity` 0 mismatches, 11 accepted deviations.

## Items

Each item: the difference (CC / noeta), the planned resolution — **match**, **row** (a
deviation with a reason), **owner** (main's behaviour kept, row written, owner question),
**resolve** (an uncertain item settled from evidence) — and the work branch.

### Replay tool (`rr-parity-tools`)
- **K0** `scripts/parity/compare.py` compares the union of a session's tools, never each
  request's tool order (CC request `035d2a4552f1` `… Workflow, DeferredToolPlaceholder, Write`,
  noeta `… Workflow, Write, DeferredToolPlaceholder`). Compare per request, in order; also any
  other blind spot the report names. New mismatches are fixed or become rows.

### System prompt and request envelope (`rr-prompt-env`)
- **P1** Older Claude models (Sonnet 5, Sonnet 4.x, Haiku 4.5) get CC's full prompt, harness
  notes as `<system-reminder>` in the user turn, the full auto-mode text; noeta sends one lean
  prompt and `role: "system"` turns to every model. **owner**.
- **P2** `max_tokens` = CC's per-model default (64,000 / 32,000), effort = the model's default,
  every model has a knowledge cutoff, Sonnet 4.6 window 200K; noeta sends the ceiling, `medium`,
  one cutoff, 1M. **match**.
- **P3** Per-model paragraphs (Fable identity, Haiku 5.5 early stopping). **match** where the
  model is in noeta's catalog, else row.
- **P4** "# Session-specific guidance" skill line only when skills exist and `Skill` is offered.
  **match**.
- **P5** `outputStyle` (prompt opening + "# Output Style") and `language` (environment note).
  `language` **match**; `outputStyle` **owner** (a whole feature) with row.
- **P6** Identity line variants (CLI / SDK / non-interactive). **row**.
- **P7** Harness bullet about terminal display dropped. **row**.
- **P9** Beta order, `safeguards.classifier_context`. **resolve**.

### Environment and git (`rr-prompt-env`)
- **E1** A model switch is announced before the next call; noeta never re-tells the model.
  **match**.
- **E2** "Additional working directories:" list. **match**.
- **E3** Linked worktree lines (git worktree; shared stash). **match**.
- **E4** MCP server instructions after the agent list. **match** (order).
- **E5** Shell line: CC bare names `bash (login shell: fish)`; noeta full paths, own wording.
  **match** CC's format (CLO E4 kept: name the shell commands run in).
- **E6** Model line uses the short display name ("Opus 5.5"), the trailer the full one.
  **match**.
- **E7** Attribution from `attribution.commit` / `attribution.pr` settings; both empty → the
  model is told not to add lines. **match**.
- **E8** Main-branch fallback `origin/HEAD` → `origin/main|master` → `main`. **match**.
- **E9** Status trimmed (`stdout.trim()`). **match**.
- **E10** "# Environment update" note on repo/worktree flag, extra dirs or cwd change.
  **resolve**.

### Reminders and modes (`rr-reminders`)
- **R1** No empty-reply nudge after any `StructuredOutput` call (or a terminal MCP tool).
  **match**; fixes `rem.empty-reply.5`.
- **R2** "ultracode" reminder and on/off notes (Workflow's description promises one). **match**.
- **R3** "ultrathink" reminder, turn effort raised to xhigh. **match**.
- **R4** "Exited Auto Mode" note on leaving auto (not to plan). **match**.
- **R5** Auto-mode Bash-first guidance also in `bypassPermissions`. **match**.
- **R6** Auto guidance re-sent only when none since the last auto exit or compaction. **match**.
- **R7** Full "Auto Mode Active" text for non-lean models (with P1: **owner**); nothing for an
  agent without Edit/Write (Explore): **match**.
- **R8** `plan_mode_exit` on any exit from plan mode. **match**.
- **R14** Hook plain stdout as "`<event> hook success: …`". **match**.
- **R15** Hook `continue: false` told to the model ("hook stopped continuation"). **match**.
- **R16** `async` / `asyncRewake` hook output reaches the model later. **match** or row.
- **R17** Hook output over 10,000 characters saved to a file with a preview. **match**.
- **R18** `--max-budget-usd` note before each call. **match**.
- **R19** `@agent-<type>` mention note. **match**.
- **R20** @-mention of an already-read unchanged file sends nothing. **match**.
- **R21** `criticalSystemReminder_EXPERIMENTAL`; `mcp_dropped_tools_delta`. **resolve**.

### Models and nudges (`rr-prompt-env`)
- **R9** Quiet nudge only for models with the capability. **match**.
- **R10** Task tools + `task_reminder` on older Claude models and unknown models. **owner**
  (with P1); fixes `rem.absent.1`.

### Shell, background jobs, sandbox (`rr-shell`)
- **R11** Background `grep`/`rg`/`find`/`diff`/`test`, `git diff/grep` exit 1 → completed
  "(exit code 1: …)". **match**.
- **R12** Background job silent 45 s with a prompt-like last line → interactive-input notice.
  **match**.
- **R13** Sandbox instructions as a `sandbox_instructions` reminder (re-sent on change, a
  "disabled" note), not a section of `Bash`'s description. **match**; fixes `tool.Bash.s.*`.
- **T2** `TaskStop` stops background agents and Workflow runs, by id or name. **match**.
- **T11** Default mode: `Bash` description adds the "avoid cat/head/…" line. **match**.
- **T12** Monitor stderr written to the output file. **match**.
- **T13** `Write` refuses over 8 MB. **row**.

### Skills (`rr-skills`)
- **S1** Skill name = directory name; frontmatter `name` a display name; CC's charset.
  **match**.
- **S2** No `description` → first body line (≤100 chars). **match**.
- **S3** Only `skills/<dir>/SKILL.md`, one level. **match**.
- **S4** Skill folders from cwd up to home, managed folder, `--add-dir` dirs. **match** (noeta's
  own folder names).
- **S5** `.noeta/commands/*.md` (project/user) are listed and model-callable via `Skill`.
  **match**.
- **S6** Frontmatter `model`, `effort`, `disallowed-tools`, `hooks`, `shell`,
  `user-invocable`. **match** each that maps onto noeta; row for any left.
- **S7** After compaction the listing is rebuilt from current skills. **match**; fixes
  `main.env.16`.
- **S8** Mid-conversation new skills fitted against a fresh budget, ranked. **match**.
- **S9** Usage counted at most once per skill per 60 s. **match**; fixes `main.env.17`.
- **S10** Budget knobs (`SLASH_COMMAND_TOOL_CHAR_BUDGET`, …). **row** or match.
- **S11** Skill re-given after compaction = text captured at invocation. **match**.
- **S12** Shell reads unlock `paths:` skills and nested skill folders. **match** (CLO Q3).
- **S13** `${CLAUDE_PLUGIN_ROOT}` / `${CLAUDE_PLUGIN_DATA}` / `userConfig` in plugin skill
  bodies. **match**.
- **S14** `${CLAUDE_SESSION_ID}`, `${CLAUDE_EFFORT}` in skill bodies. **match** (leftover L1).
- **S15** DEV row 71 wrong in nested folders. **match** the row's rule everywhere.
- **S16** Name-clash precedence. **resolve**.

### Memory and instruction files (`rr-memory-compaction`)
- **M1** MEMORY.md and recall caps count characters, not bytes. **match**; fixes
  `rem.instructions.9`.
- **M2** Removed instruction files announced; files created mid-task shown. **match**.
- **M3** Only block-level `<!-- -->` removed. **match**.
- **M4** Nested instruction file header: path, no kind. **match**; fixes `rem.instructions.10`.
- **M5** `@import` boundary row wording. **row** reworded.
- **M6** Frontmatter cut only from rule files. **resolve**.
- **M7** Recall on by default? **resolve**.

### Compaction (`rr-memory-compaction`)
- **C1** A failed auto-compaction does not end the turn; after 3 consecutive failures
  auto-compaction is off for the task. **match**.
- **C2** Fallback summary request when the main summarize call errors or is empty. **match**.
- **C3** `plan_file_reference` re-attached whenever a plan file exists. **match**; fixes
  `compact.reattach.6`.
- **C4** `task_status` lines for running background agents and shells re-attached. **match**;
  fixes `compact.reattach.10`.
- **C5** Proactive vs reactive trigger. **resolve**.
- **C6** Unknown-model window 200K vs 128K. **resolve**.

### Tools (`rr-parity-tools`)
- **T1** `DeferredToolPlaceholder` always second to last; rule `tool.DeferredToolPlaceholder.3`
  wrong. **match**.
- **T3** MCP names `mcp__<norm(server)>__<norm(tool)>`, case kept, no cut, over 128 dropped
  with a note. **match** (a provider's own name limit handled in its adapter, or row).
- **T4** ToolSearch `searchHint` (+4; `_meta["anthropic/searchHint"]`). **match**.
- **T5** Full-name +3 only while the score is 0, on the split name without `mcp__`. **match**.
- **T6** No-match result names pending / failed / blocked MCP servers. **match**; fixes
  `tool.ToolSearch.r.5`.
- **T7** Per-tool `_meta["anthropic/alwaysLoad"]`; `_meta.ui.visibility`. **match**.
- **T8** WebSearch `mode` optional, default `extended`. **match** or row.
- **T9** WebSearch result shape. **match** or row (backend differs).
- **T10** ReadMcpResourceDirTool result shape. **match**.
- **T14** WebFetch of preapproved hosts not asked. **owner** (CLO Q6), row.
- **T15** SendMessage `to` patterns, `message` default. **resolve**.
- **T16** ScheduleWakeup cache wording on every provider. **resolve**.
- **K-results** 40 result-shape rows in 14 tool files lack `noeta today`. Fill.

### Sub-agents, plan mode, classifier (`rr-agents`)
- **A1** Hand-back reviewed as its own message; a flagged report delivered under a warning.
  **match**.
- **A2** Classifier transcript too long → normal approval (abort headless). **match**.
- **A3** `useAutoModeDuringPlan`: plan mode uses the classifier for non-write calls. **match**.
- **A4** Agent that should hand back and did not: "no report was delivered" notice. **match**.
- **A5** Progress label every 30 s with "Previous: …". **match**.
- **A6** Built-in Explore/Plan keep managed instruction files. **match** (CLO N5).
- **A7** Explore capped at Opus under Fable. **match**.
- **A8** ExitPlanMode with no plan file asks; approval text. **match**.
- **A9** Rejected plan text includes the plan. **match**.
- **A10** Completion notice fields (`<worktree>`, stop reason, turn limit, empty result).
  **match**.
- **A11** Plan agent prompt steps. **match**.
- **A12** `subagent_tokens` computation. **match**.
- **A13** Classifier stage-2 error after a stage-1 block. **match**; noeta's
  `untrusted_instructions` variant → row.
- **A14** EnterPlanMode allowed without asking. **resolve**.
- **K-cls** `auto-mode-classifier.md` gets a `noeta today` column (130 rows).

### Rule catalogue (each branch, for its area)
- **K1** Wrong `covered` rows: `rem.git-status.3`, `main.env.5`, `main.env.16`, `main.env.17`,
  `main.req.1`, `main.req.5`, `main.session.2`, `rem.deltas.3`, `rem.instructions.9`,
  `rem.instructions.10`, `rem.hook-context.1`, `rem.plan-exited.1`, `rem.auto-mode.3`,
  `rem.absent.1`, `tool.DeferredToolPlaceholder.3`, `tool.mcp.1`, `tool.Bash.21`,
  `tool.Bash.s.*`, `tool.ToolSearch.r.5`, `tool.Monitor.9`, `sub.common.8`, `side.progress.1`,
  `compact.reattach.6`; DEV row 71; CLO Q3, N5.
- **K2** Non-matching rules without a row: `rem.empty-reply.5`, `tool.TaskStop.2`,
  `compact.reattach.10`.

### Leftovers
- **L1** `${CLAUDE_SESSION_ID}` / `${CLAUDE_EFFORT}` (S14).
- **L2** `rem.git-status.3` says 2,048 bytes; code cuts at 2,000 characters.
- **L3** `docs/specs/2026-10-29-open-source-follow-ups.md` lists three done items as open:
  the joined-message `"\n"` (b3dfa6c1), `-p` AskUserQuestion / title / beta (470ed2d9), plugin
  `<plugin>:<name>` namespacing (0ab0f24f).

## Decisions

## Open questions (owner)

## Plan

- [ ] Spec (this file); baseline `make check`
- [ ] `rr-parity-tools`: K0, T1, T3-T10, T14-T16, K-results
- [ ] `rr-shell`: R11-R13, T2, T11-T13
- [ ] `rr-prompt-env`: P1-P9, E1-E10, R9, R10, L2
- [ ] `rr-reminders`: R1-R8, R14-R21
- [ ] `rr-skills`: S1-S16, L1
- [ ] `rr-memory-compaction`: M1-M7, C1-C6
- [ ] `rr-agents`: A1-A14, K-cls
- [ ] K1, K2 rows (per branch); L3
- [ ] Merge; gates `make check`, `make parity`, `make cluster`, `make e2e`; eval

## Acceptance criteria

1. Every item above is matched (with a test) or has a Decisions entry and a
   `parity/deviations.md` row; owner items also appear under Open questions.
2. `compare.py` compares each request's tool order; `make parity` 0 mismatches.
3. The K1 / K2 rows are correct; `auto-mode-classifier.md` and every result-shape row carry
   noeta's status.
4. `make check` green, `make cluster` and `make e2e` green; the eval shows no regression
   outside the known-noisy scenarios.

## Risks

- Many branches touch `parity/deviations.md`, the rule files and request goldens: merge
  conflicts, resolved by keeping both sides and re-recording goldens.
- Matching CC's per-model behaviour (P2, R9) changes requests for every non-baseline model.

## Progress log
- 2026-11-02 Created from the re-audit of local main @c5d3647a.
