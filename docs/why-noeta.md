# Why Noeta

Most agent libraries give you a loop that runs inside one process. That is
enough for a chat window. It stops being enough when an agent runs for hours
with nobody watching, has to wait for a person, or needs more than one
machine. Noeta is built for those cases.

## A crash is a pause, not a loss

Noeta never keeps task state in memory. Every model call, tool call and
decision is appended to an event log, and state is rebuilt from that log
whenever it is needed. A process that dies mid-task loses nothing: the next
worker replays the log and carries on from the last recorded step. A step that
was cut off halfway is re-run when that is safe; if re-running could repeat an
action that needs approval, the task stops and waits for a person instead.

<NtCrashResume />

```python
from noeta.sdk import Client, HostConfig, Options
from noeta.sdk.providers import AnthropicProvider

options = Options(system_prompt="You are a careful release engineer.")
db = HostConfig(storage_path="./noeta.sqlite")   # or a postgresql:// DSN

with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5",
            host_config=db) as client:
    task_id = client.start(goal="Draft the release notes.").task_id

# A different process, after a restart, reads the same task back.
with Client(options, provider=AnthropicProvider(), model="claude-sonnet-5",
            host_config=db) as client:
    for item in client.messages(task_id):
        print(item)
```

The log also answers "what did the agent do, and why?" after the fact: every
tool call, approval, token count and compaction is on it, and nothing is
overwritten. → [Event log & recovery](how-it-works/event-log.md)

## Waiting is a state, not a blocked thread

A task can stop and wait for:

- a human approval of a risky tool call, or an answer to a question
- a timer
- a subtask it spawned
- an event from outside (a webhook, a CI result)

<NtWaiting />

While it waits, nothing runs and nothing is held in memory. When the thing it
is waiting for arrives, exactly one worker wakes it — once, even across
crashes. A month-long approval uses the same mechanism as a five-second tool
call.

```python
turn = client.start(goal="Clean up the build directory.")
if turn.status == "suspended":          # e.g. waiting on approval of a Bash call
    print(turn.wake_handle)             # what it is waiting for
    # ...hours later, maybe from another process:
    client.approve(turn.task_id, call_id="...")
```

→ [Tasks & waking](how-it-works/tasks-and-waking.md)

## Grow without a rewrite

The agent is an `Options` value. How it runs is a separate choice, and you can
change that choice without touching the agent.

<NtScale />

| Stage | What changes |
|---|---|
| A script | `query(options, goal=...)` — one call, one answer |
| A service | `Client(options, ...)` plus `client.start_workers(4)` — a worker pool in your process |
| Several hosts | `HostConfig(storage_path="postgresql://...")` — hosts share one database; a lease makes sure only one worker drives a task at a time |

There is no daemon to operate and no extra service in the middle. You own the
process and the database. → [Deploy to production](guides/deploy.md)

## Also

- **Everything is a plugin — including the built-ins.** The kernel ships no
  capabilities. File tools, web, memory, MCP, sandboxes, storage backends and
  model adapters are plugins that reach the kernel through the same loader
  yours does; the build fails if anything takes a shortcut. A plugin declares
  what it contributes in a static manifest, so it can be listed and
  conflict-checked before any of its code runs.
  → [Write a plugin](guides/plugins.md)
- **Any model.** Anthropic, any OpenAI-compatible `/chat/completions`
  gateway, and the OpenAI Responses API. Switching is one line; the agent,
  its tools and its recorded history do not change.
  → [Connect a model](guides/models.md)
- **Control before action.** Permission modes decide which tool calls stop
  for approval. Guards can block a call before it runs; observers can only
  watch, so a broken observer can never break a task.

## Compared with other tools

| | **Noeta** | Claude Agent SDK | LangGraph | Temporal |
|---|---|---|---|---|
| What it is | Durable agent runtime, as a library | Agent loop library for Claude | Graph-based agent framework | Durable workflow platform |
| Control flow | The model decides each step | The model decides | A graph you define | Workflow code you write |
| What is saved | Every event; state is derived from it | The conversation | Checkpoints of graph state | Workflow history |
| Waiting for a human / timer | Built in, woken exactly once | Resume the conversation | Interrupt, then the caller resumes | Built in |
| Scaling out | Worker pool; many hosts on Postgres | One process | Up to you, or the hosted platform | A Temporal cluster |
| Models | Any, one line to switch | Claude | Any | — |
| Extra service to run | None | None | None | Temporal server |

**Claude Agent SDK** gives your code an agent loop on Claude and manages the
conversation for you. Noeta answers a different question: how to turn an
agent's run into a record you can resume, audit and move between machines.
If you want the lowest-friction way to call Claude with tools, use the SDK.

**LangGraph** models an agent as a graph and saves checkpoints of its state.
Noeta has no graph — the model decides each step — and saves what *happened*
rather than snapshots of what the state *was*. Scheduling (leases, workers,
reclaiming stuck tasks) ships in the library. LangGraph has a much larger
integration catalogue and community.

**Temporal** runs workflows whose shape you write in code ahead of time. Noeta
is for work whose shape the model discovers as it goes. If you know the steps,
Temporal is the better fit.

**Pi and other terminal harnesses** drive an agent interactively in your
terminal. Noeta runs agents unattended on your own infrastructure. They
combine well: a terminal front end can drive a task running on a Noeta worker
pool.

## When not to use Noeta

- **You don't want to run anything.** You operate the process and the
  database. If "call a vendor API, no operations" is the requirement, a hosted
  client library is simpler.
- **You need a large integration catalogue today.** The built-in tool set is
  small and there is no plugin marketplace.
- **One host is not enough and you can't run Postgres.** SQLite and in-memory
  storage are single-host.

More detail: [Known limitations](operations/limitations.md).

## Evidence

An agent built only on the public SDK —
[noeta-agent](https://github.com/initxy/noeta-agent)'s `main` preset on
Claude Opus 4.8 — solved **24/40** tasks of a Terminal-Bench 2.1 sample on the
first pass and **33/40** best of three attempts (`noeta-sdk` 0.6.28; the public
board spans 58.7%–83.8% on the full set), and **13/15** of a SWE-bench Verified
subset after re-running 4 setup timeouts (`noeta-sdk` 0.6.10), run on the
official harness. Each is a single run over a sample, not a full leaderboard
run. → [Benchmarks](benchmarks.md)

## Next

- [Quickstart](start/quickstart.md) — a real agent in five minutes
- [How it works](how-it-works/index.md) — the design on one page
