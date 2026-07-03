# HTTP API

`python -m noeta.agent` serves an HTTP/SSE backend for the bundled web UI.
This is an **acceptance surface for the local UI, not a stable versioned
public API**: request bodies never accept provider / base_url / credentials
(the host-side `NOETA_AGENT_*` config is authoritative).

All routes live under the base URL (default `http://127.0.0.1:8765/`).

## SSE stream

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/stream?task=<id>` | Multiplexed SSE event stream for a task. `Last-Event-ID` header resumes from a sequence. Pages filter by task client-side. |

The stream carries canonical `EventEnvelope` records as JSON, addressed by
`taskId`. The envelope `seq` doubles as the SSE id so `Last-Event-ID`
can resume mid-stream.

## Task commands

All command endpoints return `202 {"task_id": "<id>"}` (ack only); visible
changes arrive through the SSE stream (single source of truth).

| Method | Path | Purpose |
| --- | --- | --- |
| `POST` | `/tasks` | Create a task. Body: `goal` (string), `agent` (string, optional), `model` (string, optional per-turn selector), `effort` (string, optional), `permission_mode` (string, optional), `enabled_mcp` (list, optional), `workspace` (string, optional), `images` (list, optional). |
| `POST` | `/tasks/{id}/messages` | Append a follow-up goal to an existing task. Body same fields as create (except `agent`). |
| `POST` | `/tasks/{id}/approve` | Approve a gated tool call. Body: `call_id`, `reason`. |
| `POST` | `/tasks/{id}/deny` | Deny a gated tool call. Body: `call_id`, `reason`. |
| `POST` | `/tasks/{id}/answer` | Answer a model-asked question. Body: `question_id`, `answers` (dict). |
| `POST` | `/tasks/{id}/cancel` | Cancel a task. Body: `reason` (default `"cancelled"`), `cascade` (bool, cancel subtasks). |
| `POST` | `/tasks/{id}/close` | Close a conversation. Body: `reason` (optional). |
| `POST` | `/tasks/{id}/reopen` | Reopen a closed conversation. Body: `reason` (optional). |
| `DELETE` | `/tasks/{id}` | Hard-delete a session (task + subtask tree). Returns `200` with purged ids, `409` if running, `404` if unknown. |

## Read views

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/tasks` | Session list (root conversations only, most-recent first). Each row carries `task_id`, `status`, `closed`, `title`, `agent_name`, `workspace_dir`, `workspace_name`. |
| `GET` | `/capabilities` | The composer's selectable surface: `agents`, `models`, `model_capabilities`, `permission_modes`, `effort_modes`, `mcp_servers`, `workspaces`. |

## Resource services

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/content/{hash}` | Decoded content-ref body by hash. Media type is sniffed from magic bytes. |
| `GET` | `/files?task=<id>` | Workspace file tree for a task's workspace (sandboxed, read-only projection). |
| `GET` | `/file?task=<id>&path=<rel>` | Single file preview. Returns `{path, size, truncated, content}` (utf-8, max 1 MB). |

## Workspace management

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/workspaces` | List workspace (project) registry entries. |
| `POST` | `/workspaces` | Add a workspace. Body: `path`, `name` (optional). |
| `DELETE` | `/workspaces/{id}` | Remove a workspace by id. |

## MCP server management

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/mcp/servers` | List registered MCP server connectors. |
| `POST` | `/mcp/servers` | Register a new MCP server. |
| `PUT` | `/mcp/servers/{alias}` | Update an MCP server's config. |
| `DELETE` | `/mcp/servers/{alias}` | Remove an MCP server. |
| `GET` | `/mcp/servers/{alias}/tools` | List tools offered by an MCP server. |
| `PUT` | `/mcp/servers/{alias}/tools` | Set tool allowlist for an MCP server. |
| `GET` | `/mcp/servers/{alias}/prompts` | List prompts offered by an MCP server. |
| `GET` | `/mcp/servers/{alias}/resources` | List resources offered by an MCP server. |

## Static assets & UI

These are prefix-routed (not in the API router), served from the bundled
frontend build:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/` | Redirect to `/chat`. |
| `GET` | `/chat` | Chat composer SPA. |
| `GET` | `/trace` | Per-task trace view SPA. |
| `GET` | `/assets/*` | Bundled web assets (JS, CSS, images). |
| `GET` | `/preview/*` | Single-port HTML app preview gateway (sandboxed iframe). |
| `GET` | `/health` | Liveness probe → `{"status": "ok", "backend": "new"}`. |

## Error codes

Engine errors carry a stable `code` token mapped to HTTP status:

| Error code | HTTP status | Meaning |
| --- | --- | --- |
| `model_selector_rejected` | 400 | Per-turn model selector rejected (not in allowlist). |
| `provider_selector_rejected` | 400 | Provider selector rejected. |
| `not_resumable` | 409 | Task is not in a resumable state. |
| `unsupported_subtask_suspend` | 409 | Subtask cannot suspend in this configuration. |
| `task_already_terminal` | 409 | Task has already reached a terminal state. |
| *(unexpected)* | 500 | Internal error (never leaks stack traces). |

## Source

Route registration is split across these modules in
`apps/noeta-agent/noeta/agent/backend/`:

- `task_protocol.py` — SSE stream + task command endpoints
- `resource_services.py` — content / files / file (data plane)
- `read_views.py` — capabilities + session list
- `mcp_service.py` — MCP connector management
- `workspace_service.py` — workspace (project) management
- `app.py` — routing root, static assets, preview gateway, `/health`

Config parsing: `lifecycle.py` → `BackendConfig.from_env`.
See also: [Configuration](configuration.md).
