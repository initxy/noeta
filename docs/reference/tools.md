# Built-in Tools

Noeta ships a set of built-in tools assembled from the filesystem pack,
the web pack, the app pack, and (conditionally) memory and MCP tools.
Tool names are provider-safe `snake_case` and are the exact strings the
model calls.

## Filesystem tools

Built by `build_fs_tools()` in `noeta.tools.fs`. Each tool carries a
`risk_level` used by the `PermissionGuard`.

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `read` | low | Read a workspace file (utf-8), optionally sliced by line `offset` / `limit`. | `noeta/tools/fs/read.py` |
| `glob` | low | Match a workspace-relative glob pattern and return matching paths. | `noeta/tools/fs/read.py` |
| `grep` | low | Regex (`re` module) content search across the workspace. | `noeta/tools/fs/read.py` |
| `edit` | high | Replace an exact, unique `old` substring in an existing file. Dry-run by default. | `noeta/tools/fs/edit.py` |
| `write` | high | Write a file (create, or overwrite one previously read). Dry-run by default. | `noeta/tools/fs/edit.py` |
| `apply_patch` | high | Apply a small batch of edits atomically — all succeed or none. Dry-run by default. | `noeta/tools/fs/patch.py` |
| `shell_run` | high | Run a shell command in the workspace. Mode-gated: `ALLOWLIST` by default, `OFF` removes the tool entirely. | `noeta/tools/fs/shell.py` |
| `shell_poll` | low | Check status / output of a background shell job. | `noeta/tools/fs/shell.py` |
| `shell_kill` | high | Stop a background shell job you started (SIGTERM → SIGKILL). | `noeta/tools/fs/shell.py` |
| `run_skill_script` | high | Run an active skill's bundled script via an allowlisted interpreter. | `noeta/tools/fs/skill_script.py` |

### Shell allowlist (default)

When `shell_mode = ALLOWLIST`, only these argv patterns pass:

- `git status` / `git diff`
- `pytest` / `uv run pytest`
- `npm test` / `pnpm test`

Shell metacharacters (`|`, `;`, `&&`, `>`, etc.) are rejected before
tokenization. This is **path-containment + an allowlist, not a process
sandbox** — `shell_run` spawns external programs in the trusted workspace.

## Web tools

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `webfetch` | low | Fetch a public web page over HTTP(S) and render it to Markdown. Always available. | `noeta/tools/web/fetch.py` |
| `web_search` | low | Run a web search and return ranked hits as Markdown. **Only mounted when `NOETA_WEB_SEARCH_API_KEY` is set.** | `noeta/tools/web/search.py` |

## App tools

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `open_app` | low | Render a workspace HTML app in the web "App" panel via the single-port preview gateway. | `noeta/tools/app/open_app.py` |

## Memory tools

Mounted only when `Capabilities.memory` is enabled (only the `main` preset
opens it).

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `memory_write` | low | Write a markdown memory file to the memory store. | `noeta/tools/memory.py` |
| `memory_read` | low | Read the full text of a stored memory on demand. | `noeta/tools/memory.py` |

## MCP tools

Remote MCP tools appear dynamically as `mcp__<alias>__<tool>` when MCP
servers are registered and enabled per session. See
[ADR: MCP connectors](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md).

## Tool risk levels

| Level | Meaning |
| --- | --- |
| `low` | No side effects outside the agent's own state. Always allowed. |
| `high` | Modifies the filesystem or spawns external processes. Subject to `PermissionGuard` approval. |

## Notes

- There is no separate `read_file` / `write_file` / `replace_text` / `list_dir` / `git_status` / `git_diff` tool. Those old names were renamed (`read` / `write` / `edit`) or removed (`list_dir`). `git status` / `git diff` are allowlist rules inside `shell_run`.
- The `write` tool accepts an optional `allowed_path_globs` workspace-relative whitelist at construction time (empty = unrestricted). `edit` and `apply_patch` ignore the whitelist.
