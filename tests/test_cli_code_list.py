"""CW5b — code-session list (pure read).

Two layers:
* unit — `noeta.agent.sessions` projection: code-agent filter, malformed-skip,
  status-text precedence (typed wake_on), and the pure
  `filter_code_sessions` filter/sort/limit;
* read-path — the read-only library seam (`SqliteReadOnlyStore` +
  `list_code_sessions`): path guards (never create / migrate a DB) and the
  list/filter projection over a real on-disk store.

The operator-CLI argparse/stdout surface (`noeta code list`) is exercised
elsewhere; this file pins the library behaviour it is a thin wrapper over.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from noeta.execution.multi_turn import NEXT_GOAL_WAKE_HANDLE
from noeta.agent.read_models.catalog import (
    CodeSessionRow,
    _status_text,
    filter_code_sessions,
    list_code_sessions,
)
from noeta.protocols.events import (
    ConversationClosedPayload,
    TaskCreatedPayload,
    TaskStartedPayload,
)
from noeta.protocols.wake import HumanResponseReceived
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog
from noeta.storage.sqlite import SqliteReadOnlyStore, SqliteSchemaVersionError
from noeta.storage.sqlite.migrations import SCHEMA_VERSION
from noeta.testing.profile import build_sqlite_stack, is_memory_path


# ---------------------------------------------------------------------------
# Unit — status-text precedence (typed wake_on, no string matching)
# ---------------------------------------------------------------------------


def test_status_text_precedence() -> None:
    approval = HumanResponseReceived(handle="approval-c1")
    next_goal = HumanResponseReceived(handle=NEXT_GOAL_WAKE_HANDLE)

    # closed wins over everything (even terminal)
    assert _status_text("terminal", True, None) == "closed"
    assert _status_text("suspended", True, approval) == "closed"
    # terminal next
    assert _status_text("terminal", False, None) == "terminal"
    # typed approval wake
    assert _status_text("suspended", False, approval) == "awaiting approval"
    # typed next-goal wake → resumable
    assert _status_text("suspended", False, next_goal) == "resumable"
    # a suspend on some OTHER human handle is not mislabeled resumable/approval
    other = HumanResponseReceived(handle="something-else")
    assert _status_text("suspended", False, other) == "suspended"
    # no wake_on → underlying status verbatim
    assert _status_text("running", False, None) == "running"


# ---------------------------------------------------------------------------
# Unit — projection filter / malformed skip
# ---------------------------------------------------------------------------


def _mem() -> tuple[InMemoryEventLog, InMemoryContentStore]:
    return InMemoryEventLog(), InMemoryContentStore()


def test_list_filters_to_code_agents_and_skips_malformed() -> None:
    log, cs = _mem()
    # code session (registered agent)
    log.emit(
        task_id="code1",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="fix the bug", policy_name="react", agent_name="default"
        ),
    )
    # non-code session (agent not in AGENTS)
    log.emit(
        task_id="generic1",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="g", policy_name="react", agent_name="unnamed"
        ),
    )
    # malformed: a stream whose first/only event is NOT TaskCreated
    log.emit(
        task_id="malformed1",
        type="TaskStarted",
        payload=TaskStartedPayload(lease_id="L"),
    )

    rows = list_code_sessions(log, log, cs)
    assert [r.task_id for r in rows] == ["code1"]
    assert rows[0].agent == "default"
    assert rows[0].goal == "fix the bug"


def test_list_surfaces_closed_status() -> None:
    log, cs = _mem()
    log.emit(
        task_id="c1",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="g", policy_name="react", agent_name="default"
        ),
    )
    log.emit(
        task_id="c1",
        type="ConversationClosed",
        payload=ConversationClosedPayload(closed_by="leo", reason=None),
    )
    [row] = list_code_sessions(log, log, cs)
    assert isinstance(row, CodeSessionRow)
    assert row.closed is True
    assert row.status_text == "closed"


# ---------------------------------------------------------------------------
# Read-path — strictly read-only open (never create / migrate a DB) + list
# projection over a real on-disk store (the library seam the CLI wraps).
# ---------------------------------------------------------------------------


def _seed_code_session(db: Path) -> None:
    """Record one `default`-agent task into a real sqlite store at ``db``."""
    event_log, content_store, dispatcher = build_sqlite_stack(str(db))
    event_log.emit(
        task_id="seeded1",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="seeded goal", policy_name="react", agent_name="default"
        ),
    )
    for obj in (event_log, content_store, dispatcher):
        close = getattr(obj, "close", None)
        if callable(close):
            close()


def _list(db: Path) -> list[CodeSessionRow]:
    """Open ``db`` strictly read-only and project its code sessions."""
    store = SqliteReadOnlyStore(str(db))
    try:
        return list_code_sessions(store, store, store)
    finally:
        store.close()


def test_read_only_open_missing_file_errors_and_does_not_create(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "nope.db"
    # mode=ro: a missing file is an OperationalError, never a fresh DB.
    with pytest.raises(sqlite3.OperationalError):
        SqliteReadOnlyStore(str(missing))
    assert not missing.exists()


def test_is_memory_path_classifies_in_memory() -> None:
    # The read-only list path can't open a fresh in-memory store; it relies on
    # this classifier to reject ``:memory:`` / ``None`` up front.
    assert is_memory_path(":memory:") is True
    assert is_memory_path(None) is True
    assert is_memory_path("/some/file.db") is False


def test_read_only_open_rejects_non_noeta_sqlite(tmp_path: Path) -> None:
    bogus = tmp_path / "other.db"
    conn = sqlite3.connect(str(bogus))
    conn.execute("CREATE TABLE not_noeta (x INTEGER)")
    conn.commit()
    conn.close()
    # A non-Noeta sqlite file is at user_version 0 → typed version error, not a
    # silent read and not a migration.
    with pytest.raises(SqliteSchemaVersionError):
        SqliteReadOnlyStore(str(bogus))


def test_list_happy_path_rows(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed_code_session(db)
    [row] = _list(db)
    assert row.task_id == "seeded1"
    assert row.agent == "default"
    assert row.goal == "seeded goal"


def test_list_empty_store_returns_no_code_sessions(tmp_path: Path) -> None:
    db = tmp_path / "empty.db"
    # a real Noeta store, but with only a NON-code task → no code sessions
    event_log, content_store, dispatcher = build_sqlite_stack(str(db))
    event_log.emit(
        task_id="g1",
        type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="g", policy_name="react", agent_name="unnamed"
        ),
    )
    for obj in (event_log, content_store, dispatcher):
        close = getattr(obj, "close", None)
        if callable(close):
            close()
    assert _list(db) == []


def _user_version(db: Path) -> int:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def test_read_only_open_old_schema_errors_and_does_not_migrate(
    tmp_path: Path,
) -> None:
    """An older-schema Noeta DB (events table present, lower user_version) must
    produce a typed error and be left UNTOUCHED — the read-only open never
    migrates."""
    db = tmp_path / "old.db"
    _seed_code_session(db)  # creates at the current SCHEMA_VERSION
    # Tamper the version down to simulate an older store.
    old_version = SCHEMA_VERSION - 1
    conn = sqlite3.connect(str(db))
    conn.execute(f"PRAGMA user_version = {old_version}")
    conn.commit()
    conn.close()
    assert _user_version(db) == old_version

    with pytest.raises(SqliteSchemaVersionError) as exc:
        SqliteReadOnlyStore(str(db))
    assert exc.value.found == old_version
    assert exc.value.expected == SCHEMA_VERSION
    # The read-only open must NOT have migrated the store.
    assert _user_version(db) == old_version


# ---------------------------------------------------------------------------
# CW16 — unit: filter_code_sessions (pure, deterministic; no store)
# ---------------------------------------------------------------------------


def _row(
    task_id: str, *, agent: str = "default", goal: str = "g",
    status_text: str = "resumable", closed: bool = False, t: float = 100.0,
) -> CodeSessionRow:
    return CodeSessionRow(
        task_id=task_id, agent=agent, goal=goal, model="m", status="suspended",
        closed=closed, status_text=status_text, last_seq=1, last_event_time=t,
    )


def test_filter_status_substring_case_insensitive() -> None:
    rows = [
        _row("a", status_text="awaiting approval"),
        _row("b", status_text="resumable"),
        _row("c", status_text="closed", closed=True),
    ]
    # OQ1: 'approval' hits 'awaiting approval'; 'closed' hits closed.
    assert [r.task_id for r in filter_code_sessions(rows, status="approval")] == ["a"]
    assert [r.task_id for r in filter_code_sessions(rows, status="CLOSED")] == ["c"]


def test_filter_agent_exact_and_closed_and_grep() -> None:
    rows = [
        _row("a", agent="main", goal="fix the login bug", closed=True,
             status_text="closed"),
        _row("b", agent="explore", goal="add tests"),
    ]
    assert [r.task_id for r in filter_code_sessions(rows, agent="Explore")] == ["b"]
    assert filter_code_sessions(rows, agent="nope") == []
    assert [r.task_id for r in filter_code_sessions(rows, closed=True)] == ["a"]
    assert [r.task_id for r in filter_code_sessions(rows, closed=False)] == ["b"]
    assert [r.task_id for r in filter_code_sessions(rows, grep="LOGIN")] == ["a"]
    # filters AND together
    assert [r.task_id for r in filter_code_sessions(rows, agent="main", closed=True)] == ["a"]


def test_filter_sort_updated_preserves_input_order_identity() -> None:
    rows = [_row("z", t=300.0), _row("a", t=100.0), _row("m", t=200.0)]
    # 'updated' (default) must NOT re-sort — preserves the input order exactly.
    assert [r.task_id for r in filter_code_sessions(rows, sort="updated")] == ["z", "a", "m"]
    assert filter_code_sessions(rows) == rows


def test_filter_sort_agent_and_task() -> None:
    rows = [
        _row("t2", agent="b", t=100.0),
        _row("t1", agent="a", t=50.0),
        _row("t3", agent="a", t=200.0),
    ]
    # agent asc, then most-recent first, then task_id
    assert [r.task_id for r in filter_code_sessions(rows, sort="agent")] == ["t3", "t1", "t2"]
    assert [r.task_id for r in filter_code_sessions(rows, sort="task")] == ["t1", "t2", "t3"]


def test_filter_limit_after_filter_sort() -> None:
    rows = [_row("a"), _row("b"), _row("c")]
    assert [r.task_id for r in filter_code_sessions(rows, limit=2)] == ["a", "b"]


def test_filter_unknown_sort_raises() -> None:
    with pytest.raises(ValueError):
        filter_code_sessions([_row("a")], sort="bogus")


# ---------------------------------------------------------------------------
# CW16 — read-path: filter wiring over a real store, zero-write
# ---------------------------------------------------------------------------


def _seed_multi(db: Path) -> None:
    """Three code sessions: a main (login goal), an explore scout, and a
    closed main."""
    event_log, content_store, dispatcher = build_sqlite_stack(str(db))
    event_log.emit(
        task_id="s_default", type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="fix the login bug", policy_name="react", agent_name="main"
        ),
    )
    event_log.emit(
        task_id="s_review", type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="review the PR", policy_name="react", agent_name="explore"
        ),
    )
    event_log.emit(
        task_id="s_closed", type="TaskCreated",
        payload=TaskCreatedPayload(
            goal="old work", policy_name="react", agent_name="main"
        ),
    )
    event_log.emit(
        task_id="s_closed", type="ConversationClosed",
        payload=ConversationClosedPayload(closed_by="leo", reason=None),
    )
    for obj in (event_log, content_store, dispatcher):
        close = getattr(obj, "close", None)
        if callable(close):
            close()


def _file_bytes(db: Path) -> bytes:
    return db.read_bytes()


def test_list_default_projection_all_sessions(tmp_path: Path) -> None:
    """The default (unfiltered) projection surfaces every code session."""
    db = tmp_path / "s.db"
    _seed_multi(db)
    rows = _list(db)
    assert {r.task_id for r in rows} == {"s_default", "s_review", "s_closed"}


def test_list_filter_wiring(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _seed_multi(db)
    base = _list(db)

    def ids(**flags: object) -> set[str]:
        return {r.task_id for r in filter_code_sessions(base, **flags)}  # type: ignore[arg-type]

    assert ids(agent="explore") == {"s_review"}
    assert ids(status="closed") == {"s_closed"}
    assert ids(grep="login") == {"s_default"}
    assert ids(closed=True) == {"s_closed"}
    assert ids(closed=False) == {"s_default", "s_review"}
    # AND combination
    assert ids(agent="main", closed=False) == {"s_default"}


def test_list_filter_no_match_returns_empty(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _seed_multi(db)
    assert filter_code_sessions(_list(db), agent="nope") == []


def test_list_read_path_zero_write(tmp_path: Path) -> None:
    db = tmp_path / "s.db"
    _seed_multi(db)
    before_b, before_uv = _file_bytes(db), _user_version(db)
    # A full read-only open + project + filter must not perturb the file.
    rows = filter_code_sessions(_list(db), agent="main", limit=1)
    assert len(rows) == 1  # two main-agent sessions, capped to 1
    assert rows[0].agent == "main"
    assert _file_bytes(db) == before_b
    assert _user_version(db) == before_uv
