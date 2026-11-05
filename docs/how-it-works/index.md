# How noeta works

This section explains noeta's design: what each mechanism is, why it is built that way, and what
happens when something fails. This page is the map; each mechanism has its own page.

## The whole system

noeta is a small kernel that runs tasks from append-only logs, and plugins that give the model
everything it can do.

<Diagram name="core-architecture" />

- **Front ends** — the terminal UI, `noeta -p`, editors over ACP, the web and desktop apps, your
  own Python code — never touch a task directly. They call one API, the client
  (`noeta/core/client.py`): `create`, `send`, `answer`, `signal`, `cancel`, `get`, `tasks`,
  `events`, `watch`, `wait`. In process it is `noeta.Client` on a store; over the network it is the same
  API on HTTP, with `watch` as server-sent events (`noeta serve`, `noeta.RemoteClient`). ACP and Claude Code's headless
  protocol (`noeta -p`, `stream-json`) are translations of it, not second APIs
  (see [Web, desktop and editors](../guides/front-ends.md) and the [HTTP API](../reference/http-api.md)).
- **The store** holds every task: its event log, its inbox, a row in the `tasks` table (a
  projection of the log, written in the same transaction) and its fold snapshots. Two
  implementations ship: sqlite (one machine, several processes may share the file) and Postgres
  (many machines) (`noeta/backends/stores/`).
- **Workers** (`noeta worker`, or the worker inside `noeta serve` and the terminal UI) claim a
  ready task from the store, fold its log, call the model, run tools through plugins and append
  what happened. Any worker can continue any task, because everything a task needs is in its log.
- **The model provider** is reached through an adapter (`noeta/backends/providers/`: Anthropic,
  OpenAI-compatible chat, OpenAI Responses). See [Providers and model routing](./models.md).
- **The exec env** (`ExecEnv`, `noeta/core/execenv.py`) is where a task's tools read files and
  run commands: the local machine, a sandbox, or a container. See
  [Where commands run](../guides/sandbox.md).

The arrows only go one way. A front end writes to a task by posting to its inbox and reads by
reading its log; it never appends events. Only the worker that holds a task's claim appends.

## A task is a log, and other principles

A handful of decisions shape everything else. Each has a page.

| Principle | In one sentence | Page |
|---|---|---|
| A task is a log | Every task is an append-only sequence of ten event types; its state is a fold of the log and the next model request is a pure function of that state. | [A task is a log](./event-log.md) |
| One step at a time | A worker takes the inbox, decides from the folded state, then either calls the model or runs the reply's tool calls through the plugins' gates, appending each result. | [One step of a task](./one-step.md) |
| The inbox is the only way in | Messages, answers, cancels, children ending, timers and plugin signals are all posted, deduplicated, and taken only by the task's runner. | [Waiting and waking](./waiting.md) |
| Waiting costs nothing | A call that needs a person or children parks: the task logs `Waiting`, gives up its claim and holds no worker until something arrives. | [Waiting and waking](./waiting.md) |
| One writer | Every append names the claim's epoch and the sequence number it expects, so a worker that froze and lost its claim can never write again. | [One writer and crash recovery](./recovery.md) |
| No side effect repeated blindly | A tool call is logged as started before it runs; after a crash it is answered "outcome unknown" unless the tool can look the outcome up or is safe to run again. | [One writer and crash recovery](./recovery.md) |
| Every capability is a plugin | The file tools, the shell, permissions, hooks, skills, sub-agents, MCP, memory and compaction are all plugins behind one interface, `Extension`. | [The plugin system](./plugins.md) |
| What the model sees follows Claude Code | Tool names and schemas, prompt layout, reminders and compaction match Claude Code's, checked against recorded requests. | [Matching Claude Code](./parity.md) |

The rest of the section covers the parts that sit on top of these:
[context, caching and compaction](./context.md) (how the request is laid out so the provider's
prompt cache keeps hitting, and how a long conversation is summarized),
[providers and model routing](./models.md), and [many machines](./many-machines.md) (running
workers on several machines over one Postgres store).

## Kernel and plugins

The kernel is deliberately small. It knows how to keep a log, fold it, decide the next action,
call a model, gate and run a tool call, park, wake and recover. It does not know what a file is,
what a permission rule says, or how to compact a conversation. Those are plugins.

<Diagram name="core-kernel-plugins" />

A plugin implements any of the methods of `Extension` (`noeta/core/extension.py`), all
optional: it can offer tools, add text to the system prompt, add reminders before a model call,
rewrite the request (`before_model`), judge each tool call (`before_tool`), change a result
(`after_tool`), watch every event (`on_event`), offer slash commands, and keep durable state.

Plugin state never needs a new event type. A plugin records an
`ExtensionRecorded(extension, kind, data)` entry and folds it back in its own
`reduce(state, kind, data)`. The kernel uses the same event for its own bookkeeping under
`extension="core"` (`noeta/core/records.py`). So a plugin's state is as durable and as
machine-independent as the kernel's: it is in the log, and any worker refolds it.

Plugins are found by name through Python entry points; the built-ins register in
`pyproject.toml` exactly as a third-party package does, and a plugin can also be a single file.
A task records which plugins it runs with, and a worker missing one never runs it without: it
leaves the task to a worker that has the plugin, and fails it only after three such claims in a
row ([plugin pins](./recovery.md#plugin-pins)). The whole mechanism is on
[The plugin system](./plugins.md); writing one is [Write a plugin](../guides/plugins.md).

## Layers

The package is layered, and imports only go downward. `lint-imports` (the contracts in
`pyproject.toml`) and `scripts/lint-layout.py` enforce it in `make check`.

<Diagram name="core-layers" />

| Package | Holds |
|---|---|
| `noeta/core` | The kernel: events, the fold (`loop/`), the runner (`runner/`), the store, client, extension and tool interfaces, the model catalog, retries. |
| `noeta/toolkit` | Code several plugins (or backends) share: paths, shell parsing, output limits, the sandbox, typed readers of other plugins' records. |
| `noeta/plugins/<name>` | Capabilities. A plugin imports only `core` and `toolkit`, never another plugin. |
| `noeta/backends` | Replaceable infrastructure: model providers, exec envs (local, sandbox, container), stores (sqlite, Postgres). Backends never import each other. |
| `noeta/host` | Running noeta (`LocalApp`) and the ways in: HTTP, ACP, stream-json. Never imports a plugin. |
| `noeta/agents` | The agent-file format and the built-in agents (`noeta/agents/files/*.md`). |
| `noeta/cli` | The `noeta` command and its configuration in `~/.noeta`. |

Everything but `noeta/cli` is the SDK. The front ends are separate projects: `tui/` (the
terminal UI, `noeta-tui`) runs on `AppAPI`, in process or on a server with `--server`; `web/` and
`desktop/` talk HTTP.

Three more rules keep the kernel honest:

- `noeta/core/loop/` (the fold, the request builder, the decision) imports no runner, store or
  provider: it is pure, so a log always folds to the same state.
- Two plugins never import each other. One plugin reaches another only through the kernel: a
  tool by name (`TaskView.find_tool`), what a plugin publishes read through a typed reader in
  `noeta/toolkit/plugin_records.py`, or runnable agents (`TaskView.agents`).
- Front ends and hosts import no plugin. They read what they show (the permission mode, a title,
  checkpoints) from the plugins' records by name.

## Where to go next

- [A task is a log](./event-log.md): the event types, the fold, snapshots.
- [One step of a task](./one-step.md): the runner's loop, the model call, tool gates, parallel calls.
- [Waiting and waking](./waiting.md): the inbox, parking, questions, timers, kept claims, cancel.
- [One writer and crash recovery](./recovery.md): claims, epochs, what happens to a model call or a tool call cut short.
- [Context, caching and compaction](./context.md)
- [The plugin system](./plugins.md)
- [Providers and model routing](./models.md)
- [Many machines](./many-machines.md)
- [Matching Claude Code](./parity.md)
- [Events reference](../reference/events.md): every event and its fields.

The decisions behind all of this are recorded as ADRs in the repository
([`docs/adr/`](https://github.com/initxy/noeta/blob/main/docs/adr/index.md)); the vocabulary is
in [`CONTEXT.md`](https://github.com/initxy/noeta/blob/main/CONTEXT.md).
