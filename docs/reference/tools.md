# Built-in tools

This page is the catalogue of everything an agent can call out of the box: what
each tool does, what it costs you in risk, and what has to be true before it
appears in the model's tool list at all.

Tool names are provider-safe `snake_case` and are the exact strings the model
calls. Each tool carries a `risk_level` that decides whether a call needs
approval.

A bare `Options()` — that is, `allowed_tools=None` — mounts **ten** tools:
the `fs` pack (`Read`, `Glob`, `Grep`, `Edit`, `Write`,
`Bash`, `BashOutput`, `KillShell`) and the `web` pack (`WebFetch`,
`WebSearch`).

```python
from noeta.sdk import Options
options = Options(system_prompt="…")          # allowed_tools defaults to None
# the agent sees: Read, Glob, Grep, Edit, Write,
#                 Bash, BashOutput, KillShell, WebFetch, WebSearch
```

Ten of those need no configuration; `WebSearch` needs an API key. Everything
else on this page is gated somewhere else — memory and browser on an agent
activation, `open_app` on a host-wired gateway, `run_skill_script` on the
`skills` plugin config, MCP on a per-session registration.

## Filesystem tools

Declared by the `fs` built-in plugin manifest
(`packages/noeta-sdk/noeta/builtins/fs/__init__.py`).

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `Read` | low | Read a file (UTF-8), optionally sliced by line `offset` / `limit`. The full body is always offloaded as an artifact ref. **Reads are unfenced** — see below. | `noeta/builtins/fs/impl/read.py` |
| `Glob` | low | Match a glob pattern (`**` recurses) under `path` and return the matching paths, sorted and capped. | `noeta/builtins/fs/impl/read.py` |
| `Grep` | low | Content search with a Python `re` regex, scoped by `path` and filtered by `Glob`. | `noeta/builtins/fs/impl/read.py` |
| `Edit` | high | Replace an exact `old` substring in an existing file; `replace_all` switches from unique-match to every occurrence. | `noeta/builtins/fs/impl/edit.py` |
| `Write` | high | Write a file — create it, or overwrite one already `Read` this session. The parent directory must exist; `content` caps at 64 KB. | `noeta/builtins/fs/impl/edit.py` |
| `Bash` | high | Run a command in the workspace; `run_in_background` detaches it and returns a `job_id`. | `noeta/builtins/fs/impl/shell.py` |
| `BashOutput` | low | Read status (`running` / `exited`), exit code, and a fresh output snapshot of a background job. | `noeta/builtins/fs/impl/shell.py` |
| `KillShell` | high | Stop a background job you started (SIGTERM, then SIGKILL after a grace period). | `noeta/builtins/fs/impl/shell.py` |

The three write tools stage a proposed diff instead of touching disk while
`HostConfig.write_mode` is `"dry_run"` (the default); `"apply"` performs real
writes.

### Reads are unfenced

The workspace root fences **writes**. For `Read`, `Glob` and `Grep` it only
anchors *relative* paths: an absolute path is read where it points — a
neighbouring checkout, a skill pack's bundled reference, anything the server
process can read. This is deliberate (an agent routinely needs to read outside
its workspace) and it is why the boundary that matters is the **process's own**
file permissions, not the workspace root. A deployment that must not expose a
path should not run the agent as a user who can read it.

Writes are the fenced half: `Write` / `Edit` resolve inside the
workspace root. `HostConfig.write_roots` answers "may this task write here,
outside its workspace?" per call; with no resolver an out-of-workspace write
simply fails. `Write` additionally honours an optional workspace-relative
`allowed_path_globs` whitelist bound at construction (empty = unrestricted);
`Edit` ignores it.

### Shell modes

`ShellMode` (`noeta/runtime/shell_policy.py`) is bound when the pack is built:

| Mode | Effect |
| --- | --- |
| `OFF` | `Bash` is not in the pack at all. |
| `ALLOWLIST` | Default. Only the structural allowlist below passes, argv-only. |
| `ARBITRARY` | Any command without shell metacharacters runs through bash. |

Under `ALLOWLIST` these argv patterns pass
(`noeta/builtins/fs/impl/shell_rules.py`):

- `git status` / `git diff`
- `pytest` / `uv run pytest`
- `npm test` / `pnpm test`
- `Grep` / `rg` / `find` / `ls` — read-only search and listing, so an
  ALLOWLIST-mode agent without its own `Grep` / `Glob` tool can still search the
  workspace. Their validators reject the flags that shell out to another program
  or mutate the filesystem.

Host config can append more rules (`{"program": …, "subcommand": …}`); the
built-ins are always kept. An operator-configured rule is looser than the
curated built-ins: it means "this program may run", accepting any tail args
that survive the metachar scan.

Shell metacharacters (`|`, `;`, `&&`, `>`, …) are rejected before tokenization.
This is **path-containment plus an allowlist, not a process sandbox** —
`Bash` spawns external programs in the trusted workspace.

## Web tools

Declared by the `web` built-in plugin manifest.

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `WebFetch` | low | Fetch a public web page over HTTP(S) and render it to Markdown. Always available. | `noeta/builtins/web/impl/fetch.py` |
| `WebSearch` | low | Run a web search and return ranked hits as Markdown. **Mounted only when `NOETA_WEB_SEARCH_API_KEY` is set.** | `noeta/builtins/web/impl/search.py` |

## App tools

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `open_app` | low | Publish a workspace HTML app through the host's preview gateway. Mounted only when the host wires `HostConfig.app_gateway`. | `noeta/builtins/app/impl/__init__.py` |

## Memory tools

Mounted only when the agent activates `memory`. Among the official presets that
is `main` (and the internal consolidation curator).

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `memory_write` | medium | Write a Markdown memory file to the store. Optional `description` (one-line index summary) and `type` (`user` / `project` / `procedural` / `reference`) are stored as a frontmatter block the tool composes itself. | `noeta/builtins/memory/impl/store.py` |
| `memory_read` | low | Read the full text of a stored memory on demand. | `noeta/builtins/memory/impl/store.py` |
| `memory_search` | low | Case-insensitive substring match over names and full text, with grep-style excerpts (up to 3 lines per memory, 10 memories; a `truncated` flag reports when more matched). | `noeta/builtins/memory/impl/store.py` |
| `memory_archive` | medium | Retire an outdated memory into the store's `archive/` subdirectory — it drops out of the index, recall and search, but the file is never deleted, so a human can restore it. | `noeta/builtins/memory/impl/store.py` |

## Browser tools

Mounted only when **both** hold: the agent activates `browser`
(`"browser" in AgentSpec.plugins`), and the session is bound to a live sandbox
container. Among the official presets that is the `web` subagent alone — `main`
stays browser-free and delegates to it, so a non-sandbox deployment's tool set
and stable prefix are untouched.

All five are `high` risk (any browser action can egress to any site), so they
route through approval unless the session bypasses permissions.

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `browser_navigate` | high | Go to a `url`; returns the page snapshot. | `noeta/builtins/browser/impl/__init__.py` |
| `browser_click` | high | Click the interactive element at `index` (from the snapshot's numbered list). | `noeta/builtins/browser/impl/__init__.py` |
| `browser_type` | high | Type text into the element at `index`. | `noeta/builtins/browser/impl/__init__.py` |
| `browser_extract` | high | Re-read the current page as a snapshot (no arguments). | `noeta/builtins/browser/impl/__init__.py` |
| `browser_screenshot` | high | Capture a PNG and store it as a **workspace artifact**, returning its `ContentRef`. It is not fed to the model as vision. | `noeta/builtins/browser/impl/__init__.py` |

The four text tools return a *page snapshot*: page text plus numbered
interactive elements. That numbering is what `browser_click` / `browser_type`
address, so a snapshot must precede them.

Name, schema, and description are pinned by noeta, not by the container image —
the model-facing contract (and therefore the stable-prefix cache bytes) must not
drift when the sandbox changes its own tool names. Each tool delegates to a
`BrowserBackend`, the one place the container's browser wire is pinned. It is a
per-session tool pack injected like the fs pack, not an MCP connector.

## Skill tools

| Tool | Risk | What it does | Source |
| --- | --- | --- | --- |
| `run_skill_script` | high | Run an active skill's bundled script via an allowlisted interpreter. Present only when the `skills` plugin config sets `allow_skill_scripts` and an active skill ships a script. | `noeta/builtins/skills/impl/script.py` |

## Control tools

Control tools are model-facing schemas that translate into engine decisions
rather than into a `Tool.invoke`. Each is a `control_tool` contribution that
self-gates: mounting *is* enablement.

| Tool | Mounted when | Plugin |
| --- | --- | --- |
| `Task` | the agent activates `delegation` (derived automatically when it has children) | `delegation` |
| `TodoWrite` | the agent activates `TodoWrite` | `TodoWrite` |
| `AskUserQuestion` | the agent activates `AskUserQuestion` | `AskUserQuestion` |
| `skill` | the agent activates `skill_invocation` **and** the merged skill menu is non-empty | `skills` |
| `run_workflow` | `HostConfig.workflow_allowed` is on (and the agent can delegate) | `react` |
| `structured_output` | `Options.output_schema` is set | `react` |

## MCP tools

Remote MCP tools appear dynamically as `mcp__<alias>__<tool>` when MCP servers
are registered and enabled per session. See
[ADR: MCP connectors](https://github.com/initxy/noeta/blob/main/docs/adr/mcp-connectors.md).

In-process SDK MCP servers (`create_sdk_mcp_server`) are different: their tools
keep their **bare** `@tool` names, with no `mcp__` prefix. See
[Build custom tools](../how-to/build-custom-tools.md).

## Tool risk levels

There are exactly three levels, ordered `low < medium < high`.

| Level | Meaning |
| --- | --- |
| `low` | No side effects outside the agent's own state. Always allowed. |
| `medium` | Mutates durable state, but only inside a confined directory — the memory store, for example. |
| `high` | Modifies the filesystem, spawns external processes, or reaches the live web. Goes through the approval gate. |

`Options.permission_mode` decides which levels actually gate: `"default"` gates
everything above `low`, `"acceptEdits"` exempts the three edit-class tools, and
`"bypassPermissions"` gates nothing.

## Next

- [Build custom tools](../how-to/build-custom-tools.md) — add your own with `@tool`
- [Options](sdk-options.md) — `allowed_tools`, `disallowed_tools`, permission modes
- [Guard vs Observer](../concepts/guard-observer.md) — how a call gets denied or approved
- [Plugin surfaces](plugin-surfaces.md) — how a tool reaches an agent through a plugin
