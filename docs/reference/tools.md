# Built-in tools

Every tool an agent can call out of the box: what it does, its risk level, and what has to be true before it is mounted.

A bare `Options()` (`allowed_tools=None`) mounts the `fs` pack and the `web` pack:

```python
from noeta.sdk import Options
options = Options(system_prompt="…")          # allowed_tools defaults to None
# the agent sees: Read, Glob, Grep, Edit, Write,
#                 Bash, BashOutput, KillShell, WebFetch, WebSearch
```

`WebSearch` also needs `NOETA_WEB_SEARCH_API_KEY`. Everything else is gated elsewhere:

| Tools | Mounted when |
| --- | --- |
| `fs`, `web` packs | always (subject to `allowed_tools` / `disallowed_tools`) |
| `memory_*` | the agent activates `memory` |
| `browser_*` | the agent activates `browser` **and** the task is bound to a live sandbox |
| `open_app` | the host sets `HostConfig.app_gateway` |
| `run_skill_script` | `plugin_config["skills"]["allow_skill_scripts"]` is on and an active skill ships a script |
| `mcp__<alias>__<tool>` | a remote MCP server is registered and enabled for the task |
| `ToolSearch`, `McpCall` | an enabled MCP server's spec sets `deferred=True` (its own tools are then not advertised) |
| control tools | see [Control tools](#control-tools) |

## Filesystem tools

From the `fs` built-in (`noeta/builtins/fs/`).

| Tool | Risk | Parameters | What it does |
| --- | --- | --- | --- |
| `Read` | low | `file_path`, `offset?`, `limit?` | Read a UTF-8 file, optionally a line slice. One call returns at most 100 KB; past that the result says so and the model continues with `offset`. The ContentStore keeps the whole file up to 1 MiB, only the served window above that. |
| `Glob` | low | `pattern`, `path?` | Paths matching a glob (`**` recurses, `{ts,tsx}` alternatives), sorted and capped. Walks with `rg --files`: gitignore-aware, hidden files skipped. |
| `Grep` | low | `pattern`, `path?`, `glob?`, `type?`, `output_mode?`, `-i`, `-n`, `-o`, `-u`, `-A`/`-B`/`-C`, `context?`, `head_limit?`, `offset?`, `multiline?` | Ripgrep content search, run through the `ExecEnv` (which must have `rg` installed; without it the fs plugin warns once with a `RuntimeWarning`). |
| `Edit` | high | `file_path`, `old_string`, `new_string`, `replace_all?` | Replace an exact substring (unique match unless `replace_all`). The file must have been `Read` first. A CRLF file is matched and written with CRLF. |
| `Write` | high | `file_path`, `content` | Create a file (parents created) or overwrite one already `Read` in this task. `content` caps at 8 MB. |
| `Bash` | high | `command`, `timeout?` (ms, max 600000), `description?`, `run_in_background?` | Run a command with `cwd` = workspace root. Background mode returns a job id. |
| `BashOutput` | low | `bash_id`, `filter?` | Status (`running` / `exited`), exit code and new output of a background job. |
| `KillShell` | high | `shell_id` | Stop a background job (SIGTERM, then SIGKILL after a grace period). |

- **Writes are staged by default.** `HostConfig.write_mode="dry_run"` (default) records a proposed diff; `"apply"` writes to disk.
- **Writes are fenced, reads are not.** `Write` / `Edit` resolve inside the workspace root; `HostConfig.write_roots` can allow more roots per task. `Read` / `Glob` / `Grep` only anchor *relative* paths — an absolute path is read wherever it points, so the real read boundary is the process's own file permissions.
- `Write` honours an optional workspace-relative `allowed_path_globs` whitelist bound at construction (empty = unrestricted); `*` matches within one path segment, `**` crosses directories, `{a,b}` lists alternatives. `Edit` ignores it.

### Shell gating

`SdkHost.shell_mode` (default `ShellMode.ALLOWLIST`) and the permission mode decide what `Bash` may run:

| Setting | Behaviour |
| --- | --- |
| `shell_mode=OFF` | `Bash` is not mounted. |
| `default` / `acceptEdits` | The command runs through `bash -c`. A command matching the effective allowlist runs silently; anything else asks for approval. A command with an unquoted `{`, `*`, `?` or `[`, or a word starting with `~`, always asks — bash would expand it; quoted forms such as `find . -name '*.py'` are unaffected. |
| `bypassPermissions` | Any command runs, no approval. |

The built-in allowlist (`noeta/builtins/fs/impl/shell_rules.py`) matches metachar-free argv only:

| Program | Accepted |
| --- | --- |
| `git status` | no args, `--short`, `-s`, `--porcelain` |
| `git diff`, `git log` | read-only forms |
| `pytest`, `uv run pytest` | test runs — trusted workspace only |
| `npm test`, `pnpm test` | any tail — trusted workspace only |
| `grep`, `rg`, `find`, `ls` | read-only; `rg --pre`/`--hostname-bin` and `find -exec`/`-delete`/`-fprint*` are rejected |

The test runners execute repository code (`conftest.py`, `package.json` scripts), so those four rules exempt a call only when the workspace is trusted — the same `grant_trust` that gates `.noeta/shell-allowlist.json`, or `project_shell_allowlist_trust="open"`. The `git` rules stay open but run hardened, with `-c core.fsmonitor=` and `--no-ext-diff --no-textconv`; a `filter.<driver>.clean` in the repository's `.git/config` is the risk left over.

Extend it in three ways:

| Source | Shape | Notes |
| --- | --- | --- |
| `SdkHost.shell_allowlist` | `[{"program": …, "subcommand": …}]` | operator rules; any tail args that pass the metachar scan |
| `<workspace>/.noeta/shell-allowlist.json` | same JSON list | repository content, loaded only when the workspace is trusted (`grant_trust`); otherwise `UntrustedProjectShellAllowlistWarning`, once per workspace |
| `SdkHost(project_shell_allowlist_trust="open")` | — | load the workspace file unconditionally; `trust_store=` points at another store |

::: warning
This is an allowlist plus approval, not a process sandbox. `Bash` spawns real processes that can write anywhere the server user can. Use a [sandbox](../guides/sandbox.md) for isolation.
:::

## Web tools

| Tool | Risk | Parameters | What it does |
| --- | --- | --- | --- |
| `WebFetch` | low | `url`, `prompt` | Fetch a page, render it to Markdown, and answer `prompt` against it with an auxiliary model call (`Options.webfetch_model`, default: the task's main model). HTTP upgrades to HTTPS; a redirect is followed only to the same scheme, host and port, others are returned; pages are cached 15 minutes. Only `http(s)` URLs. By `Content-Type`: HTML becomes Markdown, text / JSON / XML pass through as-is, and images, PDF and other binary types are a tool error naming the type. |
| `WebSearch` | low | `query`, `count?` | Web search, ranked hits as Markdown. Mounted only when `NOETA_WEB_SEARCH_API_KEY` is set. |

`WebFetch` reaches any host. `HostConfig.webfetch_allowed_hosts` lists hosts it may reach without asking:

| Entry | Matches |
| --- | --- |
| `example.com` | exactly that host |
| `*.example.com` | subdomains at any depth, **not** `example.com` itself |

| Permission mode | Unlisted host | Listed host |
| --- | --- | --- |
| `default`, `acceptEdits` | approval per call | silent |
| `bypassPermissions` | silent | silent |

A malformed entry raises at `HostConfig` construction. Matching uses the URL's real, lowercased, IDNA-normalised host (`https://example.com@evil.test/` is `evil.test`). A redirect handed back to the model is judged again on the next call. This asks a human about unfamiliar hosts; it is not an egress boundary — an agent with `Bash` can `curl` anything. Enforce egress at the network or sandbox.

## App tools

| Tool | Risk | Parameters | What it does |
| --- | --- | --- | --- |
| `open_app` | low | `dir`, `proxy_to` | Publish a workspace HTML app through `HostConfig.app_gateway`. |

## Memory tools

Mounted when the agent activates `memory` (among presets: `main` and the consolidation curator).

| Tool | Risk | Parameters | What it does |
| --- | --- | --- | --- |
| `memory_write` | medium | `name`, `text`, `description?`, `type?`, `keywords?`, `related?` | Write a Markdown memory. Frontmatter fields merge per field over what is on disk (omit = keep, empty = remove). Stamps `created` / `updated` / `source_task` itself (a `created` the model sends is ignored); `HostConfig.memory_max_bytes` counts the whole stored page, frontmatter included; a new name reports similar existing memories. |
| `memory_read` | low | `name` | Full text of one memory. |
| `memory_search` | low | `query` | Case-insensitive substring search over names and text; up to 3 excerpt lines per memory, 10 memories, `truncated` flag. |
| `memory_archive` | medium | `name` | Move a memory to `archive/`: out of index, recall and search, never deleted. |

`type` is one of `user` / `project` / `procedural` / `reference`. `keywords` is a comma-separated list of retrieval aliases (useful across languages); `related` lists memory names recalled alongside this one.

## Browser tools

Mounted only when the agent activates `browser` and the task has a live sandbox. Among presets only the `web` subagent does. All are `high` risk.

| Tool | Parameters | What it does |
| --- | --- | --- |
| `browser_navigate` | `url` | Go to a URL; returns a page snapshot. |
| `browser_click` | `index` | Click the numbered element from the last snapshot. |
| `browser_type` | `index`, `text` | Type into the numbered element. |
| `browser_extract` | — | Re-read the current page as a snapshot. |
| `browser_screenshot` | — | Store a PNG in the `ContentStore` and return its `ContentRef`. Not fed to the model as vision. |

A snapshot is page text plus numbered interactive elements. Names and schemas are pinned by Noeta, not by the container image.

## Skill tools

| Tool | Risk | Parameters | What it does |
| --- | --- | --- | --- |
| `run_skill_script` | high | `skill`, `relpath`, `args?` | Run an active skill's bundled script through an allowlisted interpreter. No shell. |

## Control tools

Model-facing schemas that become engine decisions rather than `Tool.invoke` calls. The activation name goes in `Options.plugins`; a wrong name fails the build with a `ValueError` listing the legal ones.

| Tool | Mounted when | Activation / plugin |
| --- | --- | --- |
| `Task` | the agent can delegate (derived when it has `agents`) | `delegation` |
| `TodoWrite` | the agent activates it | `todo_write` |
| `AskUserQuestion` | the agent activates it | `ask_user_question` |
| `skill` | activated and the merged skill menu is non-empty | `skill_invocation` (mounted by `skills`) |
| `run_workflow` | `HostConfig.workflow_allowed=True` and the agent can delegate | `react` |
| `RecallHistory` | compaction is wired — always under `Client` / `query` | `react` |
| `structured_output` | a subtask / workflow helper spawned with its own schema (`Options.output_schema` uses the provider's native mode instead); it must be the only call in its response — sent alongside other calls, the whole batch is refused and the model is told to send it alone | `react` |

`RecallHistory` pages back (`offset`) the original messages that compaction collapsed into the summary note — content that exists in no file.

## MCP tools

Remote MCP tools appear as `mcp__<alias>__<tool>`. A name over 64 characters is truncated with an 8-character sha256 suffix, keeping the prefix; when two tools of one server collide once sanitised, an already-valid name keeps it and the others take the suffix; a collision across two servers is an `McpConfigError` when the agent is built. In-process SDK servers (`create_sdk_mcp_server`) keep their bare `@tool` names.

A server spec with `deferred=True` keeps its tools registered under those names but leaves their schemas out of the request; the model gets `ToolSearch` (find deferred tools and their schemas) and `McpCall` (run one by name) instead, once for all deferred servers. A `McpCall` is checked and recorded as a call to the real tool. See [MCP servers](../guides/mcp.md).

## Risk levels

| Level | Meaning | Gated under `default` |
| --- | --- | --- |
| `low` | no side effects outside the agent's own state | no |
| `medium` | durable writes inside a confined directory (the memory store) | yes |
| `high` | filesystem writes, processes, live web | yes |

`Options.permission_mode`: `"default"` gates every tool above `low`; `"acceptEdits"` additionally exempts `Edit` / `Write`; `"bypassPermissions"` gates nothing. `Bash` and `WebFetch` add the per-call checks above.

## Next

- [Custom tools](../guides/tools.md) — add your own with `@tool`
- [Options](options.md) — `allowed_tools`, `disallowed_tools`, `permission_mode`
- [Engine](../how-it-works/engine.md) — how a call is approved or denied
