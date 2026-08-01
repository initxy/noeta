# State and writers

A task's ground truth is its append-only event log; its state at any moment is
computed by folding that log, never stored as a first-class copy. That promise
only holds if nothing can change state without leaving an event behind.

This page covers the three mechanisms that enforce it: state cut into slices
with exactly one writer each, author markers that cannot be forged, and a fold
that still reads recordings written by an older version of the code.

The concept behind it is [Event sourcing](../concepts/event-sourcing.md); this
is the architecture that makes it true.

## Four slices, one writer each

If any component could assign to any part of state, fold's rebuild would stop
matching what actually ran. So task state is cut into four typed slices
(`packages/noeta-runtime/noeta/protocols/task.py`), each with exactly one
writer:

| Slice | Sole writer | Holds |
| --- | --- | --- |
| `RuntimeState` | Engine | the rolling conversation messages, per-turn usage, last transition, last input-token count |
| `TaskState` | Policy — only via a `TaskStatePatch` on a Decision | goal, phase, todos, decision records, active content |
| `ContextState` | fold | context plan ref, compaction summary, per-turn thinking, content anchors |
| `GovernanceState` | fold | cost, iteration and token counters, denials, subtask results, provenance |

The telling cell is `TaskState`. The Policy is where an agent's long-horizon
memory lives, and the Policy **cannot assign to it**. It attaches a
`TaskStatePatch` to the Decision it returns; the Engine lands that as an event;
fold writes the slice back. The write is a recorded fact, not a mutation.

`ContextState` is the second telling cell. The composer produces the context
plan, but it never writes `task.context` — it puts the plan body in the
ContentStore, the Engine attaches the resulting ref to a `ContextPlanComposed`
envelope, and fold derives the slice from that. Compaction state, stripped
thinking blocks, and content anchors all arrive the same way, through fold
handlers alone.

`GovernanceState` is derived end to end. Nothing patches it, which is why a live
run and a resumed run see identical counters.

One `RuntimeState` field is worth calling out: `last_input_tokens` holds the
input-token count the provider reported for the most recent round-trip. The
compaction trigger uses that real number as its baseline and estimates only the
messages appended since, because a pure character heuristic systematically
undercounts prompts carrying caching, structured blocks, or images.

## Two kinds of author marker

"Who wrote this" is recorded at two different levels, and they answer different
questions.

**On the envelope** — every event carries an `actor` and an `origin`. `actor` is
a free-form identity string (`"engine"`, `"llm"`, `"plugin:environment"`);
`origin` is the closed vocabulary `engine` / `llm` / `observer` / `tool` /
`system`, naming the Noeta *role* that wrote it. Readers such as
`AuditObserver` classify on `origin` and attribute on `actor`.

**On a message** — a `Message` may carry `origin`, one of `human` / `system` /
`memory`, defaulting to `None` meaning "the role's natural author". It is
orthogonal to `role`: the role says which channel the turn rides, the origin
says who authored it. A memory recall and a user's question are both
`role="user"`, and only `origin` distinguishes them.

Message origin is **single-writer too**. Only the Engine's recording path,
`Engine.append_user_message`, may set it. A message a Policy synthesizes has its
origin stripped (`strip_message_origin`) at every Decision seam that accepts
one, so a Policy cannot pass off its own text as a human turn. A marker forged
in model or tool output is just text — it never reaches the field.

Vendor tag syntax never enters the ledger either. The Anthropic adapter wraps
`system` / `memory` injections in `<system-reminder>` on the wire; the OpenAI
adapters render them as mid-history system messages led by a self-describing
preamble line, because the system role alone does not tell an arbitrary model
"this is not the user speaking". The recording stays neutral.

## The lease is the enforcement

Slice ownership is a design rule. What makes it mechanical is the lease.

A Worker holds a `Lease(lease_id, task_id, expires_at)` — an exclusive,
heartbeat-renewed hold on one task — and presents `lease_id` on every EventLog
append. The log consults the dispatcher (through the deliberately narrow
`LeaseRegistry.is_lease_valid`) on every emit, so **only the holder of a live
lease can write to a task's stream**.

That is what makes concurrent workers safe. A worker whose lease was reclaimed —
a long GC pause, a stalled step — is rejected at the append rather than allowed
to land a write behind the new generation. On Postgres the check is an
in-transaction `FOR SHARE` row test evaluated against the database clock, which
takes per-host clock skew out of the decision; SQLite and in-memory are
single-host.

Observers see each envelope synchronously after it commits, on the writer thread
but **outside** the writer lock, with their exceptions swallowed. An observer is
a reader; it can never become a second writer.

## Durable wake, once

Suspension and resume run through the same invariant.
[Wake & resume](../concepts/wake-resume.md) states the guarantee; the mechanism
is four rules:

- The dispatcher matches an incoming wake event to a suspended task by
  identity-field projection and holds the match durably. Delivery happens at
  lease time, via `Lease.wake_event`.
- The Worker threads the wake into `engine.note_woken`, which writes
  `TaskWoken(wake_event=…)` before the step continues. **That write is the
  durability commit point.**
- The match **survives the lease**. It is cleared only by a consuming
  `release(consumed_wake_event=…)`. A crash between lease and the `TaskWoken`
  write leaves the wake in place; `requeue_stale()` returns the task to ready and
  the next lease re-delivers the same wake.
- Consumption is **idempotent**. The Worker's woken branch is a recovery state
  machine keyed on the latest matching `TaskWoken`: a re-delivery whose
  `TaskWoken` already landed is reconciled instead of writing a second one.

At-least-once delivery plus idempotent consumption is exactly-once, and the
lease fence makes it single-worker. A resume attempt on a suspended task with no
queued wake reports the typed diagnostic `suspended_without_wake_event` —
"waiting for something that has not happened yet", not a fault.

A crash mid-step recovers on the next lease: the interrupted attempt is sealed
with a `StepAttemptAbandoned` marker and re-driven when it was side-effect-free,
or the task is parked for a human. The scope and the open edges are catalogued
in [known limitations](../operations/limitations.md).

## Folding recordings written by older code

Payloads and slices evolve, but a task suspended six months ago must still fold
under today's code. Three mechanisms carry that, and it is worth being precise
about what each does.

**Canonical bytes are order-independent.** `to_canonical_bytes` serializes with
sorted keys and compact separators, so where a field sits in a dataclass
declaration has no effect on the bytes. Field order is a readability
convention, not a compatibility mechanism.

**Byte-equality across versions comes from opting out of `None`.** A dataclass
declares `__canonical_omit_none__` naming fields that vanish from the byte
stream when unset. Add a field to such a class, default it to `None`, and an old
recording (which never had it) and current code (folding it to the default)
serialize to the same bytes — so hashes computed then still match now.

**Restoring tolerates keys that no longer exist.** `restore_dataclass` filters a
stored body down to the fields the current class declares, rather than splatting
unknown keys into a constructor that would raise. It covers the snapshot's
`GovernanceState` — the slice that accretes counters — and the event-payload
restore path.

Where a snapshot predates the accumulation fields entirely, fold **discards it
and replays from genesis**: slower, never wrong. That is a deliberate default —
a snapshot is an acceleration point, and a snapshot-free fold rebuilds the same
state.

## Where to go next

- [Packages and boundaries](packages.md) — the import rules under all of this
- [Fold & snapshot](../concepts/fold-and-snapshot.md) — the concept and its
  consequences
- [Wake & resume](../concepts/wake-resume.md) — the delivery guarantee in full
- [Known limitations](../operations/limitations.md) — where recovery stops
