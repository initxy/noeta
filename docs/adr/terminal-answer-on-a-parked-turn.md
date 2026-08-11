# A multi-turn turn's terminal answer rides its `TaskSuspended`

## Context

A conversation is one Task driven over many turns. `MultiTurnReActPolicy`
implements that by rewriting a terminal `FinishDecision` into a
`YieldForHumanDecision` on the next-goal handle: the turn ends, the ledger stays
open, and the next human message resumes the same task. Reusing the wake-resume
primitive this way is what keeps the conversation path free of a second
lifecycle vocabulary (see [engine-policy-dataflow](engine-policy-dataflow.md)).

The substitution was lossy in exactly one field. `FinishDecision.answer` is the
turn's terminal result — and with `Options.output_schema` set it is the value
the kernel has already deserialized from the model's JSON, with a documented
fallback to raw text when the JSON is invalid. `YieldForHumanDecision` had no
`answer`, and `TaskSuspendedPayload` was `{reason, wake_on}`, so a conversation
never recorded that value anywhere: `TaskCompleted` is the only event that
carried an answer, and a conversation never writes one.

What a host was left with was the message projection — the assistant text of the
last turn, re-parsed by hand. That is not a smaller version of the same thing:
it re-does deserialization the kernel already did, it loses the invalid-JSON
fallback (the host sees raw text and cannot tell whether the kernel would have
called it an answer), and the projection's own `Result` item stringifies with
`str(answer)`, which turns a dict into Python repr rather than JSON. So a host
that wanted both "resumable conversation" and "structured terminal answer" could
have either, not both.

## Decision

### The answer travels with the decision that carries it

`YieldForHumanDecision` gains `answer: Any = None`, set by the multi-turn
wrapper from the `FinishDecision` it is replacing. `None` means "this suspend
stands in for no finish" — every other yield (a pending question, a parked
failure) leaves it alone.

### `TaskSuspended` records it, with `TaskCompleted`'s spill

`TaskSuspendedPayload` gains `answer` + `answer_ref` and reuses `_spill_answer`:
a value too large for the envelope's payload ceiling moves to the ContentStore
and `answer` holds `None`, exactly as on `TaskCompletedPayload`. One spill rule,
not two.

`answer_from_payload` widens to accept either payload. A caller holding a
lifecycle event should not have to know which one it is to ask the same
question.

### The read surface returns the raw value

`Client.task_answer(task_id)` walks back to the latest `TaskCompleted` /
`TaskSuspended` and returns the value unchanged — dict stays dict. The
`messages()` projection keeps stringifying, because it is the transcript: the
two surfaces answer different questions and neither should be bent toward the
other.

### It is additive, not a schema bump

`__canonical_omit_none__` covers both new fields, so a suspend carrying no
answer serializes byte-for-byte as it did before they existed, and
`_payload_restore`'s `TaskSuspendedPayload(**d)` reconstructs an older row
through the defaults. This is the same "new field absent from old recordings"
pattern `ModelBound` / `AgentBound` established — no `schema_version` bump is
owed, and replay over an existing store is unaffected.

## Alternatives rejected

1. **Leave it to the host: parse the assistant text.** Rejected — this is what
   the status quo forced, and it is the reason the decision exists. It duplicates
   the kernel's deserialization, silently drops the invalid-JSON fallback, and
   every host reinvents it slightly differently.
2. **Write a `TaskCompleted` and reopen the task.** Rejected: `TaskCompleted` is
   terminal and seals the ledger. Manufacturing one for a turn that has not
   ended, then un-sealing it, would make "terminal" mean nothing and break every
   reader that treats a terminal as final.
3. **A new `TurnCompleted` event carrying the answer.** Rejected: it adds a
   second settlement vocabulary for the same landing. Every consumer of
   `TaskSuspended` would have to learn to correlate two events that always
   travel together, and a resume would have to fold both to know one thing.
4. **Stash the answer in `state_patch` / `TaskState`.** Rejected: task state is
   the working state a resumed turn reads, not a per-turn result log. The answer
   would then need eviction rules of its own, and a fold would carry every past
   turn's answer forward in live state.
5. **Give `Result` the raw value instead of adding a read verb.** Rejected:
   `Result.answer` is typed `str` and consumed as display text; widening it to
   `Any` would break every renderer for the benefit of a different use case.
6. **Bump `schema_version`.** Rejected: an omitted-when-`None` additive field is
   precisely the case the existing convention says does not warrant one, and a
   bump would force adapters to handle a version difference that produces
   identical bytes.

## Consequences

- The multi-turn substitution is now lossless: everything a `FinishDecision`
  carried reaches the ledger, and `final=True` (which writes a real
  `TaskCompleted`) is unchanged.
- `TaskSuspended` acquires a result field, so it is no longer purely a
  lifecycle event. The `None` default keeps that narrow — the field is set by
  one branch, for one reason, and reads as absent everywhere else.
- Hosts that want the structured answer of a parked turn call
  `Client.task_answer`; hosts rendering a transcript keep using `messages()`.
- An oversized answer on a suspend costs a ContentStore write, the same as it
  already did on a completion.
