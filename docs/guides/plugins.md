# Write a plugin

Everything noeta does beyond running the task log is a plugin, and the built-in ones are written
exactly as yours would be. This page shows how to write one, ship it, configure it and test it.
The interface itself is listed method by method in [Extension interface](../reference/extension.md);
why it is shaped this way is in [The plugin system](../how-it-works/plugins.md).

**noeta sends nothing anywhere except to the model provider you configure.** No telemetry, no
log upload, no update check. A plugin you install can of course make its own requests.

## Pick a shape

A plugin comes in one of three shapes. All three end up in the same registry
(`noeta/core/registry.py`) and run the same way once a task names them.

<Diagram name="guide2-plugin-shapes" />

| Shape | What it is | Where it goes | Joins agents |
|---|---|---|---|
| A plugin file | one `.py` file, or a directory with `__init__.py`, defining an `Extension` subclass with a `name` | `~/.noeta/plugins/`, a trusted project's `.noeta/plugins/`, or `--plugin-dir` for one run | by itself, through its class's `add_to` |
| A package | a Python distribution registering the class under the `noeta.plugins` entry-point group | installed next to noeta | only when an agent file or `config.toml` names it |
| A Claude Code plugin | a directory with `.claude-plugin/plugin.json`, `skills/`, `agents/`, `commands/`, `hooks/hooks.json`, `.mcp.json` | `~/.noeta/plugins/` (or a trusted project's `.noeta/plugins/`) | its parts load through the plugins that read those formats |

Start with a file. Turn it into a package when it needs dependencies of its own or is shared by
many machines.

## A plugin file, in two steps

1. Write `~/.noeta/plugins/<name>.py` defining an `Extension` subclass with a `name`. Set
   `add_to = ("main",)` (or `("*",)` for every agent) and it joins those agents by itself.
2. Start a front end, or type `/reload-plugins` in a running one.

A file that gives the model one tool:

```python
# ~/.noeta/plugins/word_count.py
from typing import Annotated

from noeta import Extension, ToolContext, function_tool


class WordCount(Extension):
    """``timeout``: seconds the count may take."""

    name = "word_count"
    add_to = ("main",)

    def __init__(self, timeout: float = 30) -> None:
        self.timeout = timeout

        @function_tool(name="WordCount", read_only=True)
        async def count(ctx: ToolContext,
                        path: Annotated[str, "a text file, relative to the working directory"]) -> str:
            """Count the words in a text file."""
            res = await ctx.env.run(["wc", "-w", path], timeout=self.timeout)
            return (res.stdout + res.stderr).decode(errors="replace")

        self._tool = count

    def tools(self, view):
        return [self._tool]
```

`function_tool` (`noeta/toolkit/function_tool.py`) turns a typed function into a tool: the name
is the function's (or `name=`), the description its docstring, and the input schema comes from
the annotations (`str`, `int`, `float`, `bool`, `list[...]`, `dict`, `Literal`, `X | None`;
`Annotated[T, "text"]` describes a parameter). A parameter typed `ToolContext` (or named `ctx`)
receives the call's context and stays out of the schema. A sync function runs in a worker
thread and must not call `ctx.record`, `ctx.post` or `ctx.keep`; make it async to use them.

The command runs through `ctx.env`, the task's exec env: on this machine, in the sandbox, or in
a container, wherever the task's commands run ([Where commands run](./sandbox.md)). Never
`subprocess` on the worker for something the task's files are involved in: the next step of the
task may run on another machine.

To write the tool as a class instead, give it `name`, `description`, `input_schema`,
`read_only`, `clearable` and `async def run(self, input, ctx) -> ToolOutput`
(`noeta/core/tool.py`); [`examples/hello-plugin`](https://github.com/initxy/noeta/blob/main/examples/hello-plugin/src/noeta_hello/__init__.py)
does.

To try a file kept elsewhere for one run: `noeta --plugin-dir ./word_count.py` (also with
`-p`, the print mode; a plugin directory or a Claude Code plugin works too, and the flag repeats). It loads into
this process only, so it is refused together with `--server`. A plugin file that joins no agent
is named in a notice that tells you to set `add_to` or `add_plugins`.

`noeta plugins` lists every plugin and where it comes from; a file that failed to import is
listed as broken with the reason, and when it imports a module noeta's environment lacks, with
the `uv tool install --with …` / `pip install` line that adds it (plugins install nothing by
themselves).

## Settings

A plugin's constructor keywords are its settings. They come from `~/.noeta/config.toml`:

```toml
[plugins.word_count]        # the constructor's keywords, by plugin name
timeout = 60

[plugins.web]
enabled = false             # off for every agent (every plugin has this switch)

[agents.main]
add_plugins = ["word_count", {hooks = {}}]   # appended after the agent file's own

[agents."*"]                # every agent
add_plugins = []
```

or from an agent file's `plugins` list, which names plugins in order, each a name or
`name: {arguments}`:

```yaml
plugins:
  - word_count: {timeout: 60}
```

Argument tables of one plugin merge key by key across the levels (`build_agents`,
`default_agents`, `from_settings`, `config.toml`). The full list of keys is in
[Configuration keys](../reference/config.md).

A secret is written as a reference, `"{env:NAME}"` or `"{keyring:service/user}"`, resolved only
when the plugin is made (`noeta/agents/agent_files.py` `resolve_secrets`). A variable named this
way is withheld from every command, hook and MCP server noeta starts.

## Where your code hooks in

Every method of `Extension` (`noeta/core/extension.py`) is optional; implement the ones you
need.

| You want to… | Write | Where it lives |
|---|---|---|
| give the model a tool | `Extension.tools()` returning a `Tool`, or `function_tool(fn)` on a typed function | `noeta/core/tool.py`, `noeta/toolkit/function_tool.py` |
| add a tool built from the other plugins' tools, or keep some out of a request | `Extension.tool_changes(view, offered)` → `ToolChanges` (as `noeta/plugins/tool_search` does for deferred tools) | `noeta/core/extension.py` |
| add system prompt text | `Extension.context()` → `ContextPart` (inside the one cached block) | `noeta/core/extension.py` |
| tell the model something each turn | `Extension.reminders()` → `Reminder` (recorded, so a resumed task resends it) | `noeta/core/extension.py` |
| allow, deny, rewrite or ask about a tool call | `before_tool` → `Allow` / `Deny` / `Rewrite` / `Ask`; `resolve_ask` answers an ask; `after_tool` changes the result | `noeta/core/extension.py` |
| run shell / HTTP hooks in Claude Code's format | settings `hooks`, or a Claude Code plugin's `hooks/hooks.json` (InstructionsLoaded included) | `noeta/plugins/hooks` |
| add a slash command | `commands()` / `run_command()`, or `commands/*.md` in a Claude Code plugin | `noeta/core/commands.py` |
| keep state across restarts and machines | `view.record(name, kind, data)` + `reduce()` (never a new event type) | `noeta/core/extension.py` |
| fold records written under another name (records front ends read by name) | `folds = ("other",)`: those records reach `reduce` too | `noeta/core/extension.py` |
| wake a task later | `view.set_timer(name, at, text)` | `noeta/core/records.py` |
| ask the person something | `ask(...)` (the call parks, the task holds no worker) | `noeta/core/questions.py` |
| add an agent or sub-agent | an agent markdown file (Claude Code's format plus `plugins`) | `noeta/agents/agent_files.py` |
| add skills | `SKILL.md` folders in `~/.noeta/skills`, `~/.agents/skills`, a project's `.noeta/skills` or `.agents/skills`, or a plugin's `skills/`; `*.md` files in `~/.noeta/commands` or a project's `.noeta/commands` | `noeta/plugins/skills` |
| connect MCP servers | `.noeta/mcp.json`, settings `mcpServers`, a plugin's `.mcp.json` | `noeta/plugins/mcp` |
| let a chat app or webhook in | a `Channel` (`run(client, stop)`), posting with `deliver()` | `noeta/core/channel.py` |
| send replies back out | `on_event` in an `Extension` inside the task | `examples/chat-bridge` |
| a new model API | a provider in the `noeta.providers` entry-point group; a `[providers.<name>]` table with `family = "<its name>"` runs it beside the others, routed by model id | `noeta/backends/providers`, `noeta/backends/providers/routing.py` |
| run commands somewhere else (a container) | an exec env in `noeta.execenvs` | `noeta/backends/execenvs` |
| store tasks elsewhere | a store in `noeta.stores`, checked by `noeta.testing.store_contract` | `noeta/backends/stores` |
| a new front end or protocol | the client API (`Client`, `RemoteClient`, HTTP); never a second API | `noeta/core/client.py`, `noeta/host` |
| change each model request, or end the turn without one | `before_model` → `ModelRequest`; raise `TurnDone` (no call) or `RequestStale` (build it again) | `noeta/core/extension.py` |
| capture something once, when a task starts | `on_start` (recorded, so a resumed task sends the same) | `noeta/core/extension.py` |
| pass config to sub-agents, or choose what a fork carries | `inherit`, `continue_state` | `noeta/core/extension.py` |
| start a child task, or post to an inbox | `view.spawn`, `view.post` / `view.enqueue` | `noeta/core/extension.py` |
| use another plugin's tool | `view.find_tool(name)` | `noeta/core/extension.py` |
| call the model from a hook or a tool | `view.complete`, `ctx.complete` (retried, not a step) | `noeta/core/extension.py`, `noeta/core/tool.py` |
| run model work in the background and use it later | start it from a hook, post the outcome as an `External`, record it in `on_event` ([ADR-031](https://github.com/initxy/noeta/blob/main/docs/adr/031-background-plugin-work-returns-through-the-inbox.md)); `TaskState.model_called_at` and `view.next_request` for work between turns (as `noeta/plugins/compaction` does) | `noeta/core/extension.py` |
| show a running call's progress | `ctx.progress(text)`: live only, never recorded | `noeta/core/tool.py` |
| survive a crash in the middle of a call | `Tool.recover` (by `ctx.idempotency_key`) or `flags = ToolFlags(retry_safe=True)`; `ctx.keep()` for work that lives in one process | `noeta/core/tool.py` |
| offer a tool only when the model asks for it | `flags = ToolFlags(deferred=True)` (loaded through `ToolSearch`) | `noeta/plugins/tool_search` |
| keep text across a compaction, or take part in one | `resident` / `lead_resident`; `before_compact`; `on_context_overflow(view, request)` | `noeta/core/extension.py` |
| show a result to front ends as data | `ToolOutput.display` → `Display(kind, data, fallback)` | `noeta/core/tool.py`, `noeta/core/types.py` |
| go on after a reply with no text and no tool call (the turn ends otherwise) | `on_empty_reply(view, response)` → the message to go on with, or `None` (as `noeta/plugins/empty_reply.py` does) | `noeta/core/extension.py` |
| block a prompt, or keep a turn going (as UserPromptSubmit and Stop hooks do) | `reminders` + `view.erase_messages` + raise `TurnFailed`; `on_event` on `ModelReplied` posting `Say(when="now")` | `noeta/plugins/hooks/extension.py` |

Everything a plugin needs is importable from the top package: `from noeta import Extension,
Tool, ToolContext, ToolOutput, ToolUse, Allow, Deny, Ask, Rewrite, Reminder, ContextPart,
Command, CommandContext, Recorded, ToolChanges, TaskState, TaskView, ask, read_answer,
function_tool, text_output` (`noeta/__init__.py`).

## State that survives a crash

A plugin never keeps task state in its own attributes: the next step of the task may run in
another process. It records what happened and folds it back.

```python
class Greeter(Extension):
    name = "greeter"

    def initial_state(self):
        return {"greeted": []}

    def reduce(self, state, kind, data):          # pure: the same log gives the same state
        if kind == "greeted":
            return {**state, "greeted": [*state["greeted"], data["name"]]}
        return state

    async def after_tool(self, view, call, out):
        if call.name == "Greet" and not out.is_error:
            view.record(self.name, "greeted", {"name": call.input.get("name", "")})
        return out
```

`view.record(name, kind, data)` appends an `ExtensionRecorded` event; every claim of the task
folds those through `reduce`, and `view.ext_state(name)` reads the result. Inside a tool,
`ctx.record(...)` does the same, and is dropped if the call fails. Records are the only state:
a plugin never adds an event type. What happens on a crash, a resume or a fork is the same for
your plugin as for the built-in ones ([A task is a log](../how-it-works/event-log.md)).

The other durable things a plugin can do all go through the same log or the inbox:

- **Ask the person.** A tool returns `ToolOutput(park=ask({...}))`; the task waits holding no
  worker, and `resume(input, ctx, outcome)` runs with the answer (`read_answer`). Every front end
  shows the question without any code of yours. `examples/hello-plugin` does this.
- **Tell the person.** `view.notify(text)` shows a notice; the model never sees it.
- **Wake the task later.** `view.set_timer(name, at, text)` is a record; the store fires the
  timer on whichever worker holds the task.
- **Capture something once.** Read it in `on_start` and record it, checking first whether it is
  already recorded. A resumed task then sends the same bytes even if the source changed
  (`examples/house-style`).
- **Named texts.** `view.prompt("<plugin>/<key>", default)` returns the text a person may
  override under that name (`noeta/core/prompts.py`); overrides are recorded when the task
  starts.

## When a hook raises

What the runner does with an exception a hook lets out (`noeta/core/runner/task.py` `_steps`,
`calls.py`, `model.py`, `closing.py`). `TurnFailed`, `TurnDone` and `RequestStale` from
`reminders`, `before_model` or `on_context_overflow` are not failures: they fail the turn with
their reason, end it, or build the request again.

| Hook | When it raises |
|---|---|
| `before_tool`, `resolve_ask` | the call is denied (`permission check failed: …`), as the model sees it |
| a tool's `run`, `resume` | the call's result is an error naming the exception; what the call staged (records, children) is dropped |
| a tool's `recover` | logged; the outcome counts as unknown (a `retry_safe` tool runs again) |
| `after_tool` | logged; the result stays as this hook was given it |
| `on_event`, `resident`, `lead_resident`, `on_empty_reply`, `commands`, `before_compact` | logged and skipped: that plugin has no say this time |
| `run_command` | raised to whoever ran the command (a front end, or the hook that expanded it) |
| `inherit` | raised from `view.spawn`, to the tool or hook that spawned |
| `reduce`, while loading the task | the task is claimed again once its lease runs out; after three such claims in a row it ends failed |
| anything else in a step: `on_start`, `tools`, `tool_changes`, `context`, `reminders`, `before_model`, `on_context_overflow`, `reduce` | the turn fails (`internal_error`); a second failure in a row ends the task failed |

## A package

Register the class in your `pyproject.toml` and install the package next to noeta. The built-ins
do exactly this (`pyproject.toml` at the repository root lists all 27).

```toml
[project.entry-points."noeta.plugins"]
hello = "noeta_hello:Hello"
```

The other groups work the same way: `noeta.channels`, `noeta.providers`, `noeta.execenvs`,
`noeta.stores` and `noeta.frontends`. [`examples/hello-plugin`](https://github.com/initxy/noeta/tree/main/examples/hello-plugin)
is a complete one: a `Greet` tool, state folded from the log, a question to the person, a
reminder with a notice, and a `/greet` command.

An installed package wins a name clash with a file, and its `add_to` is ignored: a dependency
never changes your agents by being installed. Name it where you want it, in an agent file
(`- hello: {greeting: Hi}`) or in `config.toml` (`[agents.main] add_plugins = ["hello"]`).

With `uv tool install`, a package goes into noeta's environment with `--with`:

```sh
uv tool install --reinstall --with ./noeta-hello 'noeta[tui]'
```

A reinstall drops whatever `--with` and extras the last install had, so name them all again.

## A Claude Code plugin

A Claude Code plugin directory (`.claude-plugin/plugin.json`, `skills/`, `agents/`,
`commands/`, `hooks/hooks.json`, `.mcp.json`) put in `~/.noeta/plugins/` loads through the
plugins that read those formats (`noeta/cli/claude_plugins.py`):

| Part | Loaded by | Named |
|---|---|---|
| `skills/` | the `skills` plugin | `<plugin>:<skill>` |
| `agents/**/*.md` | the agent-file reader | `<plugin>:<agent>` |
| `commands/**/*.md` | `markdown_commands` (typed) and `skills` (called by the model) | `<plugin>:<command>` |
| `hooks/hooks.json` | the `hooks` plugin | — |
| `.mcp.json` | the `mcp` plugin; never from a project's plugin directory | the server's alias |
| `settings.json` `agent` (else the manifest's `settings`) | the agent front ends start with, below your own settings | — |

A subdirectory adds `<directory>:` before the name, as Claude Code does. Commands, like skills,
run their `!` lines through the permission checks and pre-approve their `allowed-tools`.
`${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` (`~/.noeta/plugin-data/<name>`) are
substituted in hooks, MCP servers, skills, commands and agent prompts.

Its manifest's `userConfig` is asked for on `noeta plugin add` (or given with
`--config KEY=VALUE`; `noeta plugin configure <name>` asks again). The values are kept in
`~/.noeta/settings.json` `pluginConfigs.<plugin>.options`, with sensitive ones in the OS
keyring (service `noeta-plugin`, account `<plugin>/<key>`) and only a `{keyring:…}` reference
in the file (`noeta/cli/plugin_options.py`). They are substituted as Claude Code does:
`${user_config.KEY}` in MCP server configs and hook commands (every value; a shell-form hook
command may not use one, its `args` may), in commands and agent prompts the non-sensitive ones
only; each hook gets every value as `CLAUDE_PLUGIN_OPTION_<KEY>`.

A secret a machine cannot read stops only what uses it, with a notice: hooks get no variable for
it, an MCP server or hook naming it is left out, and a `PreToolUse` / `PermissionRequest` /
`UserPromptSubmit` hook naming it refuses. Its own files may name only its own keyring secrets.

From a project's `.noeta/plugins/`, a plugin's MCP servers never start: noeta says to copy them
into `.noeta/mcp.json`, which needs consent ([MCP servers](./mcp.md)). Monitors, `bin/`, LSP
servers, themes and output styles are not loaded (the reasons are in
[Compared with other agents](../operations/comparison.md#compared-item-by-item)). noeta never
reads `~/.claude` or `.claude/`.

A Python plugin directory (one with `__init__.py`) may ship `skills/`, `commands/` and
`agents/` beside its code; those load the same way.

## Installing

`noeta plugin` (`noeta/cli/plugin_command.py`) copies a plugin into `~/.noeta/plugins/`, pinned,
and records where it came from in `~/.noeta/plugin-sources.json`.

```sh
noeta plugin add https://github.com/you/your-plugin@<full commit>   # https, ssh, user@host:path or file://
noeta plugin add ./my_plugin.py                                     # a file or directory, copied
noeta plugin add --marketplace ./team-marketplace formatter         # a Claude Code marketplace.json
noeta plugin add --marketplace https://github.com/team/market@<commit> formatter --commit <sha>
noeta plugin configure formatter --config API_URL=https://example.com
noeta plugin list
noeta plugin remove your-plugin
```

<Diagram name="guide2-plugin-install" />

- **A git source names a full commit** (40 hex digits, or 64 for a SHA-256 repository). A short
  one is refused: a commit pushed later could share its prefix. Plain `http://` and `git://` are
  refused: anyone on the way could hand over other code.
- **Installing runs nothing from the plugin**: the clone runs with `core.hooksPath=/dev/null`,
  no submodules, no install scripts, no dependency install; `.git` is removed afterwards. A
  plugin that imports a package noeta's environment lacks is listed as broken
  (`noeta plugins`, `/reload-plugins`) with the line that installs it.
- **A marketplace is only read**, to find where a plugin comes from. An entry fetched from a
  repository installs at its `sha`, or at `--commit <sha>` when it pins none. An entry with only
  a `ref` (a branch or tag) is refused as pinning no commit, and the error names the ref: a
  branch or tag can move, and noeta installs only pinned commits. A marketplace that
  is itself a repository needs `<url>@<commit>`. `github`, `url`, `git-subdir` and path sources
  are supported.
- **A plugin with `userConfig`** is configured as it is added. If configuring fails it is not
  left installed half-configured; `--config` given to a plugin that declares no options is
  refused too.
- The plugin's name is its file's stem, else the manifest's `name`, else the directory's name.
  A name already in `~/.noeta/plugins/` is refused until you `noeta plugin remove` it.

Running front ends pick up an install or a removal with `/reload-plugins`.

### The machine's policy

`/etc/noeta/config.toml` (the managed directory: `/Library/Application Support/Noeta` on macOS,
`C:\Program Files\Noeta` on Windows) can allow and block plugins by name, over every
user and project setting (`noeta/cli/plugin_policy.py`):

```toml
[policy]
allowed_plugins = ["word_count"]   # when set, only these load among the plugins noeta does not ship
blocked_plugins = ["web"]          # never load, shipped or not
```

A plugin file is checked by its file name before it is imported; every plugin, channel and
Claude Code plugin by its name.

## Test it

`noeta.testing` has a scripted model: `ScriptedProvider` answers each model call with the next
step you give it and keeps every request, and `reply(text, *calls)` builds a canned reply. With
an sqlite store in a temporary directory you run a whole task in the test process:

```python
from pathlib import Path

from noeta import Agent, Runtime
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core import registry
from noeta.core.client import Client
from noeta.core.events import ToolFinished
from noeta.testing import ScriptedProvider, reply

PLUGIN_DIR = Path(__file__).parent / "plugins"     # the directory holding word_count.py


async def test_word_count(tmp_path):                 # pytest-asyncio, asyncio_mode = "auto"
    (tmp_path / "a.txt").write_text("one two three\n")
    registry.load_plugin_files([PLUGIN_DIR])
    plugin = registry.load(registry.PLUGINS, "word_count")()
    provider = ScriptedProvider([reply("", ("WordCount", {"path": "a.txt"})), reply("done")])
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={"main": Agent(name="main", model="m", extensions=(plugin,))},
                 provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(tmp_path)))
    client = Client(store)
    task = await client.create("main", "count the words in a.txt")
    await rt.run_until_idle()
    [result] = [r.event.result for r in await client.events(task)
                if isinstance(r.event, ToolFinished)]
    assert "3" in result.text
    assert len(provider.requests) == 2               # what the model was sent, request by request
    await store.close()
```

`provider.requests` holds every `ModelRequest`, so a test can check exactly what a reminder or a
context part put in front of the model. `examples/run-tests/tests/test_run_tests.py` checks a
reminder is sent once and never repeated after a second turn. `make example` runs the tests of
every example.

A store backend runs the shared contract: define a `store` fixture yielding a fresh, empty
store and `from noeta.testing.store_contract import *` next to it.

## Durable and multi-machine

A plugin that follows the rules above already works on many machines. These are the rules the
kernel adds:

- **Pinned plugin set.** A task records which plugins it runs with (name, package version or
  file hash, a hash of the arguments; `registry.pin`) when it starts. A worker that lacks one
  never runs the task without it: it lets its lease run out, so a worker that has the plugin
  picks the task up, and after three such claims in a row by the same worker, with nothing new in the log, it
  fails the task with `plugin_missing` (the count is kept in that worker's memory, so a restart
  starts it again). A changed set is recorded again.
- **Reload.** `/reload-plugins` re-imports plugin files, reads the settings files again and remakes the agents. A turn in
  progress finishes on the old code; each task runs the new code from its next turn. On servers
  the reload is a rolling restart: stop one `noeta worker` at a time (its running tasks go back
  to the store and continue on another worker from their logs), install, start it again. Finish
  within a few leases: a task pinned to a new plugin that keeps landing on a worker without it
  fails after three of its leases. Different code on two workers for the same task is visible
  in its log and never silent.
- **Channels** run in every `noeta worker` / `noeta serve` that enables them; inbound posts are
  idempotent, so several replicas are safe, and replies leave from the task's one writer
  ([Run it as a server](./server.md)).
- **Event delivery.** `on_event` gets each event once, after it is committed; a crash before
  it ran loses it. An extension that records which events it has handled and returns that
  seq from `delivered_through(view)` gets them at least once: each claim of the task first
  passes it, again, every event after that seq (with the task's current state). A `Waiting`
  hands the task back only after every `on_event` for it ran and its records landed, so a crash
  in between leaves the task to be claimed once its lease runs out, and the wait is not written
  twice. A failed side effect (a post that raised) is retried at the next claim, which for a
  waiting task is its next inbox item. The `TaskEnded` that ends the task is the exception:
  nothing claims an ended task, so its `on_event` runs once and records made there are dropped.
  Make the side effect idempotent on the event's seq (chat-bridge's `Idempotency-Key`).
- **Work that lives in one process** (a child process, an open connection) keeps the task's
  claim on that worker with `ctx.keep()`; if the worker dies the next claimer tells the model the
  work is lost ([One writer and crash recovery](../how-it-works/recovery.md)).
- **Policy.** `/etc/noeta/config.toml` `[policy]` wins over every user and project setting
  (above).

## Replacing a built-in plugin

Switch the built-in off (`[plugins.<name>] enabled = false`) and add yours. A plugin that
replaces one front ends read keeps its name and records: `permissions` (`mode`, `modes`: the
mode shown and cycled) and `checkpoints` (`snapshot`: rewind). `plan_mode` writes its switches
as `permissions` `mode` / `plan_exit` records and folds them back (`folds`), so plan mode works
with the permissions plugin off (it then asks about every call that is neither read-only nor
`safe_while_planning`, the plan file's write aside), and the permissions plugin offers no
`plan` mode with `plan_mode` off. A replacement `permissions` plugin therefore never writes
these record kinds with another meaning: `mode`, `plan_exit`, `modes` (front ends,
`plan_mode`), `plan_name`, `noted`, `compacted`, `plan_file`, `plan_file_sent` (`plan_mode`,
from logs before the split), `step`, `tool_call`, `cost` (`budget`, likewise).

Who reads which record is held in one place, `noeta/toolkit/plugin_records.py`.

## Worked examples

Each is one file (hello-plugin a package), a short README and a test `make example` runs:

- [`examples/run-tests`](https://github.com/initxy/noeta/tree/main/examples/run-tests) — a tool:
  runs the test command where the task's commands run, folds the last result from the log,
  reminds the model once after an edit. `[plugins.run_tests] command = "uv run pytest -q"`.
- [`examples/house-style`](https://github.com/initxy/noeta/tree/main/examples/house-style) —
  prompt and reminder text: an output style captured when the task starts, so a resumed task
  sends identical bytes; joins every agent (`add_to = ("*",)`).
- [`examples/chat-bridge`](https://github.com/initxy/noeta/tree/main/examples/chat-bridge) — a
  channel: a chat thread becomes a task, requests carry a shared secret, senders are
  allowlisted, replies relayed from the task.
- [`examples/chat-approvals`](https://github.com/initxy/noeta/tree/main/examples/chat-approvals) —
  a second channel next to chat-bridge: an approval the task waits on is posted to its thread,
  and the chat app's Allow or Deny press answers it.
- [`examples/hello-plugin`](https://github.com/initxy/noeta/tree/main/examples/hello-plugin) —
  the same kind of plugin as a package with an entry point.

The built-in plugins are listed with their settings in [Built-in plugins](../reference/plugins.md).
