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
  directory, `filesystem.allowWrite` and a private `$TMPDIR` (never `denyWrite`, `.noeta`
  config, `.git/hooks`, `.git/config`), read everything but `denyRead`, credential stores
  and noeta's home, and reach only `network.allowedDomains` through a filtering HTTP/SOCKS
  proxy; refused hosts follow the output in `<sandbox_violations>`. `Edit` allow rules,
  `additionalDirectories`, `Read` deny rules and `WebFetch(domain:)` allow rules feed it;
  `excludedCommands`, `allowUnsandboxedCommands`, `autoAllowBashIfSandboxed` (default on),
  `failIfUnavailable`, `credentials` (`mask` acts as `deny`), `httpProxyPort` /
  `socksProxyPort`, `enableWeakerNestedSandbox`, `filesystem.disabled` and `bwrapPath` are
  honoured, and a project's settings may tighten the sandbox but never loosen it.
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

### Added

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
- **Kernel**: the `config` signal changes a running task's `model`, `effort` or `max_turns`
  (`CONFIG_SIGNAL`); `config["max_turns"]` fails a turn after that many model calls
  (`max_turns`); `Client.create(history_from=, history_through=)`; `Runtime.preview` (the
  next request, unrecorded); `Command.local` and `run_typed` (a plugin command whose output
  is shown, not sent); an `Answer` deny with `"interrupt": false` gives the model an error
  result and lets the turn go on.

### Fixed

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
  snapshots. A conversation rewind restores through the rewound task; edits a sub-agent made
  are not restored.
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
  `Runtime.spill_root`, so Bash, Grep, WebFetch and MCP output past the inline limit was cut
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
- `make example` ran a build uv had cached; it now installs the example plugin editable.
- A second `registry.provider()` call for a family failed once that family's adapter module
  had been imported (the entry point named a package attribute the submodule rebound).

### Changed

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
  its tree (`Store.hold` / `unhold`, a `holds` table, `ToolContext.hold`), so the next turn can
  read and stop it with several workers on one store; a job whose worker is gone is reported
  lost to the model. Third-party stores implement `hold` and `unhold`.
- **Plugins as files.** `~/.noeta/plugins/*.py` (or a directory) is a plugin without packaging;
  a project's `.noeta/plugins/` loads once trusted (`noeta trust`). `noeta plugins` lists where
  each plugin comes from.
- **Any plugin can ask the person and notify them.** `noeta.ask` / `noeta.read_answer`
  (`noeta.core.questions`, moved from `ask_user`) and `notify` on `TaskView` / `ToolContext`;
  front ends import no plugin. `Mcp` takes servers in Claude Code's `mcpServers` shape.
