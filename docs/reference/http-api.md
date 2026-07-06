# HTTP API reference (noeta-agent backend)

The routes served by `python -m noeta.agent`. This is the contract between
the backend and the bundled web app — a **local acceptance surface, not a
stable versioned public API**. Request bodies never carry provider /
base-URL / credentials; the host-side `NOETA_AGENT_*` config is authoritative
(see the [coding-agent manual](noeta-agent.md)).

Design rule: command endpoints return **`202` + a small ack only**; every
visible change is observed through the SSE stream (single source of truth).
All bodies are JSON.

## Task protocol

Source: `apps/noeta-agent/noeta/agent/backend/task_protocol.py:208-218`.

### `GET /stream` — the SSE event stream (`task_protocol.py:208`)

Query: `task=<root_task_id>` (**required**; 400 without it). One multiplexed
stream per root conversation: canonical `EventEnvelope`s (the
`envelope_to_dict` wire shape) for the root and its subtasks, with the
envelope `seq` doubling as the SSE id — send `Last-Event-ID` to resume from a
cursor.

The same stream carries **ephemeral token-delta frames** while a streaming
LLM call is in flight: named `event: delta` frames whose data is
`{"task_id", "call_id", "kind": "text"|"thinking", "text", "index"}`. Delta
frames carry **no SSE id** — the resume cursor never moves for them, a
reconnect replays envelopes only, and a slow consumer may have deltas
dropped. They are a live preview: the durable truth is always the
`MessagesAppended` envelope that follows (`stream.py:79`, ADR
`token-streaming-projection`). Consume them via
`EventSource.addEventListener("delta", …)`; `onmessage` sees only envelopes.

### `POST /tasks` — create a conversation (`task_protocol.py:209`)

Body: `goal` (string), `agent` (optional preset/agent name), `images`
(optional attachments; bad MIME / base64 / >5 MB ⇒ 400), `workspace`
(optional workspace id or path; unknown ⇒ 400), `permission_mode`,
`enabled_mcp` (list of MCP aliases), `model`, `effort` (all optional,
per-turn). Response: `202` `{"task_id": "..."}`.

### Command verbs (`task_protocol.py:210-217`)

All respond `202` `{"task_id": "<id>"}`; progress rides the stream.

| Method & path | Body | Purpose |
| --- | --- | --- |
| `POST /tasks/{id}/messages` (`:210`) | `goal` + optional `images` / `permission_mode` / `enabled_mcp` / `model` / `effort` | append a follow-up user turn |
| `POST /tasks/{id}/approve` (`:211`) | `call_id`, optional `reason` | approve a gated tool call |
| `POST /tasks/{id}/deny` (`:212`) | `call_id`, optional `reason` | deny a gated tool call |
| `POST /tasks/{id}/answer` (`:213`) | `question_id`, `answers` (object) | answer a structured question |
| `POST /tasks/{id}/events` (`:214`) | `event_kind` (string), optional `payload` (any JSON value) | deliver an external event to a `wait_external` suspend |
| `POST /tasks/{id}/cancel` (`:215`) | optional `reason` (default `"cancelled"`), `cascade` (default `false`) | cancel; `cascade` also cancels subtasks |
| `POST /tasks/{id}/close` (`:216`) | optional `reason` | close / archive |
| `POST /tasks/{id}/reopen` (`:217`) | optional `reason` | reopen a closed conversation |

`POST /tasks/{id}/events` wakes a task suspended by the `wait_external`
Decision branch; matching is exact on `event_kind`. The optional `payload`
is recorded on the resumed turn as an `origin="system"` message (it never
rides the wake event). A task not waiting on that `event_kind` — including a
repeat delivery after the wake was consumed — answers `409` with code
`not_resumable`, the same contract as a repeat `answer`.

### `DELETE /tasks/{id}` — hard-delete (`task_protocol.py:218`)

Purges the task and its subtask tree from storage (content blobs are shared
and left for offline GC). Synchronous, unlike the command verbs: `200` with
`{"ok": true, "task_id", "deleted": [...]}`, `409` when a task in the tree is
actively running, `404` when the root is unknown.

## Read views

Source: `apps/noeta-agent/noeta/agent/backend/read_views.py:212-213`.

### `GET /capabilities` (`read_views.py:212`)

The composer's selectable surface: `{"command_in": true, "chat": true,
"agents": [...], "models": [...], "model_capabilities": {...},
"permission_modes": [...], "effort_modes": [...], "mcp_servers": [...],
"workspaces": [...], "skills": [], "slash_commands": [], "providers": {},
"default_provider": ""}` (`read_views.py:81-110`; the last four stay empty /
unwired in the current backend).

### `GET /tasks` (`read_views.py:213`)

The session list — **root** conversations only (a subtask rides its parent's
stream), most-recent first. Each row: `task_id`, `status` (`created` /
`running` / `waiting` / `completed` / `failed` / `cancelled`), `closed`,
`title` (from the genesis goal), `agent_name`, `parent_task_id`,
`workspace_dir`, `workspace_name`, `last_seq` (`read_views.py:179-207`).

## Resources

Source: `apps/noeta-agent/noeta/agent/backend/resource_services.py:168-170`.

| Route | Query | Response | Errors |
| --- | --- | --- | --- |
| `GET /content/{hash}` (`:168`) | — | raw bytes, sniffed media type | 404 unknown hash |
| `GET /files` (`:169`) | optional `task` (serve that session's workspace) | `{"root", "tree": [...]}` — nested `{name, path, type, size?, children?}` | — |
| `GET /file` (`:170`) | `path` (**required**), optional `task` | `{"path", "size", "truncated", "content"}` (UTF-8, capped) | 400 missing `path`; 404 not found / escapes the workspace |

## Workspaces

Source: `apps/noeta-agent/noeta/agent/backend/workspace_service.py:84-86`.
Registry CRUD only — per-session binding happens via `POST /tasks`'
`workspace` field. Absent registry ⇒ `503`.

| Route | Body / response | Errors |
| --- | --- | --- |
| `GET /workspaces` (`:84`) | `{"workspaces": [...]}`, default first | — |
| `POST /workspaces` (`:85`) | body `path` (required), `name` → `201` + the entry | 400 bad path/name |
| `DELETE /workspaces/{id}` (`:86`) | `{"ok": true, "id"}` — removes the registry entry, **not** the directory | 404 unknown / default |

## MCP connectors

Source: `apps/noeta-agent/noeta/agent/backend/mcp_service.py:226-233`.
CRUD + discovery over the host's connector store; the live per-turn
connection is separate (aliases are enabled per turn via `enabled_mcp`).
Credentials (header/env values) are stored host-side and never echoed back.
Absent registry ⇒ `503`; connect/handshake failure on discovery ⇒ `502`.

| Route | Body / response | Errors |
| --- | --- | --- |
| `GET /mcp/servers` (`:230`) | `{"servers": [...]}` (credential-scrubbed) | — |
| `POST /mcp/servers` (`:231`) | `alias` + `type` (`"http"` ⇒ `url`, `headers`; `"stdio"` ⇒ `command`, `args`, `env`) + optional `tools` subset → `201` + entry | 400 bad config |
| `PUT /mcp/servers/{alias}` (`:232`) | merge-edit; omitted fields kept | 400 / 404 |
| `DELETE /mcp/servers/{alias}` (`:233`) | `{"deleted": alias}` | 404 |
| `GET /mcp/servers/{alias}/tools` (`:226`) | `{"tools": [...]}` — the full tool menu | 404 / 502 |
| `PUT /mcp/servers/{alias}/tools` (`:227`) | body `tools`: list or `null` (= all) → the entry | 400 / 404 |
| `GET /mcp/servers/{alias}/prompts` (`:228`) | `{"prompts": [...]}` | 404 / 502 |
| `GET /mcp/servers/{alias}/resources` (`:229`) | `{"resources": [...]}` | 404 / 502 |

## Process routes

Source: `apps/noeta-agent/noeta/agent/backend/app.py`.

| Route | Behavior |
| --- | --- |
| `GET /health` | `{"status": "ok", "backend": "new"}` (`app.py:302`) |
| `GET /` | 302 redirect to `/chat` (`app.py:273`) |
| `GET /chat`, `/trace`, `/assets/*`, `/src/*` | bundled SPA assets; 404 with no frontend build (`app.py:262-292`) |
| `ANY /preview/<token>/...` | single-port HTML-app preview gateway; falls through when no gateway is mounted (`app.py:224-252`) |

Anything else: `404` `{"error": "not found", "path": ...}`.

> **Note.** Earlier versions of the docs described routes that do not exist
> in this backend (`GET /tasks/{id}`, `GET /events`, `POST /tasks/{id}/goals`,
> `POST /tasks/{id}/resume`, `/mcp-servers`, …). The table above is generated
> against the code; treat it as the only authoritative list.

## See also

- [Coding-agent manual](noeta-agent.md) — starting the server, env config
- [SDK reference](sdk.md) — the in-process equivalent of these verbs
