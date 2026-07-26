# Built-in Tools

Noeta ships a set of built-in tools assembled from the filesystem pack,
the web pack, the app pack, and (conditionally) the memory, browser, and MCP
tools. Tool names are provider-safe `snake_case` and are the exact strings the
model calls.

Not everything here is mounted by default. `Options.allowed_tools=None` selects
the 11-name **built-in whitelist** (`read`, `glob`, `grep`, `edit`, `write`,
`apply_patch`, `shell_run`, `shell_poll`, `shell_kill`, `webfetch`,
`web_search`) — of which 10 mount with no extra configuration, since
`web_search` needs an API key. The rest are gated elsewhere: memory and browser
on a capability, `open_app` on a host-wired gateway, `run_skill_script` on
`allow_skill_scripts`, MCP on a per-session registration.

## Filesystem tools

Built by `build_fs_tools()` in `noeta.tools.fs`. Each tool carries a
`risk_level` used by the `PermissionGuard`.

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `read` | low | Read a file (utf-8), optionally sliced by line `offset` / `limit`. **Reads are unfenced** — see below. | `noeta/tools/fs/read.py` |
| `glob` | low | Match a workspace-relative glob pattern and return matching paths. | `noeta/tools/fs/read.py` |
| `grep` | low | Regex (`re` module) content search across the workspace. | `noeta/tools/fs/read.py` |
| `edit` | high | Replace an exact, unique `old` substring in an existing file. Dry-run by default. | `noeta/tools/fs/edit.py` |
| `write` | high | Write a file (create, or overwrite one previously read). Dry-run by default. | `noeta/tools/fs/edit.py` |
| `apply_patch` | high | Apply a small batch of edits atomically — all succeed or none. Dry-run by default. | `noeta/tools/fs/patch.py` |
| `shell_run` | high | Run a shell command in the workspace. Mode-gated: `ALLOWLIST` by default, `OFF` removes the tool entirely. | `noeta/tools/fs/shell.py` |
| `shell_poll` | low | Check status / output of a background shell job. | `noeta/tools/fs/shell.py` |
| `shell_kill` | high | Stop a background shell job you started (SIGTERM → SIGKILL). | `noeta/tools/fs/shell.py` |
| `run_skill_script` | high | Run an active skill's bundled script via an allowlisted interpreter. | `noeta/tools/fs/skill_script.py` |

### Reads are unfenced

The workspace root fences **writes**. For `read` (and `grep`'s path argument)
it only anchors *relative* paths: an absolute path is read where it points —
a neighbouring checkout, a skill pack's bundled reference, anything the server
process can read. This is deliberate (an agent routinely needs to read outside
its workspace) and it is why the boundary that matters is the **process's own**
file permissions, not the workspace root. A deployment that must not expose a
path should not run the agent as a user who can read it.

Writes are the fenced half: `write` / `edit` / `apply_patch` resolve inside the
workspace root, and `write` additionally honours an optional
`allowed_path_globs` whitelist.

### Shell allowlist (default)

When `shell_mode = ALLOWLIST`, only these argv patterns pass:

- `git status` / `git diff`
- `pytest` / `uv run pytest`
- `npm test` / `pnpm test`
- `grep` / `rg` / `find` / `ls` — read-only search and listing, so an
  ALLOWLIST-mode agent (notably `general-purpose`, which has no `grep` / `glob`
  tool of its own) can still search the workspace. Their validators reject the
  flags that shell out to another program or mutate the filesystem.

Host config can append more rules (`{"program": …, "subcommand": …}`); the
built-ins are always kept. An operator-configured rule is looser than the
curated built-ins: it means "this program may run", accepting any tail args
that survive the metachar scan.

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
| `open_app` | low | Publish a workspace HTML app through the host's preview gateway (mounted only when the host wires one). | `noeta/tools/app/open_app.py` |

## Memory tools

Mounted only when `Capabilities.memory` is enabled (only the `main` preset
opens it).

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `memory_write` | medium | Write a markdown memory file to the memory store. Optional `description` (one-line index summary) and `type` (`user` / `project` / `procedural` / `reference`) parameters are stored as a frontmatter block the tool composes itself. | `noeta/tools/memory.py` |
| `memory_read` | low | Read the full text of a stored memory on demand. | `noeta/tools/memory.py` |
| `memory_search` | low | Find memories by content: case-insensitive substring match over names and full text, with grep-style excerpts (up to 3 lines per memory, 10 memories; a `truncated` flag reports when more matched). | `noeta/tools/memory.py` |
| `memory_archive` | medium | Retire an outdated memory into the store's `archive/` subdirectory — it leaves the index, recall and search but is never deleted (a human can restore it). | `noeta/tools/memory.py` |

## Browser tools

Built by `build_browser_tools()` in `noeta.tools.browser`. Mounted only when
**both** hold: the agent's spec opens `Capabilities.browser`, and the session is
bound to a live sandbox container. Among the official presets that is the `web`
subagent alone — `main` stays browser-free and delegates to it, so a
non-sandbox deployment's tool set and stable prefix are unchanged.

All five are `high` risk (any browser action can egress to any site), so they
route through approval unless the session bypasses permissions.

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `browser_navigate` | high | Go to a `url`; returns the page snapshot. | `noeta/tools/browser/__init__.py` |
| `browser_click` | high | Click the interactive element at `index` (from the snapshot's numbered list). | `noeta/tools/browser/__init__.py` |
| `browser_type` | high | Type text into the element at `index`. | `noeta/tools/browser/__init__.py` |
| `browser_extract` | high | Re-read the current page as a snapshot (no arguments). | `noeta/tools/browser/__init__.py` |
| `browser_screenshot` | high | Capture a PNG and store it as a **workspace artifact**, returning its `ContentRef`. It is not fed to the model as vision. | `noeta/tools/browser/__init__.py` |

The four text tools return a *page snapshot*: page text plus numbered
interactive elements. That numbering is what `browser_click` / `browser_type`
address, so a snapshot must precede them.

Name, schema, and description are **pinned by noeta**, not by the container
image — the model-facing contract (and therefore the stable-prefix cache bytes)
must not drift when the AIO Sandbox changes its own tool names. Each tool
delegates to a `BrowserBackend`, the one place the container's browser wire is
pinned. It is a per-session tool pack injected like the fs pack, not an MCP
connector.

## MCP tools

Remote MCP tools appear dynamically as `mcp__<alias>__<tool>` when MCP
servers are registered and enabled per session. See
[ADR: MCP connectors](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md).

In-process SDK MCP servers (`create_sdk_mcp_server`) are different: their tools
keep their **bare** `@tool` names, with no `mcp__` prefix. See
[Build custom tools](../how-to/build-custom-tools.md).

## Tool risk levels

| Level | Meaning |
| --- | --- |
| `low` | No side effects outside the agent's own state. Always allowed. |
| `medium` | Mutates durable state, but only inside a confined directory (e.g. the memory store). |
| `high` | Modifies the filesystem or spawns external processes. Subject to `PermissionGuard` approval. |

## Notes

- There is no separate `read_file` / `write_file` / `replace_text` / `list_dir` / `git_status` / `git_diff` tool. Those old names were renamed (`read` / `write` / `edit`) or removed (`list_dir`). `git status` / `git diff` are allowlist rules inside `shell_run`.
- The `write` tool accepts an optional `allowed_path_globs` workspace-relative whitelist at construction time (empty = unrestricted). `edit` and `apply_patch` ignore the whitelist.
