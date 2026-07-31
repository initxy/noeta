"""File-on-disk durability for ``SqliteDispatcher``."""

from __future__ import annotations

import threading

from noeta.protocols.events import TaskCreatedPayload
from noeta.protocols.wake import SubtaskCompleted
from noeta.sdk.storage import SqliteDispatcher, SqliteEventLog


def test_dispatcher_state_survives_close_reopen(tmp_path) -> None:
    db = tmp_path / "noeta.db"

    disp = SqliteDispatcher(db)
    try:
        disp.enqueue("t1")
        disp.enqueue("t2")
        lease = disp.lease(worker_id="w")
        assert lease.task_id == "t1"
        wake_on = SubtaskCompleted(subtask_id="t-child")
        disp.release(
            lease.lease_id, next_state="suspended", wake_on=wake_on
        )
    finally:
        disp.close()

    reopened = SqliteDispatcher(db)
    try:
        # The leased+suspended t1 should be holding the typed wake_on
        # we stored, and t2 should still be ready.
        next_lease = reopened.lease(worker_id="w")
        assert next_lease is not None and next_lease.task_id == "t2"
        # Matching wake should still re-queue t1.
        assert reopened.wake("t1", SubtaskCompleted(subtask_id="t-child")) is True
        relaunch = reopened.lease(worker_id="w")
        assert relaunch is not None and relaunch.task_id == "t1"
    finally:
        reopened.close()


def test_close_is_idempotent(tmp_path) -> None:
    disp = SqliteDispatcher(tmp_path / "noeta.db")
    disp.close()
    disp.close()


def test_context_manager_closes_on_exit(tmp_path) -> None:
    db = tmp_path / "noeta.db"
    with SqliteDispatcher(db) as disp:
        disp.enqueue("t1")
        lease = disp.lease(worker_id="w")
        assert lease is not None
    assert disp._closed
    with SqliteDispatcher(db) as reopened:
        # The leased task remained 'leased' in storage; nothing is
        # currently ready.
        assert reopened.lease(worker_id="w") is None


def test_eventlog_emit_and_dispatcher_lifecycle_do_not_deadlock(tmp_path) -> None:
    """The ABBA deadlock between the SQLite writer lock and the Dispatcher
    Python lock must not recur.

    Setup: file-backed SqliteEventLog + SqliteDispatcher wired
    together (EventLog has Dispatcher as its lease validator). One
    thread spins ``emit(... lease_id=...)`` calls — each of those
    acquires the file's SQLite writer lock and, inside that lock,
    calls ``is_lease_valid``. Concurrently another thread spins
    ``enqueue / requeue_stale`` lifecycle methods, each of which
    grabs the Dispatcher Python lock and then ``BEGIN IMMEDIATE``.
    The hazard is a lock-order inversion: EventLog blocking on the
    Dispatcher lock while holding the writer lock, and the lifecycle
    thread blocking on the writer lock while holding the Dispatcher
    lock. A separate read connection for ``is_lease_valid`` keeps the
    two orders from crossing; this test exercises it.
    """
    db = tmp_path / "noeta.db"
    log = SqliteEventLog(db)
    disp = SqliteDispatcher(db)
    log.bind_lease_registry(disp)

    try:
        # Set up a task + a real lease so ``emit`` can validate it.
        disp.enqueue("t-emitter")
        lease = disp.lease(worker_id="w-emitter", lease_seconds=3600.0)
        assert lease is not None

        errors: list[BaseException] = []
        stop = threading.Event()

        def emit_loop() -> None:
            try:
                seq = 0
                while not stop.is_set():
                    log.emit(
                        task_id="t-emitter",
                        type="TaskCreated",
                        payload=TaskCreatedPayload(
                            goal=f"g{seq}", policy_name="p"
                        ),
                        lease_id=lease.lease_id,
                    )
                    seq += 1
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def lifecycle_loop() -> None:
            try:
                for i in range(50):
                    if stop.is_set():
                        return
                    disp.enqueue(f"t-bg-{i}")
                    disp.requeue_stale()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        e = threading.Thread(target=emit_loop, daemon=True)
        c = threading.Thread(target=lifecycle_loop, daemon=True)
        e.start()
        c.start()
        c.join(timeout=10)
        stop.set()
        e.join(timeout=10)

        assert not c.is_alive(), "lifecycle thread did not finish — deadlock?"
        assert not e.is_alive(), "emit thread did not finish — deadlock?"
        assert not errors, f"unexpected errors during concurrent run: {errors!r}"
    finally:
        disp.close()
        log.close()
