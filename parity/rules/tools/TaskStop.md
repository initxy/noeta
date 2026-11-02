# TaskStop

Claude Code 2.1.312 · re-recording r2, session `m5` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `r2 m5/0015 tools[TaskStop].description` | `8435701ab3677d82c11ae4140d6a1cd37430d8481fbf056581b494ff44253166` |
| `r2 m5/0015 tools[TaskStop].input_schema` | `87b661664be96ffde24f90d21bed6829425249b34531695eb0e5e0329275b3b2` |

## Purpose

Stops a running background task by its id. Deferred: named in the baseline's deferred list
(`main.env.7`) and loaded through `ToolSearch` (`select:TaskStop` in r2 `m5`), after which it
is sent with `defer_loading: true`. Called once in r2 `m5`, on a background `Bash` command.
noeta's is `TaskStopTool` (`noeta/plugins/shell/tools.py`).

## Parameters

`additionalProperties: false`; nothing required by the schema.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.TaskStop.p.task_id` | `task_id` | string | no | the background task's id; agent-team teammates and named background agents are also accepted by agent id or name | covered for background commands and monitors; sub-agents are not stopped by this tool (see `.2`) |
| `tool.TaskStop.p.shell_id` | `shell_id` | string | no | deprecated name for `task_id` | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.TaskStop.1` | Stops a running background task (a background `Bash` command or a `Monitor`) given its id; for terminating long-running work the model no longer needs. | covered |
| `tool.TaskStop.2` | An agent-team teammate is stopped by its agent id or bare teammate name, and a background agent spawned with a name by that name. | n/a — noeta has no agent teams, and its sub-agents are stopped through the task tree (`cancel`), not through this tool |
| `tool.TaskStop.3` | The result reports success or failure. | covered — an unknown id, a foreground job or one no longer running is a tool error saying which |
| `tool.TaskStop.4` | A task stopped with this tool sends no completion notification afterwards (`.r.2`). | covered — the job is stopped quietly (`Registry.kill(quiet=True)`); a job killed for another reason still sends its notification |

## Result shape

| id | shape (observed) | noeta today |
|---|---|---|
| `tool.TaskStop.r.1` | Success (r2 `m5/0016`): a JSON object as text with `message` (stopped, with the id and the command in parentheses), `task_id`, `task_type` (`local_bash` for a background command) and `command`. noeta returns the same keys, `task_type` `monitor_ws` for a WebSocket monitor. | covered (`shell/tools.py` `TaskStopTool`; `task_type` `monitor_ws` for a WebSocket monitor) |
| `tool.TaskStop.r.2` | The stopped command sends no completion notification afterwards in the recording (r2 `m5/0016`–`0022`). | covered (the job is stopped quietly) |
