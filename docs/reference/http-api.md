# HTTP / WebSocket API

Every endpoint `noeta serve` answers, its authentication, and the two streaming formats: the
event stream of `/tasks/{id}/watch` and Claude Code's headless protocol at `/stream-json`.
The code is `noeta/host/http.py`, `_http_routes.py`, `_http_plugins.py`, `_http_stream.py` and
`_http_guard.py`;
`GET /openapi.json` returns the same API as an OpenAPI 3.1 document. Running the server is
covered in [Run it as a server](../guides/server.md).

The API is the client API on the wire: create, send, answer, signal, cancel, read and watch.
`noeta.RemoteClient` (and `noeta.connect("https://…")`) calls it with the same methods as
`noeta.Client`, and the terminal UI (`noeta --server URL`), `noeta acp --server URL`, the web
app and the desktop app all use it.

## Authentication and the checks every request passes

<Diagram name="ref-request-guard" />

1. **Host and origin.** On a loopback bind only the loopback names (`127.0.0.1`, `localhost`,
   `::1`) and the `--allow-host` names are answered; on another bind also this machine's
   hostname, fully qualified name and addresses. Any other `Host` is refused (DNS rebinding). A request a browser sends from another origin is
   refused unless that origin is the server's own page or an `--allow-origin`. Refused: `403
   {"error": "cross-site request refused"}`.
2. **Bearer token.** `Authorization: Bearer <token>` on every request. A WebSocket may pass it
   as `?token=` instead, because a browser cannot set the header there; no other route takes
   the query parameter, and the server keeps `?token=` values out of its logs. Missing or
   wrong: `401 {"error": "unauthorized"}`. `noeta serve --no-token` (loopback only) turns
   this off.
3. **What a request may ask for** (`GuardedClient`). A request may set only these task config
   keys when it creates a task: `model`, `effort`, `max_turns`, `cwd`, `permission_mode`,
   `output_schema`, `timezone`, `interactive`, `permission_prompt_tool`, `ultracode`,
   `max_budget_usd`, `continued_as`, `bash_mode`, `plugins_off`. `max_budget_usd` must be a number above 0 (not a bool or
   a string), here and in a `config` signal; anything else is `400`. `plugins_off` is a list of
   plugin names; it may not name a built-in plugin a task is built on (the ones it may name:
   [Switching a plugin off](./plugins.md#switching-a-plugin-off)): `403`. Keys the server runs
   with (`--effort`, `--permission-mode`) are laid under the request's, which win. It may post only the signals front ends send: `permission_mode`, `config`,
   `resumed`, `restore_files`, `rename`, `archive`. It may not start or switch a task into
   `bypassPermissions` unless the server runs with `--allow-bypass-permissions` (`403`). A `cwd`
   may be any directory (a relative one is taken from the server's directory and stored
   absolute); one that does not exist or is not a directory is `400`. A task created without
   one runs in a scratch directory of its own, `~/.noeta/scratch/<YYYY-MM-DD>/<first 8
   characters of its id>/`, made then. Any other refusal is `403` with the reason.

Other errors: an unknown task is `404 {"error": "unknown task …"}`; a malformed body or query
is `400` with the reason. Every error body is JSON `{"error": …}`; a path not listed here
is a plain `404` (unless the server serves the web app's files, below).

The token is `NOETA_SERVER_TOKEN`, else the one in the server's `~/.noeta/server-token`
(owner only), made on its first start ([ADR-034](https://github.com/initxy/noeta/blob/main/docs/adr/034-the-server-token-file-holds-the-token-and-a-request-may-name-any-directory.md)); every start prints the address to
open with it as `#token=…`.
Host code that builds the app itself calls `noeta.host.http.create_app(client, token=…)` or
`token_sha256=…`; `auth=False` is the only way to serve without one.

## Tasks

| Method and path | Body / query | Answers |
|---|---|---|
| `POST /tasks` | `{agent, input?, config?, queue?, dedupe_key?, history_from?, history_through?}` | `201 {"task_id": …}` |
| `GET /tasks` | `?parent_id=&status=&limit=&roots_only=` (`status`: `ready`, `running`, `waiting`, `ended`; `limit` default 100; `roots_only=true`: only tasks without a parent) | a list of task rows |
| `GET /tasks/{id}` | | one task row |
| `GET /summaries` | `?limit=&archived=` (`limit` default 50; `archived=true` / `false` keeps only those / the others) | the root tasks, most recently updated first, each `{task_id, agent, status, waiting_on, end_status, created_at, updated_at, title, cwd, prompt, archived}` (`prompt`: the first one, 200 characters) |
| `GET /tasks/{id}/events` | `?after=N` | the task's events after seq `N`, as `Recorded` objects (`seq`, `at`, `event`) |
| `GET /tasks/{id}/watch` | `?after=N` | `text/event-stream`, below |

- `input` is a string, or a user message `{"role": "user", "content": [...]}` holding only
  text and image blocks; it is always the person's (a client never writes another role or
  origin).
- `dedupe_key` makes the create idempotent: a repeat returns the first task's id.
- `history_from` / `history_through` start the task from another task's conversation up to
  that task's event `history_through` (a fork).
- A task row has `task_id`, `root_id`, `parent_id`, `agent`, `queue`, `status`, `waiting_on`,
  `end_status`, `last_seq`, `created_at`, `updated_at`, `ready_at`, the claim's `owner`,
  `owner_epoch` and `lease_until`, and the inbox counters `inbox_seq` / `inbox_read_seq`. A
  field still at its default (`null`, `0`) is left out of the JSON.
- The ten event types and their fields are in [Events](./events.md).

## Talking to a task

| Method and path | Body | Answers |
|---|---|---|
| `POST /tasks/{id}/messages` | `{input, when?, dedupe_key?}` (`when`: `next_turn`, the default, or `now`) | `{"inbox_seq": n}`; for a local slash command `{"output": text}` and nothing is sent |
| `GET /tasks/{id}/pending` | | approvals and questions waiting on a person, the task's sub-agents' included |
| `POST /tasks/{id}/answers` | `{request_id, decision?, always?, data?}` | `{"ok": true, "inbox_seq": n}`; `409` when the task is not waiting on `request_id` |
| `POST /tasks/{id}/signals` | `{name, data?, dedupe_key?, wake?}` | `{"ok": true, "inbox_seq": n}` |
| `POST /tasks/{id}/cancel` | `{reason?, turn_only?}` or no body | `{"ok": true}` |

**Messages.** A text starting with `/name args` runs the task agent's slash command of that
name first: a prompt command's text is what gets sent; a local command answers `{output}`.
A text that names no command is sent as it is.

**Pending.** Each item has `task_id` (the task that asks, maybe a sub-agent), `request_id`,
`kind` (`approval` or `question`), `tool`, `input`, `prompt`, `detail`, `agent`, a `title`
for a card ("Bash command · from the Explore agent") and, for an approval, `choices`
(`[{id, label}]`: yes, the "don't ask again" rule, a mode switch, no).

**Answers.** For an approval, `decision` is `allow` or `deny`, and `always: true` also
remembers the rule the card offered. `data` adds: `{"mode": m}` picks a card's mode choice,
`{"message": t}` adds the reason for a no or words with a yes. For a question, `data` maps
each question id to `{"selected": [labels]}` or `{"other": "text"}`. A `request_id` the task
is not waiting on (unknown, or answered and already taken) is `409`, so a typo or a stale card
is not answered silently; a request stays open until its answer is taken, so a retried answer
still succeeds, and a second answer to the same request is ignored.

**Signals.** `permission_mode` with `{mode}` switches the permission mode; `config` with any of
`model`, `effort`, `max_turns`, `output_schema`, `interactive`, `permission_prompt_tool`,
`ultracode`, `max_budget_usd`, `plugins_off` changes the task's config (a `null` value removes the key; a
request may only remove `client_hooks`, never set it, and `max_budget_usd` must be a number
above 0); `resumed`, `restore_files`, `rename`
`{title}` and `archive` `{archived}` are what front ends send when a person reopens, rewinds,
renames or archives. `wake: false` leaves a waiting task waiting. A model's route (which
provider serves it) is recorded by the server, never taken from a request.

**Cancel.** Ends the task, or with `turn_only: true` stops only the current turn.

**A front end's task.** A conversation the TUI, `noeta -p` or an editor started is on the
`front-end` queue, which a server's workers never take by themselves. A message, an answer, a
cancel, a signal that wakes, or a rewind sent here for one makes the server take its task tree
over: it moves to the server's queue and runs here from then on (a front end opening it later
still can). While a front end is running it (or keeps its claim over a wait), nothing moves:
what was sent reaches that front end, which handles it. A rewind's new task is always on the
server's queue.

## Reading more about a task

| Method and path | Answers |
|---|---|
| `GET /tasks/{id}/usage` | the task tree's `tasks`, `cost_usd`, `api_seconds`, `usage`, and `by_model` (`usage`, `calls`, `cost_usd`, `api_seconds` per model) |
| `GET /tasks/{id}/context` | what fills the next request: `{model, window, total, categories, deferred_tools, tools}`, `categories` being estimated tokens per part (`System prompt`, `System tools`, `MCP tools`, `Messages`, `Free space`, Claude Code's `/context` order). Only when a worker in this server process runs the task's agent (not with `--no-worker`); else `501` |
| `GET /tasks/{id}/rewind` | the person's prompts, each `{index, task_id, seq, at, text, files}` (the files changed since) |
| `POST /tasks/{id}/rewind` | `{index, conversation?, files?}` (both default true): go back to before prompt `index`, restoring the conversation (in a new task), the files, or both. Answers `{task_id, prompt, files}` |

## Commands, agents, models, files

| Method and path | Query / body | Answers |
|---|---|---|
| `GET /commands` | `?agent=&task_id=&cwd=` | the slash commands an agent's plugins offer: `[{name, description, argument_hint, local}]` |
| `POST /commands/run` | `{text, agent?, task_id?, cwd?}` | `{text, local}` for a `/name args` text, or `null` when it names no command; nothing is sent |
| `GET /agents` | | `[{name, description, model, one_shot}]` |
| `GET /models` | | the model catalog: `[{id, context_window, max_output_tokens, aliases}]` |
| `GET /files` | `?q=&task_id=&cwd=&limit=` | paths under a task's (or a `cwd`'s, else the server's) directory holding `q`, for a prompt's `@` mentions (`limit` default 30) |
| `GET /openapi.json` | | this API as an OpenAPI 3.1 document |

With a `task_id`, commands are listed and run in that task's own agent and directory; with a
`cwd`, the directory is checked as a new task's would be.

## Projects and directories

A project is a directory: one a person saved, or one where root tasks run. The web app lists
them down its side and picks a new one with `GET /dirs`.

| Method and path | Query / body | Answers |
|---|---|---|
| `GET /projects` | | `[{path, name, saved, sessions, scratch}]`: the saved directories in the order saved, then the other directories of unarchived root tasks, most recently used first. `path` is the real path (symlinks resolved), `name` its last part, `sessions` the count of unarchived root tasks there (of the 500 most recent). Scratch directories are never projects: when root tasks run in them, the last entry is `{path: <the scratch root>, name: "Scratch", saved: false, sessions: n, scratch: true}`; every other entry has `scratch: false` |
| `POST /projects` | `{path}` | saves it and answers its entry; `path` is checked as a new task's `cwd` (relative to the server's directory; `400` when it is not a directory, or is a scratch directory) |
| `DELETE /projects` | `{path}` | `{ok: true}`: no longer saved; its tasks stay |
| `GET /dirs` | `?path=` (default: the server's directory) | `{path, parent, dirs: [{name, path}]}`: the subdirectories of any directory, by name, hidden ones (a leading `.`) left out; `parent` is `null` only at `/`. `400` for a path that is not a readable directory |

The saved list is kept in `projects.json` beside a sqlite store's database file, else in
`~/.noeta/`.

## Plugins

| Method and path | Query / body | Answers |
|---|---|---|
| `GET /plugins` | `?agent=&cwd=` (`agent` default `main`) | `{"plugins": [PluginInfo], "ui_api": [min, max]}` |
| `PUT /plugins/{name}` | `{enabled}` | `{"ok": true}`: the plugin on or off for tasks created from now on |
| `GET /tasks/{id}/ui` | | `{"items": [UiEntry]}`: what the task's plugins show now |
| `POST /plugins/{name}/call` | `{task_id, name, args?}` | `{"result": …}`, the plugin's `ui_call` answer; `404` when the task does not run the plugin or it raises `UnknownUiCall`; `500` when the plugin fails |
| `GET /plugins/{name}/web/{path}` | | a file of the plugin's web part, **without a token** |
| `GET /market` | | `{"entries": [MarketEntry], "notices": [str]}`: the configured marketplaces' plugins, and why a marketplace could not be read (its last listing is shown then) |
| `POST /plugins/install` | `{source, marketplace?, commit?}` | `201` with the installed plugin's `PluginInfo` |
| `DELETE /plugins/{name}` | | `{"ok": true}` |

- A `PluginInfo` is `{name, version, description, source, builtin, enabled, switchable,
  broken, parts, ui_api, web}`. `enabled`: in the agent and on for new tasks; `switchable`:
  front ends may switch it (an installed plugin, or a built-in a task may run without); `broken`: why it, or its web part, cannot be used (`null` when
  nothing is wrong); `parts` among `backend`, `ui`, `web`, `skills`, `agents`, `commands`,
  `hooks`, `mcp`; `web`: `{"entry": "/plugins/<name>/web/<file>?v=<hash>"}` or `null`. The
  listing names the agent's plugins first, then installed ones it does not run, plugin files
  that failed to load and Claude Code plugins.
- A task's own switch is the `config` signal with `plugins_off`; `PUT /plugins/{name}` changes
  only the machine's default for new tasks (`~/.noeta/plugin-switches.json`, joined into
  every new task's `plugins_off`). A plugin a task may not run without is `400`; with
  `--no-worker` the switches stay with the workers (`501`).
- A `UiEntry` is `{plugin, type, key, …}`: `type` `status` with `text` and `tone`, `widget`
  with `markdown` and `placement`, `notice` with `text` and `level`. Front ends fetch it
  again when the watched log advances. With `--no-worker` it is always empty.
- Web files are served only for a plugin an agent here runs whose manifest's `ui_api` is in
  the server's range, from the directory of its `web` module and never outside it, with the
  web app's headers (a JavaScript file as `text/javascript`). The `Host` and origin checks
  still apply.
- A `MarketEntry` is `{marketplace, name, description, version, source, commit, parts,
  ui_api, installed}`.
- Installing and removing run what `noeta plugin add` and `noeta plugin remove` run, then
  reload the plugins as `/reload-plugins` does. They are allowed only on a server listening on
  a loopback address (the desktop app's) with no `--allow-host` or `--allow-origin` (a proxy
  in front), or started with `--allow-plugin-install`; elsewhere they are `403`. A plugin with options to set (`userConfig`) is refused over HTTP (`400`):
  install it from a terminal.

## The event stream: `GET /tasks/{id}/watch`

<Diagram name="ref-http-stream" />

A server-sent event stream (`text/event-stream`, `cache-control: no-cache`) of the task's
events after `?after=N`:

```text
event: event
data: {"seq":42,"at":1793872072.95,"event":{"type":"ModelCalled","model":"claude-sonnet-5-5",…}}

event: delta
data: {"kind":"text","text":"Reading the","call_seq":42,"attempt":1,…}

: keep-alive

event: end
data: {}
```

- `event` messages are recorded events, in order, each exactly once from `after` on.
- `delta` messages are live output that is never stored: model text (`kind: "text"`),
  thinking (`thinking`), or a running tool's latest output (`tool`, with `call_id`, `tool`,
  `elapsed`). `call_seq` and `attempt` name the `ModelCalled` a fragment belongs to; a retry
  starts over, so drop fragments of an earlier attempt. The recorded `ModelReplied` is the
  truth. Deltas come from workers in the server's own process, and, on a Postgres store, from
  workers on other machines through Postgres notifications (best effort). On sqlite with
  `--no-worker` there are none.
- A comment line `: keep-alive` is sent after 15 seconds without a message.
- `end` closes the stream when the task has ended and every event was sent; `{"error": …}`
  when following the task failed.

To resume after a dropped connection, reconnect with `after` set to the last `seq` you saw.
`RemoteClient.watch` does this by itself (up to five reconnects in a row without an event).

## Claude Code's headless protocol: `/stream-json`

The same protocol as `noeta -p --input-format stream-json --output-format stream-json`
(`noeta/host/stream_json.py`), so a program written for Claude Code's Agent SDK can drive a
remote noeta.

**WebSocket** `GET /stream-json` (upgrade): one JSON message per frame, both ways. The client
sends `user` messages and `control_request`s; the server sends `system` / `init`, `assistant`
and `user` messages, `stream_event`s with partial messages on, system notes, a `result` per
prompt and `control_request`s of its own (`can_use_tool`: the client answers permission
prompts on this socket). Query parameters: `resume` (a task id), `fork`, `agent`, `model`,
`cwd`, `permission_mode`, `max_turns`, `include_partial_messages`, `replay_user_messages`,
and `token`. A request from another site's page or to a rebound `Host` name is refused in the
handshake (`HTTP 403`), so it gets no connection. A client of this server that is refused is
accepted and then closed with a code it can read: `4401` (reason `unauthorized`) without a
valid token, `4403` with the refusal as the reason (cut to 123 bytes) for a mode or `cwd` the
server does not allow. An unknown `resume` task sends `{"type": "error", …}` and closes.

**POST** `/stream-json`: the same, one way. The request body is JSON lines in; the response is
JSON lines out (`application/x-ndjson`) until the work is done. Nothing can answer a
permission prompt on a POST, so anything that would ask is refused and the turn goes on. A
mode or `cwd` the server does not allow is a `403` before anything runs.

```bash
printf '%s\n' '{"type":"user","message":{"role":"user","content":"what is in this folder?"}}' |
  curl -sN -X POST "http://127.0.0.1:8642/stream-json?permission_mode=default" \
       -H "Authorization: Bearer $NOETA_SERVER_TOKEN" --data-binary @-
```

The control requests and message kinds are the ones `noeta -p` handles
([The noeta command](./cli.md#headless-noeta-p)).

## The web app's files

With `noeta serve --web DIR` (the built web app, `web/dist`), or without `--web` when the
installed package carries the web app (a wheel from PyPI does), every path not listed above
serves that directory's files without a token: they hold none. They go out with a content
security policy (the page's scripts, styles and requests are its own server's; images also
`data:` and `blob:`), `X-Frame-Options: DENY` and `X-Content-Type-Options: nosniff`. The page
asks for the token once, or takes it from the address's `#token=…`, which never reaches the
server.

## Examples

```bash
export NOETA_SERVER_TOKEN=$(cat ~/.noeta/server-token)   # on the server's machine
H="Authorization: Bearer $NOETA_SERVER_TOKEN"
S=http://127.0.0.1:8642

# start a task
curl -s -X POST $S/tasks -H "$H" -H 'content-type: application/json' \
  -d '{"agent":"main","input":"list the TODOs","config":{"permission_mode":"default"}}'
# → {"task_id":"40efba61…"}

# follow it
curl -sN $S/tasks/40efba61…/watch -H "$H"

# answer what it waits on
curl -s $S/tasks/40efba61…/pending -H "$H"
curl -s -X POST $S/tasks/40efba61…/answers -H "$H" -H 'content-type: application/json' \
  -d '{"request_id":"toolu_…","decision":"allow"}'

# say more, then stop the turn
curl -s -X POST $S/tasks/40efba61…/messages -H "$H" -H 'content-type: application/json' \
  -d '{"input":"now fix the first one"}'
curl -s -X POST $S/tasks/40efba61…/cancel -H "$H" -H 'content-type: application/json' \
  -d '{"turn_only":true}'
```

The same from Python:

```python
import os
import noeta
client = noeta.connect("http://127.0.0.1:8642", token=os.environ["NOETA_SERVER_TOKEN"])
task = await client.create("main", "list the TODOs", config={"permission_mode": "default"})
async for rec in client.watch(task):
    print(rec.seq, type(rec.event).__name__)
```
