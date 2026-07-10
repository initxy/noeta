"""Default runtime profile — test-support assembly helpers.

Single-file wiring of all five built-in hooks
(``BudgetGuard / PermissionGuard / AuditObserver / MetricsObserver /
EventFanout``) plus :func:`noeta.core.wiring.wire_default_observers`
for ``ChildLifecycleObserver``.

This used to live in ``noeta.cli.profile`` and back the operator CLI; the
operator command suite was removed, so its only remaining consumers are the test
suite (the official backend wires its own storage/observers inline — see
:func:`noeta.agent.backend.lifecycle.serve_backend`). It is therefore test-support and
lives under ``noeta.testing``; it is cli-free, so production layers never reach it
(the ``production-cannot-import-testing`` contract still forbids importing
``noeta.testing``).

Three shared builders — ``build_tools`` / ``build_composer`` /
``build_policy_factory`` — keep a recording and its later resume on the same
prompt source so the rebuilt prompt prefix stays stable (which is what the
provider's stable-prefix prompt cache keys on). Also re-homes the reusable
defaults from the old ``noeta.cli._common`` (``default_budget`` /
``permission_policy_for`` / ``default_permission_policy``).

The storage-stack helpers (``is_memory_path`` / ``build_memory_stack`` /
``build_sqlite_stack`` / ``open_storage_stack``) now live in
:mod:`noeta.storage.stacks` (the single source of truth shared with the
``python -m noeta.agent`` runner) and are re-exported here for existing
importers.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

from noeta.core.engine import Engine
from noeta.core.hooks import HookManager
from noeta.core.wiring import wire_default_observers
from noeta.guards.budget import Budget, BudgetGuard
from noeta.guards.permission import PermissionGuard, PermissionPolicy
from noeta.observers.audit import AuditObserver
from noeta.observers.metrics import MetricsObserver
from noeta.observers.fanout import EnvelopeBroadcaster, EventFanout
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.messages import LLMProvider
from noeta.protocols.tool import Tool
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.stacks import (
    is_memory_path,
    build_memory_stack,
    build_postgres_stack,
    build_sqlite_stack,
    open_storage_stack,
)

if TYPE_CHECKING:
    from noeta.context.composer import ThreeSegmentComposer
    from noeta.policies.react import ReActPolicy


__all__ = [
    "RuntimeBundle",
    "TOOL_PACKS",
    "build_composer",
    "build_policy_factory",
    "build_runtime",
    "build_tools",
    "build_memory_stack",
    "build_postgres_stack",
    "build_sqlite_stack",
    "default_budget",
    "default_permission_policy",
    "is_memory_path",
    "open_storage_stack",
    "permission_policy_for",
    "resolve_tool_pack",
]


# ---------------------------------------------------------------------------
# Default budget / permission policy (re-homed from noeta.cli._common, TL6)
# ---------------------------------------------------------------------------


def default_budget() -> Budget:
    """Minimal budget: caps that won't trip in normal demos but give
    BudgetGuard real values to evaluate."""
    return Budget(
        max_iterations=20,
        max_tool_calls=40,
        max_cost_usd=None,
        max_spawned_subtasks=5,
    )


def permission_policy_for(allowed_tools: frozenset[str]) -> PermissionPolicy:
    """A permission policy allowing exactly ``allowed_tools`` (and any
    subtask agent). Used to widen the policy to a resolved tool pack so the
    pack's tools are not denied at runtime."""
    return PermissionPolicy(
        allowed_tools=allowed_tools,
        denied_tools=frozenset(),
        max_risk_level=None,
        allowed_subtask_agents=None,
    )


def default_permission_policy() -> PermissionPolicy:
    """Minimal permission policy: allow the built-in echo tool."""
    return permission_policy_for(frozenset({"echo"}))


#: Accepted tool-pack names. ``none`` = the built-in ``echo`` only.
TOOL_PACKS = ("none",)


_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeBundle:
    """Wired runtime returned by :func:`build_runtime`.

    Stays open until ``shutdown()`` is called — the test suite invokes
    it in a ``finally`` block so observers + ChildLifecycleObserver
    unsubscribe cleanly.
    """

    engine: Engine
    event_log: EventLogFull
    content_store: ContentStore
    dispatcher: Dispatcher
    hook_manager: HookManager
    observers: tuple[Any, ...]
    shutdown: Callable[[], None]


# ---------------------------------------------------------------------------
# Shared builders (rev3 B6)
# ---------------------------------------------------------------------------


def build_tools() -> dict[str, Tool]:
    """Minimal tool registry: only the in-tree ``echo`` FakeTool is wired."""
    # ``noeta.tools`` ships in noeta-runtime alongside this module, so the
    # lazy import isn't about install boundaries; it keeps importing
    # ``noeta.testing.profile`` cheap for callers who only need one of the
    # other build_* helpers (or the re-exported storage-stack helpers),
    # not the FakeTool machinery.
    from noeta.tools.fake import FakeTool

    return {
        "echo": FakeTool(
            name="echo",
            script={("hello",): "echo-said: hello"},
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
        )
    }


# ---------------------------------------------------------------------------
# Tool packs
# ---------------------------------------------------------------------------


def resolve_tool_pack(
    name: str,
) -> tuple[dict[str, Tool], frozenset[str]]:
    """Resolve a tool-pack name to ``(tools, allowed_tool_names)``.

    The single source of truth for what tools a run wires.

    Returns ``(tools, allowed_tool_names)`` for the given pack name.
    Currently only "none" (built-in echo tool) is supported.
    """
    if name == "none":
        tools = build_tools()
        return tools, frozenset(tools)
    raise ValueError(
        f"unknown tool pack {name!r}; expected one of {TOOL_PACKS}"
    )


def build_composer(
    *,
    system_prompt: str,
    tools: dict[str, Tool],
    content_store: ContentStore,
) -> "ThreeSegmentComposer":
    # ``noeta.context`` ships in noeta-runtime alongside this module; the
    # import stays lazy so importing ``noeta.testing.profile`` for a
    # single unrelated helper doesn't also pull in ThreeSegmentComposer.
    from noeta.context.composer import ThreeSegmentComposer

    return ThreeSegmentComposer(
        system_prompt=system_prompt,
        tools=tools,
        content_store=content_store,
    )


def build_policy_factory(
    *,
    system_prompt: str,
    model: str,
    tools: dict[str, Tool],
    max_steps: int,
) -> Callable[[Any], "ReActPolicy"]:
    """Return a factory that takes an LLMClient and returns a wired
    ReActPolicy. ``build_runtime`` injects a RuntimeLLMClient.
    """
    # ``noeta.policies`` ships in noeta-runtime alongside this module; the
    # import stays lazy for the same reason as ``build_composer`` above —
    # keep this module's cheap helpers cheap to import.
    from noeta.policies.react import ReActPolicy

    def factory(llm: Any) -> ReActPolicy:
        return ReActPolicy(
            llm=llm,
            tools=tools,
            system_prompt=system_prompt,
            model=model,
            max_steps=max_steps,
        )

    return factory


# ---------------------------------------------------------------------------
# Build everything
# ---------------------------------------------------------------------------


def build_runtime(
    *,
    provider: LLMProvider,
    model: str,
    system_prompt: str,
    tools: dict[str, Tool],
    sqlite_path: Optional[str],
    sse_broadcaster: Optional[EnvelopeBroadcaster],
    max_steps: int,
    permission_policy: PermissionPolicy,
    budget: Budget,
    trace_file: Optional["Path"] = None,
) -> RuntimeBundle:
    event_log, content_store, dispatcher = open_storage_stack(sqlite_path)

    llm = RuntimeLLMClient(
        provider=provider, event_log=event_log, content_store=content_store
    )
    composer = build_composer(
        system_prompt=system_prompt, tools=tools, content_store=content_store
    )
    policy_factory = build_policy_factory(
        system_prompt=system_prompt,
        model=model,
        tools=tools,
        max_steps=max_steps,
    )
    policy = policy_factory(llm)

    # rev2 B2: real HookManager + register guards + pass to Engine
    hook_manager = HookManager()
    hook_manager.register(BudgetGuard(budget=budget))
    hook_manager.register(PermissionGuard(policy=permission_policy, tools=tools))

    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools=tools,
        hooks=hook_manager,
    )

    # rev2 B2: wire ChildLifecycleObserver so spawn-subtask works in the
    # assembled runtime. ``event_log`` is typed ``EventLogFull`` (read + write +
    # subscribe), which is exactly what ``wire_default_observers``
    # needs and trivially narrows to ``EventLogSubscriber`` for the
    # individual Observer constructors (issue A / C1 cleanup).
    unsubscribe_child = wire_default_observers(event_log, dispatcher)

    audit = AuditObserver(event_log=event_log)
    metrics = MetricsObserver(event_log=event_log)
    observer_list: list[Any] = [audit, metrics]

    if sse_broadcaster is not None:
        fanout = EventFanout(event_log=event_log, broadcaster=sse_broadcaster)
        observer_list.append(fanout)

    # T1: external trace export — a live-only lifecycle-owning observer
    # (JSONL sink behind a non-blocking async worker). Default off.
    if trace_file is not None:
        from noeta.observers.trace_export import make_jsonl_trace_observer

        observer_list.append(
            make_jsonl_trace_observer(event_log=event_log, path=trace_file)
        )

    def shutdown() -> None:
        for obs in reversed(observer_list):
            with contextlib.suppress(Exception):
                obs.stop()
        with contextlib.suppress(Exception):
            unsubscribe_child()

    return RuntimeBundle(
        engine=engine,
        event_log=event_log,
        content_store=content_store,
        dispatcher=dispatcher,
        hook_manager=hook_manager,
        observers=tuple(observer_list),
        shutdown=shutdown,
    )
