"""Test-support assembly: a whole runtime — storage stack, engine, governance
guards, built-in Observers — from one call.

The shared ``build_*`` helpers exist so that a recording and its later resume
compose from the same prompt source, keeping the stable prefix (which the
provider's prompt cache keys on) byte-identical. Durable storage backends and
the guard / policy classes live in built-in plugins and resolve through
call-time dynamic imports, so this module imports with noeta-runtime alone while
calling :func:`build_runtime` requires noeta-sdk. Hosts assemble through
``noeta.sdk`` instead; this shape only serves tests.
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
from noeta.runtime.governance import Budget, PermissionPolicy
from noeta.observers.audit import AuditObserver
from noeta.observers.metrics import MetricsObserver
from noeta.observers.fanout import EnvelopeBroadcaster, EventFanout
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.messages import LLMProvider
from noeta.protocols.tool import Tool
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import build_stack as _build_memory_stack

if TYPE_CHECKING:
    from noeta.context.composer import ThreeSegmentComposer
    from noeta.protocols.policy import Policy


__all__ = [
    "RuntimeBundle",
    "TOOL_PACKS",
    "build_composer",
    "build_policy_factory",
    "build_runtime",
    "build_tools",
    "default_budget",
    "default_permission_policy",
    "permission_policy_for",
    "resolve_tool_pack",
]


def default_budget() -> Budget:
    """Caps high enough not to trip a normal demo, but real enough that
    BudgetGuard has something to evaluate."""
    return Budget(
        max_iterations=20,
        max_tool_calls=40,
        max_cost_usd=None,
        max_spawned_subtasks=5,
    )


def permission_policy_for(allowed_tools: frozenset[str]) -> PermissionPolicy:
    """Allow exactly ``allowed_tools`` and any subtask agent — how a caller
    widens the policy to a resolved tool pack so the pack's own tools are not
    denied."""
    return PermissionPolicy(
        allowed_tools=allowed_tools,
        denied_tools=frozenset(),
        max_risk_level=None,
        allowed_subtask_agents=None,
    )


def default_permission_policy() -> PermissionPolicy:
    return permission_policy_for(frozenset({"echo"}))


#: Accepted tool-pack names. ``none`` = the built-in ``echo`` only.
TOOL_PACKS = ("none",)


_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeBundle:
    """Wired runtime returned by :func:`build_runtime`.

    Holds live subscriptions until ``shutdown()`` runs, so a caller that skips
    it leaks Observers into the next test; call it from a ``finally``.
    """

    engine: Engine
    event_log: EventLogFull
    content_store: ContentStore
    dispatcher: Dispatcher
    hook_manager: HookManager
    observers: tuple[Any, ...]
    shutdown: Callable[[], None]


def build_tools() -> dict[str, Tool]:
    # Lazy purely to keep importing this module cheap for callers who want one
    # of the other build_* helpers rather than the FakeTool machinery.
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


def resolve_tool_pack(
    name: str,
) -> tuple[dict[str, Tool], frozenset[str]]:
    """Resolve a pack name to ``(tools, allowed_tool_names)`` — the single
    source of truth for what tools a run wires."""
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
    # Lazy for the same reason as build_tools: cheap import for callers who
    # never touch a Composer.
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
) -> Callable[[Any], "Policy"]:
    """Return a factory that takes an LLMClient and returns a wired
    ReActPolicy. ``build_runtime`` injects a RuntimeLLMClient.
    """
    # ReActPolicy lives in the ``react`` built-in plugin: resolved through the
    # loader's dynamic-import doorway so this module keeps no static edge into
    # ``noeta.builtins``.
    import importlib

    ReActPolicy = importlib.import_module("noeta.builtins.react.impl").ReActPolicy

    def factory(llm: Any) -> "Policy":
        return ReActPolicy(
            llm=llm,
            tools=tools,
            system_prompt=system_prompt,
            model=model,
            max_steps=max_steps,
        )

    return factory


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
    # ``sqlite_path`` doubles as the backend selector: ``None`` / ``":memory:"``
    # → memory, a ``postgresql://`` DSN → postgres, anything else → a sqlite
    # file. Only the InMemory backend is a static kernel import; the durable
    # ones live in the ``storage`` built-in and resolve dynamically.
    if sqlite_path is None or sqlite_path == ":memory:":
        event_log, content_store, dispatcher = _build_memory_stack()
    else:
        import importlib

        if sqlite_path.startswith(("postgresql://", "postgres://")):
            stack_module = "noeta.builtins.storage.impl.postgres.stack"
            stack_config: dict[str, str] = {"dsn": sqlite_path}
        else:
            stack_module = "noeta.builtins.storage.impl.sqlite.stack"
            stack_config = {"path": sqlite_path}
        event_log, content_store, dispatcher = importlib.import_module(
            stack_module
        ).build_stack(**stack_config)

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

    # The guard classes live in the ``governance`` built-in; same dynamic-import
    # discipline as the storage backends above.
    import importlib

    _governance = importlib.import_module("noeta.builtins.governance.impl")
    hook_manager = HookManager()
    hook_manager.register(_governance.BudgetGuard(budget=budget))
    hook_manager.register(
        _governance.PermissionGuard(policy=permission_policy, tools=tools)
    )

    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools=tools,
        hooks=hook_manager,
    )

    # ChildLifecycleObserver is what makes spawn-subtask work at all in an
    # assembled runtime.
    unsubscribe_child = wire_default_observers(event_log, dispatcher)

    audit = AuditObserver(event_log=event_log)
    metrics = MetricsObserver(event_log=event_log)
    observer_list: list[Any] = [audit, metrics]

    if sse_broadcaster is not None:
        fanout = EventFanout(event_log=event_log, broadcaster=sse_broadcaster)
        observer_list.append(fanout)

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
