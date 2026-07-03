# The Task model

Everything Noeta runs is a **Task** — there is no Session, Run, Job, or
Workflow standing beside it. A Task is an addressable unit of agent work: it
has a `task_id`, a `status`, and a `parent_task_id` when it was spawned by
another Task. Its full state is folded from its own EventLog on demand; the
Engine never holds task state in memory across runs (see
[Event sourcing](event-sourcing.md)).

## Lifecycle

<p align="center">
  <img src="../assets/task-lifecycle.svg" alt="Task lifecycle — unified suspension, wake events, and terminal exits" width="820">
  <br>
  <em>All waiting is one <code>suspended</code> status plus a typed wake condition; a wake event re-enqueues the Task for the next lease.</em>
</p>

A Task moves through four statuses:

- **`pending`** — created (or re-enqueued) and waiting for a Worker to lease it.
- **`running`** — a Worker holds the Lease and the Engine is advancing the
  Task step by step (see [Engine & execution](engine-execution.md)).
- **`suspended`** — the Task released execution and is waiting. All waiting —
  a subtask finishing, a human answering, a timer firing — is this one status
  plus a typed `WakeCondition` describing what it waits for (see
  [Wake & resume](wake-resume.md)).
- **terminal** — completed, failed, or cancelled. A snapshot and a terminal
  event close the stream.

## Parent and child

A Task can spawn Subtasks. A Subtask is structurally identical to its parent —
its own EventLog, its own fold, its own lifecycle — related only through
`parent_task_id`. "Multi-agent" in Noeta is therefore just many Tasks: the
parent suspends after spawning, and results flow back to it as a wake event.
The whole tree is reconstructable from events alone, and each node recovers
independently.

## What a Task is not

- **Not a Session.** A multi-turn conversation is one Task receiving user
  input repeatedly: each turn is a wake → a few steps → suspend cycle, with
  the Task resting at `suspended` between turns.
- **Not a Workflow instance.** Fixed procedures are a deterministic Policy
  plus subtask spawning — there is no separate workflow engine or workflow
  primitive.
- **Not an Agent.** An Agent is a named, spawnable configuration — prompt,
  tools, capabilities — the "class" of a Task. One Agent can be instantiated
  by many Tasks.

Related: [Event sourcing](event-sourcing.md) ·
[Wake & resume](wake-resume.md) ·
[Engine & execution](engine-execution.md)
