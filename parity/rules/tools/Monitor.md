# Monitor

Claude Code 2.1.312 · re-recording r2, session `m5` · rules in our words (see `../README.md`).

| source | sha256 |
|---|---|
| `r2 m5/0007 tools[Monitor].description` | `086c0d55792bdbd2872dab6a07e35ed62ee9394b6c4939fb20f0010c1a81c812` |
| `r2 m5/0007 tools[Monitor].input_schema` | `f6de00853dce991dc39ea943f324bd6806f7e2ee3c436449c8a33a10fa99aab3` |

## Purpose

Starts a background watch whose stdout lines (or WebSocket text frames) reach the model as
notifications while it keeps working. Deferred: named in the baseline's deferred list
(`main.env.7`) and loaded through `ToolSearch` (`select:Monitor` in r2 `m5`), after which it
is sent with `defer_loading: true`. Called once in r2 `m5`. noeta's is `MonitorTool`
(`noeta/plugins/shell/tools.py`, text in `noeta/plugins/shell/descriptions/monitor.md`,
events and notices in `noeta/plugins/shell/notices.py`).

## Parameters

`additionalProperties: false`; required: `description`, `timeout_ms`.

| id | name | type | required | enum / default / limits | noeta today |
|---|---|---|---|---|---|
| `tool.Monitor.p.description` | `description` | string | yes | a short label of what is watched, shown with every notification | covered |
| `tool.Monitor.p.timeout_ms` | `timeout_ms` | number | yes | default 300000, minimum 1000, maximum 3600000; anything above 1800000 is treated as 1800000; a notice comes at expiry | covered |
| `tool.Monitor.p.command` | `command` | string | no | a shell script; each stdout line is an event, its exit ends the watch | covered |
| `tool.Monitor.p.ws` | `ws` | object | no | `url` (string, required) and `protocols` (array of token-pattern strings), no other keys; each text frame is an event, a binary frame a placeholder line, the socket closing ends the watch; not together with `command` | covered |

## Rules

| id | rule | noeta today |
|---|---|---|
| `tool.Monitor.1` | Events arrive whenever the source produces them and are never the user's reply, even one that arrives while the model waits for an answer. | covered |
| `tool.Monitor.2` | Choose by how many notifications are needed: a single one is a background `Bash` command that exits once the condition holds (an `until` loop), not a monitor. | covered |
| `tool.Monitor.3` | One per occurrence with no known end: a command that never exits (`tail -f`, `inotifywait -m`, `while true`), re-armed when it expires. One per occurrence with a known end: a command that prints lines and then exits. | covered |
| `tool.Monitor.4` | The description gives example sources: a filtered log tail, a directory watcher, a polling loop over an API that prints only new items, a script listening for pushed events, and a poll of CI checks that prints each finished one and exits when none is pending. | covered — the same kinds of example, in our own commands (no Node listener example) |
| `tool.Monitor.5` | An open-ended command for a single notification keeps the monitor armed until it expires; `grep -m 1` after `tail -f` does not fix it, because `tail` never learns the pipe closed once the log goes quiet. | covered |
| `tool.Monitor.6` | Every pipe stage must flush per line (`grep --line-buffered`, `fflush()` in `awk`); `head -N` delivers nothing until it has N lines. | covered |
| `tool.Monitor.7` | Polling loops tolerate one failed request (`|| true`); remote APIs are polled every 30 s or slower, local checks every 0.5–1 s. | covered |
| `tool.Monitor.8` | `description` should be specific, since it appears in every notification. | covered |
| `tool.Monitor.9` | Only stdout makes events; stderr goes to the output file (readable with `Read`) and triggers nothing; merge it with `2>&1` for a command run directly. | covered — stderr goes to the output file with stdout (events come from stdout alone, kept beside it); the end notice names the file in `<output-file>` and carries no stderr |
| `tool.Monitor.10` | A quiet monitor does not mean things went well: the filter must match every terminal state (crash, hang, early exit), not only the happy path; polling loops emit every terminal status; when unsure, match more. | covered |
| `tool.Monitor.11` | Each line printed lands in the conversation, so the filter passes only the lines that would be acted on, never a raw log. A monitor that emits too much is stopped automatically and can be restarted with a narrower filter. | covered — the same rate limits (`notices.py`: 500-character lines, 3000-character batches, a token bucket of 10 refilled every 2 s, stopped after 30 s of suppression) |
| `tool.Monitor.12` | Lines printed within 200 ms of each other are delivered as one notification. | covered |
| `tool.Monitor.13` | The script runs in the shell environment `Bash` uses; its exit ends the watch and the exit code is reported. | covered |
| `tool.Monitor.14` | Every monitor expires after `timeout_ms` (5 minutes by default, 30 at most): it is killed and one notice gives the number of events; re-arm if still needed, using the maximum for long watches and widening the filter if an expiry with no events was unexpected. `TaskStop` ends it early. | covered |
| `tool.Monitor.15` | `ws` opens a WebSocket with no shell and no polling: each text frame is one event however many lines it has; a binary frame shows as `[binary frame, N bytes]`; the socket closing ends the watch with its close code, and an error is reported before the close. | covered — the worker opens the socket itself (`noeta/plugins/shell/websocket.py`), within the sandbox's network rules |
| `tool.Monitor.16` | A WebSocket source is rate-limited like a command, so a filtered feed is preferred; `ws` is preferred to running a WebSocket client in `command`, and a command is used when frames need shell tools to filter or reshape them. | covered |

## Result shape

| id | shape (observed) |
|---|---|
| `tool.Monitor.r.1` | Started (r2 `m5/0009`): one text naming the task id, when it expires unless the source ends first, that one notice comes at expiry and to re-arm if still needed, that each event will be notified, not to poll or sleep, and that an event arriving while waiting for the user is not their reply. noeta's says the same in its own words (`MonitorTool.run`). |
| `tool.Monitor.r.2` | Each event (r2 `m5/0010`–`0012`): a user-role `<system-reminder>` with the background-notification guard (`reminders.md`, `rem.task-notification.2`), then `<task-notification>` with `<task-id>`, `<summary>` naming the monitor's description as a monitor event, and `<event>` holding the line(s). No `tool-use-id`, `output-file` or `status`. |
| `tool.Monitor.r.3` | The end (r2 `m5/0013`): the same guard, then `<task-notification>` with `<task-id>`, `<tool-use-id>`, `<output-file>`, `<status>` `completed` and a `<summary>` saying the monitor's stream ended. noeta: `monitor_end_summary` in `notices.py`. |
