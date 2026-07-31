# Concepts

These pages explain *why* Noeta is shaped the way it is. They are background
reading rather than instructions — nothing here asks you to run a command. If
you would rather see something work first, do the
[quickstart](../tutorials/quickstart.md) and come back.

Read them in the order below. Each page assumes only the ones above it, and
every page opens with a plain-language summary and a diagram.

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
