# The Task model

Everything Noeta runs is a **Task**. A Task is an addressable unit of agent
work: a `task_id`, a `status`, a `parent_task_id` when another Task spawned it,
and a `subtask_depth` fixed at creation. Its full state is folded from its own
EventLog on demand; the Engine holds no task state across steps (see
[Event sourcing](event-sourcing.md)).

## Four slices, one writer

A Task's mutable state is cut into four typed slices:

| Slice | Holds |
| --- | --- |
| `RuntimeState` | the rolling message log and last-turn token usage |
| `TaskState` | the Policy's long-horizon memory — goal, todos, decisions, active content |
| `ContextState` | the latest context plan, the compaction summary, thinking blocks kept out of the message stream |
| `GovernanceState` | folded counters — cost, iterations, denials, subtask results, approvals, bindings |

Every slice is written back by `fold` from the events on the Task's own stream,
and the Engine is the sole emitter on that stream. A Policy that wants to change
`TaskState` attaches a `TaskStatePatch` to the Decision it returns; the Engine
lands that as an event and fold applies it. That is what keeps `fold(events)`
equal to the state that actually ran.

## Lifecycle

<p align="center">
  <img src="../assets/task-lifecycle.svg" alt="Task lifecycle — unified suspension, wake events, and terminal exits" width="820">
  <br>
  <em>All waiting is one <code>suspended</code> status plus a typed wake condition; a wake event re-enqueues the Task for the next lease.</em>
</p>

`status` is one of four values:

- **`pending`** — created (or re-enqueued) and waiting for a Worker to lease it.
- **`running`** — a Worker holds the Lease and the Engine is advancing the Task
  step by step (see [Engine & execution](engine-execution.md)).
- **`suspended`** — the Task released execution and is waiting. All waiting — a
  subtask finishing, a human answering, a timer firing, an external signal — is
  this one status plus a typed `WakeCondition` on `wake_on` (see
  [Wake & resume](wake-resume.md)).
- **`terminal`** — the Task ended. `TaskCompleted`, `TaskFailed`, or
  `TaskCancelled` closes the stream, with a snapshot written ahead of the finish
  and fail exits.

## Parent and child

A Task can spawn Subtasks. A Subtask is structurally identical to its parent —
its own EventLog, its own fold, its own lifecycle — related only through
`parent_task_id`. "Multi-agent" is therefore just many Tasks: the parent
suspends after spawning, and each child's outcome arrives back as a wake event.
The whole tree is reconstructable from events alone, and each node recovers
independently.

## What a Task is not

- **Not a Session.** A multi-turn conversation is one Task receiving input
  repeatedly: each turn is a wake → a few steps → suspend cycle, with the Task
  resting at `suspended` between turns.
- **Not a Workflow instance.** An orchestration script is one Task whose Policy
  interprets it, and every helper it dispatches is a real Subtask. There is no
  separate workflow engine and no workflow primitive.
- **Not an Agent.** An Agent is a named, spawnable configuration — prompt,
  tools, plugins — the "class" of a Task. One Agent can be instantiated by many
  Tasks.

Related: [Event sourcing](event-sourcing.md) ·
[Wake & resume](wake-resume.md) ·
[Engine & execution](engine-execution.md)
