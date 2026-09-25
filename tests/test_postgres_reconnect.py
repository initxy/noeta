"""Postgres adapters survive a dropped connection.

Unit half: ``_ReconnectingConnection`` over a scripted fake connection pins
the retry rules — a standalone statement is re-sent once, a transaction lost
before ``COMMIT`` is re-run from ``BEGIN``, a ``COMMIT`` that loses the
connection is ambiguous and propagates (except an idempotency-keyed append),
and only connection-level errors qualify.

Integration half (gated on ``NOETA_TEST_POSTGRES_DSN``): each adapter's
backend is killed with ``pg_terminate_backend`` and the next call succeeds
with the data intact.
"""

from __future__ import annotations

import os
from typing import Any, Callable, Iterator, Optional

import pytest

psycopg = pytest.importorskip("psycopg")

from noeta.builtins.storage.impl.postgres._connection import (  # noqa: E402
    _CommitOutcomeUnknown,
    _ReconnectingConnection,
    _TransactionLost,
    _rerun_lost_transaction,
)
from noeta.protocols.events import (  # noqa: E402
    EventEnvelope,
    TaskCreatedPayload,
    TaskStartedPayload,
)
from tests._pg import POSTGRES_DSN_ENV, isolated_schema_dsn  # noqa: E402


# ---------------------------------------------------------------------------
# Fake connection
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, query: Optional[str]) -> None:
        self.query = query

    def fetchone(self) -> Any:
        return None


class _FakeRaw:
    """Scripted stand-in for ``psycopg.Connection``.

    ``fail_on`` maps a statement to ``(exception, drop)``; the entry is
    consumed on first use, and ``drop`` marks the connection broken the way
    psycopg does when the server goes away.
    """

    def __init__(self, fail_on: Optional[dict[str, tuple[Exception, bool]]] = None):
        self.fail_on = dict(fail_on or {})
        self.log: list[str] = []
        self.closed = False
        self.broken = False

    def execute(self, query: str, params: Any = None) -> _FakeCursor:
        if self.closed:
            raise psycopg.OperationalError("the connection is closed")
        self.log.append(query)
        scripted = self.fail_on.pop(query, None)
        if scripted is not None:
            exc, drop = scripted
            if drop:
                self.drop()
            raise exc
        return _FakeCursor(query)

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(None)

    def drop(self) -> None:
        self.closed = True
        self.broken = True

    def close(self) -> None:
        self.closed = True


class _Factory:
    """Hands out the scripted fakes in order, then clean ones."""

    def __init__(self, *scripted: _FakeRaw) -> None:
        self.queue = list(scripted)
        self.made: list[_FakeRaw] = []

    def __call__(self) -> _FakeRaw:
        raw = self.queue.pop(0) if self.queue else _FakeRaw()
        self.made.append(raw)
        return raw


def _lost() -> tuple[Exception, bool]:
    return psycopg.OperationalError("server closed the connection"), True


def _wrap(*scripted: _FakeRaw) -> tuple[_ReconnectingConnection, _Factory]:
    factory = _Factory(*scripted)
    return _ReconnectingConnection(factory), factory  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Standalone statements
# ---------------------------------------------------------------------------


def test_standalone_statement_is_resent_once_on_a_fresh_connection() -> None:
    conn, factory = _wrap(_FakeRaw({"SELECT 1": _lost()}))
    cursor = conn.execute("SELECT 1")
    assert cursor.query == "SELECT 1"  # type: ignore[attr-defined]
    assert len(factory.made) == 2
    assert factory.made[0].closed
    assert factory.made[1].log == ["SELECT 1"]


def test_second_failure_propagates_and_the_next_call_reopens() -> None:
    conn, factory = _wrap(
        _FakeRaw({"SELECT 1": _lost()}), _FakeRaw({"SELECT 1": _lost()})
    )
    with pytest.raises(psycopg.OperationalError):
        conn.execute("SELECT 1")
    assert len(factory.made) == 2
    conn.execute("SELECT 2")
    assert len(factory.made) == 3
    assert factory.made[2].log == ["SELECT 2"]


def test_dropped_idle_connection_is_reopened_before_the_statement() -> None:
    conn, factory = _wrap()
    factory.made[0].drop()
    conn.execute("SELECT 1")
    assert len(factory.made) == 2
    assert factory.made[1].log == ["SELECT 1"]


@pytest.mark.parametrize(
    "exc",
    [
        psycopg.errors.LockNotAvailable("lock timeout"),
        psycopg.errors.QueryCanceled("canceling statement"),
        psycopg.errors.UniqueViolation("duplicate key"),
        psycopg.errors.DataError("bad value"),
    ],
    ids=["lock-not-available", "query-canceled", "unique-violation", "data-error"],
)
def test_non_connection_errors_are_never_retried(exc: Exception) -> None:
    conn, factory = _wrap(_FakeRaw({"SELECT 1": (exc, False)}))
    with pytest.raises(type(exc)):
        conn.execute("SELECT 1")
    assert len(factory.made) == 1
    assert factory.made[0].log == ["SELECT 1"]


def test_admin_shutdown_sqlstate_qualifies_before_the_connection_reads_closed() -> None:
    conn, factory = _wrap(
        _FakeRaw({"SELECT 1": (psycopg.errors.AdminShutdown("terminating"), False)})
    )
    conn.execute("SELECT 1")
    assert len(factory.made) == 2


def test_begin_is_a_standalone_statement() -> None:
    conn, factory = _wrap(_FakeRaw({"BEGIN": _lost()}))
    conn.execute("BEGIN")
    conn.execute("SELECT 1")
    conn.execute("COMMIT")
    assert factory.made[1].log == ["BEGIN", "SELECT 1", "COMMIT"]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def test_loss_inside_a_transaction_raises_transaction_lost_and_reopens() -> None:
    conn, factory = _wrap(_FakeRaw({"INSERT": _lost()}))
    conn.execute("BEGIN")
    with pytest.raises(_TransactionLost):
        conn.execute("INSERT")
    # The statement is NOT re-sent on the fresh connection by the wrapper.
    assert len(factory.made) == 2
    assert factory.made[1].log == []
    # The adapter's ``except: ROLLBACK; raise`` must not mask the loss.
    conn.execute("ROLLBACK")
    assert factory.made[1].log == []
    conn.execute("BEGIN")
    conn.execute("INSERT")
    conn.execute("COMMIT")
    assert factory.made[1].log == ["BEGIN", "INSERT", "COMMIT"]


def test_drop_between_two_statements_of_a_transaction_is_a_lost_transaction() -> None:
    conn, factory = _wrap()
    conn.execute("BEGIN")
    factory.made[0].drop()
    with pytest.raises(_TransactionLost):
        conn.execute("SELECT 1")
    assert len(factory.made) == 2


def test_drop_just_before_commit_is_sent_is_a_lost_transaction() -> None:
    conn, factory = _wrap()
    conn.execute("BEGIN")
    factory.made[0].drop()
    with pytest.raises(_TransactionLost):
        conn.execute("COMMIT")


def test_loss_during_commit_is_ambiguous_and_not_a_lost_transaction() -> None:
    conn, factory = _wrap(_FakeRaw({"COMMIT": _lost()}))
    conn.execute("BEGIN")
    conn.execute("INSERT")
    with pytest.raises(_CommitOutcomeUnknown) as info:
        conn.execute("COMMIT")
    assert not isinstance(info.value, _TransactionLost)
    assert isinstance(info.value, psycopg.OperationalError)
    # Reopened for the next call, and no transaction is considered open.
    assert len(factory.made) == 2
    conn.execute("ROLLBACK")
    conn.execute("SELECT 1")
    assert factory.made[1].log == ["SELECT 1"]


def test_non_connection_error_on_commit_propagates_unchanged() -> None:
    conn, factory = _wrap(
        _FakeRaw({"COMMIT": (psycopg.errors.SerializationFailure("x"), False)})
    )
    conn.execute("BEGIN")
    with pytest.raises(psycopg.errors.SerializationFailure):
        conn.execute("COMMIT")
    assert len(factory.made) == 1


def test_data_error_inside_a_transaction_rolls_back_normally() -> None:
    conn, factory = _wrap(
        _FakeRaw({"INSERT": (psycopg.errors.UniqueViolation("dup"), False)})
    )
    conn.execute("BEGIN")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT")
    conn.execute("ROLLBACK")
    assert factory.made[0].log == ["BEGIN", "INSERT", "ROLLBACK"]
    assert len(factory.made) == 1


def test_rollback_on_a_connection_that_drops_does_not_raise() -> None:
    conn, factory = _wrap(
        _FakeRaw(
            {
                "INSERT": (psycopg.errors.UniqueViolation("dup"), False),
                "ROLLBACK": _lost(),
            }
        )
    )
    conn.execute("BEGIN")
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute("INSERT")
    conn.execute("ROLLBACK")  # swallowed: the server discarded the tx
    conn.execute("SELECT 1")
    assert len(factory.made) == 2


def test_closed_wrapper_never_reopens() -> None:
    conn, factory = _wrap()
    conn.close()
    assert conn.closed
    with pytest.raises(psycopg.InterfaceError):
        conn.execute("SELECT 1")
    assert len(factory.made) == 1


def test_dropped_connection_does_not_read_as_closed() -> None:
    conn, factory = _wrap()
    factory.made[0].drop()
    assert not conn.closed


# ---------------------------------------------------------------------------
# Re-running a lost transaction
# ---------------------------------------------------------------------------


class _Scripted:
    def __init__(self, *outcomes: Optional[Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    @_rerun_lost_transaction
    def op(self) -> str:
        self.calls += 1
        exc = self.outcomes.pop(0) if self.outcomes else None
        if exc is not None:
            raise exc
        return "done"


def test_lost_transaction_is_rerun_once() -> None:
    obj = _Scripted(_TransactionLost("lost"))
    assert obj.op() == "done"
    assert obj.calls == 2


def test_second_lost_transaction_propagates() -> None:
    obj = _Scripted(_TransactionLost("lost"), _TransactionLost("lost again"))
    with pytest.raises(_TransactionLost):
        obj.op()
    assert obj.calls == 2


def test_ambiguous_commit_is_not_rerun() -> None:
    obj = _Scripted(_CommitOutcomeUnknown("maybe"))
    with pytest.raises(_CommitOutcomeUnknown):
        obj.op()
    assert obj.calls == 1


def test_other_errors_are_not_rerun() -> None:
    obj = _Scripted(psycopg.errors.LockNotAvailable("busy"))
    with pytest.raises(psycopg.errors.LockNotAvailable):
        obj.op()
    assert obj.calls == 1


# ---------------------------------------------------------------------------
# EventLog._append retry dispatch (COMMIT rule + idempotency-key exception)
# ---------------------------------------------------------------------------


def _bare_event_log(
    outcomes: list[Any],
) -> tuple[Any, list[EventEnvelope], list[dict[str, Any]]]:
    """A ``PostgresEventLog`` with no connection whose ``_append_tx`` plays
    ``outcomes`` in order (an exception to raise, or an ``(envelope,
    inserted)`` result)."""
    from noeta.builtins.storage.impl.postgres.eventlog import PostgresEventLog

    log: Any = PostgresEventLog.__new__(PostgresEventLog)
    log._subscribers = []
    notified: list[EventEnvelope] = []
    log.subscribe(notified.append)
    calls: list[dict[str, Any]] = []

    def fake_tx(envelope: EventEnvelope, body: bytes, **kw: Any) -> Any:
        calls.append(kw)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    log._append_tx = fake_tx
    return log, notified, calls


def _envelope(id: str = "evt-1") -> EventEnvelope:
    return EventEnvelope.build(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
        id=id,
        actor="engine",
        trace_id=None,
        causation_id=None,
        schema_version=1,
        occurred_at=1.0,
        origin="engine",
    )


def _append(log: Any, env: EventEnvelope, *, key: Optional[str]) -> EventEnvelope:
    result: EventEnvelope = log._append(
        env,
        lease_id="L" if key is not None else None,
        expected_seq=None,
        idempotency_key=key,
        require_lease=True,
    )
    return result


def test_append_reruns_a_transaction_lost_before_commit() -> None:
    env = _envelope()
    stamped = env.with_seq(0)
    log, notified, calls = _bare_event_log(
        [_TransactionLost("lost"), (stamped, True)]
    )
    assert _append(log, env, key=None) == stamped
    assert len(calls) == 2
    assert notified == [stamped]


def test_append_without_key_propagates_an_ambiguous_commit() -> None:
    env = _envelope()
    log, notified, calls = _bare_event_log(
        [_CommitOutcomeUnknown("maybe"), (env.with_seq(0), True)]
    )
    with pytest.raises(_CommitOutcomeUnknown):
        _append(log, env, key=None)
    assert len(calls) == 1
    assert notified == []


def test_keyed_append_retries_an_ambiguous_commit_that_landed() -> None:
    env = _envelope()
    stamped = env.with_seq(0)
    # The retry's key lookup returns the event the first COMMIT stored.
    log, notified, calls = _bare_event_log(
        [_CommitOutcomeUnknown("maybe"), (stamped, False)]
    )
    assert _append(log, env, key="k1") == stamped
    assert len(calls) == 2
    assert notified == [stamped]  # subscribers see it exactly once


def test_keyed_append_retries_an_ambiguous_commit_that_did_not_land() -> None:
    env = _envelope()
    stamped = env.with_seq(0)
    log, notified, calls = _bare_event_log(
        [_CommitOutcomeUnknown("maybe"), (stamped, True)]
    )
    assert _append(log, env, key="k1") == stamped
    assert notified == [stamped]


def test_keyed_append_cache_hit_on_an_earlier_call_does_not_notify() -> None:
    env = _envelope("evt-new")
    earlier = _envelope("evt-earlier").with_seq(0)
    log, notified, _ = _bare_event_log(
        [_CommitOutcomeUnknown("maybe"), (earlier, False)]
    )
    assert _append(log, env, key="k1") == earlier
    assert notified == []


# ---------------------------------------------------------------------------
# Real server: kill the adapter's backend, then use the adapter
# ---------------------------------------------------------------------------

_needs_pg = pytest.mark.skipif(
    not os.environ.get(POSTGRES_DSN_ENV),
    reason=f"{POSTGRES_DSN_ENV} not set",
)


@pytest.fixture()
def schema_dsn() -> Iterator[str]:
    with isolated_schema_dsn() as dsn:
        yield dsn


@pytest.fixture()
def closing() -> Iterator[Callable[[Any], Any]]:
    opened: list[Any] = []

    def _track(adapter: Any) -> Any:
        opened.append(adapter)
        return adapter

    yield _track
    for adapter in opened:
        try:
            adapter.close()
        except Exception:
            pass


def _backend_pid(conn: _ReconnectingConnection) -> int:
    row = conn.execute("SELECT pg_backend_pid() AS pid").fetchone()
    assert row is not None
    return int(row["pid"])


def _terminate(conn: _ReconnectingConnection) -> int:
    """Kill ``conn``'s server backend from an admin connection and wait
    until it is gone; return the killed pid."""
    pid = _backend_pid(conn)
    with psycopg.connect(os.environ[POSTGRES_DSN_ENV], autocommit=True) as admin:
        row = admin.execute(
            "SELECT pg_terminate_backend(%s, 5000)", (pid,)
        ).fetchone()
        assert row is not None and row[0] is True
    return pid


def _created(goal: str) -> TaskCreatedPayload:
    return TaskCreatedPayload(goal=goal, policy_name="p")


@_needs_pg
def test_event_log_survives_a_killed_backend(schema_dsn: str, closing: Any) -> None:
    from noeta.sdk.storage import PostgresEventLog

    log = closing(PostgresEventLog(schema_dsn))
    first = log.emit(task_id="t1", type="TaskCreated", payload=_created("a"))

    killed = _terminate(log._conn)
    assert log.read("t1") == [first]
    assert _backend_pid(log._conn) != killed  # served by a new backend

    _terminate(log._conn)
    second = log.system_emit(
        task_id="t1",
        type="TaskCreated",
        payload=_created("b"),
        actor="host",
        origin="host",
    )
    assert second.seq == 1

    _terminate(log._conn)
    assert [s.task_id for s in log.list_task_streams()] == ["t1"]

    _terminate(log._conn)
    assert log.purge_task("t1") is True
    assert log.read("t1") == []


@_needs_pg
def test_dispatcher_survives_killed_backends(schema_dsn: str, closing: Any) -> None:
    from noeta.sdk.storage import PostgresDispatcher

    disp = closing(PostgresDispatcher(schema_dsn))
    disp.enqueue("t1")

    killed = _terminate(disp._conn)
    lease = disp.lease(worker_id="w1")
    assert _backend_pid(disp._conn) != killed
    assert lease is not None and lease.task_id == "t1"

    _terminate(disp._read_conn)
    assert disp.is_lease_valid("t1", lease.lease_id)
    assert disp.task_status("t1") == "leased"

    _terminate(disp._conn)
    disp.heartbeat(lease.lease_id)

    _terminate(disp._conn)
    disp.release(lease.lease_id, next_state="terminal")
    assert disp.task_status("t1") == "terminal"


@_needs_pg
def test_content_store_survives_a_killed_backend(
    schema_dsn: str, closing: Any
) -> None:
    from noeta.sdk.storage import PostgresContentStore

    store = closing(PostgresContentStore(schema_dsn))
    ref = store.put(b"alpha", media_type="text/plain")

    _terminate(store._conn)
    assert store.get(ref) == b"alpha"

    _terminate(store._conn)
    ref2 = store.put(b"beta", media_type="text/plain")

    _terminate(store._conn)
    assert store.get_many([ref, ref2]) == {ref.hash: b"alpha", ref2.hash: b"beta"}


@_needs_pg
def test_read_only_store_reconnects_still_read_only(
    schema_dsn: str, closing: Any
) -> None:
    from noeta.sdk.storage import PostgresEventLog, PostgresReadOnlyStore

    log = closing(PostgresEventLog(schema_dsn))
    first = log.emit(task_id="t1", type="TaskCreated", payload=_created("a"))

    ro = closing(PostgresReadOnlyStore(schema_dsn))
    _terminate(ro._conn)
    assert ro.read("t1") == [first]
    # The read-only session setting is re-applied on the new connection.
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        ro._conn.execute("CREATE TABLE should_fail (a int)")


@_needs_pg
def test_emit_killed_mid_transaction_is_rerun_without_duplicates(
    schema_dsn: str, closing: Any
) -> None:
    from noeta.sdk.storage import PostgresEventLog

    victim: dict[str, int] = {}
    pauses: list[int] = []

    def _pause() -> None:
        # First attempt: kill this adapter's own backend between the fence
        # probe and the INSERT, i.e. before COMMIT is ever sent.
        pauses.append(1)
        if len(pauses) == 1:
            with psycopg.connect(
                os.environ[POSTGRES_DSN_ENV], autocommit=True
            ) as admin:
                admin.execute(
                    "SELECT pg_terminate_backend(%s, 5000)", (victim["pid"],)
                )

    log = closing(PostgresEventLog(schema_dsn, _emit_pause=_pause))
    seen: list[EventEnvelope] = []
    log.subscribe(seen.append)
    victim["pid"] = _backend_pid(log._conn)

    env = log.emit(task_id="t1", type="TaskCreated", payload=_created("a"))

    assert len(pauses) == 2  # the transaction ran twice
    assert env.seq == 0
    assert log.read("t1") == [env]
    assert seen == [env]


class _CommitDropper:
    """Proxy over a real psycopg connection that drops it at ``COMMIT``,
    either after the server committed (``landed``) or before."""

    def __init__(self, raw: Any, *, landed: bool) -> None:
        self._raw = raw
        self._landed = landed

    def execute(self, query: str, params: Any = None) -> Any:
        if query == "COMMIT":
            if self._landed:
                self._raw.execute("COMMIT")
            self._raw.close()
            raise psycopg.OperationalError("server closed the connection")
        return self._raw.execute(query, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


def _leased_pair(closing: Any, dsn: str) -> tuple[Any, Any, str]:
    from noeta.sdk.storage import PostgresDispatcher, PostgresEventLog

    disp = closing(PostgresDispatcher(dsn))
    log = closing(PostgresEventLog(dsn, lease_validator=disp))
    disp.enqueue("t1")
    lease = disp.lease(worker_id="w1")
    assert lease is not None
    return disp, log, lease.lease_id


@_needs_pg
@pytest.mark.parametrize("landed", [True, False], ids=["landed", "not-landed"])
def test_keyed_emit_retries_a_commit_lost_in_flight(
    schema_dsn: str, closing: Any, landed: bool
) -> None:
    _, log, lease_id = _leased_pair(closing, schema_dsn)
    seen: list[EventEnvelope] = []
    log.subscribe(seen.append)
    log._conn._raw = _CommitDropper(log._conn._raw, landed=landed)

    env = log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id=lease_id),
        lease_id=lease_id,
        idempotency_key="k1",
    )

    assert env.seq == 0
    assert log.read("t1") == [env]
    assert seen == [env]


@_needs_pg
def test_unkeyed_emit_propagates_a_commit_lost_in_flight(
    schema_dsn: str, closing: Any
) -> None:
    _, log, lease_id = _leased_pair(closing, schema_dsn)
    log._conn._raw = _CommitDropper(log._conn._raw, landed=True)

    with pytest.raises(psycopg.OperationalError):
        log.emit(
            task_id="t1",
            type="TaskStarted",
            payload=TaskStartedPayload(lease_id=lease_id),
            lease_id=lease_id,
        )

    # Not retried: exactly the one event the in-flight COMMIT stored, and
    # the adapter is usable again.
    events = log.read("t1")
    assert [e.seq for e in events] == [0]
    nxt = log.emit(
        task_id="t1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id=lease_id),
        lease_id=lease_id,
    )
    assert nxt.seq == 1
