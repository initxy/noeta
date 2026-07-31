# One LLM round-trip always records the same three events; failure is a `stop_reason` value, not a separate event type

## Context

fold rebuilds a Task purely by walking its event stream, and a suspended task resumes by folding that stream and continuing. So every LLM round-trip has to appear on the stream as a complete, self-describing unit — including the round-trips that failed, because a failed call is real history a resume must land on identically. At the same time the Policy owns the decision about what to do with a provider failure (fail, fall back, compact and retry on an overflow), which rules out letting the exception escape upward.

## Decision

A single call through the runtime LLM client always produces **exactly three events** — `LLMRequestStarted`, `LLMResponseRecorded`, `LLMRequestFinished` — whether the provider answers or raises. A provider exception is translated into `LLMResponse(stop_reason="error", content=[], usage=Usage(), raw={...})`, with the failure category carried inside `raw` so a Policy can branch on it without re-deriving the exception class. That response still walks all three events and is then **returned** to the Policy rather than re-raised.

`stop_reason` is a closed set — `tool_use`, `end_turn`, `max_tokens`, `error` — in which `error` is a first-class value, not an out-of-band signal. Every failure mode is expressed through that value plus the category in `raw`; no outcome-bearing LLM event type exists beyond the three.

Transient retries do not multiply the trio. Intermediate attempts record no request/response pair, so one logical request is always exactly one trio; each scheduled backoff records only an observational retry marker that fold treats as a no-op, so a live consumer can render the stall without changing what a resume derives.

## Rationale

- **The constant three-event set is what fold and resume depend on.** fold expects every round-trip to appear as a complete triple. A failure branch that skipped the response event would leave a half-written call fold cannot rebuild, and a suspended task could not cleanly resume past it. Routing failures through the same three events keeps the triple intact and lets a failed call — a provider 5xx, say — be reconstructed exactly from history.
- **Returning the error preserves the Policy's intervention point.** The retry / fallback / compact-and-retry decision belongs to the Policy; an exception that unwinds to the Engine takes that decision away and leaves the stream with a request that has no recorded outcome.

## Alternatives considered

1. **On failure, skip the response event and append a dedicated failure event instead.** Rejected: it breaks the "one call, one triple" premise, leaves fold a half-written call, and enlarges the event-type surface for something a `stop_reason` value already expresses.
2. **Have the client raise and let the Engine catch it.** Rejected: the Policy loses its intervention point, and the stream degrades to a bare "started" with no recorded outcome, so the failure state cannot be reconstructed.
3. **Express failure as a union return type.** Rejected: every Policy path would have to branch on the shape, and canonical serialization would have to handle two of them. `stop_reason` already carries a non-success value in `max_tokens`, so putting `error` in the same set is consistent with what that field is for.
4. **Record every transient attempt as its own triple.** Rejected: a live backoff is not history. N triples for one logical request would make a resumed fold derive different state than the live run produced.

## Consequences

- The three-event recording and the exception-to-response translation live in `noeta.runtime.llm`; the event types are in `noeta.protocols.events`; the traversal that consumes them is in `noeta.core.fold`.
- Of the three, only the finish event folds state — the per-token and cost counters plus the real-usage baseline the compaction trigger reads. The other two are registered as fold no-ops so a recording never trips the unknown-event warning.
- Any new failure mode goes through a `stop_reason` value and a category in `raw`. A fourth outcome event for an LLM call is off the table.
