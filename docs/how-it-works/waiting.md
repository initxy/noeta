# Waiting and waking

A task spends most of its life waiting: for the person's next message, an approval, an answer
to a question, a sub-agent, a timer. This page explains the one way anything gets into a task
(the inbox), how a task waits without holding a worker, how it wakes, and the two exceptions:
work that lives in one worker's process, and a plugin's model work running in the background.

## The inbox

Nothing outside the task's runner ever writes to the task's log. Everyone else **posts** to the
task's inbox, and the runner **takes** what is there at its next step boundary, recording it in
the log as one `InputTaken` event
([ADR-001](https://github.com/initxy/noeta/blob/main/docs/adr/001-task-is-an-event-log-with-one-writer.md)).

<Diagram name="core-inbox" />

There are six kinds of inbox item (`noeta/core/events.py`):

| Item | Posted by | Effect when taken |
|---|---|---|
| `Say(message, when)` | The person (`client.send`), a parent, a sub-agent handing back, a plugin's notice | A message for the agent. `when="next_turn"` (default) is queued until the current turn ends; `when="now"` joins the running turn at its next step. |
| `Answer(request_id, decision, always, data)` | The person, through a front end (`client.answer`) | Answers an approval (`allow` / `deny`, optionally `always`) or a question (`data`). |
| `Cancel(reason, turn_only)` | The person (`client.cancel`), a parent | Ends the task, or with `turn_only` only stops the current turn. |
| `ChildEnded(child_id, status, ...)` | A child task, in the same transaction as its own end | Tells the parent a sub-agent finished, with its answer or error and what it used. |
| `TimerFired(name)` | The runner, for a due timer | Fires the timer; its text, if any, arrives as a message. |
| `External(name, data)` | Plugins, channels, `client.signal` | A named signal a plugin reads (a permission-mode switch, a background job's end, a model change). |

Why one way in: every front end, parent, timer and plugin is then serialized through the same
ordered queue, so there is no second path to race with the runner, and a task's log shows
exactly what it saw and in what order.

**Idempotent posts.** A post may carry a `dedupe_key`: a second post with the same key does
nothing and returns the first one's inbox seq. The kernel uses this everywhere a post may be
repeated after a crash: `answer:<request id>` for answers, `child:<task id>` for a child's end,
`timer:<name>:<seq>` for a timer firing. A front end that retries a `send` passes its own key.

**Waking.** A post to a waiting task makes it `ready`, in the same transaction, unless it is
posted with `wake=False` (news that needs no turn of its own, such as archiving; it is read at
the task's next wake). And if a runner goes to wait while unread items exist, the store leaves
the task `ready` instead. Between the two, a post can never fall into the gap between "the
runner looked" and "the task went to sleep": on Postgres both the post and the waiting append
lock the task row, so one lands strictly before the other.

**Taken exactly once.** `InputTaken.through` is the last inbox seq taken; the next take starts
after it, as folded from the log. The task row's `inbox_read_seq` catches up when the runner hands
the task back (waiting or ended), which is what lets the store see unread items then. A worker
that crashes after posting but before taking leaves the item for the next claimer; one that
crashes after taking has it in the log.

## Parking: waiting without a worker

A tool call that has to wait for something outside the task **parks**. Its tool returns
`ToolOutput(park=Park(on=..., keys=...))` instead of a result: on `"question"` (the person's
answer, keyed by the call id) or on `"children"` (sub-agents it spawned, keyed by their ids).
The runner records which call waits on what (`core/parked`), and once nothing else in the reply
can run, appends `Waiting(on=..., ids=..., detail=...)`. An approval is the same thing driven by
the gates instead of the tool: `Waiting(on="approval")`.

That append gives the claim back in the same transaction. The task is `waiting`, no worker holds
it, no process keeps anything for it. It can wait a minute or a month at no cost, survive every
worker restarting, and be answered from a different machine.

<Diagram name="core-park-wake" />

When the answer arrives, the post makes the task `ready`; any worker claims it, folds the log,
takes the `Answer`, sees the parked call's answer has arrived (`ResumeCalls`), and calls the
tool's `resume(input, ctx, outcome)` with it. The tool finishes as if it had never stopped, and
its `ToolFinished` is appended like any other.

A few rules keep this predictable:

- **Gating stops at the first ask.** Calls of the same reply after a call waiting for approval
  are neither run nor refused; they are gated once the approval is answered. The calls before
  it in the reply still run.
- **A wake that brings nothing new** (a post the parked call is not waiting for) hands the task
  back still waiting, without writing the same `Waiting` twice.
- **Nobody to ask.** A task whose config says `unattended` (a background sub-agent) that reaches a
  question or an approval fails its turn with `needs_human`, rather than waiting forever.
- **Front ends see what to show.** `Waiting.detail` carries, per call id, the approval prompt or
  the questions and their options. Every front end renders questions from that one shape
  (`noeta/core/questions.py`), without importing the plugin that asked.

### Questions to the person

A plugin asks by parking on `ask(...)` from `noeta/core/questions.py`: one or more questions,
each with optional options and multi-select. The person's answer comes back as
`Answer(request_id=<call id>, data={"answers": ...})`, and the tool's `resume` reads it with
`read_answer`. Telling the person something without asking is `notify(...)`: a `core/notice`
record front ends show and the model never sees. Neither reaches the model as if the person had
said it. `AskUserQuestion` is built this way (the `ask_user` plugin).

### Children ending

A sub-agent is a child task (`parent_id`, `root_id`), created in the same append as the parent's
tool call result or park, so a retried call never creates a second child. A foreground `Agent`
call parks on its children; each child, when it ends, posts `ChildEnded` to its parent in the
same transaction as its own `TaskEnded` (dedupe key `child:<id>`). When every child the call
waits on has reported, the call resumes with their answers. A background sub-agent's call
returns at once, and its `ChildEnded` arrives later as news for the parent. When a parent ends,
its live children are cancelled. Details: [Sub-agents and background work](../guides/subagents.md).

## Timers

A plugin or tool sets a timer with `set_timer(name, at, text)` on its view or tool context (a
`core/timer` record; `clear_timer` removes it). Timers are kernel state in the log, so a
scheduled wake-up survives every worker restarting.

When the task goes idle, the runner writes `Waiting(on="input", until=<earliest timer>)`, and
the store sets the row's `ready_at`. Once that time passes, the task is claimable, any worker
claims it, and `fire_timers` (`noeta/core/runner/timers.py`) posts `TimerFired(name)` with the
dedupe key `timer:<name>:<seq of the record that set it>`; a crash between posting and taking it
is harmless. The timer's text arrives as a message for the next turn.

Timers fire only while the task is idle. A timer that comes due during a turn, or while the
turn is parked on an approval, a question or children, fires once when the turn ends, as Claude
Code fires a scheduled prompt only when it is idle. The `schedule` plugin's `ScheduleWakeup` and
cron tools, the expiry of a `SendMessage` idle notice and the compaction plugin's idle check
all use these timers.

The store decides "due" by its own clock (the database's on Postgres), which may differ from a
worker's. If a worker is woken for a timer its own clock says is not yet due, it fires the
timers due by the store's time anyway, once per claim, rather than writing the same `Waiting`
again and being woken again in a loop.

## Kept claims

Parking works because everything a task needs is in its log. Some work is not: a background
`Bash` job, a `Monitor`, an MCP call that is itself waiting for the person's answer. That work
lives in one worker's process and cannot move. If the task went back to the store as usual,
another worker could claim it and find no such job.

So a tool that starts such work **keeps its task's claim**
([ADR-029](https://github.com/initxy/noeta/blob/main/docs/adr/029-a-task-keeps-its-own-claim-while-it-owns-process-bound-work.md)).
`ToolContext.keep()` returns a `KeptClaim`; while one is held, the runner hands the task back
waiting with `Patch.keep`. The row says `waiting`, but its owner, epoch and lease stay. The
runner goes on renewing the lease and watching the task's inbox itself (`Runtime.run_claim`
returns, and the run carries on in the background, taking one of the worker's `concurrency`
slots only while a wake has it stepping). A claim skips a row whose lease has not run out, so no
other worker runs the task, and a post to it is taken by the same run. Once nothing keeps the
claim (the job ended and its result was taken), the run gives the claim back and the task waits
like any other.

<Diagram name="core-kept-claim" />

When things go wrong:

- **The worker dies.** Its kept lease runs out, which makes the waiting task claimable at once.
  The next claimer has no such job; the shell plugin notices in `on_start` and posts
  `JOBS_LOST` behind whatever the inbox already holds, and the model is told the job is lost,
  instead of waiting forever. A job's guard deletes its `.alive` file every third of a lease and
  kills the job when the file is already gone, so a frozen worker's job dies no later than
  another worker can call it lost.
- **The worker stops cleanly.** `Runtime.let_go` gives up the kept claims on the way out of
  `serve`: the jobs die, the tasks stay waiting, and they hear of it with whatever wakes them next.
- **The worker froze past its lease and woke up.** It finds its claim taken (the epoch fences
  every write, [recovery](./recovery.md)) and stops what it kept.

This is per task, not per task tree. A task's jobs are its own; a parent stops a child's job by
posting to the child's inbox; a task's end kills its jobs (a sub-agent's when it hands back). The
cost of a waiting task with a kept claim is one lease renewal and one inbox check per period.

The rejected alternatives: ending every background job when its turn ends (simple, but every
background dev server would die at the end of the turn, even on one machine); handing a job to
another worker (a process cannot move); and the earlier design of "holds" that pinned a whole
task tree to one worker, which grew a new rule with every fix and was replaced by this.

## Background model work from plugins

Some plugins call the model beside the conversation: the compaction plugin writes a summary in
advance while the task keeps working (Claude Code's precomputed compaction), the title plugin
names the task. A result computed beside the step cannot be written to the log by whoever
computed it (that would be a second writer), and a step must never depend on something held only
in one process's memory.

So such work comes back through the inbox
([ADR-031](https://github.com/initxy/noeta/blob/main/docs/adr/031-background-plugin-work-returns-through-the-inbox.md)):

1. A plugin starts the call from a hook and records only a random job id.
2. When the call finishes, its outcome (and what it cost) is posted to the task's own inbox as
   an `External` signal.
3. The runner takes it at its next boundary like any input; the plugin's `on_event` records it
   if it names the job the log says is running. Anything else (outdated, or posted by someone
   else) leaves only its cost. From then on it is ordinary folded state.
4. The recorded result names what it was computed from (for a summary, the compaction count and
   a digest of the messages it covers). The step that would use it checks that against the
   current state and ignores it if they differ.

If the worker dies mid-call, the call is lost and simply started again. A result posted to a
waiting task wakes it once, to be recorded. The compaction side of this is on
[Context, caching and compaction](./context.md).

## Cancel and stop

Both are inbox items, and both act quickly. When a `Cancel` is posted, the runner hears it (from
the store's change feed, or by polling the inbox every `poll_interval`, every third of the lease
on a store whose change feed reaches across machines) and cancels the step in
flight: a streaming model call or running tools stop at once. Then the next boundary takes the
item:

- **`Cancel`** ends the task `cancelled`. Its live children get a `Cancel` too, and its parent
  gets `ChildEnded(status="cancelled")`.
- **`Cancel(turn_only=True)`** (the person pressing Esc) stops only the turn: open calls are
  answered "interrupted", parked calls are withdrawn, and children they wait on are cancelled.
  If the reply was still streaming, the text so far stays in the conversation, followed by
  Claude Code's `[Request interrupted by user]` marker. The task waits for the next message;
  messages sent with the stop are answered in a new turn.

## Related

- [One step of a task](./one-step.md): where the inbox is taken and the wait is written.
- [One writer and crash recovery](./recovery.md): claims, leases and epochs.
- [Sub-agents and background work](../guides/subagents.md), [Permissions and hooks](../guides/permissions.md).
- [Events reference](../reference/events.md): every inbox item's fields.
