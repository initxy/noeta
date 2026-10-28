# ExitPlanMode

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0060 tools[ExitPlanMode].description` | `8293b7e6fccc18175e2d11fdda25030a76756bc9ddc4ee8cacfac98244a92be1` |
| `m1/0060 tools[ExitPlanMode].input_schema` | `626fdcf43100dc4a523eda6d31c2be3aec6253d8a7ec52f8977535dd6a9bd69f` |

## Purpose

Ends plan mode by asking the user to approve the plan the model wrote to the plan file.
**Deferred** (`defer_loading: true`): listed by name in the deferred-tool list and loaded in
m1 with `ToolSearch` `select:ExitPlanMode` after `/plan`; from then on sorted into the list by
name (between `Edit` and `ListAgents`). noeta: `noeta/plugins/permissions/plan.py` (deferred, main agent only; the approval is a question with the mode choices).

## Parameters

Schema has no `required` and `additionalProperties: {}` (any extra field accepted — unlike the
other baseline tools).

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.ExitPlanMode.p.allowedPrompts` | `allowedPrompts` | array of `{tool, prompt}` | no | deprecated, unused; `tool` enum `Bash`; `prompt` a semantic action description ("run tests") | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.ExitPlanMode.1` | Call it in plan mode once the plan is written to the plan file and ready for approval. | covered |
| `tool.ExitPlanMode.2` | The plan file's path is given in the plan-mode system text (see `reminders.md`); the plan must already be written there. | covered |
| `tool.ExitPlanMode.3` | The tool takes no plan content; it reads the plan from that file. | covered |
| `tool.ExitPlanMode.4` | It only signals that planning is done; the user reviews the file's contents. | covered |
| `tool.ExitPlanMode.5` | Use it only for tasks whose plan is implementation steps for writing code; not for research, searching, reading or understanding code. | covered |
| `tool.ExitPlanMode.6` | Before calling, the plan must be complete and unambiguous; open questions on requirements or approach go to `AskUserQuestion` earlier. | covered |
| `tool.ExitPlanMode.7` | Never ask "is the plan okay?" / "should I proceed?" through `AskUserQuestion`; this tool is the approval request. | covered |
| `tool.ExitPlanMode.8` | The description illustrates rule 5 with three cases: understanding existing code (no), implementing a feature (yes), a feature with an unclear choice (ask first, then yes). | covered |

## Result shape

| id | shape (observed) |
|---|---|
| `tool.ExitPlanMode.r.1` | On approval (m1/0063), a plain string: the user approved and coding may start, starting with the todo list if one applies. |
| `tool.ExitPlanMode.r.2` | Then the saved plan path: `$CFG/plans/<adjective>-<adjective>-<noun>.md` (three random words, e.g. a "swirling-tickling-bubble" pattern), with a note that it can be consulted during implementation. |
| `tool.ExitPlanMode.r.3` | Then a `## Approved Plan:` heading followed by the full plan file text. |
| `tool.ExitPlanMode.r.4` | The plan was written with `Write` to that path before the call (the plan file lives under the config dir, not the project). |

## Variants

Not present in s1 (no plan-mode scenario there). Same hash in m1 and m4.
