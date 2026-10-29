# Changelog

All notable changes to Noeta are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The 0.x history (packages
`noeta-runtime` and `noeta-sdk`) is in `CHANGELOG.md` at tag `v0.6.42`.

## [Unreleased]

### Changed

- **Stop every worker before upgrading; workers of different versions must not share a
  store.** This release writes log entries an older worker cannot decode (messages with
  `role="system"`, origins `inline` and `agent`, `ToolReference` blocks), so an older worker
  that picks up a task a newer one touched fails on it again and again. Logs written by
  older versions still load.
- **noeta reads only its own places, with Claude Code's mechanisms.** `~/.claude/`,
  `.claude/`, `CLAUDE.md`, `CLAUDE.local.md`, `.mcp.json` and `CLAUDE_CONFIG_DIR` are no
  longer read, so the two tools never pick up each other's hooks, env or instructions.
  Instructions are `NOETA.md` in Claude Code's layering: the managed `NOETA.md` and
  `.noeta/rules` (`/etc/noeta`; macOS `/Library/Application Support/Noeta`, Windows
  `C:\Program Files\Noeta`), the user's `~/.noeta/NOETA.md` and `~/.noeta/rules/`, then per
  directory `NOETA.md`, `.noeta/NOETA.md`, `.noeta/rules/` and `NOETA.local.md` (`AGENTS.md`
  where a directory has neither project file). Settings are `~/.noeta/settings.json`, a
  trusted project's `.noeta/settings.json` and `.noeta/settings.local.json`, and the managed
  `managed-settings.json` (same directory), in Claude Code's format and precedence
  (`noeta.cli.claude_settings` -> `noeta.cli.settings_files`, `ClaudeSettings` ->
  `FileSettings`). Skills come from `.noeta/skills`, `.agents/skills` and `~/.noeta/skills`;
  agent files from `~/.noeta/agents` and `.noeta/agents`; project MCP servers from
  `.noeta/mcp.json` (same format and consent keys); isolated agents' worktrees go under
  `.noeta/worktrees/`. Claude Code's environment knobs carry noeta's prefix:
  `NOETA_AUTO_COMPACT_WINDOW`, `NOETA_AUTOCOMPACT_PCT_OVERRIDE`, `NOETA_MCP_TIMEOUT`,
  `NOETA_MCP_TOOL_TIMEOUT`, `NOETA_MAX_MCP_OUTPUT_TOKENS`. Texts the model sees name
  `NOETA.md` and `.noeta/` where they mean noeta's files (`parity/deviations.md`).
- **Compaction works as Claude Code's.** It fires at Claude Code's threshold (the window, less
  `min(output ceiling, 20K)`, less 13K; `auto_compact_window` / `auto_compact_percent` or
  `NOETA_AUTO_COMPACT_WINDOW` / `NOETA_AUTOCOMPACT_PCT_OVERRIDE` lower it), checked
  before every request. The summary is asked of the main request itself, with the
  instruction (nine sections, `<analysis>` then `<summary>`) appended to its last user
  message; a failed call is retried once with a 64K output ceiling. When the context was
  under the threshold before the newest tool results, that reply and its results stay after
  the summary verbatim. After it come the project instructions (ahead of the summary), the
  summary with the tools used before it and the path of the full transcript
  (`<home>/projects/<workspace>/<task>.jsonl`), the kept messages, then the recently read
  files as Read calls, the invoked skills ("do not run again"), the listings and the
  environment. Old tool results are no longer cleared, `RecallHistory` and the
  compaction reminders are gone, and auto-compaction stops after four rapid refills in a
  row. `Compacted` gains `lead` and `after`; `Extension.lead_resident` puts content ahead
  of the summary.
- **`/compact [instructions]`** summarizes the conversation on demand in the TUI, ACP and
  HTTP (`POST /tasks/{id}/messages` with `/compact`), without a model turn after it.
- **Memory is Claude Code's auto memory.** The `memory_*` tools, the store, recall by
  keywords and the consolidation agent are gone. Memory is a directory of markdown files
  under `~/.noeta/projects/<workspace>/memory/`, written with `Write` / `Edit` as the system
  prompt's memory section describes (types `user`, `feedback`, `project`, `reference`);
  `MEMORY.md` is loaded at the start (200 lines / 25,000 bytes) and after a compaction.
  With `recall_model`, each user message attaches up to five memories that model picks.
  The plugin's arguments are now `home`, `directory`, `recall_model`, `rehang`.
- **A sandbox for local commands, with Claude Code's `sandbox` settings.** With
  `sandbox.enabled` in a settings file, `Bash` (foreground, background and `Monitor`) runs
  each command confined: bubblewrap on Linux (own network, PID, IPC namespaces; a seccomp
  filter refusing Unix sockets and new user namespaces unless `allowAllUnixSockets` /
  `allowNestedUserNamespaces`), Seatbelt on macOS. Commands write only in the working
  directory, `filesystem.allowWrite` and a private `$TMPDIR`. Never in `denyWrite` or what
  runs or configures code outside the sandbox later: the permissions plugin's protected
  paths (`noeta.toolkit.protected_paths`: the whole `.noeta` directory, `.vscode`, `.husky`,
  `.envrc`, `.pre-commit-config.yaml`, shell and package-manager settings, ...),
  `.git/hooks`, `.git/config`, `.git/config.worktree`, each submodule's config and hooks
  under `.git/modules`, and the hooks directory git uses (`core.hooksPath`). What is
  executed on the next ordinary command without a confirmation step cannot be created
  when missing either (an empty read-only placeholder covers it while the command runs):
  `.noeta`, `.git`, git's hooks directory and config, and the settings a package manager
  or build tool runs code from (`.npmrc`, `.yarnrc`, `.yarnrc.yml`, `.yarn`,
  `.pnpmfile.cjs`, `.pnp.cjs`, `.pnp.loader.mjs`, `bunfig.toml`, `.bunfig.toml`, `.cargo`,
  `.mvn`, `.bazelrc`, `.bazelversion`, `.bazeliskrc`; lefthook's configs once lefthook is
  in use) — 16 placeholders in an empty directory. Git does not list them when the working
  directory is a repository's top (the command's `.git/info/exclude` names the files). The other protected names are read-only
  only when present, since each runs only after a separate step of the person's (`direnv
  allow`, an editor trusting the workspace). No directory above a protected path can be
  renamed away. Git keeps working (status, add, commit,
  checkout, submodule status), but **in a directory that is not a repository `git init`
  fails inside the sandbox** (no `.git` can be created), and **nothing under `.noeta/` —
  skills and commands included — can be written from the sandbox** (the permissions plugin
  still lets `Edit`/`Write` change `.noeta/skills` and `.noeta/commands`). Commands read
  everything but `denyRead`, credential stores and noeta's home, and reach only
  `network.allowedDomains` through a filtering HTTP/SOCKS proxy, which connects to the
  address it checked and refuses one that is loopback, link-local, multicast, a cloud
  metadata service or this machine's own unless an IP or CIDR entry allows it (as Claude
  Code); this machine, in any spelling of its address, needs an entry with its port even
  under `*`. Refused hosts follow the output in `<sandbox_violations>`.
  A `Monitor` WebSocket, which the worker opens itself, keeps to the same rules. `Edit` allow rules,
  `additionalDirectories`, `Read` deny rules and `WebFetch(domain:)` allow rules feed it;
  `excludedCommands`, `allowUnsandboxedCommands`, `autoAllowBashIfSandboxed` (default on),
  `failIfUnavailable`, `credentials` (`mask` acts as `deny`), `httpProxyPort` /
  `socksProxyPort`, `enableWeakerNestedSandbox`, `filesystem.disabled` and `bwrapPath` are
  honoured, and a project's settings may tighten the sandbox but never loosen it: its
  `excludedCommands`, `allowWrite`, `allowRead`, `allowedDomains`, `allowUnixSockets`, the
  loosening switches, and its permission allow rules and `additionalDirectories` never reach
  the sandbox (only the user's and managed settings widen it).
  `dangerouslyDisableSandbox: true` runs a command outside after an approval that allow
  rules do not skip. `Bash`'s description gains a sandbox section while the sandbox is on.
  Without bwrap (or namespaces) front ends say why and commands run unconfined, or are
  refused with `failIfUnavailable`. API: `LocalExecEnv(sandbox=...)`, `env.sandbox`,
  `noeta.toolkit.sandbox`.
- **`start_container` is locked down by default:** no network, a read-only root with tmpfs
  scratch, a reduced capability set, `no-new-privileges` and a 1024-process limit (each an
  argument: `network`, `read_only`, `caps`, `pids_limit`, plus `memory` / `cpus`). Its
  `sandbox=` maps `network.allowedDomains` onto an internal Docker network whose only way
  out is noeta's filtering proxy (`ensure_container_proxy` serves it again after a restart).
- **MCP transports as Claude Code's:** `sse` (legacy HTTP+SSE) and `ws` (WebSocket) join
  `stdio` and `http` (`type: "streamable-http"` reads as `http`); `alwaysLoad` and
  `bareElicitationCapability` are read from settings. Requests are multiplexed: a timed-out
  call is cancelled and no longer breaks a stdio connection.
- **MCP OAuth 2.1** for `http` / `sse` servers: resource and authorization-server discovery,
  dynamic client registration or `oauth.clientId`, PKCE, `oauth.callbackPort` /
  `NOETA_MCP_OAUTH_CALLBACK_PORT`, `oauth.authServerMetadataUrl`, `oauth.scopes`; tokens in
  `$NOETA_HOME/mcp-auth` (0600) and refreshed. A server that needs a sign-in offers
  `mcp__<server>__authenticate` / `mcp__<server>__complete_authentication` in place of its
  tools. `headersHelper` runs a command for the headers when a connection opens
  (`NOETA_MCP_SERVER_NAME` / `NOETA_MCP_SERVER_URL` set).
- **MCP servers can ask the user** (elicitation, turned into the question form; the call
  waits for the answer), read the task's workspace from `roots/list`, and refresh their tools
  with `list_changed`.
- **`noeta mcp add|add-json|list|get|remove|login|logout`** with Claude Code's flags, writing
  `~/.noeta/settings.json` (user), `.noeta/mcp.json` (project) and
  `.noeta/settings.local.json` (local). `McpPool(http=...)` routes remote MCP connections
  and sign-ins through the host's `httpx.AsyncClient`.
- **`noeta -p` is Claude Code's headless mode.** `-p` is a flag and the prompt positional (or
  stdin); `--output-format text|json|stream-json`, `--input-format stream-json`,
  `--include-partial-messages`, `--replay-user-messages`, `--max-turns`, `--max-budget-usd`,
  `--json-schema`, `--allowedTools` / `--disallowedTools` / `--tools` / `--add-dir`,
  `--permission-prompt-tool stdio`, `-c` / `-r` / `--fork-session` / `--session-id`, `-v`;
  flags an Agent SDK passes that noeta does not use are accepted and named on stderr.
  Without `--permission-prompt-tool stdio` anything that would ask is refused and the turn
  goes on (it used to exit 2). `--json` is gone; use `--output-format stream-json --verbose`.
- **`create_app` requires a bearer token**, as `noeta serve` does: without `token=` it
  raises unless the host passes `auth=False`.
- **The main loop sends what Claude Code 2.1.312 sends an API-key user.** The system prompt is
  two cached blocks: an identity line (`Agent.identity`, agent-file `identity:`, prompt name
  `identity/<agent>`) and the main prompt, rewritten to Claude Code's rules and section order
  in our own words (noeta-only rules dropped, `parity/rules/main-prompt.md`). The project
  instructions, a git snapshot and a commit/PR attribution note ride in the first user turn;
  the environment (with the model's name and knowledge cutoff), the skill listing and the date
  follow it as a mid-conversation system turn (`Message.role="system"`, `Reminder.role` and
  `Reminder.order`; the Anthropic adapter joins adjacent ones, the OpenAI adapters send them as
  framed system messages). A `<total_tokens>` budget note follows every prompt and tool result,
  and five tool-only steps in a row earn one nudge to update the user (`quiet_nudge` plugin).
  The Anthropic adapter sends Claude Code's beta set, adaptive thinking with summarized
  updates, `context_management` keeping old thinking, effort `medium` by default,
  `metadata.user_id` (a random device id kept in `~/.noeta/device_id`, the task tree's id),
  three cache marks (no tool mark; one message mark, on the last message), single-text turns
  as bare strings, and a message sent mid-turn inside the tool results' turn.
  `AnthropicProvider(system_turns=False)` is for an endpoint without system turns.
  `ContextPart.cache` is gone (the prompt is one cached block). Accepted differences are listed
  in `parity/deviations.md`.
- **Packages say what they hold** (old paths are gone, no aliases):
  `noeta.core.shell` / `tool_helpers` / `helpers` -> `noeta.toolkit.shell` / `tools` /
  `context` (code plugins share; the kernel never used it); `noeta.core.store.base` ->
  `noeta.core.store`; `noeta.core.store.sqlite` -> `noeta.backends.stores.sqlite`;
  `noeta.backends.postgres` -> `noeta.backends.stores.postgres`; `noeta.backends.execenv` ->
  `noeta.backends.execenvs`; `noeta.apps.local` / `remote` -> `noeta.host.*`,
  `noeta.apps.server` -> `noeta.host.http`, `noeta.apps.acp` -> `noeta.host.acp` (the ways in
  sit together); `noeta.apps` -> `noeta.cli` (the command is `noeta.cli.main`), with
  `from_settings` in `noeta.cli.config`; `noeta.presets` -> `noeta.agents` (agent files in
  `noeta/agents/files/`); `noeta.core.agent_files` -> `noeta.agents.agent_files`. `noeta`
  exports `LocalApp` and `RemoteClient`. The store contract tests ship as
  `noeta.testing.store_contract`. `scripts/lint-layout.py` (in `make check`) keeps code where
  CONTEXT.md's "Where code goes" puts it.
- **1.0 is a rewrite, one package `noeta`.** Tasks are append-only event logs with one writer
  (conditional appends, claims in the store), an inbox as the only way in, and one `Extension`
  interface for every capability. Includes the `noeta` command (terminal UI, `-p`, `acp`,
  `worker`, `serve`). Nothing from 0.x is compatible: APIs, stores and plugins.
- **Plugins are set from one table.** `config.toml` takes `[plugins.<name>]` (arguments,
  `enabled = false`) and `[agents.<name>] add_plugins`; `default_agents` takes `plugins=` and
  `add=` instead of one keyword per built-in plugin.
- **MCP servers from settings.** The terminal front ends start the `mcpServers` of
  `~/.noeta/settings.json` (Claude Code's format) and the `.noeta/mcp.json` servers it
  consents to.
- **Plugin slash commands** show in the TUI's `/help` and as the editor's commands over ACP;
  MCP prompts are commands.
- **Background jobs hold their task tree.** While a job runs, no other worker claims a task of
  its tree (`Store.hold` / `unhold` / `reap_holds`, a `tree_holds` table, `ToolContext.hold`),
  so the next turn can read and stop it with several workers on one store. A hold carries an
  epoch; every worker reaps a silent worker's holds and posts `External("hold_lost")` to the
  tree's tasks in the same transaction, and the model is told the job was lost (Claude Code's
  `<task-notification>` shape). A job runs beside a guard that kills it when its worker dies
  or freezes; its end is posted as `External("shell_job_ended")` before the hold is released
  and settled once by the task's runner. A worker stopping on purpose (closing the TUI) posts
  `hold_lost` without waking the task (`Post.wake`): it hears with its next message. Third-party
  stores implement `hold`, `unhold` and `reap_holds` (a store an earlier
  development build opened keeps an unused `holds` table).
- **Plugins as files.** `~/.noeta/plugins/*.py` (or a directory) is a plugin without packaging;
  a project's `.noeta/plugins/` loads once trusted (`noeta trust`). `noeta plugins` lists where
  each plugin comes from.
- **Any plugin can ask the person and notify them.** `noeta.ask` / `noeta.read_answer`
  (`noeta.core.questions`, moved from `ask_user`) and `notify` on `TaskView` / `ToolContext`;
  front ends import no plugin. `Mcp` takes servers in Claude Code's `mcpServers` shape.
- **Kernel and toolkit surfaces moved to where they are used** (no aliases):
  the permission-mode vocabulary is `noeta.toolkit.permission_modes`; approval cards, the
  pending projection and the typed-answer helpers are `noeta.host.pending` /
  `noeta.host.questions`; env scrubbing and Claude Code's command tags are
  `noeta.toolkit.local_env` / `noeta.toolkit.commands`; `noeta.toolkit.context` is split into
  `tokens`, `paths` and `memory_index`; shell hardening (`HardeningRule`, was `AllowRule`) is in
  the shell plugin; `toolkit.simulated_state` is `TaskView.folded()`. The core's own records
  have typed payloads (`noeta.core.records`, `records.event` / `records.decode`; bytes
  unchanged). `core.client.ClientAPI` names the client API that `Client` and `RemoteClient`
  implement. Renamed for what they do: `Agent.max_steps_per_turn` -> `step_budget`,
  `ModelSpec.family` -> `vendor` (a models file keeps `"family"`), the module
  `noeta.plugins.structured` -> `noeta.plugins.structured_output` (its registry name).
- **Store interface.** `append` returns `Appended(seq, at)` (the stored time); a claim clears
  the row's `waiting_on`; giving back a claim or a hold reports a `released` change, only when a
  row changed; optional `snapshot` / `put_snapshot`; `claim(tenant_limits=)`; `Post.wake`.
  Creating a task with a known id is idempotent under a race (store contract test).
- **The plugin set is pinned per task.** A task records each plugin's name, source (package
  version or file hash) and a hash of its arguments when it first starts and when a pickup
  finds them changed; a worker missing one ends the task failed with `plugin_missing` instead
  of running it without.
- **`Agent` fields for what a sub-agent definition asks**: `inherit_model`, `permission_mode`,
  `isolation` (were read from `Agent.extra`). An agent built in code runs its sub-agents on its
  own model unless it sets `inherit_model`.
- `Runtime.output_ceiling(model)` gives a request's default `max_tokens` (default: the catalog);
  plugins read the working directory through `TaskView.cwd` (config, else the exec env), so a
  task created without a `cwd` runs its hooks and finds its project MCP servers where its
  commands run; `UsageRank` reads through a client; `ToolContext.post` takes a `dedupe_key` and
  reaches any task of the caller's tenant; permissions records `probed` / `compacted` and a
  front end's `resumed` signal is SessionStart `resume` (so a task moving between workers
  sends the same request).
- **The permissions plugin publishes its modes** (a `modes` record without
  `bypassPermissions` when that is disabled); the TUI and ACP offer what it lists. `noeta serve`
  turns on the plugin's own `bypassPermissions` refusal.

- **Built-in tools answer as Claude Code 2.1.312's tools do.**
  - `Read` without `limit` returns the whole file (within 256 KB and 25,000 tokens) instead of
    its first 2,000 lines, and keeps long lines whole instead of cutting them at 2,000
    characters. A whole-file read past the token cap returns the lines that fit and a note with
    the next `offset` instead of being refused. Images and PDFs are recognised by extension
    (content is sniffed only without one); empty, headerless or over-100 MB PDFs, known binary
    types, devices and `/proc/<pid>` files are refused with a reason. An image comes back as the
    image alone; a UTF-8 byte-order mark is hidden; a missing file names the working directory
    and a likely intended path; an empty file or an offset past the end is a warning. The
    "unchanged since last read" answer needs the same `offset` / `limit` and covers notebooks.
  - `Edit` matches straight quotes against curly ones and `\uXXXX` escapes against the
    characters they stand for, keeping the file's own spelling; deleting text takes the
    following line break along; an empty `old_string` creates the file (or fills an empty one).
    It refuses notebooks (use `NotebookEdit`) and files over 1 GB, its not-found error repeats
    the string it looked for, and a file changed on disk since the read is still edited when
    the write is allowed outright. `Edit` and `Write` skip the read-first check for a write no
    one had to approve. `Write` keeps a file's UTF-8 byte-order mark and, given a directory,
    asks for the file name.
  - `NotebookEdit` takes `cell-<index>` for any cell, answers with the cell's new source, and
    gives an inserted cell an id only in an nbformat 4.5+ notebook.
  - `Bash`: exit status 1 of `grep`, `rg`, `find`, `diff` and `test` is an answer, not an
    error; a command still running at its timeout moves to the background (with its job id and
    output file) instead of being killed; output that is one `data:image` URI is an image.
  - `WebFetch` refuses URLs over 2,000 characters or with credentials, waits 60 s and accepts
    10 MB (was 10 s, 5 MB); an HTTP error status, an offset past the end and a cross-host
    redirect (naming the status and the call to make) are ordinary results; `www.` redirects are
    followed; a PDF or other binary body is saved to a file the model can `Read`. `WebSearch`
    returns the query, the hits as JSON and a reminder to cite, and refuses `allowed_domains`
    together with `blocked_domains`.
  - An MCP result over the token budget is saved and pointed to, not cut inline;
    `ReadMcpResourceTool` saves a binary resource and gives its path.
  - `Agent` matches `subagent_type` ignoring case, spaces, dashes and underscores when exactly
    one type fits, and a bad `Agent` call fails alone while the reply's other calls run.
    `SendMessage` refuses `to: "*"`; `TaskStop` answers with Claude Code's result object.
  - `AskUserQuestion` no longer refuses long or empty texts, refuses two questions with the
    same text, steers a one-option question, and tells the model to read typed answers as
    written. `Skill` accepts `/name`, follows a skill called again with its new arguments,
    names a user-only skill as such and suggests a near match for an unknown name.
    `ToolSearch` ranks keyword queries as Claude Code does and `select:` finds an MCP tool by
    its own name. `ReportFindings` refuses values outside its schema; `Workflow` runs at least
    two agents at once and lists the saved workflows for an unknown name; `ExitPlanMode`
    outside plan mode says what to do next; `EnterWorktree` refuses `.` and `.git` segments.

### Store contract

Third-party `Store` implementations must add both; `noeta.testing.store_contract` checks them.

- **Refuse to end a task over an unread inbox.** An append that ends a task while its inbox
  holds items past the read seq raises `InboxUnread` (a `Conflict`), checked under the task's
  row lock. The runner reads the input and decides again, so a sub-agent can no longer end
  without reading a `hold_lost` and drop a background job silently.
- **`Post.forward_if_ended`.** When set and the target task has ended, the item goes to the
  root's inbox instead (same dedupe key), decided under the target's row lock; `post` returns
  the seq of the inbox it went to. Background jobs' ends and monitor events use it
  (`ToolContext.post(forward_if_ended=True)`).

### Added

- **The rest of Claude Code's deferred tools and frontmatter.** `EnterPlanMode` (asks the
  person outside auto mode), `EnterWorktree` / `ExitWorktree` (the shell moves into a worktree
  under `.noeta/worktrees/`), `ReadMcpResourceDirTool`; `WebSearch` takes `allowed_domains`,
  `blocked_domains` and a required `mode` (its `count` is gone). Skills read `when_to_use`,
  named `arguments`, `${NOETA_SKILL_DIR}`, `` !`cmd` ``, `paths` and `context: fork`, and
  `allowed-tools` pre-approves. Agent files apply `skills`, `mcpServers`, `hooks`, `memory`,
  `autoCompactWindow` and `initialPrompt`, and warn about unknown fields. Hooks gain the `agent`
  and `mcp_tool` types and the PostToolBatch, StopFailure, PostModelSwitch and CwdChanged
  events. `noeta -p` applies `--settings`, `--setting-sources`, `--mcp-config`,
  `--strict-mcp-config` and `--agents`, and a `can_use_tool` answer's `updatedInput` for every
  tool. MCP and Agent results reach Anthropic models as a block list
  (`ToolOutput.block_list`); a reply with unparseable tool-call arguments is retried once.
- **Front ends run on one API, in process or against a server** (`noeta.host.app.AppAPI`):
  ACP and stream-json no longer reach into a local `Runtime`; `noeta acp --server URL`
  drives a `noeta serve` server's tasks. New `POST /commands/run`.
- **Live output across machines**: on Postgres a worker publishes its streamed text
  (`PostgresStore.publish_deltas`, channel `<schema>_deltas`, best effort) and a server's
  `/watch` hears it (`on_delta`), so `noeta serve --no-worker` streams tasks that `noeta
  worker` processes elsewhere run. On sqlite live output stays in its process.
- **Tenants**: a task carries `TaskCreated.tenant` (children inherit it; the `tasks` table
  gains a `tenant` column at store open, existing rows `default`). `Client.scoped(tenant)`,
  `create_app(tokens=)` and `noeta token add --tenant X` (`~/.noeta/tokens.json`) confine every
  read, list, watch, message, answer, signal and cancel to the caller's tenant; a task's
  `SendMessage` / `ToolContext.post` reaches only its own tenant's tasks. The server token
  stays the default tenant's.
- **Per-tenant settings** in `~/.noeta/tenants/<name>/config.toml`: a `workspace` its tasks'
  `cwd` must lie in (and default to), `max_running` (the claim leaves its other tasks
  waiting; exact across workers), `budget_tokens` / `budget_usd` per UTC month (a model call
  past it fails the turn with `budget_spent`; counted in a new `tenant_usage` table), and its
  own `provider` / `base_url` / `api_key` (`{env:}` / `{keyring:}`). `Runtime(tenants=)`,
  `Store.claim(tenant_limits=)`, `create_app(workspaces=)`.
- **Claude Code's headless protocol** (`noeta.host.stream_json`): the stream-json messages
  (`system`/`init`, `assistant`, tool-result `user`, `stream_event`, `result` with usage, cost,
  durations, turns, permission denials and `structured_output`; the root task's id is the
  `session_id`) and control requests (`initialize`, `interrupt`, `set_permission_mode`,
  `set_model`, `rewind_files`, `get_context_usage`, `get_session_cost`, `can_use_tool` to the
  client), so a program written for Claude Code's Agent SDK drives `noeta` (checked with the
  Python SDK). The HTTP server speaks it too: `WS /stream-json` and one-way
  `POST /stream-json`.
- **Rewind with file checkpoints.** The `checkpoints` plugin (main and general-purpose
  agents) records a file's content before `Edit`, `Write` or `NotebookEdit` changes it;
  `noeta.host.rewind` goes back to before a prompt — a new task continuing the log up to it,
  the files restored, or both — from the TUI (`/rewind`, Esc Esc), ACP, HTTP
  (`/tasks/{id}/rewind`) and the headless `rewind_files`.
- **TUI commands with mechanisms**: `/model` (switches the running task's model), `/context`,
  `/cost`, `/memory`, `/config`, `/rewind`, `/clear`, `/resume <n>`; `@path` attaches a file,
  directory or image; a pasted image path or Ctrl+V attaches an image. ACP answers `/model`,
  `/context`, `/cost`, `/rewind`.
- **HTTP**: `/commands`, `/agents`, `/models`, `/tasks/{id}/usage`, `/tasks/{id}/context`,
  `/tasks/{id}/rewind`, `/openapi.json`; `POST /tasks` takes `history_from` /
  `history_through`. `websockets` joins the `server` extra.
- **`noeta serve` always asks for a bearer token**: `NOETA_SERVER_TOKEN`, else one made
  once in `~/.noeta/server-token` (owner-only, printed at start, read by `noeta --server`);
  `--no-token` drops it on a loopback address only. A request from another web origin
  (`--allow-origin` adds one) or with a `Host` other than a loopback name on a loopback
  address is refused, and no request creates or switches a task into `bypassPermissions`
  (`--allow-bypass-permissions`) or runs one outside the server's directory. **`noeta
  --server` started in a directory outside it is refused**: start the server there, or add
  the directory with `noeta serve --allow-root PATH` (repeatable). A relative `cwd` in a
  request is taken from the server's directory, not the server process's, and stored
  absolute (`create_app(allowed_hosts=, origins=, allow_bypass=, cwd=, roots=)`).
- **Kernel**: the `config` signal changes a running task's `model`, `effort` or `max_turns`
  (`CONFIG_SIGNAL`); `config["max_turns"]` fails a turn after that many model calls
  (`max_turns`); `Client.create(history_from=, history_through=)`; `Runtime.preview` (the
  next request, unrecorded); `Command.local` and `run_typed` (a plugin command whose output
  is shown, not sent); an `Answer` deny with `"interrupt": false` gives the model an error
  result and lets the turn go on.
- **Plugins as single files that join agents by themselves**: `Extension.add_to` (`("main",)`,
  `("*",)`) for a file in `~/.noeta/plugins` or a trusted project's `.noeta/plugins` (an
  installed package's is ignored). `/reload-plugins` (`LocalApp.reload_plugins`) re-imports
  plugin files and remakes the agents between turns.
- **`noeta plugin add|list|remove`**: installs from `<git-url>@<commit>` (a commit is
  required), a local path, or a Claude Code `marketplace.json` (`--marketplace`, only read,
  an entry installed at its sha, or at `--commit` when it pins none); nothing from the plugin runs while installing.
- **Claude Code plugin directories** in noeta's plugin places load through the plugins that
  read their formats (skills, agents, hooks, `.mcp.json`; `commands/*.md` through the new
  `markdown_commands` plugin as `/<plugin>:<command>`), with `${CLAUDE_PLUGIN_ROOT}` and
  `${CLAUDE_PLUGIN_DATA}` filled in.
- **Channels** (`noeta.core.channel.Channel`, entry-point group `noeta.channels`,
  `config.toml` `[channels.<name>]`): long-running listeners run by `noeta worker` and
  `noeta serve`, posting idempotently through `deliver()` so replicas are safe.
- **`SendMessage` `notify_when_idle`**: one notice when another open session next goes idle
  or ends (expires after 12 hours); `message` is optional with it. A background agent keeps its
  transcript in `<spill root>/<root>/tasks/<agent id>.output`, named by the launch result and
  the completion notice. `TaskView.enqueue(task_id=)` posts to another task of the tenant.
- **ACP**: `Edit`, `Write` and `NotebookEdit` changes are sent as diffs (old and new text);
  plan mode's plan file is sent as the editor's plan; model and mode are session config
  options, also advertised by a loaded session.
- PyPI metadata in `pyproject.toml` and a `keyring` extra for `{keyring:}` secrets.
- `function_tool(fn)`: a typed Python function becomes a `Tool` (exported from `noeta`).
  Secrets in plugin arguments as `{env:NAME}` / `{keyring:service/user}` (the optional
  `keyring` package), resolved only when the plugin is made. Machine policy:
  `/etc/noeta/config.toml` `[policy] allowed_plugins` / `blocked_plugins` win over every other
  setting.
- **Crash-safe tool calls beyond "outcome unknown"**: `ToolContext.idempotency_key`
  (`<task id>:<call id>`, also sent as MCP `tools/call` `_meta`), `Tool.recover` (look the
  outcome up) and `Tool.retry_safe` (run again, bounded). Read, ToolSearch, WebFetch,
  WebSearch, CronList, ListAgents and the MCP resource tools are retry-safe.
- **Fold snapshots** (`noeta.core.snapshots`, a `snapshots` table in both stores): a claim
  folds the task's newest snapshot plus the events after it; written every
  `Runtime.snapshot_every` (100) events, versioned by the code that folds, never trusted when
  the version differs. A 20,002-event log claims in 21 ms instead of 120 ms on sqlite.
- `noeta worker --lease` and `--snapshot-every`; one worker id `host:pid:random`.
- `make cluster`: worker processes on Postgres under random SIGKILL / SIGSTOP with a scripted
  provider (tool calls, background jobs, sub-agents, timers, Cron, mid-run messages); a CI job
  runs it. Example plugins `examples/run-tests`, `examples/chat-bridge` and
  `examples/house-style`, run by `make example`. Docs: `docs/extending.md`,
  `docs/server.md`, `docs/architecture.md`, `docs/adr/`.

### Fixed

- **An unknown model is announced once.** The front end's notice now also says its cost
  counts as $0.00, and the catalog no longer logs a second "not in the catalog" warning about
  its price.
- **An interrupted reply keeps the thinking it finished.** Only the streamed text was kept;
  completed thinking blocks (signature included) now go back ahead of it
  (`TurnStopped.thinking`; older records fold as before).
- **A background job's end is no longer lost around a sub-agent's end.** A sub-agent could end
  without reading `hold_lost`, and a job finishing just as its sub-agent ended posted its end
  to an inbox no one reads; both now reach a reader (see Store contract).
- **The terminal UI no longer stays "working" after a fast turn.** It marked itself busy only
  once the message was sent, so a turn that ended (and was shown ending) before the send
  returned left it busy until the next one; it is now busy from the moment it sends.
- **A command missing its optional dependency says which extra to install.** `noeta serve`,
  `noeta acp`, the terminal UI, a Postgres `--store` and the other extras' imports exit with
  `pip install 'noeta[<extra>]'` instead of a `ModuleNotFoundError` traceback.
- **A fork of a fork keeps the whole conversation.** A task continuing another
  (`history_from`) folded only that task's own log, so a second rewind, or a sub-agent
  resumed twice through `SendMessage`, lost everything before the first continuation.
- **A continued task's conversation no longer moves.** A task created with `history_from`
  and no `history_through` (`--fork-session`, a resumed sub-agent) folded its source's whole
  log on every load, so it changed whenever the source went on. Creating it now records the
  source's last seq (`noeta.core.store.pin_history`).
- **A continued task keeps the tools its source loaded** with `ToolSearch`; they were no longer
  sent while the history still referenced them, and calls to them were refused. Calls its
  source left open are answered right after their `tool_use`, ahead of messages held behind
  them (the API refused the first request of a fork taken with an interjection).
- **A continued task leaves its source's timers and jobs behind.** Plugin state was copied
  whole: cron jobs and `/loop` wake-ups were listed in the new task and never fired, a
  running workflow could be driven twice. `Extension.continue_state` decides per plugin;
  schedule starts empty, workflow keeps finished runs, shell its cwd and finished jobs.
- **`restore_files` puts back only what the task snapshotted.** The signal (also open as
  `POST /tasks/{id}/signals`) wrote any content to any path. It now writes a file inside the
  workspace only when the task's log snapshotted it and the content hashes to one of its
  snapshots — snapshots of this task, of the conversations it continues and of the
  sub-agents under them (`TaskView.read_log`, `TaskView.child_tasks`), so a rewind still
  puts back what a sub-agent edited. A rewind restores through the task the front end goes
  on with (the fork, when the conversation is rewound), which shows the notice.
- A timer set again for the same name and time fired only once, and the task then spun;
  `TaskView.spawn` without a `key` reused the first child's id; children `view.spawn` staged
  in `after_tool` were never created.
- **An "always" shell approval no longer widens to the program and its first argument.**
  Approving `rm -rf build` used to let `rm -rf /` run unasked (and wrote that rule to a
  trusted project's `.noeta/shell-allowlist.json`). The remembered rule is now Claude Code's
  `Bash(...)` form, seeded as Claude Code seeds it (`git push *`, `ls *`), and the exact
  command for wrappers, destructive file programs and interpreters. The approval card (TUI)
  and the ACP option name the rule (`Always allow Bash(git push *)`), and no "always" choice
  is offered when nothing would be remembered. Approval details carry `always_label`.
- **A command that only reads with `git` cannot run a repository's helper programs.** It
  runs under a `git` shell function that turns off the fsmonitor hook, hooks, pager,
  signature programs and transports, and adds `--no-ext-diff --no-textconv`, wherever it
  reads (`-C`, `cd`). Unless a person approved the command, the function also refuses a
  repository whose git directory lies inside the workspace without being a `.git` directory
  or the one a `.git` file names (a bare one committed into a clone); a bare repository's
  worktree, `--separate-git-dir` and git directories outside the workspace read normally.
  Deny and ask rules match a command's words as they run (`\rm`, `'rm'`, `env rm`, `git -C
  . push`); reads are judged where symlinks lead, and a recursion that follows every link it
  meets (`grep -R`, `rg -L`, `find -L`, `du -L`, `tree -l`, `ls -LR`) asks, as does a glob
  that takes in a link out of the workspace (`cat *` beside `k -> ~/.ssh/id_rsa`: the glob is
  expanded as bash expands it and each match resolved); a write under a
  link an earlier part of the same command copies asks. Auto mode keeps exact `Bash(...)`
  allow rules. **Known limit:** a repository directory unpacked from an archive (not
  cloned) brings its own `.git/config`, whose `filter.<driver>.clean` program `git status`
  and `git diff` still run; git has no setting that turns every filter off. A command a
  person approved runs hardened but without the planted-repository refusal, as Claude Code
  runs an approved command as written.
- **An untrusted project no longer configures the agent.** `noeta` read `<cwd>/.noeta/agents`
  unconditionally, so a cloned repository could replace `main` with one in
  `bypassPermissions` running a command hook. A project's `.noeta/agents`, `.noeta/prompts`,
  `.noeta/skills` and `.agents/skills` now load only after `noeta trust`, like its
  `.noeta/plugins`; each one skipped is a notice. `Runtime.workspace_trusted` gates the
  workspace's prompt overrides (`None`, the default, trusts every workspace).
- **A sub-agent ends after its turn**, like Claude Code's, whatever its agent file says. An
  agent file without `one_shot: true` (every Claude Code agent file) used to leave its
  sub-agent waiting for input and its foreground parent waiting forever. The main agent
  stays conversational.
- **An image that the API would refuse no longer enters the history.** `Read` compared raw bytes
  with 5 MB, so a 4–5 MB image (over 5 MB once base64 encoded) failed every later request of
  the task. Images from `Read` and MCP results now go through `noeta.toolkit.images.fit_image`
  with Claude Code's limits: shrunk into 2000 x 2000 px and 3.75 MB raw with Pillow (the new
  `images` extra), else passed only when already within 5 MB of base64 and 8000 px a side, and
  refused with a reason otherwise. A scaled image's result says how coordinates map back.
  `Read`'s image file ceiling is now 128 MB (`IMAGE_MAX_BYTES`); `noeta.plugins.mcp.results`
  no longer exports `IMAGE_MAX_BYTES`.
- **Long tool output is saved, as the tools promise.** No built-in host set
  `Runtime.spill_root`, so Bash, WebFetch and MCP output past the inline limit was cut
  instead of saved to a file. `LocalApp` now defaults it to `<tmp>/noeta-<uid>/tool-results`
  (`noeta.host.local.default_spill_root`) and the `noeta` command (TUI, `-p`, ACP, `serve`,
  `worker`) to `~/.noeta/tool-results`; one directory per task below it.
- **MCP results and timeouts follow Claude Code.** A result is held to 25,000 tokens
  (`NOETA_MAX_MCP_OUTPUT_TOKENS`; four characters a token, 1,600 an image) instead of 1 MiB: images
  past it are left out with a note, text past it is cut to a marked head and saved.
  A tool call is bounded by the server's `timeout` (milliseconds, read from settings files
  now; under 1000 ignored), else `NOETA_MCP_TOOL_TIMEOUT`, else 1e8 ms, instead of 30 s; every
  other request by `NOETA_MCP_TIMEOUT` (default 30 s). `noeta.plugins.mcp.results` no longer exports
  `INLINE_MAX_BYTES`.
- **Skill arguments are filled in, not dropped.** A skill body's lines holding `$ARGUMENTS`
  were removed. The `skill` tool now takes Claude Code's optional `args` string, and `/name
  args` passes what follows the name; `$ARGUMENTS`, `$ARGUMENTS[N]` and `$N` (from 0) are
  filled as Claude Code fills them (`\$` keeps a `$`), arguments no placeholder took follow
  the body on an `ARGUMENTS:` line, and a call without `args` leaves the body as written.
  The arguments are kept with the activation, so the body re-attached after a compaction is
  the same (`noeta.plugins.skills.registry.substitute_arguments`).
- **Model catalog: the Claude 5.5 family and Fable 5.1.** `claude-opus-5-5`,
  `claude-sonnet-5-5`, `claude-haiku-5-5` and `claude-fable-5-1` are catalogued with Claude
  Code 2.1.312's windows, output caps and prices, and the aliases follow Claude Code
  (`opus` / `sonnet` / `haiku` -> the 5.5 family, new `fable` -> Fable 5.1). An uncatalogued
  model is now a notice the `noeta` command shows (`noeta.core.models.unknown_model_notice`),
  not only a log line.
- `noeta -p` announced an uncatalogued model three times (the loader's log, the front end and
  the catalog's own warning); it says it once.
- `make example` ran a build uv had cached; it now installs the example plugin editable.
- A second `registry.provider()` call for a family failed once that family's adapter module
  had been imported (the entry point named a package attribute the submodule rebound).
- **No request switches a served task into `bypassPermissions` through an answer**: HTTP
  answers (and the stream-json front) go through the same guard as create and signal; send and
  cancel do too.
- **Crashes at the wrong moment no longer lose work.** A worker dying right after a timer fired
  no longer drops a recurring Cron job, a fired one-shot job's deletion or a `/loop` fallback; a
  root that dies after taking `hold_lost` still tells its ended sub-agents' lost jobs; a parked
  MCP call whose worker died ends at once instead of when someone answers; a job of an ended
  sub-agent that is lost is told to the root; a finished job is no longer recorded lost when
  another worker takes its end; a background job counts as running until its end is posted.
- A timer due while a turn is parked fires once the turn ends (as Claude Code fires only when
  idle); documented and tested.
- `--continue` found no conversation behind more than 200 newer tasks.
- The TUI's status-bar cost is the whole task tree's, as `/cost` reports it, and resets when
  another task opens; the TUI no longer shows the hooks plugin's own `retry` records as model
  retries; every front end builds approval cards the same way (`host.pending`).
- After `/compact`, what is given again goes ahead of the command, and a local command's tags
  reach the model as one block each with the next prompt joined to them, as Claude Code sends
  them.
- Workflow end notices keep their dedupe key.
- MCP prompts and resources (`list_prompts`, `expand_prompt`, `list_resources`,
  `read_resource`) use the pool they are given even while it holds no connection; before, an
  empty pool was passed over for the process-wide one.
- A worktree-isolated agent resumed with `SendMessage` after its unchanged worktree was
  removed runs in its caller's directory; before, it was started in the deleted directory
  and its end reported work "kept" at a path that no longer existed.
- A foreground `Bash` command moved to the background is no longer killed after 30–60 s
  when the worker's lease is longer than 90 s: its liveness file is re-created as often as
  its guard checks it, not at the slower pace of the lease.
- The output file a lost job's notice names still exists: a worker sweeps a dead worker's
  log directory only a day after its last write, not as soon as it prepares its own.
- `ExitWorktree` with `remove` removes the worktree `EnterWorktree` made even after the
  shell switched between two other worktrees; the second switch forgot it.
- An `Agent` call with `isolation: "worktree"` that a crash cut short, after its worktree
  was made but before its agent launched, no longer leaves that worktree behind: the call's
  recovery removes it.
- A command hook whose run is cancelled (its turn stopped) is killed with its process
  group, as a timed-out one was; its output is capped while it is read, not after.
- Compaction's request estimate (and the files restored after it) and the MCP result budget
  count a CJK character as one token, as the other budgets do; they counted four characters
  a token, so Chinese, Japanese or Korean text was undercounted three- to four-fold.
- MCP sign-ins that ended are let go three minutes after they started, instead of being
  kept for the life of the worker.
- `noeta -p` in an untrusted directory no longer warns that it skips `.agents/skills` and
  `.noeta/skills` when neither exists; the warning names only the skill directories that do.

### Security

- `noeta serve` takes only `model`, `effort`, `max_turns`, `cwd`, `permission_mode` and
  `output_schema` in a new task's `config`, and only `model`, `effort` and `max_turns` in a
  `config` signal; any other key is refused with 403 naming it. Before, a request could set
  the permissions plugin's inherited allow rules (`permission_rules`) or `subtask_depth` and
  run tools without being asked.
- `POST /tasks/{id}/signals` takes only the signals front ends send (`permission_mode`,
  `config`, `resumed`, `restore_files`). Before, a request could post a plugin's own signal,
  such as a background job's end (a forged `<task-notification>`) or a sub-agent's idle
  subscription naming another tenant's task, which then received the notice.
- `GET /commands` and `POST /commands/run` check `cwd` as a new task's (outside the server's
  directories or the tenant's workspace: 403) and, given a `task_id`, use the directory that
  task was created in. Before, any `cwd` was scanned for skills and could start a trusted
  directory's MCP servers.
- Resuming or forking over `/stream-json` looks the task up through the tenant's client
  first. Before, resuming another tenant's task had this server serve its tree and could
  name its agent in the error.
- A tenant creates tasks only on its own queues: `queues = [...]` in
  `~/.noeta/tenants/<name>/config.toml`, by default `default` and the queue of its own name
  (`create_app(queues=)`). Before, any tenant could push tasks onto another tenant's dedicated
  worker pool.
- `noeta serve` and `noeta worker` refuse to start with tenants besides the default one
  unless the sandbox confines every command (`enabled`, `failIfUnavailable`, no
  `allowUnsandboxedCommands`) or `[server] shared_os_user_tenants = true` accepts tenants
  separated at the API only; a tenant token added later is not accepted by such a server.
  Before, every tenant's tools ran as the server's OS user and could read `~/.noeta`, the
  store and the process environment.
- A task of a tenant other than the default one is denied any file tool call (`Read`,
  `Edit`, `Write`, `NotebookEdit`, a workflow's `scriptPath`) that reaches noeta's home, the
  sqlite store's files, `/etc/noeta`, `/proc` or another tenant's workspace, after symlinks
  and `..` are resolved; no rule, mode (`bypassPermissions` included) or approval lifts it
  (`noeta.toolkit.tenant_fence`; `Permissions(tenant_workspaces=, store_files=,
  spill_root=)`). Such a task keeps its own spilled outputs, its plan files (now under
  `plans/<tenant>/`) and its project memory. Before, file tools ran in the worker process
  bounded only by permission rules the tenant answered itself, so even under the strict
  sandbox a tenant could approve reading the tokens file, the store or other tenants'
  settings.
- The same fence holds when the file is opened: `LocalExecEnv(fence=)` checks the file it
  actually opened for a read and the opened directory a write lands in, and `noeta serve` /
  `noeta worker` give every non-default tenant's task such an env, so a symlink swapped by a
  background command between the permission check and the read cannot redirect it.
- A worker with no OS-level boundary for tenants (no strict sandbox, no `[server]
  shared_os_user_tenants = true`) that claims a task of a tenant other than the default one
  runs none of its tools: the turn fails with `tenant_not_isolated` and the reason before any
  model call (`Permissions(tenant_refusal=)`, set by `noeta serve` and `noeta worker`).
  Before, a worker that knew no tenants (no tokens file or tenant directories, e.g. on
  another machine) started and ran such tasks unconfined.
- A non-default tenant's task shares a repository's memory only with a main checkout that
  holds its working directory, and keeps an agent's `user`-scope memory in
  `.noeta/agent-memory/` of its working directory. Before, a `.git` file naming another
  tenant's repository loaded that tenant's `MEMORY.md`, and `~/.noeta/agent-memory/` was
  shared by every tenant.
- Under the sandbox, a non-default tenant's commands may not read noeta's home (but the
  task's own spill directories, its tenant's plans, its working directory's project memory,
  `skills/` and `workflows/`), the spill directory wherever it is (but the task's own), the
  sqlite store's files wherever they are, `/etc/noeta` or another tenant's workspace; they
  always run inside it (no `excludedCommands`, no `dangerouslyDisableSandbox`, no
  `filesystem.disabled`, no `enableWeakerNestedSandbox`), with `/proc` of their own PID
  namespace (`TenantFence.confine`). Before, the sandbox hid only noeta's home, minus every
  task's spilled outputs and every project's memory.
- A non-default tenant's skill scripts (`run_skill_script`), inline skill commands run
  without `Bash`, `git` for worktrees (with its hooks) and a rewind's deletes run inside its
  sandbox (`noeta.toolkit.sandbox.run_in_tenant_sandbox`). Before, they ran as the worker's
  OS user, so a hook the tenant wrote into its own repository could read the tokens file.
- Read of a PDF by a non-default tenant's task reads the file once through the fence and
  hands the page converters a copy. Before, `pdfinfo` and `pdftoppm` opened the path
  themselves after only its first bytes were checked.
- A non-default tenant's task gets no MCP server that runs a process on the worker (a stdio
  server, a `headersHelper`); they show as down with the reason, also when its slash commands
  are listed (`CommandContext.tenant`). Remote servers connect in a partition of the tenant's
  own (`tenant:<name>/<scope>`), so connections and OAuth sign-ins are never shared across
  tenants. Before, such servers ran as the worker's OS user and tenants shared the default
  tenant's connections unless the host scoped them.
- The tenant fence lets a tenant read the operator's saved workflows (`~/.noeta/workflows`,
  which the `Workflow` tool reads by name) as it reads `skills/`.
- `~/.noeta/server-token` keeps only the server token's SHA-256, like tenant tokens; the token
  is printed once, when made (`noeta serve` no longer prints it on every start). A file an
  older noeta left holding the token itself is rewritten to its digest on the next start,
  which prints the token one last time. Clients (`noeta --server`, `noeta acp --server`) read
  the token from `NOETA_SERVER_TOKEN`.
- The bearer token is taken from `?token=` only on the `/stream-json` WebSocket; every other
  route needs the `Authorization` header, so tokens stay out of ordinary URLs and their logs.
- `noeta plugin add` fetches only from git URLs (`https://`, `ssh://`, `git://`, `file://`,
  `user@host:path`) and passes `--` before them, so a marketplace entry's `url` cannot be a
  git option such as `--upload-pack=…`.
- The git snapshot of the first user turn and the memory plugin's repository lookup run
  inside the sandbox for a non-default tenant's task. Before, they ran as the worker's OS
  user in the tenant's repository, so a `gpg.program` the tenant set in its own
  `.git/config` (with `log.showSignature` and a signed commit) ran outside the sandbox and
  could read the tokens file.
- The first user turn's git snapshot runs no signature program (`-c log.showSignature=false`)
  and does not look inside submodules (`--ignore-submodules=dirty`), so a cloned
  repository's `gpg.program` or a submodule's filter driver no longer runs when a task
  starts; a submodule with changed files but no new commit is no longer listed.
- `noeta serve` logs a `?token=` value as `REDACTED` in uvicorn's access and error logs
  (`noeta.host.http.redact_token_logs`, for host code serving `create_app`). Before, every
  WebSocket connection logged its path with the token at info level
  (`"WebSocket /stream-json?token=…" [accepted]`).
- `noeta serve --allow-host NAME` (repeatable) names the `Host` headers the server answers
  on any bind, added to the loopback names on a loopback bind. Without it a non-loopback
  bind checks no `Host`, which SECURITY.md now says (it claimed unexpected `Host` headers
  were always refused).
- `noeta serve --no-token` refuses to start when a tenant token or tenant directory exists.
  Before, it served every request as the default tenant and ignored the tenants' tokens.
- A command missing an optional extra names the install from this repository
  (`pip install 'noeta[server] @ git+https://github.com/initxy/noeta'`, and the same for
  `uv tool install --reinstall`). Before, it named `noeta[server]` on PyPI, where noeta 1.0
  is not published and the name may install someone else's package.
