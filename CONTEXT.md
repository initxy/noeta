# Noeta

Durable AI agents: every task is an append-only event log with one writer, so a task survives
crashes, waits for a person at no cost, and runs on one process or many machines unchanged.

## Task

The only unit of work. A task has an agent, a config, an event log, an inbox and a row in
`tasks` (a projection of the log written in the same transaction). Sub-agents are child tasks
with `parent_id` / `root_id`; a task may start from another's conversation
(`TaskCreated.history_from`: a resumed or forked sub-agent). There is no session, run or workflow
entity.

## Event log

Ten event types (`noeta/core/events.py`). Plugin state is never a new event type: a plugin records
`ExtensionRecorded(extension, kind, data, message?)` and folds it in its own `reduce`. The core's
own entries use the same event under `extension="core"`, one typed payload per kind
(`noeta/core/records.py`), which the kernel writes and folds them through. The loop
state is a pure fold of the log; the model request is a pure function of that state.

## Inbox

The only way into a task: `Say`, `Cancel`, `Answer`, `ChildEnded`, `TimerFired`, `External`,
each idempotent on an optional `dedupe_key`. Front ends, parents, timers and plugins all post;
only the task's runner reads.

## One writer

A runner claims a task from the store (`claim` / `renew` / `release`; Postgres uses
`SKIP LOCKED`). Every append is conditional on `expected_seq` and the claim's `owner_epoch`, so a
stale runner can never write. A tool call is logged as started before it runs; on recovery an
unfinished call is answered "outcome unknown", never re-run. What lives only in one worker
process (a background job) holds the task tree there (`Store.hold`): other workers skip it until
the hold is released or runs out.

## Layers

Top to bottom, imports only downward: `noeta/cli` (the `noeta` command, the TUI, `~/.noeta`
config) > `noeta/agents` (agent files, the built-in agents) > `noeta/plugins/<name>`
(capabilities) | `noeta/backends` (providers, exec envs, stores) | `noeta/host` (`LocalApp`, and
the ways in: HTTP with `RemoteClient`, ACP) > `noeta/toolkit` (code plugins share) >
`noeta/core`. `|` siblings never import each other, nor do two plugins or two backends
(`lint-imports`). One plugin reaches another only through the kernel: a tool by name
(`TaskView.find_tool`), inherited config (`Extension.inherit`), runnable agents
(`TaskView.agents`), or a record documented as read by others (memory's `dir` for
instructions, shell's `cwd` and permissions' `mode` for permissions and hooks). Front ends
import no plugin; they read what plugins publish (permissions' `modes`). SDK = all but `noeta/cli`.

## Ways in

One API, the client (`noeta/core/client.py`): create, send, answer, cancel, read and watch
events. HTTP (`noeta/host/http.py`, `noeta serve`) is that API on the wire; ACP
(`noeta/host/acp.py`) translates the protocol editors speak into it, and
`noeta/host/stream_json.py` Claude Code's headless protocol (`noeta -p`, the server's
`/stream-json`). A new outside protocol is another translation in `host`, never a second API.
A long-running listener for an outside conversation (a chat) is a `Channel`
(`noeta/core/channel.py`, group `noeta.channels`), run by `noeta worker` / `noeta serve`; its
posts are idempotent (`deliver`), and replies go out from inside the task (a plugin).

## Where code goes

Ask in order, take the first yes: the kernel itself uses it -> `core`; an agent uses it in a
task -> a plugin; several plugins (or backends) share it -> `toolkit`; it is replaceable
infrastructure -> `backends`; it runs noeta or lets the outside in (a protocol) -> `host`; it is
an agent or the agent-file format -> `agents`; it is the `noeta` command or its terminal UI ->
`cli`. Front ends in other languages (web, desktop) live outside the package and use HTTP.
`scripts/lint-layout.py` (in `make check`) fails on any other top-level package and on a `core`
module the kernel never imports, unless listed there as kernel surface with a reason.

## Registry

Plugins and backends are found by name through entry points (`noeta/core/registry.py`): groups
`noeta.plugins`, `noeta.providers`, `noeta.execenvs`, `noeta.stores`, `noeta.channels`. The
built-ins register in `pyproject.toml` exactly as a third-party package would
(`examples/hello-plugin` is one, installed and tested by `make check`). A plugin's registry name
is its `Extension.name`. A plugin (or channel) can also be a plain file in `~/.noeta/plugins` or
a trusted project's `.noeta/plugins` (`registry.load_plugin_files`); an installed plugin wins a
name clash. Such a file joins the agents its `Extension.add_to` names by itself (an installed
package's `add_to` is ignored). `noeta plugin add <git-url>@<commit>` installs one there pinned
(also from a Claude Code `marketplace.json`, only read), running nothing from it.

## Plugin pin

A task records the plugins it runs with (`records.Plugins`: each one's `registry.pin` — name,
source, version or file hash, arguments hash) when it first starts and whenever a pickup finds
them changed. A worker missing a recorded plugin ends the task failed with `plugin_missing`
instead of running it without (`noeta/core/runner/task.py`). A reload (the TUI's
`/reload-plugins`, `LocalApp.reload_plugins`; on servers a rolling restart) re-imports plugin
files and remakes the agents: a task runs the new code from its next turn, recorded so.

## Plugin setting

What a host says about one plugin, by name: its arguments (`enabled: false` among them switches
it off), a ready `Extension`, or on/off. One table at every level (`build_agents`,
`default_agents`, `from_settings`, `config.toml` `[plugins.<name>]`); argument tables of one
plugin merge key by key. `add` (`[agents.<name>] add_plugins`, `"*"` for all) appends plugins
to an agent without copying its file. The built-in agents keep memory and MCP off until set.
A secret is written as a reference, `{env:NAME}` or `{keyring:service/user}`, resolved only when
the plugin is made (`agent_files.resolve_secrets`).

## Config places

noeta reads only its own places: `~/.noeta/` (`NOETA_HOME`), a project's `.noeta/` (most of it
only once trusted), `NOETA.md` and the cross-tool `AGENTS.md`, and the machine's managed
directory (`/etc/noeta`; its `config.toml` `[policy]` allows and blocks plugins by name over
every other setting, `config.PluginPolicy`). The mechanisms and formats inside are Claude Code's
(instruction layering, settings, skills, agents, `.noeta/mcp.json`, plugin directories:
`noeta/cli/claude_plugins.py`); its own places (`~/.claude/`, `.claude/`, `CLAUDE.md`,
`.mcp.json`, `CLAUDE_*` variables) are never read (`noeta/cli/settings_files.py`,
`noeta/plugins/instructions/files.py`). Trusted projects are listed in `~/.noeta/trusted.json`
(`noeta trust`); a trusted project's MCP servers start only with Claude Code's consent keys in
its own settings, so a cloned repository never starts a process by itself.

## Extension

The one capability-plugin interface (`noeta/core/extension.py`): tools, context parts, commands,
reminders, `on_start`, `before_model`, `before_tool` (gates: Deny > Rewrite > Ask > Allow;
an approving Allow satisfies asks that are not firm), `resolve_ask` (answers asks before the
call parks), `after_tool`, `on_event`, `reduce`, `continue_state`, `inherit`, `on_context_overflow`,
`before_compact` (asked by the compacting plugin), `resident`, `lead_resident`. Every method is
optional. Commands (`commands(ctx)` / `run_command`) are slash commands front ends list and
expand through `noeta/core/commands.py`. A `before_model` that raises `TurnDone` ends the turn
without a model call (`/compact`); `RequestStale` has the request built again (a compaction).
A tool asks the person by parking on `ask(...)` (`noeta/core/questions.py`) and tells them
something with `notify`; neither reaches the model as the user.

## Deferred tool

A tool that sets `deferred = True` is named to the model (`TaskView.deferred_tool_names`) and
sent only once the model loads it with `ToolSearch`, which the runner adds while any is offered
(`noeta/core/deferred.py`). MCP tools are deferred by default.

## Agent file

An agent is a markdown file (`noeta/agents/agent_files.py`): YAML frontmatter (Claude Code's
fields, plus `plugins` as names or `name: {arguments}` and `one_shot`) and the system prompt as
body. A sub-agent runs in the background; in auto mode it ends its run with `SubagentHandback`,
whose report reaches its caller as an `agent`-origin message, elsewhere its last reply is the
result in its completion notice (`noeta/plugins/subtasks`). One plugin instance per name and arguments is shared
by every agent naming it. The built-ins are `noeta/agents/files/*.md`.

## Prompt name

Every model-facing text has a name: `system/<agent>`, `identity/<agent>`, `tool/<Tool>`,
`<plugin>/<key>`, `reminder/<key>`. The owning code holds the default; overrides (host prompt dirs <
`<cwd>/.noeta/prompts` < task config `prompts`) are recorded when the task first starts, so a
resumed task sends the same bytes (`noeta/core/prompts.py`).

## Reminder

Text for the model that is not the user speaking. A plugin returns `Reminder`s from
`reminders(view)`, called before every model call; the runner records them and puts them in
that request: `role="user"` ones as `system`-origin text in the user's turn, then `role="system"`
ones as system turns by `order` (adapters join adjacent system turns, as Claude Code sends its
environment and budget notes). Text fixed at task start is captured in `on_start`; what must
wake a waiting task goes through the inbox.

## Park

A tool call that waits (a question, an approval, children) parks: the task ends its step with
`Waiting` and holds no worker. An `Answer` or `ChildEnded` in the inbox resumes it.

## origin

A message's `origin` says who speaks in the user channel: `user`, `system` / `memory` for
text the host or a plugin adds (reminders, notices, recalled memory), or `agent` for another
agent's framed message (a sub-agent's hand-back, sent as plain text), or `inline` for host text
joined into the person's turn as plain text (a typed command's skill body, the interrupted
marker). Providers render `system` / `memory` as
`<system-reminder>` blocks (Anthropic) or with a preamble saying it is not the user (OpenAI
shapes); user text cannot forge the tag.

## Naming: Session is a scope, never an identity

No `session_id`, session list or session-keyed state in `noeta/`; use `task_id` or `root_id`.
Exempt: the ACP adapter, which uses the protocol's own names.
_Avoid_: `SessionStore`, `ConversationManager`, `class Session`

## Naming: Run, Workflow, Mutator, Pattern

Not concepts — say Task. A fixed procedure is an agent plus sub-agents.
_Avoid_: `class Run`, `class Workflow`, `WorkflowRunner`, `class Mutator`, `class Pattern`

## Naming: model-visible tool names

Tool names the model sees follow Claude Code (`Read`, `Edit`, `Write`, `Bash`, `Agent`,
`AskUserQuestion`, `Skill`, `ToolSearch`, `WebFetch`, `Workflow`, …) because models carry their
strongest prior on those names; they are exempt from the bans above. Everything else is
snake_case.

## Naming: enforcement

`scripts/lint-naming.py` (in `make check`) rejects the banned class names and identifiers above
everywhere, and compound `session` identifiers inside `noeta/`. Files that state the bans
(`CONTEXT.md`, `AGENTS.md`, `CHANGELOG.md`, the script) are exempt.
