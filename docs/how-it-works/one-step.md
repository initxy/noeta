# One step of a task

This page follows a worker through one claim of a task: how it picks the task up, what it does at
each step, how a model call and a batch of tool calls run, and how the step ends. The code is
`noeta/core/runner/` (`Runtime` claims and serves, `TaskRun` runs one claim; `model.py`,
`calls.py` and `closing.py` hold the model step, the tool step and the ways a turn closes) and
`noeta/core/loop/decide.py` (what to do next).

<Diagram name="core-step" />

## Words used here

- A **step** is one pass of the loop: take the inbox, decide, do one thing (a model call, or the
  reply's tool calls), append what happened.
- A **turn** runs from a message the task takes until the model replies without tool calls (or
  the turn is stopped or fails). An interactive task then waits for the next message; a
  one-shot task, and every sub-agent, ends.
- A **claim** is a worker's right to append to a task, with a lease it keeps renewing
  ([One writer and crash recovery](./recovery.md)).

## Picking a task up

A worker process runs one `Runtime` (`noeta/core/runner/runtime.py`). `Runtime.serve` claims
ready tasks while it has free slots (`concurrency`, default 8; `noeta worker --concurrency`) and
runs each claim as a `TaskRun`. It looks for work whenever the store reports a change that can
make a task claimable (a task created, a post, a claim given back), and otherwise every
`poll_interval` (0.5 s).

`TaskRun.run` (`noeta/core/runner/task.py`) then:

1. Starts the **lease keeper**, which renews the claim every third of the lease.
2. **Loads** the task: reads its snapshot of this worker's fold version, if any, and folds the
   events after it; otherwise folds the whole log, after the conversation it continues
   ([A task is a log](./event-log.md#snapshots)). It finds the task's agent by the name its log
   records. A worker that does not have that agent ends the task `failed`, so a parent waiting on
   it hears.
3. **Records the start**: the prompt override files on the task's first pickup, and the plugins
   it runs with (`core/plugins`) whenever they changed since it last ran. A plugin the log
   records and this worker lacks stops the run here, writing nothing
   ([plugin pins](./recovery.md#plugin-pins)).
4. Calls each plugin's `on_start(view)`, and passes events again to plugins that keep a delivery
   cursor (`Extension.delivered_through`), so their `on_event` sees each event at least once.
5. Steps until the task waits or ends.

## The step boundary

Each step (`TaskRun._step_once`) starts the same way:

1. **Fire due timers.** If the task is idle and a timer is due, a `TimerFired` is posted to the
   inbox ([timers](./waiting.md#timers)).
2. **Take the inbox.** Every item after the last one taken is appended in one `InputTaken`.
   What a plugin posts while handling that input is taken in the same step, so it reaches the
   same model call.
3. **Decide.** `next_action(state)` looks only at the folded state and returns one action:

| Action | When | What the runner does |
|---|---|---|
| `Idle` | The task already ended. | Nothing. |
| `End` | A cancel was taken; or a one-shot task's turn closed and no background child is still running. | Appends `TaskEnded`, cancels live children, tells the parent. |
| `StopTurn` | The person stopped the turn. | Answers open calls "stopped", keeps any text that streamed, waits. |
| `Interrupted` | A call is started but has no result and is not parked: a crash cut it short. | Recovers it ([recovery](./recovery.md#a-tool-call-cut-short)). |
| `RunCalls` | The reply has calls not started yet, and no approval is pending. | The tool step, below. |
| `ResumeCalls` | A parked call's answer or children have arrived. | Calls the tool's `resume`. |
| `WaitApproval` | An approval asked earlier is still unanswered. | Keeps waiting, gating nothing again. |
| `WaitParked` | Every open call is parked and nothing they wait for has arrived. | Appends `Waiting` (question or children). |
| `FinalReport` | The turn made `step_budget` model calls (the agent file's `maxTurns`, default 1,000). | One last call with tools switched off, asking for a report. |
| `Continue` | The last reply was cut at the output limit without a tool call. | Asks the model to go on. |
| `MaxTurns` | The turn reached the task config's `max_turns`. | Fails the turn with `max_turns`. |
| `CallModel` | A turn is open and nothing above applies. | The model step, below. |
| `WaitInput` | No turn is open. | Appends `Waiting(on="input")`, with the next timer's time as `until`. |

The order matters: a cancel wins over everything, an interrupted call is dealt with before any
new call starts, and an open approval keeps the calls after it waiting.

## The model step

`call_model` (`noeta/core/runner/model.py`):

1. **Guard against a crash loop.** If three `ModelCalled` in a row have no reply (a worker died
   during the call each time), the turn fails with `llm_error` instead of trying forever.
2. **Build the request** from the folded state (`build_request`; see
   [A task is a log](./event-log.md#the-request-is-a-function-of-the-state)).
3. **Reminders.** Each plugin's `reminders(view)` returns text for the model that is not the
   person speaking (Claude Code's `<system-reminder>` notes, recalled memory, a todo nudge).
   They are recorded in the log first, then placed in the request, so a rebuilt request is the
   same.
4. **`before_model`.** Each plugin may change the request. A hook can also end the turn without
   a model call (`TurnDone`, e.g. `/compact`), make the runner build the request again from the
   new state (`RequestStale`, after a compaction), or fail the turn (`TurnFailed`).
5. **Append `ModelCalled`**, with the request's hash, before anything is sent.
6. **Call the provider** through `complete_with_retries` (`noeta/core/retry.py`). The reply
   streams: each text delta goes to front ends watching live (`Runtime.on_delta`; across
   machines through Postgres notifications), never into the log.
7. **Append `ModelReplied`.** Tool-call ids the provider left empty or reused are replaced with
   ids unique in the task first, since answers, approvals and child ids are keyed by them.

Some replies are not recorded as they are:

- **Cut at the output limit with nothing in it** (it was still reasoning): not recorded (an empty
  assistant turn is a provider error on every later request); a `core/continue` record asks the
  model to go on, reasoning more briefly. A reply cut with text is recorded, and the next step
  asks for the rest. After 3 such recoveries in a row the turn fails `llm_truncated`.
- **Empty** (no text, no tool call): not recorded. A plugin may answer it with a message
  (`Extension.on_empty_reply`); otherwise, or after 3 in a row, the turn ends without an answer.
  An empty reply that stopped for some other reason than an ordinary end or the output limit
  fails the turn with `llm_empty_response`.
- **The step limit's final round**: tools are switched off for that one call and the answer is
  prefixed with a note that the turn stopped at the limit.

### When the provider fails

Adapters never retry; each raises one neutral error, and `complete_with_retries` is the one
place that decides:

| Error | What happens |
|---|---|
| `Transient` (network, 5xx, overloaded), `RateLimited` | Retried, 9 attempts in all, exponential backoff with jitter capped at 30 s; a `Retry-After` is used as given. Each retry appends a `core/retry` record so front ends can say "retrying". |
| `StreamBroken` (a stream that began, then broke) | Where the provider can answer in one piece (`complete_whole`), the call goes on that way, as Claude Code falls back from a failed stream. |
| `MalformedToolArguments` | Asked for again, once. |
| `ContextOverflow` that states the input size and window | Made again once with the output cap the window leaves. |
| Any other `ContextOverflow` | Each plugin's `on_context_overflow` may make room (the compaction plugin summarizes) and the next step calls again; otherwise the turn fails `context_overflow`. |
| `Fatal` (auth, bad request, refusal) | Not retried. The turn fails `llm_error` with the provider's message. |

A cancel during the call or during a backoff sleep stops it at once. Which provider a model goes
to is on [Providers and model routing](./models.md).

## The tool step

`run_calls` (`noeta/core/runner/calls.py`) handles the reply's tool calls in order.

**Checks before any gate.** A call to a tool withheld from the request (a deferred tool not
loaded yet) is refused. A call
whose input does not match the tool's JSON schema is refused with the problems listed (Claude
Code checks the same way), unless the tool takes its input unchecked (MCP tools). A call the
person already declined is refused, and so is every later call of that reply; the turn stops
once the reply's results land.

**Gates.** Every plugin's `before_tool(view, call)` is asked about the call and may return
`Allow`, `Deny`, `Ask` or `Rewrite`:

<Diagram name="core-tool-gates" />

- Any `Deny` wins. A `before_tool` that raises counts as a deny ("permission check failed").
- Then the first `Rewrite`: the call runs with different input (or as a different tool), and
  the new call is gated again, so every guard sees, and a person approves, the call that
  actually runs. A call rewritten more than 3 times is denied.
- Then the asks. A person's recorded answer satisfies them. An approving `Allow` (a hook that
  vouches for the call) satisfies asks that are not `firm`. Each plugin's `resolve_ask` may still
  answer. Otherwise the call **parks on approval**: the task appends
  `Waiting(on="approval")` and gives its claim back.
- No objection at all: the call runs.

Gating stops at the first call that must ask, as Claude Code asks one call at a time: the calls
after it are neither started nor refused, and are gated once it is answered. Permission rules
and hooks themselves are plugins; see [Permissions and hooks](../guides/permissions.md).

**Running.** The allowed calls are grouped: consecutive read-only calls run together, at most 10
at once; every other call runs alone, in order. Calls whose tool says `run_last` (delegation to
sub-agents) run after the reply's other calls. For each group the runner appends
`ToolStarted` for every call in it, *then* runs them. Each tool gets a `ToolContext`: the task's
exec env, a way to record plugin entries, to stage child tasks, to post to inboxes, to keep the
claim, and an `idempotency_key` (`<task id>:<call id>`).

**Results.** A tool that raises gets an error result with the exception, not a dead task. The
kernel then applies its result limits (an oversized result is saved to a file and previewed,
`noeta/core/large_output.py`), runs each plugin's `after_tool`, and appends `ToolFinished`
together with the records the tool made and any children it staged, in one append (a call that
parks writes its children with its park record the same way). So a child task exists only if its
parent's call finished or parked, and a retried call never makes a second child.

**Parking.** A tool that must wait returns `ToolOutput(park=...)` instead of a result: on a
question to the person, or on children it spawned. The runner records which call waits for
what (`core/parked`) and, once nothing else can run, appends `Waiting`. See
[Waiting and waking](./waiting.md).

When the last call of the reply has its result, the results join the conversation as one user
message, and the next step calls the model again. A tool can end the turn with its result
instead (`ToolOutput.end_turn`, a sub-agent's hand-back).

## How a turn ends

- **The model replies without tool calls.** The turn closes with that reply as its answer. An
  interactive task appends `Waiting(on="input")` and gives the claim back. A one-shot task, or
  a sub-agent, ends `done` with the answer (after waiting for its background sub-agents, whose
  reports may open another turn).
- **The turn fails** (the provider gave up, the context overflowed, `max_turns`, a hook's
  `TurnFailed`). Open calls are answered so the conversation never keeps a call without a result;
  started ones get "outcome unknown". A `core/turn_failed` record says why. An interactive task
  keeps its conversation and waits for the next message; a one-shot task ends `failed`.
- **A bug** in the runner, a plugin hook or a provider adapter fails the turn with
  `internal_error`; a second one in a row ends the task `failed`, so a broken task is not
  claimed and failed again forever.
- **It is stopped or cancelled** from outside, through the inbox
  ([Waiting and waking](./waiting.md#cancel-and-stop)).

A task never ends over an inbox item it has not taken: the store refuses that append
(`InboxUnread`), and the runner takes the item and decides again.

## The task's states

The `tasks` row is what the store uses to decide who may claim what. Its `status` follows the
log:

<Diagram name="core-task-states" />

| Status | Meaning |
|---|---|
| `ready` | Claimable now (or at `ready_at`). A new task, one woken by a post, one handed back. |
| `running` | A worker holds the claim; its lease is in `lease_until`. If the lease runs out, any worker may claim it. |
| `waiting` | The task waits (`waiting_on`: input, approval, question, children). No worker holds it, unless its claim is kept ([kept claims](./waiting.md#kept-claims)). A due `ready_at` makes it claimable. |
| `ended` | Done, failed or cancelled (`end_status`). Never claimed again. |

Apart from a claim, a release, a lease renewal and a post that wakes the task, the row changes
only in the same transaction as an append, as `patch_for(events)` says: a `Waiting` makes the
task wait, a `TaskEnded` ends it, anything else keeps it running. The one
exception to "waiting": going to wait while unread inbox items exist leaves the task `ready`
instead, so no message is ever left unseen.

## Messages that arrive mid-turn

`client.send(..., when="now")` is taken at the next step boundary of the running turn: if the
reply's calls are still open, it joins the conversation right after their results. The default,
`when="next_turn"`, is queued until the turn ends and then opens the next one. Both are just
inbox items; see [Waiting and waking](./waiting.md#the-inbox).

## Related

- [A task is a log](./event-log.md): the events this page appends.
- [Waiting and waking](./waiting.md): parking, questions, children, timers.
- [One writer and crash recovery](./recovery.md): what happens when a step is cut short.
- [Context, caching and compaction](./context.md): reminders, the request layout, compaction.
- [The plugin system](./plugins.md): every hook named here.
