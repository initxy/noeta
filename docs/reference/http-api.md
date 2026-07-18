# HTTP API reference (noeta-agent platform)

The versioned REST + SSE surface served by `python -m noeta.agent`. Every
route below is prefixed **`/api/v1`** (omitted from the tables). All bodies
are JSON. Source of truth: the routers in
`apps/noeta-agent/noeta/agent/api/` wired by `noeta/agent/main.py`.

Conventions:

- **Auth** — every endpoint requires the signed session cookie
  (`noeta_session`) set by login, except the public trio: `GET /health`,
  `GET /auth/config`, and `POST /auth/dev-login` itself.
- **Visibility = membership.** A session, space resource, or channel you are
  not entitled to see returns **404** (existence is hidden), not 403. 403 is
  reserved for "you can see it but may not do that" (e.g. member vs owner).
- **Command endpoints ack with 202** and a small body; every visible change
  arrives on the session's SSE stream.
- **Credentials never round-trip.** Connector headers/env values and gateway
  keys are stored server-side and scrubbed from every response.

## Auth

| Method & path | Purpose |
| --- | --- |
| `GET /auth/config` | Public login-page config: `dev_login_enabled` + provider-contributed fields (the `AuthProvider` seam). |
| `POST /auth/dev-login` | Body `{username}`. Sets the signed `noeta_session` cookie and upserts the user. 403 when dev-login is disabled (dynamic config). |
| `GET /auth/me` | The current user: `username`, `email`, `name`, `avatar`, `is_admin`. |
| `POST /auth/logout` | Clears the session cookie. |

## Misc

| Method & path | Purpose |
| --- | --- |
| `GET /health` | `{"ok": true, "provider": "mock"\|"openai"}` — no auth. |
| `GET /models` | The model menu from `models.json` (`id`, `label`, `default`, `efforts`, `default_effort`) + the effective provider. |
| `GET /capabilities` | Snapshot of the agent capability switches (memory / delegation / mcp / …). |
| `GET /content/{hash}` | Raw ContentStore bytes by SHA-256 hash (64 hex chars; a capability — you can only ask for hashes you have seen). Media type sniffed from magic bytes (PNG/JPEG/GIF/WebP/PDF), else `application/octet-stream`. Used to render composer image attachments back and by the admin trace view. |

## Sessions

Prefix `/sessions`. A session belongs to a space; visibility = space
membership.

| Method & path | Purpose |
| --- | --- |
| `GET /sessions?space_id=` | List the space's sessions. |
| `POST /sessions` | `201`. Body `{space_id, model?, template_id?, workflow_template_id?, params?}`. `template_id` starts the session from a prompt template; `workflow_template_id` starts a multi-node workflow session (the two are mutually exclusive). Model defaults to the space agent-config default, then the platform default. |
| `GET /sessions/{id}` | Session detail; workflow sessions carry a `workflow` view (node tab bar). |
| `DELETE /sessions/{id}` | Delete (creator or space owner only; members get 403). |
| `POST /sessions/{id}/messages` | `202`. Body `{content?, model?, effort?, task_id?, images?}` — text and/or composer image attachments (below). 409 while a turn is running or a question is pending; 422 for an unknown model or unsupported effort. |
| `POST /sessions/{id}/answer` | `202`. Body `{question_id, answers, task_id?}` answering a structured question. Each answer value is an object `{choice_id?, text?}` (at least one; freeform `text` only when the question allows it). 409 when no question is pending. |
| `POST /sessions/{id}/cancel` | Stop the running turn (optional `task_id` for a workflow node). |
| `POST /sessions/{id}/advance/preview` | Workflow sessions: generate the handoff into the next node (prefilled params + handoff summary + full handoff document). Idempotent; 409 when there is no next node or the previous node is still running. |
| `POST /sessions/{id}/advance/confirm` | `202`. Body `{node_index, params, summary?, handoff_doc?}` — start the next node; the handoff document is saved under the session workspace's `handoff/` directory. |
| `GET /sessions/{id}/events` | The per-session SSE stream (below). Query: `since_seq?`, `task_id?` (workflow node filter). |
| `GET /sessions/{id}/files` | The session workspace file listing (`{path, size, mtime}`). Empty when the sandbox is disabled (pure conversation mode has no file surface). |
| `GET /sessions/{id}/files/content?path=` | One workspace file (UTF-8, capped at 200 KB, `truncated` flag). 400 for a path escaping the workspace. |
| `GET /sessions/{id}/preview` | Sandbox live-preview discovery: `{token, port, panels}` for the Browser / Terminal / Code iframes, served from a **separate** preview origin (`http://<host>:<port>/sandbox-preview/<token>/…`). 404 when the session has no container. |

### Composer image attachments

`POST /sessions/{id}/messages` may carry
`images: [{media_type, data_base64}]`. Constraints (violations are **400**,
the turn is never seeded): MIME whitelist PNG / JPEG / GIF / WebP; valid
base64; ≤ 5 MB per image. Bytes go into the content-addressed store and ride
the user turn as `ImageBlock`s; the UI event exposes `{hash, media_type}`
and the frontend renders them back via `GET /content/{hash}` — image bytes
never travel the event stream.

### The SSE stream and `since_seq`

One stream per session. Frames follow the SSE format: durable events carry
`id: <seq>` (the envelope sequence number in the root task's EventLog);
synthetic frames carry no id. Source:
`apps/noeta-agent/noeta/agent/host/translator.py` — a deterministic pure
function from engine `EventEnvelope`s to UI events, shared by replay and
live, so the two paths cannot diverge.

**Replay is re-derivation.** On connect the backend replays the session's
EventLog through the translator, skipping events with `seq <= since_seq`,
then emits a synthetic `replay_done` and switches to live frames (deduped by
seq across the replay/live overlap). There is no stored UI projection; the
EventLog is the only durable truth. Reconnect by passing the last seen `id`
as `since_seq`.

Durable event vocabulary (translated, carry a seq):

| Event | Data | Meaning |
| --- | --- | --- |
| `user_message` | `{content, images?}` | A user turn (host-injected messages are filtered out). |
| `assistant_text` | `{text}` | Assistant body text (never clipped). |
| `thinking` | `{text}` | Reasoning summary (clipped to 2000 chars). |
| `tool_call` | `{call_id, tool_name, arguments, subtask_id?}` | Tool execution started. |
| `tool_result` | `{call_id, success, summary, output, subtask_id?}` | Tool finished (output clipped to 2000 chars). |
| `memory_op` | `{call_id, op, name}` | A memory tool call folded to a semantic marker (`write`/`read`/`search`/`archive`). |
| `skill_activated` | `{skill}` | The model activated a skill. |
| `todo_update` | `{todos: [{id, content, status}]}` | The todo list was replaced. |
| `subtask_started` / `subtask_finished` | `{subtask_id, agent_name?, goal?, status, summary}` | Subagent delegation lifecycle. |
| `question` | `{question_id, reason?, questions}` | A structured question; the session waits for `POST …/answer`. |
| `question_answered` | `{question_id}` | The answer was recorded. |
| `compaction` | `{replaced_count}` | Early history was compacted into a summary. |
| `llm_retry` | `{call_id}` | A transient LLM failure is retrying (clients clear the delta buffer for that call). |
| `turn_started` / `turn_finished` | `{}` / `{status}` | Turn boundaries; `status` ∈ `awaiting_input` / `completed` / `failed` / `cancelled`. |
| `error` | `{message}` | A failed turn's error (paired with `turn_finished`). |

Synthetic frames (no id, never replayed — except `replay_done`, which ends
every replay):

- `delta` — `{call_id, kind: "text"|"thinking", text, index}`: ephemeral
  token-streaming previews while an LLM call is in flight. Never persisted,
  never replayed; the durable record is always the appended message event
  that follows.
- `replay_done` — end-of-replay marker.
- `session_meta` — `{title}`: the async-generated session title.
- `workflow_update` — the workflow view changed (node started / finished).
- Subtask-stream `tool_call` / `tool_result` / `subtask_finished` frames are
  also synthetic (a subtask's seq counts independently of the root stream;
  replay reads only the root stream).

Raw, untranslated envelopes are **not** on this surface — they live on the
admin trace endpoint (below).

## Spaces

| Method & path | Purpose |
| --- | --- |
| `GET /spaces` | The spaces you belong to. |
| `POST /spaces` | `201`. Create a team space (you become owner). |
| `GET /spaces/{id}` | Space detail (member-only; 404 otherwise). |
| `PATCH /spaces/{id}` | Rename / edit (owner). Personal spaces: 400. |
| `DELETE /spaces/{id}` | Delete (owner). Personal spaces: 400. |
| `POST /spaces/{id}/members` | `201`. Add a member (owner). |
| `PATCH /spaces/{id}/members/{member}` | Change a member's role (`owner` / `member`); the last owner cannot be demoted. |
| `DELETE /spaces/{id}/members/{member}` | Remove a member (owner; the last owner cannot be removed). |
| `GET /users/search?q=` | Username search for the member picker. |
| `GET /spaces/{id}/agent-config` | The space's agent configuration (member-readable): `prompt` (persona, written into the session workspace `AGENT.md`), `memory_enabled`, `knowledge_sources` (null = all), `default_model`, `default_effort`. |
| `PUT /spaces/{id}/agent-config` | Update it (owner). |

## Skills

Two tiers, one `SKILL.md` format:

**Builtin skills** — platform-wide, admin console only (prefix `/skills`,
all gated by the admin allowlist; non-admins get 404):

| Method & path | Purpose |
| --- | --- |
| `GET /skills` | List builtin skills (with enabled flag). |
| `POST /skills` | Upload (zip or single `SKILL.md`); the frontmatter `name` names the directory; re-upload = reinstall. |
| `PATCH /skills/{name}` | Enable / disable platform-wide. |
| `DELETE /skills/{name}` | Remove the skill and its directory. |
| `GET /skills/{name}/preview` | Read-only content preview. |

**Space skills** — per space (prefix `/spaces/{space_id}/skills`; members
read, owner writes):

| Method & path | Purpose |
| --- | --- |
| `GET /spaces/{id}/skills` | List the space's skills. |
| `POST /spaces/{id}/skills` | `201`. Upload a skill into the space. |
| `PATCH /spaces/{id}/skills/{name}` | Enable / disable in this space. |
| `PUT /spaces/{id}/skills/{name}/group` | Set the skill's display group. |
| `DELETE /spaces/{id}/skills/{name}` | Remove. |
| `GET /spaces/{id}/skills/{name}/preview` | Read-only preview. |

## Knowledge

Prefix `/spaces/{space_id}/knowledge`. Members read; owner manages. Source
types: `git_repo` (clone URL + optional token) and `local_dir` (managed
directory).

| Method & path | Purpose |
| --- | --- |
| `GET …` | List the space's knowledge sources (with sync status). |
| `POST …` | `201`. Add a source. |
| `PATCH …/{source_id}` | Edit config. |
| `DELETE …/{source_id}` | Remove the source and its materialized copy. |
| `POST …/{source_id}/sync` | `202`. Trigger a sync (async; status via GET). |
| `GET …/{source_id}/sync` | Sync status / last error. |
| `POST …/resolve-paths` | Resolve citation footnote paths back to source locations. |

## MCP connectors

Prefix `/spaces/{space_id}/mcp`. Members read; owner manages. Transport
`http` (`url` + `headers`) or `stdio` (`command` + `args` + `env`).
Discovery is HTTP-only (a stdio connector's menus answer 400 — the server
does not spawn operator-configured subprocesses from a management GET);
connect/handshake failures map to 502. Enabled connectors are resolved into
the agent host **per turn**; their tools appear as `mcp__<alias>__<tool>`.

| Method & path | Purpose |
| --- | --- |
| `GET …/servers` | List connectors (credential-scrubbed). |
| `POST …/servers` | `201`. Create / replace a connector. |
| `PUT …/servers/{alias}` | Merge-edit (omitted fields kept). |
| `PATCH …/servers/{alias}` | Enable / disable. |
| `DELETE …/servers/{alias}` | Remove. |
| `GET …/servers/{alias}/tools` | The connector's full tool menu. |
| `PUT …/servers/{alias}/tools` | Set the enabled tool subset (`null` = all). |
| `GET …/servers/{alias}/prompts` | The connector's prompts. |
| `GET …/servers/{alias}/resources` | The connector's static resources. |

## Templates

Prefix `/spaces/{space_id}`. Members read and use; owner manages. Structural
errors are 422, name conflicts 409; placeholder-consistency warnings ride
along in `warnings` without blocking.

| Method & path | Purpose |
| --- | --- |
| `GET …/templates` · `POST` · `PATCH /{id}` · `DELETE /{id}` | Single-node prompt templates (prompt + typed params). |
| `GET …/workflow-templates` · `POST` · `PATCH /{id}` · `DELETE /{id}` | Multi-node workflow definitions (ordered template references). Deleting a template referenced by a workflow: 409. |

## Memories

Prefix `/spaces/{space_id}/memories` — the space's long-term agent memory
pool (one markdown file per memory). Members read **and** edit/archive
(their sessions write memories anyway); physical deletion is owner-only.

| Method & path | Purpose |
| --- | --- |
| `GET …` | List memories (name, type, summary). |
| `GET …/{name}` | Full text. |
| `PUT …/{name}` | Create / update. |
| `POST …/{name}/archive` | Retire into `archive/` (the routine path; traceable). |
| `DELETE …/{name}` | Hard delete (owner only). |

## Feedback

Member-level collection, owner-gated action:

| Method & path | Purpose |
| --- | --- |
| `POST /sessions/{id}/feedback` · `GET` | Rate a message (up/down + comment) / list the session's feedback. |
| `GET /spaces/{id}/feedback` | The space's feedback list. |
| `PUT/GET /spaces/{id}/feedback/{fid}/reference` | Attach / read the corrected reference artifact. |
| `POST /spaces/{id}/feedback/analyze` | `202`. Owner: run the analysis agent over collected feedback. |
| `GET /spaces/{id}/feedback/runs/latest` | Latest analysis-run status. |
| `GET /spaces/{id}/feedback/suggestions` | The suggestion list. |
| `POST …/suggestions/{sid}/adopt` · `/dismiss` | Owner: adopt (write into space memory, or apply a skill patch after a backup) or dismiss. |
| `GET …/suggestions/{sid}/skill-diff` | Preview a suggestion's skill patch. |
| `POST /spaces/{id}/feedback/report` · `GET …/reports` · `POST …/reports/{rid}/publish` | Aggregate selected suggestions into a **markdown report**, list reports, publish. |

## Channels & board (collaboration preview)

Team-space channels (`GET/POST /spaces/{id}/channels`, messages, topics,
`GET /channels/{id}/stream` SSE, unread watermark) and a three-column task
board (`GET /spaces/{id}/board`, card CRUD, card → session). Personal spaces
answer 422. This layer is a **preview surface**: the agent-side
collaboration tools that make it useful (`channel_read_*`, `board_*`) are
**feature-gated off by default** (`COLLAB_TOOLS_ENABLED=false`); turning the
collaboration layer on is a deployment decision.

## Admin

Prefix `/admin`; every route requires the `ADMIN_USERS` allowlist —
non-admins get 404. Read-only except dynamic-config writes and the builtin
skill surface.

| Method & path | Purpose |
| --- | --- |
| `GET /admin/stats` | Platform counts: users, spaces, sessions by status, knowledge sources, skills. |
| `GET /admin/users` · `/sessions` · `/spaces` | Cross-space listings. |
| `GET /admin/spaces/{id}/members` · `/knowledge` · `/skills` | Per-space drilldowns. |
| `GET /admin/sessions/{id}/raw-events` | **The raw trace surface**: untranslated `EventEnvelope`s for the root task and its full subtask tree. Cursor = the `{task_id: last_seq}` JSON echoed by the previous response (each task stream counts seq independently). This is the only place raw envelopes cross the wire. |
| `GET /admin/config` · `PUT /admin/config/{key}` | Dynamic config: the registered hot-reloadable keys (e.g. `dev_login_enabled`), DB override over the static setting. |

## See also

- [Platform reference](noeta-agent.md) — architecture, boot modes, admin console
- [Configuration](configuration.md) — every `.env` key
- [SDK reference](sdk.md) — the in-process library surface underneath
