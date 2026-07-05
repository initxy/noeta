# Noeta coding agent (`python -m noeta.agent`)

A workspace-scoped coding agent. It reads, edits, runs shell commands, and
holds multi-turn sessions over one directory, recording every step in a
durable EventLog so the run's state can be re-derived offline by folding
that log. Point it at a directory, start the server, and drive it through
the bundled web UI or over HTTP.

## Starting the server

The **only** entry point is `python -m noeta.agent` — zero positional args,
all configuration through environment variables or a JSON config file. It
boots an HTTP/SSE chat server plus a bundled web SPA at `<url>/chat` and
blocks until SIGINT/SIGTERM. There is no `noeta` console script and no
operator CLI.

```bash
NOETA_AGENT_WORKSPACE=./my-project \
NOETA_AGENT_PROVIDER=openai \
NOETA_AGENT_MODEL=gpt-5.5 \
NOETA_AGENT_BASE_URL=https://api.openai.com/v1 \
NOETA_AGENT_API_KEY=sk-… \
NOETA_AGENT_STORAGE=./session.sqlite \
python -m noeta.agent
# → noeta.agent serving at http://127.0.0.1:<port>/ ; chat at <url>/chat
```

Launcher and env parsing live in
`apps/noeta-agent/noeta/agent/__main__.py` and `RunnerConfig.from_env` in
`apps/noeta-agent/noeta/agent/host/runner_cli.py` — the authoritative list
of `NOETA_AGENT_*` knobs.

For library use (no server), the SDK exports `Options`, `query`, `Client`,
`compile_options` from `noeta.sdk`
(`packages/noeta-sdk/noeta/sdk/__init__.py`); the official four-agent
recipe is `noeta.presets.main_options()` / `official_specs()`
(`packages/noeta-runtime/noeta/presets/__init__.py`).

## Environment configuration

| Variable | Default | What it controls |
| --- | --- | --- |
| `NOETA_AGENT_WORKSPACE` | `.` | Directory the agent operates on |
| `NOETA_AGENT_PROVIDER` | `stub` | LLM adapter: `stub`, `openai`, `anthropic`, `openai-responses` |
| `NOETA_AGENT_MODEL` | provider default | Model name passed to the provider |
| `NOETA_AGENT_API_KEY` | — | Provider API key |
| `NOETA_AGENT_BASE_URL` | provider default | Override base URL (e.g. for OpenAI-compatible endpoints) |
| `NOETA_AGENT_STORAGE` | `:memory:` | Path to durable SQLite file; `:memory:` is dev/test only |
| `NOETA_AGENT_WRITE_MODE` | `dry_run` | `dry_run` (propose diffs only) or `apply` (perform real writes) |
| `NOETA_AGENT_SHELL_MODE` | `allowlist` | `allowlist` (argv-structural allowlist) or `off` |
| `NOETA_AGENT_HOST` | `127.0.0.1` | Bind address |
| `NOETA_AGENT_PORT` | `0` (OS-assigned) | Listen port |
| `NOETA_AGENT_CONFIG` | — | Path to JSON config file (alternative to individual env vars) |

`NOETA_AGENT_PROVIDER=stub` (the default) is a fully offline, deterministic
LLM double — no API key or network needed. Use it to verify install,
storage, and wiring on a fresh checkout.

## Built-in tools

Tool names are provider-safe `snake_case` and are the exact strings the
model calls. Source of truth: `noeta.tools.fs.build_fs_tools()` plus the
`app/` and `web/` packs in `packages/noeta-sdk/noeta/tools/`.

| Tool | Risk | What it does |
| --- | --- | --- |
| `read` | low | Read a workspace file (UTF-8), optionally sliced by `offset` / `limit` |
| `glob` | low | Match a workspace-relative glob, return matching paths |
| `grep` | low | Regex (`re`) content search across the workspace |
| `edit` | high | Replace an exact, unique `old` substring in an existing file |
| `write` | high | Write a file (create, or overwrite one previously read) |
| `apply_patch` | high | Apply a batch of edits atomically — all succeed or none |
| `shell_run` | high | Run a shell command in the workspace (mode-gated) |
| `shell_poll` | low | Check status / output of a background shell job |
| `shell_kill` | high | Stop a background shell job you started |
| `run_skill_script` | high | Run an active skill's bundled script via an allowlisted interpreter |
| `open_app` | low | Render a workspace HTML app in the web "App" panel |
| `webfetch` | low | Fetch a public web page, rendered to Markdown |
| `web_search` | low | Run a web search, return ranked hits (key-gated: `NOETA_WEB_SEARCH_API_KEY`) |

There is no separate `read_file` / `write_file` / `replace_text` /
`list_dir` / `git_status` / `git_diff` tool — those old names were renamed
(`read` / `write` / `edit`) or removed; `git status` / `git diff` are now
allowlist rules inside `shell_run`.

Remote MCP tools appear dynamically as `mcp__<alias>__<tool>`
(`noeta/tools/mcp/tool.py`).

## Agent presets

`noeta.presets` ships the official quartet, aligned with Claude Code's
roster (`packages/noeta-runtime/noeta/presets/__init__.py`). The agent is
chosen **per task** in the `POST /tasks` body (`{"goal": …, "agent": …}`),
not at process launch; custom agents go through the flat `Options.agents`
dict.

| Agent | Role |
| --- | --- |
| `main` | Default coding agent: full built-in tool surface, spawns the three subagents, all capabilities |
| `general-purpose` | Self-contained coding worker: full read/write/edit/shell set, no delegation |
| `explore` | Read-only scout: glob/grep/read + read-only shell, fans out to report facts, never edits |
| `plan` | Read-only architect: reads the code and returns an ordered implementation plan, never writes |

The runner filters the tool pack by the agent's `allowed_tools` **before**
the Engine sees it, and the `PermissionGuard` uses the same allow-list, so
a forbidden tool is provably unreachable.

## Skills

A skill pack is `<workspace>/.noeta/skills/<name>/SKILL.md` (plus a global
`~/.noeta/skills` tier) — YAML frontmatter (`name`, `description`,
optional `version` / `priority`) + a Markdown body, with any sibling files
bundled as on-demand resources.

Activation is **two-stage** and model-driven:

1. At startup the index renders a menu (name + one-line description) into
   the `skill` control tool's schema.
2. When the model calls `skill: <name>`, the body plus an absolute
   base-directory line are folded into the next turn's semi-stable
   context, and the model `read`s bundled resources on demand (no eager
   inlining).

```text
# model picks from the skill menu, then calls the control tool:
skill: pdf-extract
# → next turn carries SKILL.md body + "Base directory: <abs path>"
# → model reads resources via `read`.
```

Indexer code: `packages/noeta-sdk/noeta/context/skills/`.

## Write & shell safety

Writes are **dry-run by default**. `NOETA_AGENT_WRITE_MODE` (`dry_run` vs
`apply`) decides whether `edit` / `write` / `apply_patch` change bytes or
only emit a proposed unified-diff artifact. This is host config, not a
request field — a client cannot escalate to `apply`. `apply_patch` is the
all-or-nothing path (validate every edit, then write; in-process rollback
on apply error); sequenced `edit` / `write` calls are non-atomic.

`shell_run` is gated by `NOETA_AGENT_SHELL_MODE`: `allowlist` (default —
argv-only structural allowlist for `git status` / `git diff` / `pytest` /
`uv run pytest` / `npm test` / `pnpm test`; shell metacharacters rejected
before tokenisation) or `off`.

Every path goes through `WorkspaceRoot` (realpath + containment,
symlink-safe; checked before any IO), so absolute / `..` /
out-of-tree-symlink escapes fail before reading or writing. Approval and
write/shell gating are expressed as neutral control mechanisms — see
[Guard vs Observer](../concepts/guard-observer.md).

## MCP & hooks

**MCP** — remote or stdio connectors are registered in
`~/.noeta/mcp_servers.json` (alias → transport/url/credentials;
credentials never travel in request bodies) and enabled per session via
the `enabled_mcp` field. Their tools show up as `mcp__<alias>__<tool>`.

**Hooks** — the only extension roles are **Guard** (veto or mutate at
`before_tool_call` / `before_spawn_subtask` / `before_finish`) and
**Observer** (read-only subscriber to committed events). There is no
Mutator role — see [Guard vs Observer](../concepts/guard-observer.md).

## Sub-agent fan-out

`main` can spawn the three subagents (`general-purpose`, `explore`,
`plan`) in parallel. Each spawned subtask is an independent event-sourced
task with its own EventLog; the result is recorded into the parent's log
via a `SubtaskCompleted` wake, so the whole tree folds back into state.
See [Wake & resume](../concepts/wake-resume.md).

## See also

- [HTTP API reference](http-api.md) — every route the backend serves
- [SDK reference](sdk.md) — the programmatic equivalent
- [How-to: use the coding agent](../how-to/use-the-coding-agent.md)
- [Configure a provider](../how-to/configure-provider.md)
