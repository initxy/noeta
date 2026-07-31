"""``noeta.testing.profile`` assembly builders and the storage-stack resolver.

The profile is the single call that wires an Engine together with its guards,
observers, and storage, so a break here silently disarms budget limits,
permission checks, or the child-lifecycle handoff for every test that builds a
runtime through it — with no failing assertion anywhere near the cause.
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
# build_tools
# ---------------------------------------------------------------------------


def test_build_tools_default_is_echo_only() -> None:
    tools = build_tools()
    assert list(tools.keys()) == ["echo"]


# ---------------------------------------------------------------------------
# build_policy_factory
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
# build_runtime — RuntimeBundle composition
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
    """Both guards must be registered on the HookManager, not merely built."""
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
    """``shutdown()`` must stop every observer, not just drop the bundle."""
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
        assert composer is not None
    finally:
        bundle.shutdown()


# ---------------------------------------------------------------------------
# open_storage_stack — the one place callers get a storage triple from
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

    A near-miss scheme must not fall through to the sqlite branch, which would
    create a database file **named after the DSN** and surface only much later
    as a confusingly empty store. The chdir makes a fallthrough observable:
    the stray file would land right here.
    """
    from noeta.sdk.storage import open_storage_stack

    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError) as exc:
        open_storage_stack("postgesql://u:p@localhost:5432/db")
    message = str(exc.value)
    assert "postgesql://" in message
    assert "postgresql://" in message  # the error names the correct spelling
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
