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

Matched items need no entry: CC's way, with a test each (the branches' commits name them).
Resolved items, with the evidence that settled them:

- **P9** One recorded beta order; a beta noeta adds goes in at CC's place. `classifier_context` is
  server-side input the model never reads (its field differences: rr-final).
- **E10** The bundle (`XTn` / `Ekt` / `Rkt`) recomputes the environment once per prompt and sends
  "# Environment update" for cwd, worktree, repository, extra directories (and scratchpad, `gh`,
  proxy, which noeta does not show). Matched for noeta's facts; scope below.
- **R21** `criticalSystemReminder_EXPERIMENTAL` is an `--agents` / SDK agent field sent with every
  call (matched); `mcp_dropped_tools_delta` is CC's schema check of MCP tools (owner).
- **S12** CC's skill discovery and `paths:` unlock run only from `Read` / `Write` / `Edit` (bundle
  offsets 26229228, 27949542, 29441476), not from shell reads: CLO Q3 was wrong for skills; noeta
  now matches (and `NotebookEdit` no longer finds skills).
- **S16** CC loads managed, user, project (nearest first), additional directories, legacy
  commands, plugins, first name wins. Matched.
- **M6** The bundle's `oVe` cuts frontmatter from every instruction file (noeta already did).
- **M7** CC's recall is gated off by default (`tengu_moth_copse` / `CLAUDE_MEMORY_STORES`); noeta's
  is off unless `recall_model` is set. Match.
- **C5, C6** see rows below.
- **T15** `to` one line, at most 300 characters; `message` default `""`. Matched.
- **T16** Three ScheduleWakeup variants keyed to the Anthropic cache TTL; row below.
- **A1** CC reviews `SubagentHandback` as an ordinary transcript entry (m1/0035), as noeta did;
  `<subagent_hand_back>` with severity scoring is CC's end-of-run review of an agent that ended
  without handing back (owner). A flagged hand-back is now delivered under a warning (matched).
- **A3** The bundle applies `useAutoModeDuringPlan` to plan mode entered from any mode except
  `bypassPermissions` (`_LtL`, `y1n`, `_e()`). Matched.
- **A5** CC builds a "Previous: …" line but never sends it (dead code in `Li()`); noeta sends none.
- **A14** CC's `EnterPlanMode` asks only in diskless (remote) sessions; row below.
- **cls.input.12 / .13** CC's meta and outcome lines sit behind switches that are off by default
  (`vJe`, `J1r`, `thn`); noeta writes none, as CC's default. **cls.input.16** CC's `autoMode`
  lists replace their section's built-in text (`"$defaults"` keeps it; `iAn`, `Mg`): matched.
- **UserPromptSubmit `continue: false`** CC keeps the prompt (`promptKept: true`, `TRr`), adds
  "Operation stopped by hook" and makes no model call: matched.
- **Compaction fallback tools** the call site's `_a` is lodash `uniqBy` (names once, order kept,
  no sort): matched.

Kept differences (each also a `parity/deviations.md` row; pi `badlogic/pi-mono`, dsh
`deepseek-ai/DeepSeek-Harness`):

- **P1 / R7 (full text) / R10 one lean prompt for every model.** CC: models without
  `lean_prompt` (Sonnet 5, Sonnet 4.6, Haiku 4.5) get the full prompt, `<system-reminder>`
  carriers and the full auto-mode text; older and unknown models get the `Task*` tools and
  `task_reminder`. pi: one prompt, no task tools. dsh: one prompt per deployment. noeta: one lean
  prompt, plus the Fable 5.1 and Haiku 5.5 paragraphs. Why: a second prompt, a second reminder
  carrier and four tools are whole features for models the replay never runs. Owner question 1.
- **P5 `outputStyle`** read and ignored. CC: a different prompt opening and `# Output Style`. pi,
  dsh: none. Why: a whole feature (style files, picker); a plugin's prompt part does it
  (`examples/house-style`). Owner question 2.
- **P6 identity line**: one noeta line; CC's variants name Claude Code. **P7** "rendered as
  markdown", not a terminal: noeta's output also goes to HTTP clients, editors and channels.
- **E7 managed attribution** lines are not told apart: noeta has no organization policy.
- **E10 scope**: CC re-sends the environment when platform, shell or OS change; pi none; dsh cwd and
  time. noeta tells repository, worktree and directory changes only. Why: a durable task may wake
  on another worker of the same deployment while commands run in the task's exec env; re-sending
  one worker's OS release would break the prompt cache for no change.
- **R2 / R3 switches**: `ultrathink` raises effort when the task is interactive and the model is
  catalogued `is_reasoning` (CC: its terminal, models with an effort table); `ultracode` is task
  config, not an effort value (CC: a setting or `--effort ultracode`). pi, dsh: no keywords. Why: a
  per-task decision every worker makes alike. **R18** "used" is the task tree's cost read from the
  store (CC: its process's tracker): the same total `-p` enforces, on any worker. **MCP
  instruction notes** keep being re-announced (CC only behind a flag gate): the model is told the
  truth.
- **R5 no shell-first note in `bypassPermissions`** (CC sends it there as in auto mode; pi, dsh:
  none). Measured: the eval (DeepSeek V4.1, all trials in bypass mode) with the note scored
  `deny_rule_respected` 0.78 and `read_over_shell_cat` 0.61 against the 1.00 / 0.89 baseline; an
  A/B on the same head (9 trials each) gave 0.67 / 0.78 with the note and 0.89 / 0.83 without. The
  misses edit a denied file through the shell. A deny rule holds in bypass mode too, and a note
  inviting shell edits makes working around it likelier on an unattended server. Owner question 10.
- **R21 MCP schema check** not built (CC drops tools whose schema the API would reject and tells
  the model). Owner question 3.
- **T2 `TaskStop`** by id only: noeta has no agent teams and `Agent` has no `name` (nor has CC's
  baseline schema); dsh's `job_kill` is by id too. **T11** the steer decision is recorded in the
  task log (CC: in memory per session), so every worker sends the same description.
- **T13 `Write` 8 MB cap.** CC, pi: none; dsh: none found. Why: a call's content is logged and
  resent with every later request; 128K output tokens is about 0.5 MB, so the cap stops only a
  runaway call.
- **Workflow end notice** without output file, transcripts, results journal, per-state counts, or
  the 8,000-character cut. CC: all of them; pi: no Workflow; dsh: no journal, no resume. Why: a
  run's results live in the task log, not a run directory.
- **T3** two raw MCP names that normalize alike get a hash suffix (CC sends both, which the API
  rejects; pi hashes over 64; dsh raw); on OpenAI-family wires a name over 64 is cut with a hash
  and mapped back (CC speaks only the Messages API, 128; pi cuts to 64 everywhere).
- **T6** a fruitless `ToolSearch` names down servers and the `mcp` plugin lists them again once a
  turn; CC reconnects inside the call and also names connecting and policy-blocked servers. pi:
  plain "no match". dsh: no search tool. Why: the tool set is fixed per turn in the log, so any
  worker can serve the next step; noeta has no managed policy.
- **T9** `WebSearch` keeps CC's frame with Tavily snippets where CC's search model writes a
  summary (pi: none; dsh: hits and excerpts): a summary would cost a second model call per search.
- **T14 preapproved `WebFetch` hosts** still asked per domain (CC: not asked; pi: no WebFetch;
  dsh: per request). Owner question 4 (CLO Q6).
- **T16** the 5-minute cache wording on every model (CC: by Anthropic TTL; pi, dsh: none): noeta's
  Anthropic requests use the 5-minute TTL and other providers cache as long or longer.
- **S1 directory-name characters**: letters, digits, marks, `_`, `.`, `-` (CC: any up to 256
  characters without control characters or `<>`; pi `[a-z0-9-]`; dsh checked names). Why: `:`
  marks plugin and scoped names that `Skill(<prefix>:*)` rules match, and a space would split a
  typed `/<name> <args>`.
- **S6 frontmatter** `hooks`, `shell: powershell`, `disallowed-tools` for fork agents not honoured
  (pi, dsh: none of these). Why: skill-scoped hooks need a new mechanism in the hooks plugin; no
  PowerShell tool; `Agent` has no parameter for them. Owner question 5 (hooks).
- **S9 usage ranking** from the store's 200 newest tasks in a background scan (CC: its config
  file; pi, dsh: none). Why: every worker ranks alike; waiting for the scan cost 2.9 s on a new
  conversation's first reply.
- **S10 listing settings**: `NOETA_SLASH_COMMAND_TOOL_CHAR_BUDGET`, no `skillOverrides` (pi: no
  budget; dsh: an off switch). Why: noeta reads its own names; a per-skill switch table is a
  settings feature. Owner question 6.
- **M5 `@import` boundary** row reworded: the working directory or the folder of the chain's
  first file, for project and local files.
- **C5 threshold compaction for every window.** CC skips the threshold for a window no setting
  chose and for a 1M window not set by settings, compacting only after the API refuses the prompt.
  pi: threshold plus overflow; dsh: threshold plus overflow. Why: a refused 1M-token request costs a
  full round trip, and a gateway overflow error noeta cannot recognize would fail the turn.
- **C6 unknown-model window** 128,000 / 16,384 (CC 200,000 / 32,000; pi 128,000 / 16,384; dsh
  1M with 256K or 128K output). Why: noeta serves gateway and self-hosted models, many with 128K;
  a 200K assumption overflows before compaction fires. `register_models` or
  `NOETA_AUTO_COMPACT_WINDOW` give real numbers.
- **A1 end-of-run review** not built (CC: one more severity-scored classifier call over the
  transcript of an auto-mode agent that ended without handing back; pi, dsh: no classifier). Why:
  its text is not delivered anyway and every action was classified when it ran. Owner question 7.
- **A2 headless**: a transcript too long for the classifier falls back to the ask in every host
  (CC aborts a headless agent): noeta hosts answer asks their own way.
- **A5 progress-label timing** at model-call boundaries against recorded times (CC: a 30 s timer):
  works across workers.
- **A13 `<untrusted_instructions>`** outside a trusted workspace (CC: always `<user_claude_md>`;
  pi, dsh: no classifier). Why: CC runs only in folders its trust dialog accepted; noeta runs in
  repositories nobody vetted, where a planted NOETA.md would speak for the person.
- **Classifier context rule shape**: permission rules go as plain strings (CC: objects
  `{rule, source, …}`; pi, dsh: no classifier). Why: the host passes noeta merged rule lists with no
  settings source, so a source would be invented; with an API key the server does not run the
  check, so the field is unread.
- **A14 `EnterPlanMode` asks** in `default` / `acceptEdits` (CC asks only in remote sessions; pi:
  no plan mode; dsh: only the person's `/plan`). Why: noeta runs server-side like CC's remote
  sessions. Owner question 8.

## Open questions (owner)

1. P1 / R10: give older Claude models CC's full prompt, `<system-reminder>` carrier and `Task*`
   tools? Recommended: no, until someone runs those models.
2. P5: support `outputStyle`? Recommended: no; a plugin's prompt part covers it.
3. R21: drop MCP tools whose schema the API would reject, with CC's note? Recommended: yes, as a
   follow-up spec (one bad schema fails every request).
4. T14 (CLO Q6): let CC's preapproved documentation hosts through `WebFetch` unasked?
   Recommended: yes, with an opt-out setting for unattended servers.
5. S6: skill frontmatter `hooks:` active while the skill is loaded? Recommended: no, until asked.
6. S10: read `skillOverrides`? Recommended: no.
7. A1: CC's end-of-run review for an agent that ends without handing back? Recommended: no.
8. A14: stop asking before `EnterPlanMode`, as CC's local sessions do? Recommended: keep asking.
9. `--effort ultracode` and `--settings` in the CLI? Recommended: not until asked (task config
   covers HTTP and SDK hosts).
10. R5: send CC's shell-first note in `bypassPermissions` too? Recommended: no (eval above).

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
