# Concepts

These pages explain *why* Noeta is shaped the way it is. They are background
reading rather than instructions — nothing here asks you to run a command. If
you would rather see something work first, do the
[quickstart](../tutorials/quickstart.md) and come back.

Read them in the order below. Each page assumes only the ones above it, and
every page opens with a plain-language summary and a diagram.

## Three ideas carry the whole design

Everything else — audit, replay, suspend/resume, provider neutrality — falls
out of these three.

### 1. State is a fold over an event log

<p align="center">
  <img src="../assets/diagrams/event-sourcing.svg" alt="Event sourcing — events append to the EventLog, large bodies go to the ContentStore, fold rebuilds four state slices" width="820">
</p>

Each task owns one append-only stream of events: the goal it was given, each
composed context plan, each model response, each tool call and its result, each
suspend and wake. There is no task table the engine reads and writes. When
something needs the current state, it folds the stream from the beginning and
gets it — the state object is a disposable projection, the log is the master
copy. Event payloads stay small (capped at 4 KB); anything bigger, such as a
full response body or a large tool output, goes to a content-addressed store and
the event carries only a reference. Because folding is the only way state comes
into being, "what the agent did" and "what the agent is" can never disagree.

→ [Event sourcing](event-sourcing.md) ·
[Fold & snapshot](fold-and-snapshot.md) ·
[State & writers](../architecture/state-and-writers.md)

### 2. Kill it mid-task, it resumes

<p align="center">
  <img src="../assets/diagrams/crash-resume.svg" alt="Crash and resume — worker A dies mid-step, its lease expires, worker B folds the log and resumes exactly once" width="820">
</p>

A worker takes a *lease* on a task — a short, heartbeat-renewed exclusive hold —
and drives it to the next suspend or terminal state. Every write to the log
presents that lease id, so exactly one worker can ever be writing a given task.
If the worker dies, its heartbeat stops, the lease expires, and the task returns
to the ready queue; the next worker folds the log, seals the interrupted attempt
as dead history, and carries on from the last durable point. The same machinery
covers deliberate waiting: a task can suspend for a human answer, a timer, or a
subtask, costing nothing while it sleeps, and the wake that revives it is
durable, single-worker, and delivered exactly once — at-least-once delivery plus
idempotent consumption.

→ [Wake & resume](wake-resume.md) · [Task model](task-model.md) ·
[Deploy a worker](../how-to/deploy-worker.md)

### 3. Two packages, capabilities as plugins

<p align="center">
  <img src="../assets/diagrams/architecture.svg" alt="Noeta architecture — your code imports noeta.sdk over the noeta-runtime kernel, builtins reach it only through the plugin loader" width="820">
</p>

Noeta ships as two libraries sharing one `noeta.` namespace. **`noeta-sdk`** is
the only thing you import: `query` / `Client` / `Options` / `@tool`, the preset
agents, and every official capability. **`noeta-runtime`** is the pure kernel —
engine, fold, snapshot, worker, dispatcher, lease, context composer — and it
declares no dependencies at all. The kernel carries no capability of its own:
the file tools, web tools, memory, browser, MCP, sandbox, storage backends, and
every provider adapter are built-in *plugins* that reach the kernel only through
the loader's dynamic reference resolution, a rule an import linter enforces on
every build. That single boundary is why provider neutrality is structural
rather than a promise, and why your plugins ride the exact same path Noeta's own
do.

→ [Architecture overview](../architecture/overview.md) ·
[Packages & boundaries](../architecture/packages.md) ·
[Extension planes](../architecture/extension-planes.md)

## Reading order

1. **[Event sourcing](event-sourcing.md)** — Noeta never treats "current state"
   as the truth. It appends a record of everything that happens to a per-task
   log and recomputes state from that log. Start here; every other page leans
   on this one idea.

2. **[The Task model](task-model.md)** — a Task is the only unit of work there
   is. Conversations, background jobs, and delegated subagents are all Tasks,
   each with one log, four state slices, and four statuses.

3. **[Engine & execution](engine-execution.md)** — how a Task actually moves
   forward: build the model's input, ask what to do next, carry it out, repeat
   until the Task waits or finishes.

4. **[Fold & snapshot](fold-and-snapshot.md)** — the function that turns a log
   back into state, why it is deliberately boring, and how snapshots make it
   fast without ever becoming a second source of truth.

5. **[Wake & resume](wake-resume.md)** — what happens while a Task is waiting
   on a person, a timer, a subtask, or an outside system, and how it gets
   picked back up exactly once even if a machine dies mid-step.

6. **[Guard vs Observer](guard-observer.md)** — the two ways to hook into a
   running agent: one can stop an action before it happens, the other can only
   watch after the fact. There is no third.

7. **[Composer & context caching](composer-and-cache.md)** — how Noeta decides
   what the model sees on each call, and why that layout is arranged around the
   provider's prompt cache.

8. **[Provider neutrality](provider-neutrality.md)** — why no vendor's message
   format is allowed to become Noeta's internal one, and how that is enforced
   by an import rule rather than by good intentions.

## The short version

A **Task** is one run of an agent. Everything that happens to it is appended to
its **EventLog**; its state is **folded** from that log on demand. The
**Engine** advances one Task by one step: compose what the model sees, ask a
**Policy** what to do, record the result. When the Task has to wait, it
suspends and its wake condition is held durably until something matches it.
Guards can veto actions on the way through; Observers watch the log afterwards.
Everything the model sees is assembled fresh each turn by the **ContextComposer**,
and every LLM vendor sits behind an adapter.

## Next

- [Quickstart](../tutorials/quickstart.md) — run an agent in about five minutes.
- [Architecture overview](../architecture/overview.md) — the same system from
  the module and package angle.
- [Glossary](../reference/glossary.md) — every term on these pages, defined in
  one place.
