"""The host seam the :class:`InteractionDriver` drives: the L0 triple, engine
resolution, the host-fixed default model, and name → :class:`AgentSpec` lookup.

Structural Protocols rather than a concrete host type, so single-agent fakes,
remote-proxy hosts and the SDK host all slot into the same driver plumbing
without subclassing. The surface is bounded by what the driver actually calls.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from noeta.agent.spec import AgentSpec
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.engine import EngineProtocol
from noeta.protocols.event_log import EventLogFull


__all__ = [
    "AgentRegistryProtocol",
    "ResidentHost",
]


@runtime_checkable
class AgentRegistryProtocol(Protocol):
    """Name → :class:`AgentSpec` resolve seam for the execution layer.

    The whole spec is returned so execution-layer code (budget caps, capability
    gating, per-agent policy wiring) needs no second lookup seam. Implementations
    MUST raise on unknown names — the driver treats ``start(agent=unknown)`` as a
    hard error before any durable write. ``UnknownAgentError`` is the
    conventional type, but any ``Exception`` works: the driver surfaces it
    verbatim. Alias support is implementation-defined.
    """

    def resolve(self, name: str) -> AgentSpec:
        """Return the spec registered under ``name``; unknown names MUST raise."""
        ...


@runtime_checkable
class ResidentHost(Protocol):
    """The resident multi-agent execution surface the :class:`InteractionDriver` drives.

    A structural seam over the L0 triple (``event_log`` / ``content_store`` /
    ``dispatcher``), engine resolution, the agent registry, and ``model`` — the
    host-fixed default bound when a Task carries no folded ``ModelBound``. The
    driver records that default as the opening ``ModelBound`` in ``start``, so a
    resumed turn folds and binds the same model.

    ``drive_pending_subtasks(parent_task)`` is an optional synchronous
    delegation-tree drain: the driver calls it after a driven command only when
    the parent task suspended on a delegation wake, and a host that never
    delegates omits the attribute entirely.
    """

    event_log: EventLogFull
    content_store: ContentStore
    dispatcher: Dispatcher

    model: str
    agent_registry: AgentRegistryProtocol

    def resolve_engine(self, task: Any) -> EngineProtocol:
        """Resolve the Engine driving ``task`` by its folded state.

        The Engine MUST be keyed on the Task's folded
        ``(agent_name, model_binding)``, so a resumed turn rebuilds the same
        Engine and composes the same bytes.
        """
        ...

    def resolve_engine_for_agent(
        self,
        agent_name: str,
        *,
        model: Optional[str] = None,
        workspace: Optional[str] = None,
        provider: Optional[str] = None,
        permission_mode: Optional[str] = None,
        mcp_aliases: tuple[str, ...] = (),
        effort: Optional[str] = None,
        exec_env_ref: Optional[str] = None,
    ) -> EngineProtocol:
        """Resolve a (cached) Engine **by agent name** — used for Task creation.

        ``start`` calls this before a Task (and therefore its recorded
        ``agent_name``) exists, so it cannot go through :meth:`resolve_engine`.
        Every selector is passed explicitly because the seed Engine writes
        ``TaskCreated`` *before* the durable binding is folded back: ``model``,
        ``workspace`` (the workspace name the fs/skill tools run under),
        ``provider`` (the provider name the LLM round-trips run on) and
        ``exec_env_ref`` (the sandbox container ``base_url``, which must match
        the ref the driver is about to weld into ``TaskHostBound``) each fall
        back to the host default when ``None``; ``permission_mode`` /
        ``mcp_aliases`` / ``effort`` are per-turn, non-durable selectors that
        still have to shape the seed Engine.
        """
        ...

    # Optional members a Protocol cannot express:
    #   def drive_pending_subtasks(self, parent_task: Any) -> Any: ...
    #   provider_models: Mapping[str, tuple[str, ...]]
    # A host that downsinks provider selection to session level exposes the
    # provider→model-list table the driver pair-checks against; absent or empty
    # means the single-provider path with no pair check.
