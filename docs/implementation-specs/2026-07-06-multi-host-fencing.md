# Implementation spec: multi-host Postgres lease fencing

Target ADR: `docs/adr/multi-host-lease-fencing.md`
Date: 2026-07-07
Scope: Postgres adapter only. Zero code changes to sqlite or in-memory adapters, zero EventLog envelope/payload changes.

## Goal

Close the two multi-host correctness gaps identified in the ADR:

- G1 (zombie-append window): between `is_lease_valid` returning true and the emit INSERT committing, another host can reclaim the lease and a new generation can start; the zombie's write lands after the new lease's writes, breaking the completion-order theorem step-attempt recovery relies on.
- G2 (clock skew): per-host `time.time()` clocks can diverge, producing split-brain lease-liveness views and early/late timer fires.

Plus a lightweight observability addition: persist `worker_id` on the dispatcher row.

## Non-goals (out of scope)

- Any sqlite or in-memory adapter behaviour change (single-host semantics stay).
- Any EventLog envelope, payload, event-type, or idempotency-key schema change.
- Any Dispatcher / LeaseRegistry / EventLog Protocol surface change (no new verbs, no new method signatures).
- Any change to worker InvalidLease handling (already correct per review — §"Worker-side verification" below).
- Any change to `enqueue()` semantics on leased tasks (force-clear preserved; flagged for maintainer).
- Closing the seed-path "enqueue then targeted lease" race (other hosts can steal between the two calls; flagged for maintainer).
- Cluster membership, service discovery, leader election, or deployment topology.
- A release / version bump / changelog (maintainer handles per `docs/releasing.md`).
- Migration for sqlite (its multi-host story does not exist; the worker_id column is a nice-to-have for sqlite too, covered below but optional).

## Schema changes (Postgres migration 3)

Add one column to `dispatcher_tasks`:

```sql
-- Postgres migration 3
ALTER TABLE dispatcher_tasks
  ADD COLUMN worker_id TEXT NULL;
```

No new indexes. `worker_id` is populated by `lease()` and cleared by every transition that drops `lease_id`. It is nullable (existing rows; terminal/suspended/ready states don't carry a worker). No CHECK constraint needed — it is an audit/observability field, not a correctness field.

Apply at the end of `MIGRATIONS` in `packages/noeta-runtime/noeta/storage/postgres/migrations.py`, updating `SCHEMA_VERSION` to 3. A migration transaction already runs under `_ADVISORY_CLASS_MIGRATIONS`, so concurrent migrators serialise.

Sqlite can pick up the same column in migration 9 if desired, but this round's correctness guarantees are Postgres-only, so sqlite can defer. If added, match the pattern of existing `ALTER TABLE ADD COLUMN` statements in `packages/noeta-runtime/noeta/storage/sqlite/migrations.py`.

## Per-file change list

### `packages/noeta-runtime/noeta/storage/postgres/eventlog.py`

Precede the out-of-tx `self._lease_validator.is_lease_valid(...)` call (currently at lines 258-268) with an in-tx SELECT ... FOR SHARE probe, issued on `self._conn` inside the already-open transaction, **at the same position the validator call sits today** (after the idempotency dedup and the `expected_seq` check, before the INSERT). Placement matters twice over: moving it earlier would flip the StaleSequence-before-InvalidLease error precedence and would validate leases on the idempotent-retry path, both of which the contract suite pins.

```python
if (
    require_lease
    and lease_id is not None
    and self._lease_validator is not None
):
    # In-tx fence probe (ADR multi-host-lease-fencing.md D1):
    # select the dispatcher row FOR SHARE so a concurrent
    # reclaim / release / heartbeat-cap UPDATE blocks until this
    # emit commits or rolls back. A returned row proves the lease
    # current in THIS database — skip the registry. Zero rows →
    # fall back to the bound registry (mixed wiring, e.g. an
    # InMemoryDispatcher validating this log, has no row here).
    if self._db_clock:
        row = self._conn.execute(
            "SELECT 1 FROM dispatcher_tasks "
            "WHERE task_id = %s AND lease_id = %s "
            "  AND status = 'leased' "
            "  AND lease_expires_at > EXTRACT(EPOCH FROM clock_timestamp())::double precision "
            "FOR SHARE",
            (envelope.task_id, lease_id),
        ).fetchone()
    else:
        # Test seam: caller injected clock; use Python-side time
        # to preserve deterministic contract tests.
        row = self._conn.execute(
            "SELECT 1 FROM dispatcher_tasks "
            "WHERE task_id = %s AND lease_id = %s "
            "  AND status = 'leased' "
            "  AND lease_expires_at > %s "
            "FOR SHARE",
            (envelope.task_id, lease_id, self._clock()),
        ).fetchone()
    if row is None and not self._lease_validator.is_lease_valid(
        envelope.task_id, lease_id
    ):
        raise InvalidLease(
            f"task_id={envelope.task_id}, lease_id={lease_id}"
        )
```

No validator bound → no check at all (unchanged — `test_no_validator_accepts_any_lease_id` pins this). The `dispatcher_tasks` table always exists in the eventlog's database (migrations run at construction), so the probe is well-formed even when a mixed wiring keeps it permanently empty.

Add a `self._db_clock` boolean: when constructed with the default clock (i.e. `clock is None` and we are using `time.time` at production runtime), switch to DB-clock comparisons; when an explicit clock callable is injected, use client-side comparison (the contract tests all inject deterministic clocks and must not change).

Also add a test-only `_emit_pause: Callable[[], None] | None` constructor keyword (default None), invoked between the fence probe and the INSERT — the deterministic hook the multi-host tests use to hold an emit transaction open across a concurrent reclaim (see "How to open the check-commit window in tests").

The `LeaseRegistry.bind_lease_registry` wiring is retained and load-bearing: the registry is the fallback for mixed wirings and the gate that enables the probe; same-database wirings simply stop paying its cross-connection round trip whenever the probe finds the row.

### `packages/noeta-runtime/noeta/storage/postgres/dispatcher.py`

1. **DB clock for lease-expiry (D2).** When `now` is not injected (production default), expiry computations use `EXTRACT(EPOCH FROM clock_timestamp())::double precision` issued via `self._conn.execute(...)`. Specifically:
   - `lease()` currently does `expires_at = self._now() + lease_seconds` (line 307). In DB-clock mode, issue `SELECT EXTRACT(EPOCH FROM clock_timestamp())::double precision AS now` and use that value for both the written `lease_expires_at` and the returned `Lease.expires_at`.
   - `heartbeat()` same (line 376).
   - `requeue_stale()` currently does `now = self._now()` (line 691) and uses the parameterised `%s` for the SELECT; in DB-clock mode, inline `EXTRACT(EPOCH FROM clock_timestamp())::double precision` in the query (or SELECT it first and use the value) so the stale-selection cut-off uses DB clock.
   - `fire_due_timers(now=...)`: the Protocol explicitly passes `now` as a caller-supplied wall-clock value (see dispatcher.py Protocol lines 239-263). Keep accepting the parameter for the in-memory/sqlite path; for Postgres in DB-clock mode, the read-probe and write sweep compare `fire_at <= EXTRACT(EPOCH FROM clock_timestamp())::double precision` instead of the passed-in `now`, because the timer deadline (`fire_at`) is stored as a wall-clock epoch. This is a deliberate semantic choice: on a single host, caller `now` and DB `now` are the same clock; across hosts they can diverge by skew, and we want the due-check to be driven by one clock (DB) to avoid split-brain "timer due / not due" between two hosts. The passed-in `now` is still used when a clock is injected (test seam).
   - `is_lease_valid()` (lines 817-838) and `has_active_lease()` (lines 856-878) use DB-clock in production, client clock when injected.

2. **Record `worker_id`.**
   - `lease()`: `UPDATE dispatcher_tasks SET ... worker_id = %s ...` (and insert the passed-in `worker_id` instead of the current `del worker_id`).
   - Every state transition that clears `lease_id` must also clear `worker_id`: release (terminal and suspended paths), release_yield, fail (both branches), heartbeat-cap forced release (both branches), requeue_stale (both reclaim-cap-terminal and ready branches), fire_due_timers, enqueue (non-ready force-clear branch), restore_task (all three status branches, since it wipes and replaces the row), purge_task (deletes the row). The easiest approach is to add `worker_id = NULL` to the existing `lease_id = NULL` SET clauses at each of these sites.

3. **`__init__`**: no new public parameters; detect DB-clock mode by checking `now is None` (since `now=None` falls back to `time.time` at line 107 today, refactor to set `self._now = now` (allowing None as sentinel) and route through a helper `self._db_now_sql()` that returns either a SQL fragment (`"EXTRACT(EPOCH FROM clock_timestamp())::double precision"`) or a `%s` placeholder plus value depending on mode.

4. **Bounded row-lock waits (stall containment).** `_begin_locked()` issues `SET LOCAL lock_timeout = '5000ms'` immediately **after** the advisory acquisition (so host-to-host advisory serialisation stays unbounded, as today). Rationale: the D1 fence probe lets an emit transaction hold a row-share lock for its full duration; a wedged emitter would otherwise block a `requeue_stale` / force-clear `enqueue` / `restore_task` UPDATE while that transaction holds the **global** dispatcher advisory lock — an unbounded fleet-wide stall. With the bound, the blocked transaction aborts with `psycopg.errors.LockNotAvailable`, rolls back, and is retried by the next sweep; the worker's sweep wrapper already catches and logs. The timeout lives in `self._row_lock_timeout_ms` (private; tests shorten it).

### `packages/noeta-runtime/noeta/runtime/worker.py`

Zero code changes required. Worker-side verification below confirms the existing exception policy is already correct. Document this explicitly in a short code comment near `WorkerLoop._execute_step` InvalidLease handler if desired (optional, not required for correctness).

### `packages/noeta-runtime/noeta/core/observers.py`, `execution/*`

No code changes for fencing correctness. See "Maintainer decisions" below for two optional follow-ups that are out of scope for this round.

## Testing plan

All new tests are **additive** to existing contract suites; no existing test is modified.

### worker_id tests (Postgres-only)

`worker_id` is recorded by the Postgres adapter only this round (memory/sqlite keep `del worker_id` — single-host adapters, spec §Non-goals), so its tests live in the new postgres-only multi-host file rather than the shared contract suites (a cross-backend contract test would require implementing the column in all three adapters).

1. **`test_lease_records_worker_id`**: enqueue → lease(worker_id="w42") → read the row back via direct SQL and assert worker_id == "w42". On release, worker_id is NULL.

2. **`test_enqueue_force_clears_worker_id`**: enqueue → lease(worker_id="w1") → enqueue (force-clear) → lease(worker_id="w2") → verify the row's worker_id is "w2".

These are additive and do not change existing semantics.

### Postgres-only multi-host contract tests

Add a new `tests/test_dispatcher_multi_host.py` (or extend test_dispatcher_contract.py with a `make_two_postgres_dispatchers` fixture) that opens **two PostgresDispatcher + two PostgresEventLog instances** against the same DSN (same isolated schema, two connections), simulating two hosts.

1. **`test_zombie_emit_after_reclaim_is_rejected`**: On dispatcher A, enqueue and lease task "t1" (lease_id L_A). On event_log A, open an emit transaction (use a testing seam: expose or script a slow emit that pauses between FOR SHARE and COMMIT — see "How to open the G1 window in tests" below). From dispatcher B, advance the clock so L_A appears expired, run `requeue_stale()` (succeeds, returns ["t1"]), lease "t1" to L_B, emit one event under L_B (commits). Then resume event_log A's emit — expect InvalidLease, and event_log A's event must NOT be in the stream. Fold must see exactly L_B's event, no interleaving.

2. **`test_zombie_emit_blocks_reclaim_then_commits_first`**: On dispatcher A, enqueue and lease "t1". Emit one event under L_A (holds FOR SHARE on dispatcher row). From dispatcher B (with clock advanced), run requeue_stale. The reclaim UPDATE blocks waiting for A's transaction. Commit A's emit — B's requeue_stale then proceeds but sees the lease as still held (if heartbeat extended it) or reclaims it. Either outcome is acceptable per the theorem; the assertion is that A's event seq < B's first post-reclaim event seq.

3. **`test_double_sweeper_fires_timer_once`**: With two dispatchers on one DSN, enqueue+lease+release-suspended-on-TimerFired (fire_at = now+1s) on dispatcher A. Advance clock past fire_at. Concurrently call fire_due_timers on BOTH dispatchers (use a barrier to start them at the same time). Assert exactly one of them returns the task in its fired list (or both return it but the second one finds zero due rows and returns []); lease the task and check that exactly one matched wake is delivered (no duplicate TaskWoken).

4. **`test_two_hosts_lease_fifo_no_duplicate`**: Enqueue 20 tasks. From two threads using dispatcher A and dispatcher B respectively, race to lease until the queue is empty. Assert each task is leased exactly once (use the unique lease_id per task to verify).

5. **`test_heartbeat_invalid_after_remote_reclaim`**: Lease on A, advance B's clock past expiry, B requeue_stale(), then A heartbeat() must raise InvalidLease.

6. **`test_release_after_remote_reclaim_is_invalid`**: Same setup — release() on A must raise InvalidLease.

7. **`test_db_clock_used_when_now_not_injected`** (postgres-only): construct PostgresDispatcher without `now`, verify lease_expires_at written is within a small tolerance of DB `clock_timestamp()` (not Python `time.time()`), using a direct SQL read-back. This guards against regressions that accidentally re-introduce Python clock.

8. **`test_clock_skew_emulation`** (postgres-only, injected clocks): build two PostgresDispatchers with injected `now` callables where A's clock is 10s behind B's. Lease on A with lease_seconds=30. From B's perspective the lease expired; run requeue_stale on B. With D1 in place, A's still-valid (from A's perspective) emits — but since the lease check uses FOR SHARE and B's requeue UPDATE will either wait for A's active transaction to complete (if A is mid-emit) or see the lease as expired if A is idle. The point is to assert that when A's clock is behind and A is idle (not in an emit), B can reclaim, after which A's next emit gets InvalidLease despite A's local clock still thinking the lease is valid. **Note**: in DB-clock mode (production), this skew cannot happen; the test is for the injected-clock path and documents the invariant.

### How to open the check-commit window in tests

To reliably reproduce the G1 window (between the lease check and the INSERT commit), tests need a way to pause the emit mid-transaction. Two approaches:

- **Adapter seam for tests**: add an optional `_emit_pause: Callable[[], None] | None` parameter to PostgresEventLog.__init__ (production: None; tests pass a barrier/Event). Call it between the FOR SHARE lease check and the INSERT, holding the transaction open while the test drives the reclaim from the other dispatcher. This is a test-only hook (leading-underscore, not part of the Protocol).
- **Use a real race**: don't add a seam; instead, have thread A do an emit that takes a long time (e.g. via a slow subscriber that blocks on a lock held by the main thread), drive the reclaim from thread B, then let the subscriber proceed. This is flakier and depends on scheduling, but avoids adding a test hook.

Recommendation: add the `_emit_pause` hook; it is one optional argument, guarded by a `__debug__` or naming convention, and it lets the test be deterministic.

### EventLog contract additions

Add to `tests/test_event_log_contract.py` (cross-backend): none for multi-host (these require two connections). Add postgres-only variants in the new multi-host test file.

### Worker tests

`tests/test_worker_loop.py` already exercises InvalidLease paths. Confirm (via code inspection already done; optionally add a focused test) that when emit raises InvalidLease mid-step, the worker does NOT call fail(). Already covered by the existing exception policy in `_execute_step` (separate `except InvalidLease` clause above the generic `except Exception`).

## Acceptance criteria

1. `pytest tests/test_dispatcher_contract.py tests/test_event_log_contract.py tests/test_backend_multiworker.py tests/test_durable_wake.py tests/test_attempt_recovery.py tests/test_worker_loop.py` passes on all three backends with **zero modifications** to those existing test files.
2. New postgres-only multi-host tests pass; each scenario demonstrates the invariant (zombie reject, double-sweeper once-firing, cross-host FIFO).
3. `ruff check` clean on modified files.
4. A real Postgres instance (the suite's existing `NOETA_TEST_POSTGRES_DSN` flow) shows all postgres parametrised tests green.
5. Manual reasoning review of every lifecycle transition (enumerated in ADR §"Per-transition fence argument") confirms no remaining window. Pay particular attention to:
   - wake() arriving while a lease is held but the worker is mid-emit (buffered correctly).
   - heartbeat-cap force-release racing with an emit (emits FOR SHARE blocks the UPDATE, then is rejected post-commit).
   - restore_task/purge_task racing with an emit (both take the global dispatcher lock; an emit's FOR SHARE blocks their UPDATE; if restore/purge commit first, the emit sees zero rows and raises InvalidLease).
6. Old recordings (existing snapshot test fixtures in `tests/snapshots/`) fold and replay byte-identically; there are zero changes to event types, payloads, or envelopes so this is by construction, but run the snapshot-using tests (`test_compaction_*`, `test_snapshot_fold_acceleration`, etc.) to confirm.
7. The seal / step-attempt-recovery flow still works across two hosts (an interrupted attempt on host A is recovered on host B; the seal is a lease-checked append under B's lease, and A's zombie writes are fenced). Exercise with a focused test using two dispatchers + a simulated crash (close dispatcher A mid-step, run requeue_stale on B, lease on B, confirm classification → seal → re-drive under B's lease produces the expected events).

## Lock ordering (deadlock freedom argument)

With D1's FOR SHARE, two lock types are now held in an emit transaction:

1. Per-stream advisory lock (`pg_advisory_xact_lock(_ADVISORY_CLASS_EVENTS, hashtext(task_id))`, eventlog.py:202-205).
2. Row-share lock on the dispatcher row for the same task (the new `SELECT ... FOR SHARE`).

Dispatcher lifecycle transactions hold:

1. Global dispatcher advisory lock (`pg_advisory_xact_lock(_ADVISORY_CLASS_DISPATCHER, 0)`, dispatcher.py:136).
2. Row-level write locks (via UPDATE/DELETE) on the dispatcher row(s) they modify.

Dispatcher transactions never touch the `events` or `idempotency` tables (confirmed by code review of postgres/dispatcher.py: every SQL statement references only `dispatcher_tasks` and `dispatcher_pending_wakes`). EventLog transactions never acquire the global dispatcher advisory lock (eventlog.py never references `_ADVISORY_CLASS_DISPATCHER`).

Wait-for graph analysis:
- EventLog transactions hold (stream advisory lock) → wait for (dispatcher row share).
- Dispatcher transactions hold (global dispatcher advisory lock) → wait for (dispatcher row exclusive, which conflicts with share).
- Dispatcher transactions do NOT acquire any stream advisory lock.
- EventLog transactions do NOT acquire the global dispatcher advisory lock.

No cycle exists. The dispatcher row FOR SHARE is only ever waited on by one side (dispatcher transactions, which hold the global advisory lock first; and eventlog transactions, which hold the stream lock first) — but the two lock classes (global dispatcher vs per-stream) are disjoint sets, so the two wait paths cannot reach back to each other.

Concurrent emits on different tasks take different per-stream advisory locks and do not conflict on dispatcher rows (different task_id → different row). Concurrent emits on the same task are serialised by the per-stream advisory lock (one emit at a time per task).

ContentStore transactions (`INSERT ... ON CONFLICT DO NOTHING`) hold no advisory locks and take no row locks on dispatcher/event tables; they are always leaf locks. Deadlock-free.

Idempotency table INSERTs happen inside the emit transaction under the per-stream lock; no additional lock class. Deadlock-free.

## Maintainer decisions (flagged, not solved in this round)

1. **`enqueue()` on a currently-leased task.** The current behaviour force-clears the lease (any non-ready status → ready, clearing lease_id, matched_wake_event_canonical, worker_id). Contract tests pin ready/terminal/suspended behaviour but not leased behaviour. Call-site review shows no in-tree caller invokes enqueue on a leased task today (all four non-test callers enqueue freshly-created or just-released tasks). Options:
   - (a) Keep as-is (force-preempt). Simple, works with D1 (the preemption is a dispatcher transaction that blocks and then fences zombie emits).
   - (b) Make enqueue a no-op when status='leased' (preserves the lease holder, loses the enqueue signal — but enqueue is idempotent-on-ready already, and the lease will eventually release or expire, at which point the task is already ready).
   - (c) Raise on enqueue-on-leased (caller error).
   Recommendation: (a) for this round (minimum change); revisit in a future ADR if we ever expose enqueue to untrusted callers.

2. **Seed-path targeted-lease race.** `InteractionDriver._seed_start` calls `dispatcher.enqueue(task_id)` then `dispatcher.lease(task_id=task_id, ...)`. In single-host mode this is synchronous and the next worker poll has not happened yet; in multi-host another host's FIFO poll can steal the task between enqueue and the targeted lease. The targeted lease then returns None, and the code raises `RuntimeError("dispatcher gave no lease for freshly enqueued task")` (driver.py:652-656). Options:
   - (a) Combine enqueue + targeted-lease into one dispatcher verb (e.g. `lease_new(task_id, ...)` that creates the row in 'leased' state atomically, skipping ready).
   - (b) After targeted-lease returns None, re-check if some other worker picked it up (normal in multi-host) and skip the drive (the task is leased elsewhere; the caller is the acking request thread which should just return success — the worker pool on another host will drive it).
   - (c) Keep the RuntimeError — treat this as a deployment error (shouldn't happen if only one host runs seed verbs for a given task).
   Recommendation: (b) is the right semantic for multi-host (seeding is idempotent and the ack only promises durability, not affinity), but it is a driver-level change that is outside this fencing ADR. File as a follow-up.

3. **CONTEXT.md Step/Attempt terminology tension** (flagged in handoff): "Step" is defined as "one compose → decide → dispatch pass" while recovery treats Step as a loop of Attempts. Acknowledged but outside scope; this ADR uses the terms per existing ADRs and does not redefine them.

## Implementation order

1. Add Postgres migration 3 (worker_id column). Run migrations on a test DB; verify forwards-apply.
2. Refactor PostgresDispatcher to support DB-clock mode (helper for now SQL); keep injected-now path byte-identical for existing tests.
3. Populate/clear worker_id in every state transition.
4. Refactor PostgresEventLog: add `_db_clock` / `_emit_pause` support, replace the out-of-tx `is_lease_valid` call with in-tx `SELECT ... FOR SHARE` in both DB-clock and injected-clock modes.
5. Keep `bind_lease_registry` and `self._lease_validator` attribute; they are no longer used on the hot emit path but remain for backwards compatibility and can be removed in a later cleanup once the Protocol is re-examined (out of scope here).
6. Run existing contract tests (zero changes expected).
7. Add the new contract tests (worker_id population/clear) across three backends.
8. Add postgres-only multi-host tests.
9. Ruff + full local pytest against a Postgres instance.
10. PR, CI, review per the repo's standard review flow.
