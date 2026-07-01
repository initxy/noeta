# EventEnvelope.origin: a typed write-origin role marker

## Context

`EventEnvelope` needs a field that answers "which emission-point role in Noeta appended this event." The field was originally introduced to feed the canonical slicer input of verify's cross-stream injection replay. The verify/replay test machinery was later removed, and that consumer disappeared with it. `origin` is **kept** as "write-origin provenance": the audit trail (`AuditObserver` → `AuditRecord.origin`) and the events HTTP/JSON API both expose it, so the read model and the frontend can show which Noeta role wrote each event. The decisions below preserve the field's shape and rationale; the verify-slicer details no longer apply.

## Decision

`EventEnvelope` carries a typed field `origin: Literal["engine", "llm", "observer", "tool", "system"]`—the Noeta emission-point role that appended the event.

- `EventLogWriter.emit` defaults `origin` to `"engine"`; `system_emit` requires `origin` to be given explicitly (a system writer must declare its own role). Each write point declares it explicitly: Engine→`"engine"`, LLMClient→`"llm"`, ToolRuntime→`"tool"`, observers→`"observer"`, and system writes such as driver / snapshot→`"system"`.
- The `actor` field is kept, but its meaning is stripped apart: **`actor` answers "who is the subject of this event"** (the writer instance may be `child_observer`, `tool_runtime`, or a future `user_id`); **`origin` answers "which Noeta emission-point role"** (5 enum values). The two are orthogonal.
- `origin` lives in L0 (`noeta.protocols.events`) and introduces no new cross-layer dependency.

## Rationale

- **A typed `Literal` beats a bare string unrelated to the emission point.** mypy strict checks every incoming literal and rejects a typo like `"engin"`.
- **`actor` and `origin` being orthogonal leaves `actor` room to evolve.** Splitting "identity" and "role" into two fields lets `actor` freely evolve finer identity slots (`user_id`, `agent_instance_id`, `worker_id`) without disturbing the role marker.
- **The role is descriptive provenance worth keeping.** Even with verify gone, "which role wrote this event" is still readable audit / observability metadata, and both the audit trail and the events API expose it.

## Alternatives considered

1. **Stuff a prefix into the `actor` string** (`"observer:child_lifecycle"`). Rejected: parsing it back out is a magic string in disguise, and it breaks `actor`'s "identity" contract.
2. **Use `Optional[EventOrigin]` for a gradual migration.** Rejected: it would drag two codepaths along forever, and there is no legitimate "cannot backfill" burden here.

## Consequences

- The field definition lives in `noeta.protocols.events` (`EventEnvelope.origin` + the `EventOrigin` Literal), in L0.
- Each write point declares its own origin: `noeta.core.engine`, `noeta.runtime.llm`, `noeta.runtime.tool`, the observers, and the driver.
- `noeta.observers.audit` projects it into `AuditRecord.origin`; the events HTTP/JSON serializer exposes it.
- Cross-stream system writes go through `system_emit` (see `docs/adr/storage-protocols-l0.md`; origin is required there).
