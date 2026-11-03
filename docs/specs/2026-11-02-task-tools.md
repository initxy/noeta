---
status: active
created: 2026-11-02
branch: task-tools
---

# Task tools as a plugin

## Goal

Claude Code (CC) 2.1.312's task list reaches the models CC gives it to: `TaskCreate`,
`TaskGet`, `TaskUpdate`, `TaskList` and the `task_reminder` after ten model turns without
them, as the built-in plugin `todos`, switched on or off per host. Closes `rem.absent.1` and the
task-tools part of `docs/specs/archive/2026-11-02-context-reaudit-closure.md` R10 (owner,
2026-11-02: "write it as a plugin, switched on and off as needed").

## Non-goals

- CC's full prompt and `<system-reminder>` carrier for the older models (the rest of P1).
- `TodoWrite` (CC offers it only with `CLAUDE_CODE_ENABLE_TASKS=false`).
- Agent teams: owners, teammate texts, task assignment messages.
- A task-list panel in noeta's front ends.

## Context

- CC evidence: the recordings never load these tools (the baseline model is Opus 5.5, which
  gets none; `m7` with `--model claude-sonnet-4-5` lists them as deferred names only). Every
  text, schema and rule below is taken from the 2.1.312 bundle's strings export (read locally,
  never committed): tool definitions `Fr` / `jr` / `qr` / `Vr`, gating `FY` / `jde` / `owr`,
  reminder `Xvr` / `Yvr` / `zEe` and its rendering (`case"task_reminder"`), input repairs `SFt`
  / `Wht` / `TBs`, storage `L9s` / `W_e` / `WWt` / `zWt` / `XE`, fork carry `CIt`, compaction
  carry `Vy`, UI reset `mSr`.
- The strings export drops empty lines and lines shorter than four characters: the blank lines
  of the four descriptions and `TaskUpdate`'s closing code fences are restored from the Markdown
  structure (a blank line before each `##` heading; each example's fence closed, examples a
  blank line apart). The reminder takes the export's reading (one blank line before the list).
- pi `badlogic/pi-mono` @ e7a0df27: no task tools. dsh `deepseek-ai/DeepSeek-Harness` @
  f3803c96: not checked.

## Decisions

- **D1 One plugin, `todos`, in the main agent.** Its tools and reminder are offered when the
  task's model is one CC gives them to: CC's canonical name of the model (`canonical`, CC's `kK`:
  its ordered Claude names, `claude-opus-4(?!-\d(?!\d))` read as `claude-opus-4-0` and the same
  for Sonnet, else the id without a date suffix) is in its list of older Claude models
  (`OLDER_MODELS`: Claude 3.x, Opus 4.0-4.7, Sonnet 4.0-4.6, Haiku 4.5), or the model is a
  Bedrock application inference profile. The Claude 5 family, Opus 4.8 and every model that is
  not Claude (DeepSeek, a gateway's routing name) get none. CC: the same rule (`FY`: `zqt()` is
  the canonical name, a string for any model once one is set), checked on every request;
  recorded: `cc-unknown/001.json` (an unknown model) has no `Task*` tool, `m7` (Sonnet 4.5)
  lists all four. Why: owner's call, CC's gating as the default.
- **D2 The switch is `all_models`, not `enabled`.** `[plugins.todos] all_models = true` offers
  the tools to every model (CC's `CLAUDE_CODE_ENABLE_TODO_TOOLS`); `enabled = false` (every
  plugin's switch) takes them from every model. CC: two variables, plus background sessions and
  a `--tools` list naming a task tool also switch them on. noeta: the agent builder takes
  `enabled` out of a plugin's arguments (`agent_files._setting`), so `enabled = true` can only
  keep a plugin, never force its tools on; one more key is the smallest switch. Background
  sessions and `--tools` are CC's own entry points (noeta's `--tools` list already decides what
  is offered).
- **D3 Texts and schemas as CC sends them** (descriptions with their trailing newline, results,
  reminder, repairs and hints), team-free variants (CC's teammate lines only appear with agent
  teams). `TaskUpdate.status` is CC's union (`anyOf` of the three statuses and `"deleted"`);
  the kernel's schema check does not read `anyOf`, so the tool checks it with the kernel's
  wording. The tools repair input before checking it (`unchecked_input`), as CC's
  `coerceInput` runs before its schema check.
- **D4 Reminder trigger as CC's.** A turn is one non-thinking content block of a reply (CC keeps
  each block as its own message); at least 10 since the last `TaskCreate` / `TaskUpdate` call and
  10 since the last reminder, checked before every model call, and only while `TaskUpdate` is
  offered. The reminder records the history's turn count it came after (`task_reminder` record,
  folded into `reminded_at`), so the next count does not look for its (overridable) text; neither
  count reaches back past a compaction. It lists every task (CC reads the whole list for it). Carrier: a system turn after the
  turn notes (order 72). CC sends it to these models as `<system-reminder>` in the user turn;
  noeta sends every per-step note to every model as a system turn (`rem.all.1`, the Main prompt
  row in `parity/deviations.md`).
- **D5 One prompt line.** CC's full prompt for these models says "Use TaskCreate to plan and
  track work. Mark each task completed as soon as it's done; don't batch." under "# Using your
  tools". noeta's lean prompt has no such section; the plugin adds that line (prompt name
  `todos/prompt_line`) before `# Session-specific guidance`, only while the tools are offered:
  +102 characters (about 25 tokens) for those models, nothing for the others.
- **D6 Multi-machine.** The list is a fold of `todos` records (`created`, `updated`, `linked`,
  `deleted`; `state.py`); a call's records land with its result, so the tools are `retry_safe`,
  and snapshots carry the state. CC: files per session on one machine.
- **D7 Continuation.** A resumed task is the same task: list kept (CC: resume keeps the session's
  list). A task continuing another's conversation (a fork, a rewind) starts with the source's
  list (default `continue_state`); CC carries the list to a fork (`CIt`) and a rewind leaves it
  as it is.
- **D8 Sub-agents get none.** CC: background agents and background forks get no task tools
  (`RSt`); foreground sub-agents get them and share the parent's list. noeta's sub-agents always
  run in the background (`tool.Agent.16`), so no task with a parent gets them. pi: no
  sub-agents. Row in `parity/deviations.md`.
- **D9 Compaction.** The list survives (a fold of the log) and is not sent again; the reminder's
  count starts over with the compacted conversation, so ten turns after a compaction without the
  tools it lists the tasks. CC: the same (its list lives outside the transcript; its compaction
  marker carries the list only for a remote session's restore).
- **D10 No reset when all are done.** CC's terminal UI deletes a list 5 s after every task in it
  is completed (ids go on from the highest). A server task has no such UI; the list stays until
  tasks are deleted. Row in `parity/deviations.md`.
- **D11 `TaskCreated` / `TaskCompleted` hooks are still skipped** with a warning (the hooks
  plugin), so no hook can refuse a create or a completion. Running them would need the hooks
  plugin to gate another plugin's tool results. Row; owner question 2.
- **D12 Permissions.** No permission needed and auto mode lets them through without the
  classifier (CC: no permission check; its auto-mode allowlist names them).

## Plan

- [x] `noeta/plugins/todos` (state, tools, descriptions, gating, reminder, prompt line); entry
  point; main agent; permissions table
- [x] Tests: `tests/plugins/test_todos.py`
- [x] Eval scenario `todo_list_multi_step` (tag `todos`)
- [x] Docs: `parity/rules/reminders.md` rem.absent.1, `parity/deviations.md`, CHANGELOG
- [x] Gates: `make check`, `make parity`, `make cluster`, `make e2e`
- [x] Eval: full suite paired (main vs branch, DeepSeek V4.1, 9 trials, concurrency 2); A/B of
  `todo_list_multi_step` with the plugin on and off

## Acceptance criteria

- The four tools and the reminder reach the older Claude models and no other model by default;
  `all_models` and `enabled` switch them.
- The list folds the same on another runtime and in a fork; sub-agents get no tools.
- Gates green; `make parity` 0 mismatches, at most 11 accepted deviations; no eval regression
  without a cause.

## Risks

- The descriptions' blank lines are reconstructed (Context); a recording of CC with an older
  model that loads the tools would settle them (owner question 1).

- Follow-up, not in this branch: `quiet_nudge` finds its earlier nudges by their (overridable)
  text, as `todos` did; moving it to a recorded count changes how a quiet stretch's nudges are
  counted, a change of its own.

## Owner questions

1. Record CC 2.1.312 once with an older model loading the four tools, to check the blank lines
   of their descriptions? Recommended: yes, with the next re-recording.
2. Run `TaskCreated` / `TaskCompleted` hooks? Recommended: not until a host asks.
3. Clear a list whose tasks are all completed, as CC's terminal UI does? Recommended: no.

## Progress log

- 2026-11-02: CC's rules read from the bundle export; the recordings have no schema or text of
  these tools. `make check` green before starting.
- 2026-11-02: plugin, tests and eval scenario in; one smoke trial on DeepSeek V4.1 made all six
  changes in two parallel batches without a task list (0.50).
- 2026-11-02: review: CC gives unknown models no task tools (`zqt()` is the canonical name, a
  string for any model); gating now by CC's canonical name (`kK` ported: `claude-opus-4-20250514`
  and Bedrock ids count); the reminder found by its record, not its text. The first paired eval
  was stopped (it measured the wrong default). The eval scenario switches the plugin on
  (`all_models`), since DeepSeek gets it off by default.
- 2026-11-03: gates on 7939f2a7: `make check` 3139 passed, coverage 90%; `make parity` 0
  mismatches (11 accepted); `make cluster` 14 passed; `make e2e` 7 passed.
- 2026-11-03: eval, DeepSeek V4.1, 9 trials, paired, concurrency 2: main 0.961, branch 0.944 on
  main's 32 scenarios (branch 0.931 with `todo_list_multi_step`); 5 of 594 trials timed out
  (`delegate_parallel_review`). No scenario past its tolerance; the largest drops
  (`concise_direct_answer` 1.00 -> 0.72, answers of 44-86 words; `delegate_parallel_review`
  0.44 -> 0.11, baseline 0.33) are noise: with the plugin off by default for DeepSeek the
  branch sends byte-for-byte main's requests (checked on a scripted session).
- 2026-11-03: A/B of `todo_list_multi_step`, 9 each: on 0.50, off 0.50. With the tools on,
  DeepSeek never loaded them through `ToolSearch` (0 searches, 0 tasks) and made all six
  changes in two parallel batches either way. Baseline row added (0.50, the paired branch run).
