"""psycopg connection construction shared by the Postgres storage adapters.

``autocommit=True`` because each adapter owns its transaction boundaries
explicitly (``BEGIN`` / ``COMMIT`` / ``ROLLBACK``); ``synchronous_commit=on``
is Postgres' default asserted here on purpose, because the EventLog is the
decision and causality source of truth and no committed event may be lost on
a crash.

Each adapter holds one :class:`_ReconnectingConnection` (no pool) that
reopens a dropped connection — server restart, idle kill, network reset,
``pg_terminate_backend`` — under the rules in its docstring.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Optional, TypeVar, cast

import psycopg
from psycopg.abc import Params
from psycopg.rows import DictRow, dict_row


__all__ = [
    "_ADVISORY_CLASS_DISPATCHER",
    "_ADVISORY_CLASS_EVENTS",
    "_ADVISORY_CLASS_MIGRATIONS",
    "_CommitOutcomeUnknown",
    "_DB_NOW_SQL",
    "_ReconnectingConnection",
    "_TransactionLost",
    "_connect",
    "_open_connection",
    "_rerun_lost_transaction",
]


#: ``pg_advisory_xact_lock(classid, objid)`` class ids, one per adapter
#: family so an EventLog stream lock can never collide with the
#: Dispatcher's global lock. Arbitrary but fixed 31-bit constants. Advisory
#: locks are database-wide rather than schema-scoped, so two schemas sharing
#: one database serialise against each other.
_ADVISORY_CLASS_MIGRATIONS = 0x6E5F6D69  # "n_mi"
_ADVISORY_CLASS_EVENTS = 0x6E5F6576  # "n_ev"
_ADVISORY_CLASS_DISPATCHER = 0x6E5F6469  # "n_di"

#: SQL expression for the database clock "now", shared by the Dispatcher
#: (lease expiry / stale detection / timer firing) and the EventLog (in-tx
#: fence probe) so the time reference cannot drift between the two adapters.
#: ``clock_timestamp()`` rather than ``now()`` / ``current_timestamp``
#: because it advances within a transaction: a long emit transaction must
#: not compare against a stale statement-start instant.
_DB_NOW_SQL = "EXTRACT(EPOCH FROM clock_timestamp())::double precision"

#: SQLSTATEs that mean "this connection is gone" even before psycopg has
#: marked it closed: class 08 (connection exception) plus the server's
#: shutdown / termination codes (57P01 is what ``pg_terminate_backend``
#: sends).
_CONNECTION_LOST_SQLSTATES = frozenset({"57P01", "57P02", "57P03"})


class _TransactionLost(psycopg.OperationalError):
    """The connection dropped inside an open transaction before its
    ``COMMIT`` was sent.

    The server discarded the transaction (and released its advisory locks),
    so re-running it from ``BEGIN`` on the reopened connection is safe;
    :func:`_rerun_lost_transaction` does exactly that once.
    """


class _CommitOutcomeUnknown(psycopg.OperationalError):
    """The connection dropped while ``COMMIT`` was in flight.

    The transaction may or may not have committed, so it is never re-run
    blindly; the connection is already reopened for the next call.
    """


def _connect(dsn: str) -> psycopg.Connection[DictRow]:
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    conn.execute("SET synchronous_commit = on")
    return conn


def _open_connection(dsn: str) -> "_ReconnectingConnection":
    """Each adapter shares one connection across threads behind its own
    :class:`threading.Lock`, so there is no pool.
    """
    return _ReconnectingConnection(lambda: _connect(dsn))


def _control_verb(query: str) -> Optional[str]:
    verb = query.strip().rstrip(";").strip().upper()
    return verb if verb in ("BEGIN", "COMMIT", "ROLLBACK") else None


class _ReconnectingConnection:
    """One autocommit psycopg connection that survives being dropped.

    Contract (``execute`` / ``close`` / ``closed``, the subset the adapters
    use; callers serialise access behind their own lock):

    * Only a connection-level failure qualifies: ``OperationalError`` /
      ``InterfaceError`` raised while the connection is closed / broken, or
      carrying SQLSTATE class ``08`` / ``57P01``-``57P03``. Any other error
      (a data error, ``LockNotAvailable``, a query cancel) propagates
      untouched.
    * Outside a transaction (``BEGIN`` included) the statement is re-sent
      once on a freshly opened connection.
    * Inside a transaction, before ``COMMIT``: the connection is reopened and
      :class:`_TransactionLost` is raised, so the caller re-runs the whole
      transaction from ``BEGIN`` (:func:`_rerun_lost_transaction`).
    * On ``COMMIT`` itself: the connection is reopened and
      :class:`_CommitOutcomeUnknown` is raised; nothing is re-run here.
    * ``ROLLBACK`` never raises a connection-level error (a dropped
      connection has no transaction left to roll back) and is a no-op when
      no transaction is open, so an adapter's ``except: ROLLBACK; raise``
      re-raises the original failure.
    * After :meth:`close` nothing is reopened.
    """

    def __init__(
        self, connect: Callable[[], psycopg.Connection[DictRow]]
    ) -> None:
        self._connect = connect
        self._raw = connect()
        self._in_tx = False
        self._closed = False

    @property
    def closed(self) -> bool:
        """True once :meth:`close` ran. A dropped connection is not
        ``closed``: the next ``execute`` reopens it."""
        return self._closed

    def close(self) -> None:
        self._closed = True
        self._in_tx = False
        self._raw.close()

    def execute(
        self, query: str, params: Optional[Params] = None
    ) -> psycopg.Cursor[DictRow]:
        if self._closed:
            raise psycopg.InterfaceError("the connection is closed")
        verb = _control_verb(query)
        if verb == "ROLLBACK":
            return self._rollback(query)
        if not self._in_tx:
            cursor = self._execute_standalone(query, params)
            if verb == "BEGIN":
                self._in_tx = True
            return cursor
        if self._raw.closed:
            # Dropped between two statements of this transaction.
            self._in_tx = False
            self._reopen_quietly()
            raise _TransactionLost("connection lost inside a transaction")
        try:
            return self._raw.execute(query, params)
        except psycopg.Error as exc:
            if not self._is_connection_error(exc):
                raise
            self._in_tx = False
            self._reopen_quietly()
            if verb == "COMMIT":
                raise _CommitOutcomeUnknown(
                    f"connection lost during COMMIT: {exc}"
                ) from exc
            raise _TransactionLost(
                f"connection lost inside a transaction: {exc}"
            ) from exc
        finally:
            if verb == "COMMIT":
                # Committed or not, the server has no open transaction now.
                self._in_tx = False

    # -- internals -------------------------------------------------------

    def _execute_standalone(
        self, query: str, params: Optional[Params]
    ) -> psycopg.Cursor[DictRow]:
        if self._raw.closed:
            self._reopen()
        try:
            return self._raw.execute(query, params)
        except psycopg.Error as exc:
            if not self._is_connection_error(exc):
                raise
            self._reopen()
            return self._raw.execute(query, params)

    def _rollback(self, query: str) -> psycopg.Cursor[DictRow]:
        was_open = self._in_tx
        self._in_tx = False
        if not was_open or self._raw.closed:
            return self._raw.cursor()
        try:
            return self._raw.execute(query)
        except psycopg.Error as exc:
            if not self._is_connection_error(exc):
                raise
            return self._raw.cursor()

    def _is_connection_error(self, exc: psycopg.Error) -> bool:
        if isinstance(exc, (_TransactionLost, _CommitOutcomeUnknown)):
            return False
        if not isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError)):
            return False
        if self._raw.closed or self._raw.broken:
            return True
        sqlstate = exc.sqlstate or ""
        return sqlstate.startswith("08") or sqlstate in _CONNECTION_LOST_SQLSTATES

    def _reopen(self) -> None:
        try:
            self._raw.close()
        except Exception:  # noqa: BLE001 — the old connection is already dead
            pass
        self._raw = self._connect()

    def _reopen_quietly(self) -> None:
        # The caller is about to raise the more useful error; a server that
        # is still down gets another reopen attempt on the next call.
        try:
            self._reopen()
        except psycopg.Error:
            pass


_F = TypeVar("_F", bound=Callable[..., Any])


def _rerun_lost_transaction(method: _F) -> _F:
    """Re-run ``method`` once when its transaction was lost before
    ``COMMIT`` (:class:`_TransactionLost`).

    Only for methods whose side effects all live inside that transaction:
    the lost attempt left nothing behind, so the re-run cannot double-apply.
    :class:`_CommitOutcomeUnknown` is not caught.
    """

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except _TransactionLost:
            return method(*args, **kwargs)

    return cast(_F, wrapper)
