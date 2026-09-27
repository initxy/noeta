# Content store retention — 2026-09-27

Status: IMPLEMENTED, unreleased. Branch `fix/content-store-growth-2026-09-27`
(base 6496404, runtime 0.6.31 / sdk 0.6.31). Distil into CONTEXT.md and archive
once the release that carries it is cut.

## Problem

A host reported a 8.7 GB `tasks.sqlite` after 14 days (about 0.6 GB a day), with
the last 2 000 model calls averaging 553 KB of `content` each. Reproduced against
0.6.31 with a fake provider (16 KB tool output per step):

| steps | `content` total | full-request bodies | share | conversation payload |
| --- | --- | --- | --- | --- |
| 40 | 16.4 MB | 13.5 MB | 82 % | 0.65 MB |
| 80 | 60.3 MB | 53.1 MB | 88 % | 1.31 MB |

Two causes, one missing affordance:

1. `RuntimeLLMClient.complete` `put`s the whole canonical request (system prompt,
   tool schemas, the entire history) before every model call. The store dedups
   by hash, and every step's request differs from the last, so a task of N
   steps stores O(N²) bytes. Nothing reads the body back: fold treats
   `LLMRequestStarted` as a no-op, the audit observer prints the ref, the usage
   read model uses the model name and token counts. The request is fully
   derivable from folded state plus the composer.
2. `TaskSnapshot` bodies (the full four-slice state) are written every turn and
   every 20 consecutive tool calls; only the latest one per task is ever read
   as a fold baseline, yet all of them stay referenced by their events forever.
3. The `ContentStore` has no deletion. `Client.delete_task` purges events and
   dispatcher rows and leaves blobs "for offline GC", and no such sweep exists.
   `docs/operations/limitations.md` documents this as a known boundary.

## Goal

A long-running host's content store grows with the conversations it keeps, not
with the number of model calls it made. Concretely:

- A model call stores no request body by default. `request_ref` keeps its hash
  (the call's replay identity, byte-stable as before); only the bytes are gone.
- A host can reclaim unreferenced content, superseded snapshots and (legacy or
  opted-in) request bodies with one call that is safe to run while tasks are
  running, on sqlite and Postgres, and can shrink the sqlite file on request.
- Fold survives a reclaimed snapshot body by folding from scratch.

## Scope

### Runtime (`noeta-runtime`)

- `noeta.protocols.content_store.content_ref_for(body, *, media_type)` — the
  ref `put` would mint, without storing. Every built-in adapter mints its refs
  through it, so the hash rule has one home.
- `RuntimeLLMClient(record_requests=False)`. Off: `request_ref =
  content_ref_for(canonical request bytes)`. On: the previous behaviour (`put`).
  `LLMRequestStartedPayload` docstring says the body is optional.
- `noeta.core.fold.fold`: a `ContentNotFound` on the accelerated path's snapshot
  body falls back to the from-scratch fold (logged at debug). The re-base
  bodies (`TaskRewound` / `StepAttemptAbandoned` / `TaskForked`) stay required
  — they are never reclaimed.
- `noeta.storage.gc.collect_garbage(event_log, content_store, *,
  grace_seconds=3600.0, vacuum=False) -> GarbageCollection`. Mark-and-sweep:
  - **Roots**: every event payload in every stream, walked for `ContentRef`s
    and bare 64-hex strings (`ContextContentRecorded.content_hash`,
    `active_content` values), with two exclusions — `LLMRequestStarted` is
    skipped entirely (a request body is never retained), and of a task's
    `TaskSnapshot` events only the highest-seq one is a root.
  - **Transitive**: each reachable body is scanned for 64-hex strings (a tool
    output dict carrying an artifact ref, a snapshot's `plan_ref`, an
    `ImageBlock.source`), until no new hash appears. Over-approximation is
    fine; under-approximation is data loss, so the scan is textual, not typed.
  - **Sweep**: `content_store.sweep(live, grace_seconds=..., vacuum=...)` — a
    maintenance method duck-typed like `purge_task`, not on the L0 Protocol.
    A store without it makes the result `ok=False, reason="unsupported"`.
- `InMemoryContentStore.sweep` (so the memory backend stays a drop-in and the
  contract suite covers the rule on all three) and `CachedContentStore.sweep`
  (forwards, then drops its whole cache).

### Storage built-in (`noeta-sdk`)

- sqlite migration 12 / Postgres migration 7: `content.touched_at` (nullable
  epoch seconds; NULL on legacy rows) plus a covering index
  `(touched_at, size)`. `put` becomes an upsert that stamps `touched_at` on
  insert **and** on the dedup hit, so a body a running turn just re-used is
  never older than the grace.
- `sweep(live, *, grace_seconds, vacuum)`: candidates are the rows with
  `touched_at IS NULL OR touched_at < now - grace` read **before** the delete;
  the delete re-checks the same predicate under the adapter lock, so a `put`
  that races the sweep wins (sqlite: one connection, one lock; Postgres: the
  upsert's row lock and `READ COMMITTED` re-evaluation give the same order
  either way). `vacuum=True` runs `VACUUM` (sqlite: rewrites the file, holds the
  write lock for the duration — a quiet-hours call; Postgres: plain `VACUUM
  content`, non-blocking, space reused rather than returned).

### Client surface (`noeta-sdk`)

- `HostConfig.record_llm_requests: bool = False` → `SdkHost` →
  `RuntimeLLMClient(record_requests=...)`.
- `Client.collect_garbage(*, grace_seconds=3600.0, vacuum=False) ->
  CollectGarbageResult` (`{ok, swept, bytes_freed, live, vacuumed, reason?}`),
  exported from `noeta.sdk`; `noeta.sdk.storage.collect_garbage` for a host
  that sweeps a store without building a `Client`.

### Non-goals

- Automatic or scheduled sweeps. The host decides when (a doctor, a cron).
- Reclaiming dead history behind a rewind, or anything an event still
  references. Append-only holds; only the two named exclusions bend it.
- A retention policy by age or by task. `delete_task` plus a sweep is that.
- Shrinking a Postgres relation (`VACUUM FULL` is the operator's call).

## Key decisions

- **Hash-only `request_ref` by default, not a smaller body.** The request is
  derivable, the hash is what derive-and-compare needs, and the tests that read
  the body back can read `FakeLLMProvider.received_requests` instead. The
  opt-in exists for a debugging session, and a sweep treats a recorded body as
  transient (older than the grace ⇒ gone), which keeps one retention rule.
- **Grace by `touched_at`, not "refuse while running".** A sweep must be
  callable from a doctor while users chat; a lease check would make it a
  never-runs on a busy host, and the candidate-snapshot trick alone leaves the
  dedup race (an orphan row re-`put` by a live turn between mark and sweep).
  Stamping the dedup hit closes it with one column and one predicate.
- **Only the latest `TaskSnapshot` is a root.** Older ones are reachable only
  through a bounded fold (rewind, fork, step recovery), which now falls back to
  from-scratch. Slower on that rare path; correct.
- **Textual transitive scan.** The runtime does not know every body shape that
  can carry a ref (tool outputs are plugin-defined), so the closure keys on the
  64-hex pattern. Keeping a blob whose hash merely appears in a tool's text is
  the cheap direction of the error.

## Acceptance

- `make check` green (ruff, pytest ≥ 85 % coverage, mypy --strict both
  packages, naming and import lints).
- Contract suite (memory / sqlite / postgres): `sweep` removes only
  unreferenced rows older than the grace; a dedup `put` refreshes a row's
  age; `get` raises / `get_many` omits after a sweep.
- sqlite migration test: a v11 file upgrades to v12 with `touched_at` NULL on
  existing rows and the index present.
- Runtime: `LLMRequestStarted.request_ref` equals `content_ref_for` of the
  canonical request and the body is absent by default; present with
  `record_requests=True`. The ref hash is unchanged by the flag.
- Fold: deleting the latest snapshot body leaves `fold` equal to
  `fold(ignore_snapshots=True)`.
- Client on sqlite: after two turns and `collect_garbage(grace_seconds=0)`,
  superseded snapshots are gone, the latest stays, `messages` / `task_summaries`
  / a fresh `Client` resume all still work; a deleted task's private blobs are
  reclaimed and a blob shared with a live task is kept; `vacuum=True` shrinks
  the file; a store without `sweep` reports `unsupported`.
- Docs: `limitations.md` (en + zh) replaces "never garbage-collected" with the
  sweep; `options.md` / `sdk.md` (en + zh) list the new field, method and
  result type; CONTEXT.md's ContentStore and TaskSnapshot entries carry the
  retention rule; CHANGELOG Unreleased.

## Outcome

Everything in scope landed as specified; what changed shape along the way:

- `SweepOutcome` (`rows`, `bytes`, `vacuumed`) sits next to `content_ref_for`
  in `noeta.protocols.content_store` so both storage packages share one
  value type; the mark's event-log requirement is the `GcEventLog` Protocol
  (reader + task index, no writer) rather than `EventLogFull`.
- `CollectGarbageResult` is a frozen dataclass, not a `TypedDict` like
  `DeleteTaskResult` — it is also the return type of the bare-stack
  `noeta.sdk.storage.collect_garbage`, where a dict shape earns nothing.
- The sqlite sweep counts freed bytes from the candidate sizes and re-lists
  survivors only when a DELETE's rowcount falls short (no `RETURNING`, so the
  floor stays sqlite 3.24 for the upsert rather than 3.35); Postgres uses
  `DELETE ... RETURNING size`.
- Fold's fallback is scoped to `TaskSnapshot`: a missing re-base body still
  raises, because those are never reclaimed and their absence is corruption.
- Tests: contract suite gains six sweep cases over all three backends (row
  aging goes through each backend's private clock column, never the wall
  clock) plus a sqlite race case that wedges a re-put between the candidate
  listing and the DELETE; `tests/test_storage_gc.py` covers the two exclusions,
  the transitive artifact reference, delete-then-sweep, resume over a swept
  file, vacuum shrinking the file, the unsupported store and the cache
  wrapper; `test_sqlite_migrations.py` covers the v11 → v12 upgrade. Four
  existing tests that read request bodies back from the store now either pass
  `record_requests=True` or read `FakeLLMProvider.received_requests`.
- Not done here: the release itself (both packages need a patch bump — the
  runtime for `RuntimeLLMClient` / fold / the storage SPI, the SDK for the
  adapters, migration and client surface) and the Postgres leg of the contract
  suite, which only CI runs.
