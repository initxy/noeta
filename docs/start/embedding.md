# Embed it in Python

The `noeta` package is also a library. This page shows how to run the agent from your own
program: create tasks, follow their events, answer their approvals and questions, add a tool,
and connect to a store or a server. Every name used here is listed in
[Python API](../reference/python-api.md).

Everything an embedder needs is outside `noeta.cli`, which is the `noeta` command itself.

<Diagram name="start-embedding" />

## The parts

- **A store** (`noeta.store(url)`) holds every task: its event log, its inbox and its row.
  `sqlite:///path` for one machine, `postgres://…` (the `postgres` extra) for many.
- **Agents** (`noeta.default_agents(model)`) are `main` and the three sub-agents `Explore`,
  `Plan` and `general-purpose`, built from the same agent files the `noeta` command uses.
- **A provider** (`noeta.provider(family, …)`) talks to the model: `anthropic`,
  `openai_compat` or `openai_responses`.
- **A `Runtime`** is a worker: it claims tasks from the store, runs them step by step, and
  releases them when they wait or end.
- **A `Client`** creates tasks and talks to them: `create`, `send`, `answer`, `signal`,
  `cancel`, `get`, `tasks`, `events`, `watch`, `wait`. It never runs anything; it writes to the
  store and the runtime picks the work up.
- **`noeta.LocalApp`** is a store, a runtime serving in the background and a client in one
  object. It is what the terminal UI, `noeta acp` and `noeta -p` run on.

## A first program

```python
import asyncio
import noeta
from noeta.core.events import TaskEnded

async def main() -> None:
    store = noeta.store("sqlite:///tasks.db")
    agents = {a.name: a for a in noeta.default_agents("claude-sonnet-5-5", one_shot=True)}
    model = noeta.provider("anthropic", api_key="sk-ant-...")   # base_url=... for a gateway
    runtime = noeta.Runtime(store=store, agents=agents, provider_for=lambda _model: model)
    client = noeta.Client(store)

    task = await client.create("main", "What does this repository do?",
                               config={"cwd": ".", "permission_mode": "default"})
    await runtime.run_until_idle()          # run until nothing is claimable

    for rec in await client.events(task):
        if isinstance(rec.event, TaskEnded):
            print(rec.event.status, rec.event.answer)

asyncio.run(main())
```

What each line does:

- `one_shot=True` replaces a field of the `main` agent: the task ends when its first turn ends,
  and `TaskEnded.answer` holds the last reply. Without it a task is a conversation: after
  each turn it waits for the next message (`Waiting` on `"input"`), and you `send` it more.
- `config` is the task's config. `cwd` is where its tools work. `permission_mode` is one of
  `default`, `acceptEdits`, `plan`, `auto` (the default), `dontAsk`, `bypassPermissions`. In
  `auto` a classifier model call judges actions that would otherwise ask; in `default` they
  wait for an answer (below). Other keys: `model`, `effort`, `max_turns`, `output_schema`,
  `timezone`.
- `run_until_idle()` claims and runs tasks until none is claimable. A task that waits for an
  approval is not claimable, so the call returns; answer it and call again.
- `provider_for` maps a model id to a provider. With one provider it can ignore the id.

## Everything in one object: `LocalApp`

`LocalApp(store, agents, provider)` takes the agents as a list and the provider as one
provider or a `provider_for` function. Started (or used with `async with`), it serves tasks in
the background, so you follow them instead of driving the runtime:

```python
import asyncio
import os
from collections.abc import Sequence

import noeta
from noeta.core.events import TaskEnded, ToolStarted, Waiting
from noeta.host.pending import pending_tree

@noeta.function_tool
def word_count(text: str) -> str:
    """Count the words in a text."""
    return str(len(text.split()))

class Counting(noeta.Extension):
    name = "counting"

    def tools(self, view: noeta.TaskView) -> Sequence[noeta.Tool]:
        return [word_count]

async def main() -> None:
    model = noeta.provider("anthropic", api_key=os.environ["ANTHROPIC_API_KEY"])
    agents = noeta.default_agents("claude-sonnet-5-5", extensions=[Counting()], one_shot=True)
    async with noeta.LocalApp(noeta.store("sqlite:///tasks.db"), agents, model) as app:
        task = await app.client.create("main", "How many words are in 'a b c'?",
                                       config={"cwd": os.getcwd(), "permission_mode": "default"})
        async for rec in app.client.watch(task):          # ends when the task ends
            event = rec.event
            if isinstance(event, Waiting) and event.on == "approval":
                for item in await pending_tree(app.client, task):
                    print(f"allowing {item.tool} {item.input}")
                    await app.client.answer(item.task_id, item.request_id, "allow")
            elif isinstance(event, ToolStarted):
                print("running", event.call.name)
            elif isinstance(event, TaskEnded):
                print(event.status, event.answer)

asyncio.run(main())
```

This program, run against a scripted model, prints:

```text
allowing word_count {'text': 'a b c'}
running word_count
done Three words.
```

- `function_tool` turns a typed function into a tool: its name, its docstring as the
  description, its parameters as the input schema. `read_only=True` lets it run alongside
  other read-only calls.
- `extensions=[…]` adds an `Extension` to every agent. A plugin can offer tools, context,
  reminders, slash commands and gates on tool calls; [Write a plugin](../guides/plugins.md)
  and [Extension interface](../reference/extension.md) have the rest.
- `watch(task, after=0)` yields every recorded event from `after` on, live, and returns once
  the task has ended. Each item is a `Recorded` (`seq`, `at`, `event`); the ten event types
  are in [Events](../reference/events.md).

### Live output

Recorded events arrive per step. The model's text as it streams arrives through
`app.deltas(sink)`, which calls `sink(task_id, delta)` for every fragment and returns a
function that unsubscribes:

```python
unsubscribe = app.deltas(
    lambda task_id, d: print(d.text, end="", flush=True) if d.kind == "text" else None)
```

`delta.kind` is `text`, `thinking` or `tool` (a running tool's latest output). Deltas are never
stored; the recorded `ModelReplied` is the truth. On a Postgres store a `LocalApp` also hears
the deltas workers on other machines publish.

## Conversations

A task without `one_shot` is a conversation. Send the next message once a turn is done:

```python
task = await app.client.create("main", "Read the README.", config={"cwd": "."})
row = await app.client.wait(task)            # waiting (for input, or an approval) or ended
await app.client.send(task, "Now summarize it in one line.")
```

- `send(task, text, when="next_turn")` queues the message after the current turn;
  `when="now"` lands it at the next step of the running turn.
- `cancel(task)` ends the task; `cancel(task, turn_only=True)` stops only the current turn.
- `create(…, dedupe_key=…)` and `send(…, dedupe_key=…)` are idempotent: a repeat returns the
  first one's result. To start a conversation idempotently, create the task without `input`
  (and with your own `task_id`) and `send` each message with its own key.
- `create(…, history_from=other_task, history_through=seq)` starts a task from another
  task's conversation, as a fork or a rewind.

A message can carry images: pass a `noeta.core.types.Message` with `Text` and `Image` blocks
instead of a string.

## Approvals and questions

A call that needs a person parks the task with `Waiting(on="approval")` or
`Waiting(on="question")`; a sub-agent's waiting shows on its own task. `pending_tree(client,
task)` (`noeta/host/pending.py`) lists every open request in a task and its sub-agents, each a
`Pending` with `task_id`, `request_id`, `kind`, `tool`, `input`, `prompt`, `detail` and
`agent`.

- **An approval:** `client.answer(task_id, request_id, "allow" | "deny", always=False)`.
  `always=True` also remembers the rule the card offered (`detail["always"]`).
  `noeta.host.pending.choices(detail)` gives the card's options (yes, the "don't ask again"
  rule, a mode switch, no) and `answer_choice` answers with one.
- **A question** (`AskUserQuestion`): `detail["questions"]` holds the questions, each with an
  `id`, `question`, optional `options` and `multiSelect`. Answer with
  `noeta.host.questions.answer_question(client, task_id, request_id, data)`, where `data` maps
  each question id to `{"selected": [labels]}` or `{"other": "text"}` (a plain string answers
  a single question). It checks the answer before posting it.

Answers are idempotent per request id: a second answer to the same request is ignored.

## Signals

`client.signal(task, name, data)` posts a named input a plugin of the task reads, without
opening a turn:

```python
await app.client.signal(task, "permission_mode", {"mode": "acceptEdits"})
await app.client.signal(task, "config", {"model": "claude-opus-5-5", "effort": "high"})
```

`wake=False` leaves a waiting task waiting; it reads the signal when it next wakes.

## Running workers yourself

`LocalApp` passes extra keywords to its `Runtime`. A long-running service usually starts one
`Runtime` per process and calls `serve(stop)`:

```python
stop = asyncio.Event()
runtime = noeta.Runtime(store=store, agents=agents, provider_for=provider_for,
                        concurrency=16, lease_seconds=30.0, queues=("default",),
                        spill_root="/var/lib/my-service/tool-results")
await runtime.serve(stop)          # until stop is set; running tasks are handed back
```

- `concurrency`: tasks run at once. `queues`: which queues this worker claims from
  (`client.create(…, queue=…)` puts a task on one).
- `lease_seconds`: how long a claim lasts without renewal. A worker that dies leaves its tasks
  claimable after about this long.
- `env_for(state)`: the exec env a task's tools run in; the default is the local machine at
  the task's `cwd`. A container env is in `noeta.backends.execenvs.container`
  ([Where commands run](../guides/sandbox.md)).
- `spill_root`: where tool outputs too long to return inline are saved. `Runtime`'s default,
  `None`, cuts them instead; `LocalApp` sets a temporary directory.
- `snapshot_every`: events between fold snapshots (default 100; 0 never).

The same store can be served by `noeta worker` processes on other machines at the same time;
any of them continues any task ([Many machines](../how-it-works/many-machines.md)). A
`LocalApp(…, own_tasks_only=True)` serves only the task trees it created or adopted, which is
how the front ends share `~/.noeta/tasks.db` without picking up each other's tasks.

## Connecting to an existing store or server

`noeta.connect(url)` gives a client for tasks somewhere else:

```python
client = noeta.connect("sqlite:///home/me/.noeta/tasks.db")     # a Client on that store
client = noeta.connect("postgres://noeta:…@db.internal/noeta")    # likewise
client = noeta.connect("https://noeta.internal:8642", token=os.environ["NOETA_SERVER_TOKEN"])
```

A store URL gives a `Client` in this process: it reads and writes the store, and some worker
must run the tasks. An `http(s)://` URL gives a `RemoteClient` with the same methods over
`noeta serve`'s HTTP API ([HTTP / WebSocket API](../reference/http-api.md)); the server's
workers run the tasks.

## What differs from the `noeta` command

The command builds its agents from `~/.noeta` (settings files, agent files, plugin files,
`config.toml`) in `noeta.cli.config.from_settings`. A program that builds agents with
`default_agents` gets only what it passes:

- The memory and MCP plugins and the repetition guard stay off until `plugins` has a setting
  for them: `default_agents(model, plugins={"mcp": {"servers": {...}}, "memory": {"home": …}})`.
- No settings files are read: pass permission rules as the permissions plugin's arguments,
  `plugins={"permissions": {"allow": ["Bash(git *)"], "mode": "acceptEdits"}}`.
- `plugins={"title": False}` turns a plugin off. The title plugin makes one small model call
  per conversation; the auto mode classifier makes calls of its own.
- A model the catalog does not know runs at 128,000 / 16,384 tokens until you register its
  limits: `noeta.core.models.register_models({...})`.

Agents can also come from agent files: `noeta.read_agent_dir(path)` and
`noeta.parse_agent_file(text)` return `AgentFile`s to pass as `default_agents(model,
extra_files=…)`.
