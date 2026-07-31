# The Task model

A **Task** is one run of an agent, and it is the only unit of work Noeta has.
A chat that goes on for weeks is a Task. A nightly job is a Task. A subagent
you delegate a piece of research to is a Task. There is no session object, no
workflow instance, and no separate conversation type underneath them.

A Task is small: a `task_id`, a `status`, a `parent_task_id` if something
spawned it, and a `subtask_depth` fixed when it was created. Everything else —
its history, its memory, its counters — is folded from its own EventLog on
demand (see [event sourcing](event-sourcing.md)).

<p align="center">
  <img src="../assets/diagrams/task-lifecycle.svg" alt="Task lifecycle — pending → running → suspended (four wake conditions) → terminal" width="820">
</p>

## The four statuses

`status` is one of exactly four values, and the diagram above is the whole
state machine:

- **`pending`** — created, or re-enqueued after a wake, waiting for a Worker to
  pick it up.
- **`running`** — a Worker holds the lease and the Engine is advancing the Task
  step by step (see [engine & execution](engine-execution.md)).
- **`suspended`** — the Task gave up execution and is waiting. *All* waiting is
  this one status plus a typed `WakeCondition` recorded on `wake_on`, whether
  it is waiting on a subtask, a human, a timer, or an external signal (see
  [wake & resume](wake-resume.md)).
- **`terminal`** — the Task ended. `TaskCompleted`, `TaskFailed`, or
  `TaskCancelled` closes the stream, with a snapshot written ahead of the
  finish and fail exits.

The payoff of collapsing every kind of waiting into one status is that there is
one resume path to write, test, and reason about — not four.

## Four state slices, one writer each

A Task's mutable state is cut into four typed slices. Splitting it this way is
what lets each piece have a single writer:

| Slice | Holds | Written by |
| --- | --- | --- |
| `RuntimeState` | the rolling message log and last-turn token usage | the Engine |
| `TaskState` | long-horizon memory — goal, phase, todos, decisions, active content | the Policy, via `state_patch` |
| `ContextState` | the latest context plan, the compaction summary, per-turn thinking | fold |
| `GovernanceState` | folded counters — cost, iterations, denials, subtask results | fold |

Every slice is written back by `fold` from events on the Task's own stream, and
the Engine is the sole emitter on that stream. A Policy that wants to change
`TaskState` attaches a `TaskStatePatch` to the decision it returns; the Engine
lands that as an event and fold applies it. That indirection is exactly what
keeps `fold(events)` equal to the state that actually ran.

`TaskState` is the slice worth knowing by name. It is where a long-horizon
agent keeps what it has figured out so far, and it is the main structural
difference between an agent that can work for hours and one that answers a
single question.

## Parents and children

A Task can spawn **Subtasks**. A Subtask is structurally identical to its
parent — its own EventLog, its own fold, its own lifecycle — and is related
only through `parent_task_id`, plus `subtask_depth`, which a budget caps so
delegation cannot recurse forever.

So "multi-agent" is not a separate feature. The parent suspends after spawning,
each child runs as an ordinary Task, and each child's outcome comes back as a
wake event. The whole tree is reconstructable from events alone, and every node
recovers independently of the others.

One vocabulary note: the root of a delegation tree is the `root_task_id`, and
it is the lifetime owner of things that outlive a single step — background
shells, background subagents, a sandbox container.

## What a Task is not

- **Not a session.** A multi-turn conversation is one Task receiving input
  repeatedly. Each turn is a wake → a few steps → suspend cycle, and between
  turns the Task rests at `suspended` with a `HumanResponseReceived` condition.
  A host that wants a user-visible "session" builds that concept itself.
- **Not a workflow instance.** A fixed procedure is a deterministic Policy plus
  spawn decisions; an improvised orchestration script is one Task whose Policy
  interprets it. There is no workflow engine and no workflow primitive.
- **Not an Agent.** An **Agent** is a named, spawnable configuration — prompt,
  tools, plugins, budget — the "class" a Task is an instance of. One Agent can
  be instantiated by many Tasks. It carries no callables and is not a runtime
  entity.

## Next

- [Engine & execution](engine-execution.md) — how a Task moves from `pending`
  to its next suspend.
- [Wake & resume](wake-resume.md) — what happens in the `suspended` state.
- [Spawn subagents](../how-to/spawn-subagents.md) — the practical version of
  parents and children.
- [SDK options](../reference/sdk-options.md) — the `Options` fields that
  compile into an Agent.
