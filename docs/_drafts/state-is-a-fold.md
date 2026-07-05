# state = fold(events): what I learned building an event-sourced runtime for AI agents

*(draft — not yet published)*

I've spent the last while building [Noeta](https://github.com/initxy/noeta), an
open-source, self-hostable runtime for AI agents. The pitch, compressed: take
the agent loop you know from Claude Code or the Claude Agent SDK and put it on
an event-sourced spine, so that a task's entire state is a deterministic fold
over an append-only log. This post is about why I made that bet, what fell out
of it for free, and — because the free stuff is only half the story — what it
cost and what remains unsolved.

## The problem: agent state dies with the process

The agents I care about are long-horizon: a coding task that runs for hours,
spawns sub-agents, waits on a human approval, maybe sleeps on a timer. Two
things kept biting me with in-process agent libraries.

First, the obvious one: **state lives in process memory, so a crash loses the
task**. Session-file persistence (the Claude Agent SDK writes JSONL
transcripts) softens this — you can resume or fork by session id — but what it
persists is the *conversation*. It's a recording of what was said.

Second, the subtler one: **a conversation transcript records utterances, not
causality**. When an agent does something surprising at step 40, the questions
I actually want answered are: what exactly did the model see in its context
window at that step? Which guard approved that tool call? What got compacted
away, and what did the summary replace? A transcript can't answer these,
because the answers were never written down — they existed transiently in
process memory and are gone. And when the transcript is auto-compacted, the
original content is displaced by a summary irreversibly; the primary record
rewrites itself.

Once an agent is long-running, suspendable, and multi-process, you're not
building a chat wrapper anymore. You're building something with the
consistency and recovery obligations of a small database. So I decided to
steal the database's trick.

## The bet: the log is the truth, state is a projection

Noeta makes one load-bearing decision and derives everything else from it:

> **A task's ground truth is its append-only EventLog. The state you want at
> any moment is `fold(all events from creation to now)`. The state object is a
> disposable projection; the log is the master copy.**

Every state change is an event. There is no "task table" the engine reads as
authoritative — no mutable row that could drift out of sync with history.
Here's a trimmed view of what one task's stream looks like (payloads
abbreviated; real envelopes also carry `actor`, `trace_id`, and
`causation_id`):

```text
seq  type                  payload (trimmed)
  1  TaskCreated           goal, contract, budget
  2  MessagesAppended      user: "add a retry to the fetch layer"
  3  ContextPlanComposed   plan_ref -> ContentStore        # what this LLM call was built from
  4  LLMRequestStarted     request_ref -> ContentStore
  5  LLMResponseRecorded   response_ref -> ContentStore, usage {tokens, cache}
  6  ToolCallStarted       name="read", call_id="tc-01"
  7  ToolResultRecorded    call_id="tc-01", output_ref -> ContentStore
  ...
 14  SubtaskSpawned        subtask_id="t-child-7"
 15  TaskSnapshot          state_ref -> ContentStore        # fold accelerator, nothing more
 16  TaskSuspended         reason="waiting_subtask",
                           wake_on=SubtaskCompleted(subtask_id="t-child-7")
 17  TaskWoken             wake_event=SubtaskCompleted(subtask_id="t-child-7", result=...)
```

The function that turns this back into state is deliberately boring:

```python
def fold(
    event_log: EventLogReader,
    content_store: ContentStore,
    task_id: str,
    *,
    ignore_snapshots: bool = False,
) -> Task:
    """Reconstruct the current ``Task`` state from its event stream."""
```

That's the whole input surface: one log, one blob store, one task id. No
clock, no randomness, no network, and it never re-calls an LLM provider. The
purity buys a concrete, testable property: the same log, folded in any process
on any machine, yields **byte-identical state**.

Two supporting decisions make this workable in practice:

**Large content lives beside the log, not in it.** Event payloads are capped
at 4 KB — a protocol-level hard constraint. Anything bigger (a full LLM
request/response body, a large tool output, a workspace snapshot) goes into a
content-addressed, hash-deduplicated **ContentStore**, and the envelope
carries only a `ContentRef(hash, size, media_type)`. The log stays a string of
small records; a thousand-event task doesn't bloat into something you can't
back up or index.

**Snapshots are events, not a second store.** Folding a long task from
genesis every time gets slow (hundreds of milliseconds for a thousand-event
task, and fold runs on every wake, every SSE reconnect, every inspect). So
fold has a fast path: start from the latest snapshot, replay only the tail. A
snapshot is itself an ordinary event whose body lives in the ContentStore,
written before each suspend. The iron rule — enforced by that
`ignore_snapshots` switch in the signature, which tests use to cross-check —
is that the snapshot path and the from-scratch path must fold to byte-equal
state. Delete every snapshot and behavior is unchanged, only slower. When fold
meets a snapshot from an older schema version, it discards it and replays from
the top: better slow than wrong. The snapshot is pinned to the status of
"performance accelerator," never a second source of truth — which means the
snapshot format is not a compatibility contract I have to carry forever.

## What falls out for free

The reason to make this bet is that a surprising number of "features" stop
being features and become corollaries.

**Crash recovery is not code.** There is no recovery module. Kill the process
mid-task, start it again, fold the log — the task is back. There is no
separate "save state" step that someone can forget to call, and the entire
class of "persisted copy out of sync with the log" bugs is structurally
impossible, because there is no persisted copy.

**Suspend/resume is one status plus a typed condition.** A task that is
waiting doesn't block a thread; it writes `TaskSuspended` with a `WakeCondition`
and releases its worker. All waiting — a subtask finishing, a human answering,
a timer firing — is the same status with a different condition:

```python
@dataclass(frozen=True, slots=True)
class SubtaskCompleted:
    subtask_id: str
    ...

@dataclass(frozen=True, slots=True)
class HumanResponseReceived:
    handle: str

@dataclass(frozen=True, slots=True)
class TimerFired:
    fire_at: float   # matched with threshold semantics: event.fire_at >= condition.fire_at
```

A pleasant consequence: **there is no session object**. A multi-turn
conversation is just one task receiving user input repeatedly — each turn is a
wake, a few steps, a suspend, and the task rests at `suspended(waiting_human)`
between turns. Nothing about the conversation lives in process memory while
the user thinks.

**Exactly-once wake comes from lease plus idempotent consumption.** Workers
lease a task, advance it to the next suspend point, and release — they never
hold a task to completion (a task might run for hours; a lease shouldn't).
The wake path was the trickiest part to get right: an earlier version
destroyed the matched wake at lease time, which meant a worker crashing
between taking the lease and durably writing `TaskWoken` *lost the wake*. The
fix is the classic distributed-systems shape: at-least-once delivery (the
matched wake survives the lease and is re-delivered by the stale-lease sweep)
plus idempotent consumption (if a `TaskWoken` envelope already landed, the next
worker reconciles against the folded log instead of writing a second one).
Notably, the idempotency check draws from state that's *already in the log* —
no dedup schema was added, so no historical recording's bytes drifted. I want
to be precise about the guarantee's scope, though: it is **single-worker
durable exactly-once** — one SQLite store, one resident worker process.
Multi-worker fencing is not shipped. More on that below.

**Compaction is reversible.** When the context grows past the token budget,
Noeta summarizes the old prefix like everyone else — but the compaction is
*a recorded event, not an edit*. A `Compacted` event lands in the log carrying
a `summary_ref` and a boundary; the composer overlays the summary at
context-assembly time; the original messages are still in the log underneath.
So a recovered task compacts the same way it did the first time, and you can
always dig up exactly what was pared away. Compare: SDK-style auto-compaction
displaces the original content irreversibly, and archiving it is on you (via a
`PreCompact` hook).

**Audit is another fold.** The trace UI, the SSE stream feeding the web app,
post-hoc debugging — all the same operation over the same log. Every LLM call
has a `ContextPlanComposed` event recording which blocks were selected, what
was compacted, what was dropped, with provenance. "What did the model actually
see at step 40" is a query, not an archaeology project.

**Even fan-out gets cheap.** A parent can spawn N subtasks in one turn and
suspend on an N-way join. The join state isn't a new dispatcher mechanism —
it's an observer counting `SubtaskCompleted` events (by deduplicated
membership, not a bare count) on the parent's stream, then firing a single
composite wake. The group id is derived deterministically from the ordered
member ids (`sha256(":".join(subtask_ids))`), so a resumed fold recomputes the
same value byte-for-byte instead of needing it stored anywhere.

## What it cost

None of the above is free. Here's the bill, in roughly descending order of how
much it shaped the system.

**Determinism is a tax you pay everywhere, forever.** "Byte-identical fold"
needs a canonicalization layer — one function that renders any typed value
into stable bytes (sorted keys, tight separators, UTF-8), which the content
hashes, the snapshot round-trip, and the 4 KB size check all share. Every new
typed field must register with it or round-tripping breaks. Fold may not read
the clock, the disk, or the network — which sounds academic until it vetoes a
feature you want (next point).

**The KV-cache constraint locked down more than I expected.** LLM providers
cache KV state by prefix: if your prompt prefix is byte-for-byte identical to
the last call's, you skip re-encoding and re-billing it. So the context
composer splits the view into three segments by volatility — a stable prefix
(system prompt + tool schemas), a semi-stable middle (activated skills, memory
index), a dynamic suffix (the rolling conversation) — and the stable prefix
must serialize reproducibly across steps: sorted tool-schema keys, no
timestamps in the persona, a fixed field order. One rogue timestamp and your
inference bill multiplies. The satisfying part is that this is the *same*
determinism discipline fold already demands; the same rigor buys cache hits.
The unpopular part: the composer is a **locked** extension point. Users can
register new content kinds into the semi-stable segment, but they cannot
replace the composer wholesale, because an innocent-looking custom composer
that perturbs the prefix quietly destroys the cache economics. Freezing an
extension point to protect an invariant is not a fashionable API choice; I
made it anyway.

**Single-writer discipline feels bureaucratic until it saves you.** Fold can
only promise "replaying the log yields exactly what ran" if nothing mutates
state without going through the log. Task state is cut into four slices —
conversation stream, the policy's long-horizon memory, the context plan,
governance counters — each with exactly one writer. The strangest-feeling
consequence: the policy cannot assign to its own memory. It attaches a state
patch to the decision it returns; the engine lands that as an event; fold
writes it back. Every "just set the field" instinct has to be rerouted
through the ledger.

**Compaction couldn't copy the state of the art.** Claude Code's compactor
re-reads files from disk while summarizing, to backfill current content. Noeta
can't: compose-time disk reads break deterministic replay, and dereferencing
stale bytes from the ContentStore is worse (feeding an agent a stale copy of a
file it is actively editing). So Noeta's summary keeps only file *paths* and
lets the model re-read current content with its ordinary `read` tool, and it
protects a verbatim tail of recent messages that the summary never touches.
Constraint-driven divergence, not preference. One measurement from before the
current design, as a cautionary tale: a 74-turn task had *zero* compaction
events while a message-*count* gate silently dropped 97 early messages —
including the original statement of requirements. Count gates fire before
token gates and strip context invisibly; we removed ours.

**The partial-step-orphan edge is not solved.** Honest flag, because it's the
sharpest one: if a worker is killed *mid-step* — after `TaskWoken` and, say, a
`ToolCallStarted`, but before the step's remaining events land — the log holds
orphan events from a partial attempt. Fold still rebuilds a consistent state,
but a from-scratch replay does not reproduce the partial attempt. On resume
the worker detects this and raises a typed `PartialStepOrphan` error rather
than silently re-running the step; a human decides whether to re-drive or
close the task. Closing this properly needs an attempt-journal mechanism with
its own replay semantics — a real ADR's worth of work I haven't done. The
current stance is: refuse to guess, never corrupt silently. (Normal SIGTERM
shutdown doesn't hit this — the grace window and heartbeat handle it. This is
the `kill -KILL` / power-loss case.)

**The stack is synchronous, and there is no token streaming.** The provider
protocol is one pure call: `complete(request) -> LLMResponse`. The web UI
receives whole events over SSE — a finished assistant message, a finished tool
result — not a token stream. For a coding agent that mostly runs tools this is
tolerable; for a chat-first UX it's a visible gap. Recording a token stream
into an event-sourced log without either fragmenting the log or hiding
mutation behind an "in-progress" event is a genuinely interesting design
problem, and I'd rather ship it right than fast.

## Where this sits

**Versus the Claude Agent SDK.** Both give you an agent loop, tools, MCP,
sub-agents. The difference is the spine. Session JSONL is also an append log —
but it records the *conversation*, and Noeta records *events* from which state
is folded. One is a recording of a dialogue; the other is a state machine's
ledger. The ledger lets resume, compaction, and audit all land on a single
mechanism — resume is a refold, compaction is a recorded event, audit is
another fold — where the recording model needs separate logic for each. To be
fair in the other direction: the SDK is the lower-friction choice today. It
tracks official Anthropic capabilities closely, its ecosystem is larger, and
"install, add API key, go" is a real virtue. Noeta's trade is that you run the
infrastructure and own the substrate.

**Versus Temporal.** Temporal is also event-sourced and also durable, so the
comparison is really about who drives. In Temporal you define the workflow —
a graph of activities known ahead of time — and the engine durably executes
it. In Noeta the LLM drives control flow dynamically: the task's structure
*emerges* from the model's decisions and is discovered by reading the log
afterward, not declared up front. Noeta has no workflow primitive at all —
fixed procedures are just a deterministic policy plus subtask spawning. If you
know the shape of the work in advance, Temporal is the more mature tool; Noeta
is for when the model finds the shape as it goes.

**Versus LangGraph.** LangGraph's persistence is checkpointing: snapshots of
graph state saved at step boundaries, with resume and time-travel built on
choosing a checkpoint. (I'll hedge here — I know LangGraph as a user of its
docs, not its internals.) The mechanism-level difference is what the *primary
record* is. A checkpoint tells you where you were; an event log tells you what
happened and why, with the state derivable. In Noeta a snapshot is explicitly
disposable — delete them all and only performance changes — whereas
snapshots-as-truth make the snapshot format itself the long-term compatibility
contract, and causal questions ("what produced this state?") need whatever
diffing you can do between adjacent checkpoints.

## Status, honestly

Noeta is an early, **pre-1.0 preview**. It runs, it's tested, the core is
stable — and the boundaries are real:

- **Single-host, single-worker.** One SQLite store, one resident worker
  draining it, one step at a time. There isn't even a `workers` knob. The
  exactly-once wake guarantee is scoped to exactly this shape; multi-worker
  fencing, distributed timer due-checks, and completion-ordering across hosts
  are unshipped.
- **The partial-step-orphan edge** described above is open, and detection —
  not automatic recovery — is what ships.
- **Human-in-the-loop has no out-of-band channel.** The in-band flow is
  complete — wake events, client verbs (`answer` / `approve` / `deny`), and
  structured question forms in the web UI — but nothing notifies you (webhook,
  email, inbox) when a task starts waiting on a human. You watch, or you poll.
- **Worker reliability events are process-local** (structured logs, not
  EventLog events) — you mount your own sink to get them into monitoring.
- **The bundled frontend is a small Vite MPA**, vanilla ES modules, and will
  stay that way through the preview. The HTTP API is the integration surface.
- **The ecosystem is small.** Fewer built-in tools than the incumbents, no
  plugin marketplace, a young community.

What's next, in rough order: token streaming, the attempt-journal ADR for
mid-step crashes, and then the multi-worker slice.

The part I'd defend most strongly is the shape of the bet itself. Agents are
becoming long-lived processes that do consequential work, and the industry's
default persistence story for them is still "a transcript, plus vibes."
Making the log the truth costs you determinism discipline everywhere — and in
exchange, recovery, resume, replay, and audit stop being features you build
and start being properties you *have*.

---

Code and docs:

- Repo: [github.com/initxy/noeta](https://github.com/initxy/noeta)
- Docs: [initxy.github.io/noeta](https://initxy.github.io/noeta/)

Trying it is deliberately cheap: `pip install noeta-agent`, then
`python -m noeta.agent` — it boots fully offline against a deterministic stub
provider, no API key and no network needed, so the storage, fold, and wake
machinery are all inspectable on a fresh checkout. Wire a real Anthropic or
OpenAI-compatible endpoint when you want a live model.
