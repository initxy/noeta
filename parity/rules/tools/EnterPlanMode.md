# EnterPlanMode

Claude Code 2.1.312 · rules in our words (see `../README.md`). The baseline sends this tool only
as a deferred name and no recorded session loads it, so the rules come from the 2.1.312 bundle
(the tool's prompt builder and its result text); there is no recorded source to hash.

| source | sha256 |
|---|---|
| bundle 2.1.312, `EnterPlanMode` prompt (default mode, no agent view) | — (not recorded) |

## Purpose

Asks to switch the conversation into plan mode, where the model explores and designs an approach
for the user to approve before writing code. **Deferred** (`defer_loading: true`); its search
hint is about switching to plan mode to design an approach before coding. noeta:
`noeta/plugins/plan_mode/plan.py` (`EnterPlanModeTool`, `descriptions/enter_plan_mode.md`).

## Parameters

Empty object schema (no properties).

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.EnterPlanMode.1` | Use it on the model's own initiative before a non-trivial implementation: agreeing on the approach first avoids redone work. The tool moves the conversation into plan mode for exploring and designing. | covered |
| `tool.EnterPlanMode.2` | Planning is preferred for any implementation that is not simple; any one of the listed conditions is enough. | covered |
| `tool.EnterPlanMode.3` | Condition: new functionality, with two examples of open questions (where a control goes and what it does; which validation rules and messages). | covered |
| `tool.EnterPlanMode.4` | Condition: several valid approaches, with two examples (where a cache lives; many ways to speed something up). | covered |
| `tool.EnterPlanMode.5` | Condition: a change to existing behavior or structure whose exact target is open, with two examples (a reworked flow; a restructured component). | covered |
| `tool.EnterPlanMode.6` | Condition: a choice between patterns or technologies, with two examples (live-update transport; state management approach). | covered |
| `tool.EnterPlanMode.7` | Condition: work likely to touch more than two or three files, with two examples (an auth rewrite; an endpoint with tests). | covered |
| `tool.EnterPlanMode.8` | Condition: requirements that need investigating before the scope is known, with two examples (find why something is slow; find a bug's cause). | covered |
| `tool.EnterPlanMode.9` | Condition: the user's preference matters; if the model would ask the user how to approach it with `AskUserQuestion`, it plans instead, so the options come with context. | covered |
| `tool.EnterPlanMode.10` | Skip it only for simple work: one- or few-line fixes, one function with clear requirements, tasks the user specified in detail, pure research. (In agent-view mode Claude Code adds that research goes to the `Agent` tool; noeta has no agent view.) | covered (agent view: n/a) |
| `tool.EnterPlanMode.11` | A section says what plan mode involves, in six steps: explore the code (naming the search and read tools), learn existing patterns, design the change, present the plan for approval, ask with `AskUserQuestion` when an approach needs a choice, leave with `ExitPlanMode` when ready to implement. | covered (noeta names `find` / `grep` through `Bash` and `Read`: it has no `Glob` / `Grep` tools, as Claude Code's variant with embedded search tools names `find` / `grep`) |
| `tool.EnterPlanMode.12` | Examples: five requests worth planning (auth, query speed, theming, a small-looking delete button with its many parts, API error handling), each with why; three not worth it (a typo, a debug log line, a where-is question). | covered (same cases, our wording) |
| `tool.EnterPlanMode.13` | Closing notes: entering needs the user's approval; when unsure, plan; users value being consulted before large changes. | covered |

## Result shape

| id | shape | noeta today |
|---|---|---|
| `tool.EnterPlanMode.r.1` | On entry: plan mode is on; then six numbered steps (explore patterns, find similar features, weigh approaches, ask with `AskUserQuestion` when needed, settle a concrete strategy, present it with `ExitPlanMode`); then that no file may be written or edited yet. | covered (`plan.py` `ENTERED`) |
| `tool.EnterPlanMode.r.2` | Called from a sub-agent: refused (agents cannot enter plan mode). | covered (the tool is offered to the main agent only) |
| `tool.EnterPlanMode.r.3` | Asked of the person where Claude Code asks (its diskless sessions); declined, the call is refused. | covered with a deviation (`deviations.md`: noeta asks in `default` and `acceptEdits`) |
