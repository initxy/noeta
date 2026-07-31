# `EventEnvelope.origin` is a typed write-origin role marker, orthogonal to `actor`

## Context

Every envelope on an EventLog stream needs an answer to "which Noeta role
appended this?" — separate from "who is the subject of it". Audit and any read
model over the log want to classify writers without parsing an identity string.

## Decision

`EventEnvelope` carries `origin: Literal["engine", "llm", "observer", "tool",
"system"]` — the emission-point role that appended the event.

The business write path defaults `origin` to `"engine"`. The cross-stream system
write requires it explicitly: a writer appending onto a stream it does not hold
the lease for must declare its own role. The Engine writes `engine`, the LLM
client `llm`, the tool runtime `tool`, observers `observer`, and driver- or
snapshot-level system writes `system`.

`actor` is kept, with its meaning cleanly separated: **`actor` answers "who is
the subject"** — a writer instance label, and the place a user or worker
identity can land — while **`origin` answers "which role"** over a closed
five-value enum. The two are orthogonal.

`origin` lives in the protocols layer and introduces no cross-layer dependency;
the durable event-log backends persist it as a column and restore it on read.

## Rationale

A typed `Literal` beats a bare string: strict type checking rejects a typo at
the write site instead of letting a misspelled role reach the log, where it is
permanent.

Keeping the two orthogonal leaves `actor` room to grow finer identity slots
without disturbing the role vocabulary, and lets the role vocabulary stay closed.

The role is descriptive provenance worth carrying on every envelope: the audit
projection reads it straight onto its record, so "which role wrote this"
survives into the audit trail and any read model built over it.

## Alternatives considered

1. **Encode the role as a prefix inside the `actor` string** (`observer:...`).
   Weighed and rejected: parsing it back out is a magic string in disguise, and
   it breaks `actor`'s identity contract.
2. **Make the field optional so writers can adopt it gradually.** Rejected: it
   would carry two codepaths, and no writer has a legitimate reason to be unable
   to state its own role.

## Consequences

- The field and its `Literal` live in `noeta.protocols.events`; the write
  contract, including the explicit-origin requirement on system writes, is
  stated on the event-log protocol (see `storage-protocols-l0.md`).
- Every write point declares its own origin: the Engine, the LLM runtime, the
  tool runtime, the observers, and the driver.
- The audit observer projects it onto its record; the SQLite and Postgres
  event-log backends persist and restore it.
