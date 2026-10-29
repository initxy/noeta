# Changelog

All notable changes to Noeta are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). The 0.x history (packages
`noeta-runtime` and `noeta-sdk`) is in `CHANGELOG.md` at tag `v0.6.42`.

## [Unreleased]

### Changed

- **A stopped turn leaves background jobs running; `TaskStop` sends no end notice.** As in
  Claude Code, stopping the root's turn (Esc, `cancel` with `turn_only`) no longer kills the
  tree's `run_in_background` commands and monitors: they keep running and report their end
  as usual, and still die when the root ends or is cancelled. A job stopped with `TaskStop`
  ends quietly; one killed for another reason still notifies.
- **A plugin's markdown command runs in the task, as a typed skill does.** Typing
  `/<plugin>:<command>` sends Claude Code's command tags, and the task adds the body: its
  `` !`command` `` lines run through the permission checks (one that would ask stops it, with a
  note), and its `allowed-tools` (frontmatter, or a manifest entry's `allowedTools`) are
  pre-approved from then on. `noeta.plugins.skills.inline_commands` and `allowed_tools` moved
  to `noeta.toolkit`.

- **Deferred tool loading is the `tool_search` plugin; the kernel holds no capability.**
  `ToolSearch`, its search, the not-loaded error and `DeferredToolPlaceholder` moved from
  `noeta.core.deferred` (removed) to `noeta/plugins/tool_search`; the requests the model gets
  are unchanged. For plugin authors: a tool still defers with `deferred = True` and is still
  announced through `TaskView.deferred_tool_names`; `TaskState.loaded_tools` is gone (the
  loaded names are the `ToolReference`s in the task's history and open batch). Every agent
  built from agent files runs `tool_search` whether it lists it or not (`[plugins.tool_search]`
  `enabled = false` turns it off); an `Agent` built in code that offers deferred tools adds
  `ToolSearch()` from `noeta.plugins.tool_search` itself, or its deferred tools cannot be
  loaded. A snapshot written before this change is not used (the fold changed): its task folds
  from the log once.
- **Stop every worker before upgrading; workers of different versions must not share a
  store.** This release writes log entries an older worker cannot decode (messages with
  `role="system"`, origins `inline` and `agent`, `ToolReference` blocks), so an older worker
  that picks up a task a newer one touched fails on it again and again. Logs written by
  older versions still load.
- **Stores are at schema version 3 and refuse a newer store.** Holds moved to `tree_holds`,
  and `tasks.tenant`, `tenant_usage` and `snapshots` (keyed by task and fold version) were
  added. The first process of this version moves a version 1 store (sqlite or Postgres)
  forward in place, posting `hold_lost` for the old `holds` rows and keeping that table
  unused (a version 2 store from a development build gets its snapshot rows re-keyed); a store at a later version than the
  code knows is refused with `StoreUnavailable` before anything is written to it (sqlite never
  checked before). Upgrading: stop every worker first; an older sqlite binary does not refuse
  the upgraded store and keeps holding trees in the old `holds` table that newer workers do
  not read.
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
  development build opened keeps its unused `holds` table; upgrading it posts `hold_lost`, with
  epoch 0, to the tasks of each tree held there, and the tree's running jobs recorded without
  an epoch are told lost). A hold is refused while another
  worker runs a task of the tree, and on Postgres a claim and a hold of one tree serialize, so a
  held tree's tasks run only on its holder; a background command refused for either reason
  says so and points the model to the foreground or a later try. A hold also takes back
  another worker's lapsed claim of the tree (a new epoch, back to `ready`), so a worker that
  stalled past its lease cannot renew it or write once the tree is held elsewhere.
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
  row changed; optional `snapshot(task_id, version=None)` / `put_snapshot`; `claim(tenant_limits=)`; `Post.wake`.
  Creating a task with a known id is idempotent under a race (store contract test).
- **The plugin set is pinned per task.** A task records each plugin's name, source (package
  version or file hash) and a hash of its arguments when it first starts and when a pickup
  finds them changed; a worker missing one never runs the task without it: it lets the lease
  run out for a worker that has the plugin (a rolling install), and ends the task failed with
  `plugin_missing` after three such claims in a row with nothing new in the log.
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
- **More tool messages in noeta's own words.** The errors and notes the recordings never show
  still repeated Claude Code's sentences; they now say the same in noeta's words, with the
  same numbers: `Edit` on several matches, the read-before-write refusal, `Read`'s size and
  token ceilings, the PDF page limits, a missing `pdftoppm`, a password-protected or
  unrenderable PDF, a notebook's oversized outputs, the note about a file changed since it
  was read, and `Bash`'s refusal of a long leading `sleep`.

### Store contract

Third-party `Store` implementations must add these; `noeta.testing.store_contract` checks them.

- **Refuse to end a task over an unread inbox.** An append that ends a task while its inbox
  holds items past the read seq raises `InboxUnread` (a `Conflict`), checked under the task's
  row lock. The runner reads the input and decides again, so a sub-agent can no longer end
  without reading a `hold_lost` and drop a background job silently.
- **`Post.forward_if_ended`.** When set and the target task has ended, the item goes to the
  root's inbox instead (same dedupe key), decided under the target's row lock; `post` returns
  the seq of the inbox it went to. Background jobs' ends and monitor events use it
  (`ToolContext.post(forward_if_ended=True)`).
- **`Claim.queue`.** A claim (and its renewal) names the queue of the task claimed; a store
  that leaves it out puts every child on `default`, as before.
- **`claim(tenant_limits=)` counts live leases only.** A running task whose `lease_until`
  has passed does not count toward its tenant's limit.
- **`Store.hold(queues=)`.** Taking a hold with the holder's queues is refused (`None`) while
  an unended task of the tree is on a queue not among them, decided in the hold's
  transaction; `Runtime.hold` passes its queues.

### Added

- **`Extension.tool_changes(view, offered)`.** A plugin changes the task's tool list from the
  tools the other plugins offer, without asking them itself: `ToolChanges` adds tools,
  withholds offered ones from the request (a call to one gets the given error before any gate)
  and adds `ToolEntry`s (request entries that are no tool, placed before a named tool).
  Every extension gets the same `offered`, so the outcome does not depend on plugin order.
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
  `Runtime.snapshot_every` (100) events, versioned by the code that folds (hashed when the
  `Runtime` is made, so code upgraded on disk under a running worker does not change it), never
  trusted when the version differs. One is kept per task and version (`SNAPSHOT_VERSIONS`, 2;
  the one at the lowest seq goes), so workers of two versions during an upgrade do not replace
  each other's. A 20,002-event log claims in 21 ms instead of 120 ms on sqlite.
- `noeta worker --lease` and `--snapshot-every`; one worker id `host:pid:random`.
- `make cluster`: worker processes on Postgres under random SIGKILL / SIGSTOP with a scripted
  provider (tool calls, background jobs, sub-agents, timers, Cron, mid-run messages); a CI job
  runs it. Example plugins `examples/run-tests`, `examples/chat-bridge`,
  `examples/chat-approvals` and `examples/house-style`, run by `make example`. Docs: `docs/extending.md`,
  `docs/server.md`, `docs/architecture.md`, `docs/adr/`.

### Fixed

- The missing-extra message's `uv tool install --reinstall` line names the README's extras
  (`tui`, `images`) with the missing one and says to list every extra wanted. Before, it
  named the missing extra alone, and running it dropped the extras installed earlier.
- **A sub-agent is not left unclaimed behind a background command.** While a worker holds the
  tree (a `run_in_background` command, a monitor), starting an agent on a queue that worker does
  not serve fails with a tool error naming the queue instead of creating a child no worker
  would claim until the command ended. A plugin's `TaskView.spawn(queue=...)` on such a queue
  raises `ValueError` the same way.
- **A plugin can no longer write a `core` record of an unknown kind.** `ToolContext.record`
  and `TaskView.record` raise `ValueError` for one; written, it left the task to a newer
  worker for good.
- **A tenant's running limit no longer counts a dead worker's task.** A running task whose
  lease ran out holds no worker and no longer keeps the tenant's other tasks waiting.
- **A worker missing a newly installed plugin no longer fails tasks during a rolling install**
  (see the pinned plugin set under Added), and a core log record of a kind this version does
  not know is left to a newer worker instead of being skipped.
- **A prompt a `UserPromptSubmit` hook blocks is erased.** It stayed in the conversation, so
  the next turn's request carried it to the model; Claude Code erases it. The hook records a
  core `erased` entry and the fold drops those messages (`TaskView.erase_messages`).
- **`Bash`: a command that opens fd 3 keeps its file, and the shell's directory.** The exit
  trap wrote the final directory to fd 3, into the command's own file; it now uses a
  descriptor out of the command's way (fd 254).
- **`Bash`: a foreground command past the 64 MB log cap shows its real end.** Its log kept
  only the head, so the result showed output from the middle; a foreground log now keeps its
  start and a rolling tail.
- **A resumed agent's worktree is no longer removed when an old job in it ends.** The note
  about a worktree kept for a running job now says a worker restart leaves it in place.
- **An agent's `project` / `local` memory survives `isolation: "worktree"`.** It lived in
  the temporary worktree; it is now under the repository's main checkout.
- **Typing `/skill args` again for an active skill sends it again** with the new arguments
  (a short note when nothing changed), as the `Skill` tool does.
- **Skill directories are scanned once a turn**, not after every `Read`, `Write`, `Edit` and
  `NotebookEdit`.
- **`Edit` and `Write` no longer stall the worker on huge files.** The diff for front ends is
  worked out in a thread and skipped past a million characters.
- **An MCP OAuth refresh that fails on the network keeps the sign-in.** Only `invalid_grant`
  clears the saved tokens, and concurrent refreshes no longer spend a rotated refresh token
  twice.
- **`Read` refuses a notebook over 100 MB before reading it.**
- **`Edit` matches a multi-line `old_string` with straight quotes in a CRLF file** whose text
  has curly ones.
- **`function_tool` accepts a positional-only `ToolContext` parameter.**
- **A blocked prompt's `/command` or `/skill` expansion is erased with it.** The expanded
  body (arguments and `` !`command` `` output filled in) stayed in the conversation and
  reached the next request; the `erased` record now also drops the `inline` text recorded
  from the blocked input on.
- **`UserPromptSubmit` checks a message sent mid-turn before the model sees it, once.** It
  ran only at a turn's first model call, so a `when="now"` message (or one held behind open
  calls) reached the model unchecked, and a block at the next turn erased it after the model
  had answered it. It now runs before any model call that carries new messages.
- **`Bash`: a fast foreground command's output is kept about 3x to 5x faster.** The log
  keeper checked the file's size after every 64 KB read (two processes each); it now takes up
  to 16 reads per check while reads come back full.
- **`Bash`: a foreground command moves to the background past its timeout even with the
  root's job slots full**, instead of being killed.
- **`function_tool` checks scalar arguments** (`str`, `int`, `float`, `bool`, optionally
  `| None`): numbers and `true` / `false` sent as text are converted, and anything else is a
  tool error naming the parameter.
- **Two envs with the same worktree path no longer see each other's jobs.** The jobs working
  under a worktree, and the removals waiting for them, are keyed by the filesystem the path
  names (this machine, or a container) as well as the path.

- **An inbox item this version cannot read no longer fails a turn on every lease.** One a
  newer version posted lets the lease run out with nothing written (a newer worker takes the
  task); a malformed one ends the task `unreadable_log` after the usual claims.
- **A spent budget refuses every model call.** It was checked after the request hooks, so
  `/compact`'s and an automatic compaction's summary ran past it, and an extension's call
  (`TaskView.complete`) was charged but never refused; that call now raises `BudgetSpent`
  (`noeta.core.errors`, a `Fatal`).
- **`ExitWorktree` does not remove a worktree a background job runs in** (it says to stop the
  job with `TaskStop` first), and an agent's worktree kept for a running job is removed, if
  unchanged, once the job ends or is killed with its root.
- **`Workflow` saves its script through the task's env**, so `scriptPath` finds it in a
  container or remote env.
- **A read-only call that fails cancels the calls running beside it** instead of leaving
  them running unowned.
- Smaller: `complete_authentication` on a cancelled sign-in; the MCP prompt commands cache is
  kept for at most 256 partitions; `ReadMcpResourceDirTool` runs again after a crash;
  `function_tool` takes `ToolContext | None` and positional-only parameters; an ended
  background job stops listening for its hold being lost (`TreeHold.on_lost` returns a call
  that takes the callback back).

- **A sub-agent is created on its caller's queue.** Children went to `default` unless a
  queue was named (`Subtasks(queue=)`, a roster entry's `queue`, `spawn(queue=)`), so a task
  on its own queue (a tenant's, a GPU worker's) had children only other workers ran, and a
  tree held by one of them waited for a worker that never came.
- **A tree is held only by a worker that can run all of it.** A worker serving one queue
  could hold a tree (for a background job) whose root was on another; other workers skip a
  held tree, so a `Cancel` or message to the root waited as long as the job ran.
  `Runtime.hold` now raises `HeldElsewhere` while an unended task of the tree is on a queue
  it does not serve.
- **A worker leaves a log a newer version wrote to a newer worker.** An event, block or
  value it does not know (msgspec's "Invalid value" / "Invalid enum value") counted toward
  ending the task `failed (unreadable_log)`; the worker now lets the lease run out without
  counting, so a newer worker claims it. A corrupt or misshapen log still ends failed after
  three claims. Workers from this release on do this; 0.x workers still fail such a task
  (see Changed).
- **A tenant's budget counts every model call it paid for.** Usage was counted after the
  reply was appended, so a reply whose claim was lost meanwhile, or an empty one, was not
  counted; it is counted as soon as the call returns.
- **A hold given back after a store hiccup is asked for once more.** One failed unhold left
  the hold to run out and be reaped, which told the tree (`hold_lost`) that a background job
  that had ended normally was lost.
- **A worker's count of unreadable logs is bounded.** A task whose log failed to load once
  and then ended elsewhere kept its entry in the worker for good; the 1,024 most recent are
  kept.
- **`WebFetch` no longer says a body was "saved to None".** A raw body of a text type
  (from a custom transport) with no spill directory, or one that could not be written,
  was reported, and cached, as saved to `None`; it is now an error, not cached.
- **`NotebookEdit` refuses a `cell-N` that names no cell object.** On a notebook whose
  N-th entry in `cells` is not an object, the call failed with an `AttributeError`; it
  now returns a tool error.
- **`ListAgents` says when its session list may be incomplete.** It lists the open
  sessions among the store's 200 most recently active tasks and said nothing of older
  ones; when that window is full it now says so, and that an older session can still be
  messaged by its id.
- **`SendMessage(notify_when_idle)` finds any open session.** It looked for the session
  among the store's 200 newest tasks, so a session behind them was "unknown"; it is now
  looked up by id (`ToolContext.task`, new, for plugin authors).
- **`function_tool` leaves `*args` and `**kwargs` out of the schema.** They became
  parameters named `args` and `kwargs` the model was told to fill.
- **A sync `function_tool` runs off the event loop.** It ran on the worker's event loop,
  so a blocking function stalled every other task of the worker; it now runs in a thread.
- **The MCP plugin keeps a bounded number of tasks in memory.** Its per-task entries (the
  resolved servers, their tools, roots and scope) were dropped only when the task waited or
  ended here, so a task another worker took, or one left by a crash, stayed for the
  process's life; past 256 tasks the least recently resolved are now forgotten.
- **A skill's `allowed-tools` grant names any tool.** With `tool_enforcement` on, the grant
  kept only names from a fixed table that had drifted (it lacked `Glob`, `Grep`,
  `Workflow`, the worktree, schedule and plan tools, and every MCP tool), and a value in
  Claude Code's space-separated form or `*` granted nothing. The grant is now the tools
  the skill's rules name, read as its pre-approval reads them (`mcp__server` covers that
  server's tools, `Read` covers `Grep` and `Glob`, `*` everything).
- **Without background jobs, `Bash` runs a long leading `sleep`.** `Shell(background=False)`
  refused it with a message pointing at `Monitor` and `run_in_background`, which that host
  does not offer; the refusal now applies only where background jobs are offered.
- **An agent's worktree is not removed under its background command.** An isolated agent
  that ended while a background job it started still ran in its worktree had the
  worktree force-removed under the job (it looked unchanged); it is now kept, and the
  completion notice says why.
- **An isolated agent's worktree goes when its caller ends first.** A task that ended or
  was cancelled while its `isolation: "worktree"` agents ran never heard their ends, so
  their worktrees stayed; they are now closed when it ends (an unchanged one removed).
- **A plugin's markdown command fills its arguments as a skill does.** It replaced only
  `$ARGUMENTS` and otherwise appended what was typed after a blank line; `$0`,
  `$ARGUMENTS[0]` and `\$` now work as in Claude Code, and arguments no placeholder took
  follow the body on an `ARGUMENTS:` line.
- **A long-lived worker keeps a bounded number of task trees' background jobs.** A tree's
  ended jobs and their logs stayed in the worker's table until its root ended here, so a
  server's idle sessions and trees taken by other workers piled up. A tree whose hold this
  worker lost now leaves with its logs, and past 256 trees the least recently used one with
  no running job does.
- **A long session keeps a bounded number of background logs on disk.** An ended
  background job let go of (beyond the 64 most recent of a root task) kept its log files,
  up to 64 MiB each, until the root task ended; they are now removed when it is let go.
- **`ExitWorktree` says when the branch was kept.** It reported the worktree's branch
  deleted even when `git branch -D` refused; it now says the worktree was removed and the
  branch kept, with git's reason.
- **`ToolSearch`'s `+word` filters on tool names only.** It also kept tools whose
  description held the word, which its description (and Claude Code's) rule out.
- **`ToolSearch` takes an infinite `max_results`.** `1e400` (an infinity once parsed) made
  the call fail with an `OverflowError`; it now means the default of 5, like any other
  unusable value.
- **Front ends attached to a server work without a local model.** `noeta --server URL`
  exited with "no model configured" unless this machine named one, and its new tasks (and
  `noeta acp --server`'s) carried this machine's directory, which the server refuses (403
  outside its directories), and the TUI's model, which overrode the server's. They now send
  a model only with `--model` and a directory only with `--cwd PATH` (a directory on the
  server; a relative one is taken from the server's); otherwise the server records its own
  directory (a tenant's: its workspace) in the task and the agent's model applies.
  `noeta -p --server` ran locally and silently ignored `--server`; it now refuses it.
- **A hold whose commit was cut off is no longer left behind.** When the store's connection
  dropped while a hold committed (`CommitUnknown`), a hold that had landed was neither
  registered nor renewed: the tree stayed unclaimable until the hold ran out and its reaping
  told the tree `hold_lost` for nothing. `Runtime.hold` now asks again (up to three times);
  taking a hold extends the one the worker has, so a hold that landed comes back with its
  epoch and is renewed and given back as usual.
- **An agent's `denied_tools` / `allowed_tools` also cover tools a plugin adds from the
  others' (`Extension.tool_changes`).** An agent that denied `ToolSearch` was still offered
  it and ran it. An added tool the lists refuse is now neither sent nor run, and the rest of
  that plugin's changes go with it; the deferred tools it withheld are sent as plain tools
  and not announced (an agent that denies `ToolSearch` gets its deferred tools up front, as
  Claude Code does when its search tool is unavailable). `TaskView.deferred_tool_names` also
  leaves out denied tools.
- **Crash recovery no longer removes another agent's worktree.** An isolated agent's
  worktree was named after the last characters of the call id, which is unique only within
  its task, so a crashed call's recovery could force-remove the worktree of another task or
  session in the same repository. The name now comes from a hash of the task id and the call
  id, and recovery keeps a worktree holding uncommitted files or commits.
- **A worktree-isolated agent can commit with the sandbox on.** Its worktree's git data
  lives in the main repository, which the sandbox kept read-only (`index.lock: Read-only
  file system`). The worktree's own git directory and the shared `objects`, `refs` and
  `logs` are now writable; hooks, config and the files that point git elsewhere stay
  read-only (`parity/deviations.md`).
- **Removing a worktree no longer drops commits left on its branch.** The unsaved-work check
  (`ExitWorktree remove`, an isolated agent's end) counted only commits on the worktree's
  current `HEAD`, so after a switch to another branch the commits on the `worktree-*` branch
  were lost with `branch -D`. It now counts both.
- **A long-lived conversation no longer piles up ended background jobs in memory.** The
  worker kept every ended background job of a root until the root ended; it now keeps the
  last 64 once their end is posted (older logs are removed with the root's), and the list of
  ended tasks forgets its oldest entry first instead of an arbitrary one.
- **Two writers of one MCP sign-in no longer lose tokens.** The OAuth token file was written
  through one fixed `.tmp` name and updated read-modify-write without a lock, so two workers
  refreshing at once could drop a field or fail on the rename. Each write uses a temporary
  file of its own and updates hold a file lock (`<file>.lock`; no lock on Windows).
- **Two per-worker caches are bounded.** The shell plugin's sandboxed tool lists (one per
  sandbox settings and working directory) and the agent rosters resolved per set of named
  agents grew for the life of a worker; each now keeps the 64 used last.
- **A sub-agent that ends without handing back reports with its final message.** In auto
  mode a sub-agent that ended without `SubagentHandback` came back as "no report" and its
  last reply was dropped: one whose agent definition lacks the subtasks plugin, whose tool
  list leaves the tool out, that runs on another worker's queue, or that simply answered in
  plain text. Its final message is now its report, as in Claude Code.
- **An unknown model is announced once.** The front end's notice now also says its cost
  counts as $0.00, and the catalog no longer logs a second "not in the catalog" warning about
  its price.
- **A task whose log cannot be read no longer loops forever.** A log no claim could read or
  fold left the task `running` to be claimed again on every lease; after three such claims in
  a row on a worker it now ends `failed` (`unreadable_log: ...` with the last error), its
  parent gets `ChildEnded` and its children are cancelled. Only a failure to load counts: one
  raised later (failing the turn failed too) is logged and the task claimed again.
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

- **A sandboxed command can no longer reach into the network relay it runs under.** The
  relay inside the Linux sandbox (same user, same PID namespace, no system-call filter, and
  holding the proxies' Unix sockets) was dumpable, so where `kernel.yama.ptrace_scope` is 0 a
  command could trace it, and under any setting read its `/proc` files. The relay is now not
  dumpable, and the command's filter refuses `ptrace`, `process_vm_readv`,
  `process_vm_writev` and `pidfd_getfd` whatever the settings allow.
- The sandbox's filter refused `socket(AF_UNIX)` but let a command set up io_uring, which
  creates sockets without `socket(2)` on Linux 5.19 and later. `io_uring_setup`,
  `io_uring_enter` and `io_uring_register` are now refused unless Unix sockets are allowed.
- **A tenant's agent memory can no longer link to a protected file.** An agent's `project` /
  `local` memory is in the tenant's workspace, and the memory plugin read its `MEMORY.md`
  and its memory files (recall's listing and the files it attached) in-process, past the
  tenant fence, so a link there put a server file into the context. For a task of a
  non-default tenant these reads now go through the task's fenced env, which refuses the
  file it opens; so does the instructions plugin's read of `MEMORY.md`.
- A tenant's skills and rules no longer list what a link leads to behind the fence. A
  skill's resources and the rules directory's files are listed without what the tenant
  fence refuses (their contents already were read through the fence), and the skill
  commands a front end lists for a non-default tenant skip a `SKILL.md` that resolves out
  of its skill directory, read with no task env to fence it.
- `noeta serve` and `noeta worker` with tenants besides the default one refuse to start
  when a server directory (where they started, `--allow-root`, `--server-dir`,
  `[server] dirs`) holds the machine's programs or temporary directories (`/`, `/var`,
  `/opt`, ...): it cannot be hidden from a tenant's commands, and was silently left
  visible.
- **An agent hook's tool calls go through the task's permission checks.** It ran every
  read-only tool (`Read`, `WebFetch`, `WebSearch`, read-only MCP tools) with no check; now a
  deny refuses the call and so does anything that would ask, as Claude Code runs an agent
  hook in `dontAsk` mode.
- **A tenant's commands no longer keep the operator's Unix-socket and user-namespace
  allowances.** With `network.allowAllUnixSockets`, `network.allowUnixSockets` or
  `allowNestedUserNamespaces` in the settings, a tenant's sandboxed commands had them too, so
  they could open the user's D-Bus (`systemd-run --user` ran a command outside the sandbox
  that read `~/.noeta/tokens.json`), Docker's or Postgres's socket. `TenantFence.confine` now
  clears all three for a task of a tenant other than the default one.
- **A tenant with its own model endpoint no longer borrows the server's key or headers.**
  A tenant directory with `provider` or `base_url` but no `api_key` got a provider that fell
  back to the worker's `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` (the server's key, sent to the
  tenant's endpoint), and every tenant provider carried the server's `extra_headers`. Such a
  tenant must now set a non-empty `api_key` (`noeta serve` and `noeta worker` refuse to start
  otherwise), and its provider gets only the `extra_headers` table of its own config.
- On Linux machines other than x86_64 and aarch64 the sandbox's system-call filter (no Unix
  sockets, no new user namespaces) was silently left out. The sandbox now reports itself
  unavailable there unless the settings allow both, so `failIfUnavailable` refuses the
  commands, a tenant's always (its settings never allow them).
- **A tenant's commands no longer follow a working directory swapped for a link.** The
  `cwd` was checked only when the task was created; the tenant could then replace a
  directory on the way with a link to noeta's home or another tenant's workspace, and its
  sandboxed commands got that place as their writable root (with noeta's home, its tokens
  and settings, and hooks in those settings run unsandboxed). The sandbox now resolves the
  working directory for every command of a tenant's task and refuses the command unless it
  lies in the tenant's workspace and outside the places its commands may not read, instead
  of making such a place writable. The permissions plugin also resolves it before every
  model call and fails the turn with `tenant_cwd_refused` and the reason, and `Bash`
  resolves the directory a command left the shell in before starting the next one there
  (one now leading out of the workspace starts back at the root).
- **A worker runs no tools of a tenant whose workspace it does not know.** Such a worker
  (one with an older copy of the tenant directories) still ran the task with the sandbox
  in place; the turn now fails with `tenant_not_isolated` before any model call, as on a
  worker without a boundary. Workers read the tenant directories at start: restart every
  worker after adding a tenant (`docs/server.md`).
- A tenant's file tools write only its commands' `TMPDIR` (`tmp/`) in the tenant's
  temporary directory. Before, they could write anywhere in it, its background jobs' logs
  and state files (`jobs/w<pid>/*.output`, `.alive`, `.cwd`) and its commands' sandbox files
  included.
- `noeta plugin add` takes only a full commit id (40 hex digits, or 64 for a SHA-256
  repository) and only `https://`, `ssh://`, `user@host:path` or a local `file://` source,
  for a plugin and for a marketplace and its entries. Before, a 7-character prefix (which a
  commit pushed later could share) and `http://` or `git://` (which anyone on the way could
  answer with other code) were accepted.
- An instruction file's `@import` is read only when it resolves inside the importing
  file's directory or the working directory. Before, the check used the path as written, so
  a repository's link (`docs/x.md` -> `~/.ssh/id_rsa`) put the target's text into the
  model's context.
- **A tenant's task may not run in a server directory inside its workspace.** Its `cwd`
  would be its sandboxed commands' writable root, so they could read and change the
  server's files there; the HTTP guard now refuses such a `cwd` as it refuses another
  tenant's workspace.
- **A child continues only its own tenant's conversation.** A tool's
  `spawn(history_from=)` could name another tenant's task, and the child folded that
  conversation in; the call now fails with `UnknownTask` and stages no child, as
  `Client.create(history_from=)` already refused it.
- **noeta's home and the sqlite store are owner-only when noeta creates them.** `~/.noeta`
  (`NOETA_HOME`) was made 0775 and the store's database with its `-wal` and `-shm` files
  0644 under the usual umask, though the store holds every conversation. noeta now makes its
  home 0700 and a new database 0600 (SQLite gives the side files the same mode). Existing
  ones keep their mode; a front end, worker or server whose home or store is open to other
  users says so at start, with the `chmod` to run.
- An extension's `TaskView.read_log` reads another tenant's task as empty and
  `TaskView.child_tasks` lists only children of its own tenant, as `ToolContext.post` and
  `ToolContext.tasks` already did. Before, a plugin given another tenant's task id could read
  its log.
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
- A task of a tenant other than the default one keeps its background job logs and its
  sandboxed commands' `TMPDIR` in its tenant's own temporary directory
  (`<temp>/noeta-temp-<uid>/tenants/<tenant>/`, made 0700), and neither its file tools nor
  its sandboxed commands can read noeta's shared temp places (`noeta-background-<uid>` in
  each temp directory, `noeta-temp-<uid>` but its own). Before, every task's command output
  (`noeta-background-<uid>/w<pid>/<job>.output`) and a worker's shared `TMPDIR`
  (`noeta-sandbox-*`) were readable, and the latter writable, by every tenant's tasks. A
  worker's sandbox directory moves to `<temp>/noeta-temp-<uid>/sandbox/`.
- A tenant other than the default one needs a workspace: `noeta serve` refuses to start
  while a tenant in `tokens.json` or `~/.noeta/tenants/` has none, `noeta token add` refuses
  such a tenant, and the HTTP API (`create_app`) answers 403 when one without a workspace
  starts a task or names a `cwd`. A request's `cwd` inside another tenant's workspace is
  refused even where that lies inside the requester's own. Before, a tenant with only a
  token could start tasks anywhere under the server's directory and `--allow-root` paths,
  another tenant's workspace and the default tenant's included.
- A tenant other than the default one keeps its auto memory and compaction transcripts in
  a project directory of its own, `~/.noeta/projects/<tenant>/<slug>/`; the default
  tenant's stay in `projects/<slug>/`. Before, every tenant working in one directory shared
  its `projects/<slug>/` (memory read into the prompt and writable by each, transcripts of
  each other's tasks).
- In its project directory a tenant's task reaches only the memory (`memory/`) and, to
  read, the compaction transcripts of its own task and its root (`<task id>.jsonl`), with a
  file tool and in the sandbox. Before, the whole directory was open, so the transcripts of
  every other task working in that directory could be read and rewritten.
- A started workflow's script is kept in its task's own spill directory
  (`tool-results/<task id>/workflows/`). Before, every task's went to one shared
  `tool-results/workflows/`, which a tenant's task could not read back.
- `Read` of a PDF by a tenant's task makes its copy and rendered pages in the tenant's own
  temporary directory. Before, they went to a fresh directory in the worker's shared temp
  directory, readable by other tenants' sandboxed commands while the conversion ran.
- A non-default tenant's file tools never reach the server's own directories (the
  directory `noeta serve` / `noeta worker` started in and `--allow-root` paths), the
  sandbox's credential stores (`~/.ssh`, `~/.aws`, `~/.config/gh`, `~/.netrc`, ...) in
  every home directory, or the operator's sandbox `denyRead`, besides the places they were
  already fenced from; they write only in the tenant's workspace, its own corners of
  noeta's places and its temporary directory (`noeta.toolkit.tenant_fence`;
  `Permissions(server_dirs=, deny_read=)`). The rest of the machine stays readable. No rule,
  mode or approval lifts it, and the operator's `additionalDirectories` are no longer a
  tenant's working directories. Before, a tenant could approve its own
  `Read ~/.ssh/id_rsa`, `Read <server dir>/.env` or a write anywhere the OS user could.
- Under the sandbox, a non-default tenant's commands cannot read those places either (but
  a workspace inside a server directory), and write only where its file tools may: the
  operator's `allowWrite` / `allowRead` / `additionalDirectories` do not apply to them
  (`TenantFence.confine`). Before, they read the server's directories and other accounts'
  credential stores.
- An MCP prompt command (`/mcp__<server>__<prompt>`) runs in the connection partition of the
  request that runs it: listed prompts are kept per partition. Before, they were kept by
  command name only, so after another tenant listed the same command, a tenant's
  `/commands/run` used that tenant's connection and OAuth sign-in.
- `POST /tasks/{id}/rewind` creates the new task through the request's guard: only the
  config a request may set is carried over, `bypassPermissions` and a `cwd` outside the
  tenant's workspace are refused (403), and the tenant's queues apply. A rewind keeps the
  task's queue (`noeta.host.rewind.rewind(config_keys=)`). Before, it copied the original's
  whole config through the unguarded client, so a tenant could rewind a task host code made
  with plugin keys (`permission_rules`) or a mode a request may not ask for, on the
  `default` queue.
- A non-default tenant's sandboxed commands cannot read or write a server directory (the
  worker's working directory, serve's `--allow-root`) that lies inside the tenant's
  workspace. Before, `TenantFence.confine` left such a directory out of what it hides, so
  the tenant's `Bash` read and overwrote the server's files there while its file tools were
  refused.
- A credential store created after a non-default tenant's task started (a new `~/.aws`,
  `~/.netrc`, ... in any home directory) is hidden from that task's next sandboxed
  command. Before, `TenantFence.confine` hid only the ones that existed when the task's
  environment was built.
- `noeta serve` keeps a query-string token out of uvicorn's logs whatever way its key is
  spelled: `?%74oken=` (which the server decodes to `token` and accepts) is redacted too.
  Before, only a literal `token=` was.
- `noeta worker` hides `noeta serve`'s directories from other tenants' tasks when told
  where they are: `[server] dirs = [...]` in `config.toml` (read by both) or
  `noeta worker --server-dir PATH` (repeatable). A worker also refuses `bypassPermissions`
  unless started with `--allow-bypass-permissions`, as `noeta serve` does. Before, with
  `noeta serve --no-worker` and separate workers, serve's working directory and
  `--allow-root` paths were readable to other tenants' tasks (and a worker started in `/`
  hid no server directory), and a worker's tasks could be switched into bypassPermissions.
- **A tenant's slash commands over `/stream-json` run as that tenant.** The stream-json
  front (WebSocket and POST) listed and ran commands as the default tenant, so a tenant's
  `/mcp__…` command started the server's local stdio MCP servers and used the default
  tenant's connections; a scheduled `/loop` command expanded the same way. For plugin
  authors: `CommandContext.tenant` is now required (keyword only), so every caller names it.
- **A continued task no longer keeps bypassPermissions where bypass is disabled.** A fork,
  rewind or resumed fork of a task that ran in bypassPermissions (on the CLI, say) kept that
  mode on a server started without `--allow-bypass-permissions`, and ran tools without
  asking. The stored mode now goes through the same check as a task's config, and a
  continued task's state drops bypassPermissions there.
- **A shell command that writes a protected path asks in bypassPermissions.** A `Bash`
  command writing `.git/hooks`, `.noeta/settings*.json` or another protected path (a file
  command's target or an output redirection, as written or through a link) ran without
  asking in bypassPermissions; it now asks there, as the file tools already did. In auto
  mode the classifier still judges it, as Claude Code's safety check allows.
- A compaction's transcript file is named from the task id with anything but letters,
  digits, `_`, `.` and `-` replaced, as spill files are. A sub-agent's id carries the
  provider's tool-use id, so a gateway answering with `../` in it could have the transcript
  written outside the project directory. A spill file of a task or call named `..` now
  stays in its directory too.
- `Workflow`'s list of saved workflows (named when a name is not found) no longer lists a
  tenant's `.noeta/workflows` when it is a link to a place the tenant fence refuses, such as
  another tenant's workspace: the listing ran `ls` outside the fence.
- A tenant's memory directory is no longer created where the tenant fence refuses writes:
  the worker made it with `mkdir -p` itself, following a link in the tenant's `.noeta/` (or
  a repository's shared directory outside the workspace) to a place outside it.
- The HTTP server and `/stream-json` control requests no longer send a client the text of an
  error that a library or the system raised (it may name the server's paths); the client
  gets a generic message and the server logs the error. Errors noeta raises for the caller
  keep their text.
- In auto mode, a tenant's tasks no longer send the server's home directory, OS user name or
  permission rules (the operator's among them) to the tenant's model provider: the
  server-side check's context leaves them out, and the classifier is told the tenant's name
  as the user.
- A tenant's `Read` of a PDF no longer always fails: its copy and pages were made in the
  tenant's temporary directory itself, where the fence lets file tools write nothing; they
  now go in its `tmp/`.
- On macOS and Windows the tenant fence compares names without case, so `~/.SSH` or another
  tenant's workspace spelled in other case is refused like the name it reaches. On Linux it
  compares with case (documented in docs/server.md "Not a boundary" for case-insensitive
  file systems mounted there).
