# Fault-tolerant LLM event recording: the always-three-events-per-call contract

## Context

The repository's verify/replay machinery (fingerprints, byte-equivalent replay, that whole apparatus) has been retired and removed entirely. But the constraint this decision settles is **not** part of verify/replay — it is a **fold/resume** requirement, governing what the EventLog must carry so that a task can be deterministically rebuilt and continued. fold/resume is still in use, so this three-events contract still holds, entirely independent of the deleted verify/replay tooling.

## Decision

A single call to `RuntimeLLMClient.complete` always produces **exactly three events** (`LLMCallStarted` / `LLMResponseRecorded` / `LLMCallFinished`), **regardless of whether the provider raises**, written as one atomic group. When the provider raises, the failure is translated into `LLMResponse(stop_reason="error", content=[], raw={"error": ...})`, still walks through all three events, and is then **returned** to the Policy (not re-raised), preserving the Policy's intervention point (retry / fallback).

The value set of `stop_reason` is **closed**: `Literal["tool_use", "end_turn", "max_tokens", "error"]`, in which **error is a first-class value**, not an out-of-band signal. The EventLog's set of LLM event types is **permanently closed at three** — every failure mode is expressed via a `stop_reason` literal, without introducing a separate event type like `LLMCallFailed`.

## Rationale

- **The constant three-event set is exactly what fold and resume depend on.** As fold walks the EventLog, it expects every LLM call to appear as a complete triple; if the failure branch skipped `LLMResponseRecorded`, it would leave a half-written call that fold can't rebuild, and the suspended task couldn't cleanly resume past it. Sending failures through the same three events keeps "the three events form a group" intact while also allowing a failed call (say a production 5xx) to be reconstructed precisely from history.

## Alternatives considered

1. **On failure, skip `LLMResponseRecorded` and add an `LLMCallFailed` event instead.** Rejected: it breaks the "three events form a group" premise, leaves fold a half-written call, and enlarges the event-type surface.
2. **On failure, have `RuntimeLLMClient` raise directly, to be caught by the outer Engine.** Rejected: the Policy loses its intervention point (the retry / fallback decision gets swallowed by the Engine), and the EventLog degrades to "only Started," so the failure state can't be reconstructed.
3. **Express failure with a Result union `LLMResponse | LLMError`.** Rejected: a union return value has poor ergonomics, forcing every Policy path to branch and making canonical serialization handle two shapes; `stop_reason` already carries `"max_tokens"`, so putting `"error"` in it is consistent with that value set.

## Consequences

- The landing point is `noeta.runtime.llm` (the three-event atomic write in `RuntimeLLMClient.complete`, and translating a failure into `stop_reason="error"` before returning it to the Policy); the event types themselves are in `noeta.protocols.events`, and fold's traversal is in `noeta.core.fold`.
- Constraint: the set of LLM event types is permanently closed at three; any new failure mode goes through a `stop_reason` literal, and no new event type may be added.
- Watch-out: even though the verify/replay machinery has been removed, this contract does not lapse with it — it is a hard constraint of fold/resume, so when you touch the LLM event-recording path you must preserve "a single call always produces the three events as a group."
