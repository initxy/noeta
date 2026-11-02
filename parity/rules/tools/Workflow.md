# Workflow

Claude Code 2.1.312 · baseline `m1` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `m1/0001 tools[Workflow].description` | `d265fa636f6d687370d5fcf79080669693909611e61e017f2bdf3b7d35ecef17` |
| `m1/0001 tools[Workflow].input_schema` | `bb807e42786ea43c32e95c8268e4c61b36577e909d73c9b9d9dad4ca70a1972d` |

## Purpose

Runs a JavaScript orchestration script that fans out many sub-agents deterministically, in
the background. Always loaded; position 11 of 13 in the baseline list. Never called in the
recordings. noeta: `noeta/plugins/workflow` (the script replayed in an embedded V8, each `agent()` a
child task).

## Parameters

`additionalProperties: false`; nothing required.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.Workflow.p.script` | `script` | string | no | maxLength 524288; must start with `export const meta = { name, description, phases }` as a pure literal, then a body using `agent()`, `parallel()`, `pipeline()`, `phase()` | covered |
| `tool.Workflow.p.name` | `name` | string | no | name of a predefined workflow (built-in or `.claude/workflows/`) | covered — the project's `.noeta/workflows/<name>.js`, then `~/.noeta/workflows/<name>.js` (noeta reads only its own places, never `.claude/`); no built-in workflows |
| `tool.Workflow.p.description` | `description` | string | no | ignored; the description lives in `meta` | covered |
| `tool.Workflow.p.title` | `title` | string | no | ignored; the title lives in `meta` | covered |
| `tool.Workflow.p.args` | `args` | any (no type) | no | exposed to the script as global `args` verbatim; arrays/objects passed as JSON values, never as a JSON-encoded string | covered |
| `tool.Workflow.p.scriptPath` | `scriptPath` | string | no | path of a script on disk; every run saves its script under the session directory and returns that path; wins over `script` and `name` | covered — saved under the task's spill directory |
| `tool.Workflow.p.resumeFromRunId` | `resumeFromRunId` | string | no | pattern `^wf_[a-z0-9-]{6,}$`; finished `agent()` calls with unchanged (prompt, opts) return cached results, only edited/new calls rerun; same session only; stop the earlier run (`TaskStop`) first | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.Workflow.1` | Runs in the background: the call returns a task id at once and a `<task-notification>` arrives on completion; `/workflows` shows live progress. | covered — progress reaches front ends as `workflow` notices; there is no `/workflows` view |
| `tool.Workflow.2` | Call it only after an explicit user opt-in to multi-agent orchestration, because a workflow can start dozens of agents and spend many tokens. | covered |
| `tool.Workflow.3` | Opt-in means one of: the prompt contains the keyword `ultracode` (confirmed by a reminder); ultracode is on for the session (confirmed by a reminder); the user asked in their own words for a workflow / fan-out / orchestration; a user-invoked skill or slash command says to call `Workflow`; the user asked for a named or saved workflow. A task that would merely benefit from one does not count. | covered |
| `tool.Workflow.4` | Without opt-in, even for clearly parallel work: use `Agent` for single sub-agents, or describe what a workflow would do and roughly cost, ask whether to run it, and mention the user can say "use a workflow" next time to skip the question. | covered |
| `tool.Workflow.5` | `meta` is a pure literal (no variables, calls or interpolation) holding `name`, a one-line `description` that the approval prompt displays, and optional `phases` — one `{title, detail?}` per `phase()` call, titles matching exactly. | covered |
| `tool.Workflow.6` | The script goes inline in `script`, never via a file written beforehand; do not set `name` at the same time (that selects a saved workflow). The script is plain JavaScript, not TypeScript. | covered |
| `tool.Workflow.7` | The canonical shape is a pipeline: each item's later stage starts as soon as its own earlier stage finishes, instead of waiting for the whole stage (the description carries a worked review-then-verify example). | covered |
| `tool.Workflow.8` | Load the `workflow-authoring` skill before writing a script (API, gotchas, resume, ultracode, patterns, examples). | contradicts — no bundled `workflow-authoring` skill; a short API reference is in the description (`parity/deviations.md`) |
| `tool.Workflow.9` | Size guideline is part of the description: the default setting is "medium", keep workflows under 10 agents; it is a guideline the user's prompt may override, and the user changes it under "Dynamic workflow size" in `/config`. | covered |

## Result shape

| id | shape (observed) | noeta today |
|---|---|---|
| `tool.Workflow.r.1` | Not observed (never called). Documented: a task id returned at once, completion as a `<task-notification>`, the saved script path in the result. | covered (`workflow/tool.py` `STARTED` / `SAVED`: the run id at once, the saved script path, the end as a `<task-notification>`) |
| `tool.Workflow.r.2` | (2.1.312 bundle) The end notice: `<task-id>` (the run id), `<tool-use-id>`, `<output-file>`, `<status>` (`completed` / `failed` / `killed`), `<summary>` naming it a dynamic workflow with its `meta.name` in quotes and completed, failed with the error, or stopped. A failed or stopped run then carries `<recovery>`: the `Workflow` call with `scriptPath` and `resumeFromRunId` (and `args`) that resumes it, and the agents' transcript directory. A completed run carries `<result>` (cut at 8,000 characters, the rest in the output file) and `<diagnostics>`: the per-agent results journal, a request to read it before diagnosing an empty result, and the resume call for re-running changed post-processing. Then `<failures>` (one line per failed agent) and `<usage>` with `<agent_count>`, per-state agent counts, `<subagent_tokens>`, `<tool_uses>`, `<duration_ms>`. | covered for the summary, `<recovery>`'s resume call, `<result>`, `<diagnostics>`' resume call, `<failures>` and `<usage>`'s `agent_count`, `subagent_tokens`, `tool_uses`, `duration_ms`; no output file, transcripts, journal or per-state counts (`deviations.md`) |

## Variants

Same hashes in every session.
