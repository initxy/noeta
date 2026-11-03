---
status: done
created: 2026-11-03
---

# Plugin splits: plan mode, budget caps, repetition guard, empty-reply re-ask

## Goal

Owner, 2026-11-03: plan mode is its own plugin (rarely used), the permissions plugin can simply
be switched off, and the budget caps, the repetition guard and the empty-reply re-ask become
plugins of their own. Each is a built-in plugin on the public plugin interface, switchable with
`[plugins.<name>] enabled`, works with the others on or off, keeps its state in the task log, and
replays logs written before the split the same. Model-visible requests stay byte-identical with
the default plugins (`make parity` 0 mismatches, golden requests unchanged).

## Non-goals

- Changing what any of these do with the default plugins on.
- Moving the read-only shell-command check out of the permissions plugin (plan mode alone asks
  about every shell command instead).

## Context

Before: plan mode, budget caps and the repetition guard lived in `noeta/plugins/permissions`;
the empty-reply re-ask in the kernel (`noeta/core/runner/closing.py`). The kernel folds a record
only into the extension of the same name.

## Decisions

- Plan mode is `noeta/plugins/plan_mode` (`PlanMode`, name `plan_mode`), placed right after
  `permissions` in every built-in agent and in `DEFAULT_PLUGINS`, so the reminders' order and the
  tool order are unchanged.
- The task's mode stays the `permissions` `mode` / `plan_exit` records: front ends read them
  there, and a log from before the split has every plan record there. `plan_mode` writes its
  switches into and out of plan mode under that name and folds that name back through a new,
  general kernel mechanism, `Extension.folds` (record names besides its own whose records reach
  its `reduce`). Unavoidable kernel change: without it a plugin cannot read records written under
  another name when that plugin is off, nor replay an old log's plan records.
- `plan_mode`'s own records (`noted`, `compacted`, `plan_name`, `plan_file`, the reminders) are
  under `plan_mode`; the kinds keep the names they had under `permissions`, so one fold reads
  both.
- Switches: `plan_mode` records every switch into plan mode (`/plan`, the signal, `EnterPlanMode`,
  a task starting in plan mode) and `ExitPlanMode`'s exit; the permissions plugin records every
  switch to another mode (it knows which modes the host allows). Without the permissions plugin,
  `plan_mode` records those too.
- A task starting in plan mode: `plan_mode` asks the permissions plugin (by duck type, as the
  instructions plugin asks `read_deny_rule`) whether the task starts planning (`mode_of`) and from
  which mode (`plan_start_mode`), so the records are the same as before.
- `useAutoModeDuringPlan` (`auto_during_plan`) is a `PlanMode` argument: it is settled into the
  plan record on entry, which the permissions plugin reads.
- Permissions without `plan_mode`: `plan` is left out of the published `modes`, ignored as a
  switch, a config `permission_mode` or the default mode (default mode then); a log left
  planning reads as the plan's `pre_mode`.
- `Extension.folds` is resolved once per agent into a record name -> extra folders map
  (`also_folding`), passed to `fold` / `apply` (review finding: no reducer introspection).
- The permissions plugin's `resolve_ask` answers its own asks even when another plugin asks on
  the same call; once allowed, that other ask goes to the person.
- `plan_mode` without permissions: while planning, a call is asked about unless its tool is
  read-only or flagged `safe_while_planning` (a general tool flag, set on the tools the
  permissions plugin rates low risk that are not read-only: `Agent`, `SubagentHandback`,
  `SendMessage`, `AskUserQuestion`, `Skill`, `TaskStop`, `ScheduleWakeup`, `Workflow`, the
  task-list tools), it writes the plan file, it is a plan-mode tool, or no plugin offers the tool
  as its own (`ToolSearch`). `ExitPlanMode` asks the person in every mode, as in Claude Code (its
  approval is the tool's own question; bypass does not skip a tool that needs the person).
  `EnterPlanMode` is not asked about (nothing is without the permissions plugin).

- Budget caps are `noeta/plugins/budget.py` (`BudgetCaps`, name `budget`), on by default after
  `plan_mode` in every built-in agent (Claude Code's SDK has `maxTurns` / `maxBudgetUsd`; its
  only cap set by default, the sub-agent depth of 3, was on before too). Flat arguments, so
  `[plugins.budget]` sets them. Its counts are its own records; it folds `permissions` too, where
  a log from before the split has them (`step`, `tool_call`, `cost`).
- The repetition guard is `noeta/plugins/repetition_guard.py` (`RepetitionGuard`, name
  `repetition_guard`). Owner's rule for model-crutch plugins is "follow Claude Code", which has
  no repetition guard: the built-in agents name it but keep it off until a host sets
  `[plugins.repetition_guard]` (`OFF_UNLESS_SET`, as memory and MCP). It was already off by
  default (`repetition_threshold=0`), so no default behavior changes and parity is unaffected.
  Stateless (reads the conversation). Its ask is its own (`by: repetition_guard`), so a host's
  `can_use_tool` on the permissions plugin no longer answers it.
- The empty-reply re-ask is `noeta/plugins/empty_reply.py` (`EmptyReply`, name `empty_reply`), on
  by default (placed before `compaction`): Claude Code 2.1.312 re-asks (its bundle carries the
  exact nudge text). Moving it out needs a kernel hook, `Extension.on_empty_reply(view, response)
  -> Message | None` (first message wins; none ends the turn quietly, which is what an agent
  without the plugin does on the first empty reply). The kernel still writes the `continue` /
  `turn_done` records (reason `empty_reply`) with the same shape, and `empty_nudged` stays in its
  fold, derived from them, so old logs replay the same. The nudge text moved to
  `noeta/toolkit/empty_reply.py`, where the Anthropic adapter still finds it by its words. The
  kernel keeps a hard stop: after `MAX_EMPTY_REPLIES` (3) empty replies in a row the turn ends
  whatever the extensions say, and a failing `on_empty_reply` counts as no opinion.

## Plan

- [x] `Extension.folds` in the kernel fold
- [x] `noeta/plugins/plan_mode` (state, notes, tools, extension); permissions keeps only what may
      run while planning and the auto-mode notes
- [x] agents, settings (`useAutoModeDuringPlan`), compaction's plan-file read, evals' plans_dir
- [x] tests: plan mode alone, permissions alone, neither, an old log replayed
- [x] docs: `docs/extending.md`, parity rules, CHANGELOG
- [x] budget caps split
- [x] repetition guard split
- [x] empty-reply re-ask split (built on branch `empty-reply-plugin`, cherry-picked)
- [x] gates: check, parity, cluster, e2e; targeted eval

## Acceptance criteria

- `make parity` 0 mismatches with the same 11 accepted deviations; golden requests unchanged.
- Each plugin works alone and off (tests); old logs replay the same (tests).
- `make check`, `make cluster`, `make e2e` green; the targeted eval does not regress.

## Risks

- A front end that sends a mode switch in the same inbox batch right after `/plan` now gets the
  switch applied first (the permissions plugin folds the batch before `plan_mode`).

## Progress log

- 2026-11-03: plan mode split; parity 0 mismatches (11 accepted), full test suite green.
- 2026-11-03: budget caps split; full test suite green.
- 2026-11-03: repetition guard split; it was already off by default (threshold 0), contrary to
  the brief's premise.
- 2026-11-03: all four splits on the branch: make check 3507 passed, parity 0 mismatches (11
  accepted), cluster 14 passed, e2e 7 passed; eval (deepseek_v4_1_flash, 9 trials, concurrency
  1) on plan_mode_read_only, plan_mode_started, plan_mode_multi_file, deny_rule_respected,
  denied_not_circumvented 1.00 each, todo_reminder_long_run 0.40 (all equal to the baseline), no
  timeouts, empty-reply nudges 0.
- 2026-11-03: independent review fixed in one pass: `safe_while_planning` tool flag (P1), the
  breaking note for hosts listing plugins themselves (P2), `also_folding` map, kernel empty-reply
  cap and exception handling, `resolve_ask` on own asks, old planning log without `plan_mode`,
  reserved `permissions` record kinds in `docs/extending.md` (P3).
- 2026-11-03: after the review fixes: make check 3515 passed, parity 0 mismatches (11 accepted),
  cluster 14 passed, e2e 7 passed; eval (9 trials, concurrency 3) plan_mode_read_only,
  plan_mode_started, plan_mode_multi_file, deny_rule_respected, denied_not_circumvented 1.00
  each, no timeouts. Done; archived.
