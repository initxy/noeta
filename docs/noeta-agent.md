# Noeta coding agent (`python -m noeta.agent`)

A workspace-scoped coding agent: it reads, edits, runs shell commands, and
holds multi-turn sessions over one directory, recording every step in a
durable EventLog so the run's state can be re-derived offline by folding
that log. This doc is a
**thin map for agents** — it names the current surface and points at the
authoritative source (code or `docs/adr/`). It does **not** copy
schemas or prose; when in doubt, read the cited file.

## Entry point

The **only** entry is `python -m noeta.agent` (zero positional args; all
config via `NOETA_AGENT_*` env or a `NOETA_AGENT_CONFIG` JSON file). It boots
an HTTP/SSE chat server + bundled web SPA at `<url>/chat` and blocks until
SIGINT/SIGTERM. There is no `noeta` console script and no operator CLI.

```bash
NOETA_AGENT_WORKSPACE=./my-project \
NOETA_AGENT_PROVIDER=openai NOETA_AGENT_MODEL=gpt-5.5 NOETA_AGENT_API_KEY=… NOETA_AGENT_BASE_URL=… \
python -m noeta.agent
# → noeta.agent serving at http://127.0.0.1:<port>/ ; chat composer at <url>/chat
```

* Launcher + env parsing: `apps/noeta-agent/noeta/agent/__main__.py` and
  `BackendConfig.from_env` in `apps/noeta-agent/noeta/agent/backend/lifecycle.py`
  (the authoritative list of `NOETA_AGENT_*` knobs — workspace, port, host,
  provider/model/key/base_url, write mode, shell mode, MCP/workspace/session
  registries, etc.). `NOETA_AGENT_PROVIDER` defaults to the offline `stub`
  double; `openai` / `anthropic` / `openai-responses` are the real adapters.
* Library use (no server): `noeta.sdk` exports `Options`, `query`,
  `Client`, `compile_options` (`packages/noeta-sdk/noeta/sdk/__init__.py`);
  the official four-agent recipe is `noeta.presets.main_options()` /
  `official_specs()` (`packages/noeta-runtime/noeta/presets/__init__.py`).

## Tool surface

Built-in tools are assembled in `packages/noeta-runtime/noeta/tools/`. Names are
provider-safe snake_case and are the strings the model calls. Source of
truth: `noeta/tools/fs/__init__.py` (`build_fs_tools`) plus the `app/` and
`web/` packs.

| Tool | risk | What it does | Source |
| --- | --- | --- | --- |
| `read` | low | Read a workspace file (utf-8), optionally sliced by line `offset`/`limit`. | `noeta/tools/fs` |
| `glob` | low | Match a workspace-relative glob and return matching paths. | `noeta/tools/fs` |
| `grep` | low | Regex (Python `re`) content search across the workspace. | `noeta/tools/fs` |
| `edit` | high | Replace an exact, unique `old` substring in an existing file. | `noeta/tools/fs` |
| `write` | high | Write a file (create, or overwrite one you've read). | `noeta/tools/fs` |
| `apply_patch` | high | Apply a small batch of edits atomically — all succeed or none. | `noeta/tools/fs` |
| `shell_run` | high | Run a shell command in the workspace (mode-gated, see below). | `noeta/tools/fs` |
| `shell_poll` | low | Check status/output of a background shell job. | `noeta/tools/fs` |
| `shell_kill` | high | Stop a background shell job you started. | `noeta/tools/fs` |
| `run_skill_script` | high | Run an active skill's bundled script via an allowlisted interpreter. | `noeta/tools/fs` |
| `open_app` | low | Render a workspace HTML app in the web "App" panel (single-port preview gateway). | `noeta/tools/app` |
| `webfetch` | low | Fetch a public web page over HTTP(S), rendered to Markdown. | `noeta/tools/web` |
| `web_search` | low | Run a web search and return ranked hits as Markdown (key-gated: present only when `NOETA_WEB_SEARCH_API_KEY` is set). | `noeta/tools/web` |

There is no separate `read_file` / `write_file` / `replace_text` /
`list_dir` / `git_status` / `git_diff` tool — those old names were renamed
(`read`/`write`/`edit`) or removed (`list_dir`); `git status`/`git diff`
are now just allowlist rules inside `shell_run`. Remote MCP tools appear
dynamically as `mcp__<alias>__<tool>` (`noeta/tools/mcp/tool.py`).

## Agent presets

`noeta.presets` ships the official quartet aligned with Claude Code's roster
(`packages/noeta-runtime/noeta/presets/__init__.py`). The agent is chosen **per
task** in the `POST /tasks` body (`{"goal": …, "agent": …}`), not at process
launch; custom agents go through the flat `Options.agents` dict.

| Agent | Role |
| --- | --- |
| `main` | Default coding agent: full built-in tool surface + can spawn the three subagents + all capabilities. |
| `general-purpose` | Self-contained coding worker: full read/write/edit/shell set, no delegation. |
| `explore` | Read-only scout: glob/grep/read + read-only shell, fans out to report facts, never edits. |
| `plan` | Read-only architect: reads the code and returns an ordered implementation plan, never writes. |

The runner filters the tool pack by the agent's `allowed_tools` **before**
the Engine sees it, and the `PermissionGuard` uses the same allow-list, so a
forbidden tool is provably unreachable. See
[ADR: Library-SDK architecture](adr/library-sdk-architecture.md) (Options
creation surface) and [ADR: Tool and agent catalog](adr/tool-and-agent-catalog.md).

## Skills

A skill pack is `<workspace>/.noeta/skills/<name>/SKILL.md` (plus a global
`~/.noeta/skills` tier) — YAML frontmatter (`name`, `description`, optional
`version`/`priority`) + a Markdown body, with any sibling files bundled as
on-demand resources. Activation is **two-stage** and model-driven: at
startup the index renders a menu (name + one-line description) into the
`skill` control tool's schema; when the model calls `skill: <name>` the body
plus an absolute base-directory line are folded into the next turn's
semi-stable context, and the model `read`s bundled resources on demand
(no eager inlining).

```text
# model picks from the skill menu, then calls the control tool:
skill: pdf-extract
# → next turn carries SKILL.md body + "Base directory: <abs path>"; model reads resources via `read`.
```

Authoritative: [ADR: Model-driven skill invocation](adr/model-driven-skill-invocation.md)
and [ADR: Skill resource on-demand](adr/skill-resource-on-demand.md); indexer
code in `packages/noeta-runtime/noeta/context/skills/`.

## Write & shell safety

Writes are **dry-run by default**. The `NOETA_AGENT_WRITE_MODE` host policy
(`dry_run` (default) vs `apply`) decides whether `edit`/`write`/`apply_patch`
change bytes or only emit a proposed unified-diff artifact — it is host
config, not a request field. `apply_patch` is the all-or-nothing path
(validate every edit, then write; in-process rollback on apply error);
sequenced `edit`/`write` calls are non-atomic.

`shell_run` is gated by `NOETA_AGENT_SHELL_MODE`: `allowlist` (default —
argv-only structural allowlist for `git status`/`git diff`/`pytest`/
`uv run pytest`/`npm test`/`pnpm test`, shell-metacharacters rejected before
tokenisation) or `off`. This is path-containment + an allowlist, **not** a
process sandbox — `shell_run` spawns external programs in the trusted
workspace.

Every path goes through `WorkspaceRoot` (realpath + containment, symlink-
safe; checked before any IO), so absolute / `..` / out-of-tree-symlink
escapes fail before reading or writing. Approval and write/shell gating are
expressed as neutral control mechanisms — see
[ADR: Control tools neutral mechanism](adr/control-tools-neutral-mechanism.md)
and [ADR: Shell permission and background](adr/shell-permission-and-background.md).

## HTTP surface

`python -m noeta.agent` serves an HTTP/SSE backend for the bundled web UI.
This is an **acceptance surface for the local UI, not a stable versioned
public API**: request bodies never accept provider / base_url / credentials
(the host-side `NOETA_AGENT_*` config is authoritative).

For the full route table see [Reference › HTTP API](reference/http-api.md).
Routes are registered in these modules under
`apps/noeta-agent/noeta/agent/backend/`:

- `task_protocol.py` — SSE stream (`GET /stream?task=<id>`) + task commands
- `resource_services.py` — content / files / file (data plane)
- `read_views.py` — capabilities + session list
- `mcp_service.py` — MCP connector management (`/mcp/servers/*`)
- `workspace_service.py` — workspace (project) management
- `app.py` — routing root, static assets, preview gateway, `/health`

Config parsing: `backend/lifecycle.py` → `BackendConfig.from_env`.
See also: [Configuration](reference/configuration.md).

## MCP & hooks

* **MCP** — remote/stdio connectors are registered in
  `~/.noeta/mcp_servers.json` (alias → transport/url/credentials; credentials
  never travel in request bodies) and enabled per session; their tools show
  up as `mcp__<alias>__<tool>`. Override the registry path with
  `NOETA_AGENT_MCP_FILE`. See [ADR: MCP connectors](adr/mcp-connectors.md).
* **Hooks** — the only extension roles are **Guard** (veto at
  `before_tool_call` / `before_spawn_subtask` / `before_finish`) and
  **Observer** (read-only). There is no Mutator role. See
  [ADR: Guard-observer hooks](adr/guard-observer-hooks.md).

## Sub-agent fan-out

`main` can spawn the subagents in parallel; the result is the subagent's
return value, recorded into the EventLog so the whole tree folds back into
state — see [ADR: Subtask fan-out and durable wake](adr/subtask-fanout-and-durable-wake.md).
