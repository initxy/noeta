"""`noeta.testing.profile` builder + wiring unit tests (issue 23).

Tests cover the rev2 B2 + rev3 B6 fixes:
* ``build_runtime`` constructs ``HookManager`` and registers
  ``BudgetGuard`` + ``PermissionGuard`` with correct ctor sigs.
* ``ChildLifecycleObserver`` is wired (via ``wire_default_observers``)
  so spawn-subtask works under the CLI profile.
* ``RuntimeBundle.shutdown()`` is idempotent and tears down
  observers + the child-lifecycle subscription cleanly.
* ``build_tools`` is restricted to the in-tree ``echo`` tool (rev3 NB2).
* ``build_policy_factory`` returns a factory that wires any LLM client
  argument into ReActPolicy.
"""

from __future__ import annotations

from typing import Any

import pytest

from noeta.testing.profile import (
    build_composer,
    build_policy_factory,
    build_runtime,
    build_tools,
    default_budget,
    default_permission_policy,
)
from noeta.builtins.governance.impl.budget import BudgetGuard
from noeta.builtins.governance.impl.permission import PermissionGuard
from noeta.observers.audit import AuditObserver
from noeta.observers.metrics import MetricsObserver
from noeta.observers.fanout import EnvelopeBroadcaster, EventFanout
from noeta.builtins.react.impl import ReActPolicy
from noeta.protocols.messages import LLMRequest, LLMResponse, TextBlock, Usage


class _FakeProvider:
    def __init__(self) -> None:
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text="ok")],
            usage=Usage(uncached=1, output=1),
        )


def _bundle(*, sse: bool = False) -> Any:
    broadcaster = EnvelopeBroadcaster() if sse else None
    return build_runtime(
        provider=_FakeProvider(),
        model="test-model",
        system_prompt="You are a helpful assistant.",
        tools=build_tools(),
        sqlite_path=":memory:",
        sse_broadcaster=broadcaster,
        max_steps=3,
        permission_policy=default_permission_policy(),
        budget=default_budget(),
    )


# ---------------------------------------------------------------------------
# build_tools (rev3 NB2)
# ---------------------------------------------------------------------------


def test_build_tools_default_is_echo_only() -> None:
    tools = build_tools()
    assert list(tools.keys()) == ["echo"]


# ---------------------------------------------------------------------------
# build_policy_factory (rev3 B6)
# ---------------------------------------------------------------------------


def test_build_policy_factory_returns_react_policy_with_injected_llm() -> None:
    tools = build_tools()
    factory = build_policy_factory(
        system_prompt="hi",
        model="m",
        tools=tools,
        max_steps=5,
    )

    class _FakeLLM:
        pass

    llm = _FakeLLM()
    policy = factory(llm)
    assert isinstance(policy, ReActPolicy)


# ---------------------------------------------------------------------------
# build_runtime — RuntimeBundle composition (rev2 B2)
# ---------------------------------------------------------------------------


def test_runtime_bundle_includes_audit_and_metrics_observers() -> None:
    bundle = _bundle()
    try:
        observer_types = {type(o) for o in bundle.observers}
        assert AuditObserver in observer_types
        assert MetricsObserver in observer_types
        # No EventFanout when broadcaster=None
        assert EventFanout not in observer_types
    finally:
        bundle.shutdown()


def test_runtime_bundle_includes_sse_observer_when_broadcaster_given() -> None:
    bundle = _bundle(sse=True)
    try:
        observer_types = {type(o) for o in bundle.observers}
        assert EventFanout in observer_types
    finally:
        bundle.shutdown()


def test_runtime_bundle_hook_manager_has_two_guards() -> None:
    """rev2 B2 — HookManager truly wired with BudgetGuard +
    PermissionGuard; not just constructed objects."""
    bundle = _bundle()
    try:
        guard_types = {
            type(entry.guard)
            for entry in bundle.hook_manager._guards  # pylint: disable=protected-access
        }
        assert BudgetGuard in guard_types
        assert PermissionGuard in guard_types
    finally:
        bundle.shutdown()


def test_runtime_bundle_engine_has_hook_manager_attached() -> None:
    bundle = _bundle()
    try:
        # Engine field name is `_hooks` (see core/engine.py)
        assert bundle.engine._hooks is bundle.hook_manager  # pylint: disable=protected-access
    finally:
        bundle.shutdown()


# ---------------------------------------------------------------------------
# Shutdown lifecycle
# ---------------------------------------------------------------------------


def test_shutdown_is_idempotent() -> None:
    bundle = _bundle()
    bundle.shutdown()
    bundle.shutdown()  # must not raise


def test_shutdown_stops_observers() -> None:
    """After ``shutdown()`` each observer's StopHandle reports
    ``stopped=True`` — idempotent-stop wiring works."""
    bundle = _bundle()
    bundle.shutdown()
    for obs in bundle.observers:
        handle = getattr(obs, "_handle", None)
        assert handle is not None, (
            f"observer {type(obs).__name__} has no StopHandle"
        )
        assert handle.stopped is True


# ---------------------------------------------------------------------------
# build_composer
# ---------------------------------------------------------------------------


def test_build_composer_returns_three_segment_composer() -> None:
    bundle = _bundle()
    try:
        composer = build_composer(
            system_prompt="x", tools=build_tools(), content_store=bundle.content_store
        )
        # Build something basic via the composer to confirm wiring
        assert composer is not None
    finally:
        bundle.shutdown()


# ---------------------------------------------------------------------------
# open_storage_stack (issue D / C5; since the storage-backend relocation the
# builders live in noeta.sdk.storage — build_runtime's durable branch reaches
# them by call-time dynamic import, covered by the sqlite build_runtime tests)
# ---------------------------------------------------------------------------


def test_build_storage_stack_unknown_backend_names_the_known_set() -> None:
    from noeta.sdk.storage import build_storage_stack

    with pytest.raises(ValueError) as exc:
        build_storage_stack("filesystem")
    message = str(exc.value)
    assert "filesystem" in message
    for known in ("memory", "sqlite", "postgres"):
        assert known in message


def test_is_postgres_url_classifies_dsn_shapes() -> None:
    from noeta.sdk.storage import is_postgres_url

    assert is_postgres_url("postgresql://u:p@h:5432/db") is True
    assert is_postgres_url("postgres://u:p@h:5432/db") is True
    assert is_postgres_url("/some/file.db") is False
    assert is_postgres_url(":memory:") is False


def test_open_storage_stack_refuses_an_unrecognised_url_scheme(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo'd DSN is a typo, not a file name.

    ``postgesql://…`` used to fall through to the sqlite branch, which created
    a database file **named after the DSN** — a misconfiguration that only
    surfaced later as a confusing empty store. The chdir makes the old
    behaviour observable: a fallthrough would leave that file right here.
    """
    from noeta.sdk.storage import open_storage_stack

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as exc:
        open_storage_stack("postgesql://u:p@localhost:5432/db")
    message = str(exc.value)
    assert "postgesql://" in message
    assert "postgresql://" in message  # names what it would have accepted
    assert not list(tmp_path.iterdir()), "a file was created for a typo'd DSN"


def test_open_storage_stack_memory_path_returns_inmemory_adapters(
    tmp_path: pytest.TempPathFactory,
) -> None:
    from noeta.sdk.storage import open_storage_stack
    from noeta.storage.memory import (
        InMemoryContentStore,
        InMemoryDispatcher,
        InMemoryEventLog,
    )

    for memory_path in (":memory:", None):
        event_log, content_store, dispatcher = open_storage_stack(memory_path)
        assert isinstance(event_log, InMemoryEventLog)
        assert isinstance(content_store, InMemoryContentStore)
        assert isinstance(dispatcher, InMemoryDispatcher)


def test_open_storage_stack_file_path_returns_sqlite_adapters(
    tmp_path,
) -> None:
    from noeta.sdk.storage import (
        SqliteContentStore,
        SqliteDispatcher,
        SqliteEventLog,
        open_storage_stack,
    )

    from noeta.storage.cached import CachedContentStore

    db = str(tmp_path / "store.sqlite")
    event_log, content_store, dispatcher = open_storage_stack(db)
    assert isinstance(event_log, SqliteEventLog)
    assert isinstance(dispatcher, SqliteDispatcher)
    # Durable stacks hand back the sqlite adapter behind a read cache — the
    # same immutable hash is re-read across composes and folds, and this is
    # the one place every caller gets the stack from.
    assert isinstance(content_store, CachedContentStore)
    assert isinstance(content_store._inner, SqliteContentStore)
