"""Execution-layer host + registry Protocols.

The :class:`InteractionDriver` (and future execution machinery) is written
against these Protocols rather than the concrete
:class:`noeta.agent.execution.resolver.CodeEngineResolver`, so alternative
resident-host implementations (single-agent fakes, SDK-side
SDK-agent hosts, remote-proxy hosts) can slot into the same driver plumbing
without subclassing or code changes.

Two Protocols:

* :class:`AgentRegistryProtocol` — the name → :class:`AgentSpec` lookup seam.
  The driver resolves the spec by name when creating a Task; the full spec is
  returned so execution-layer code (budget caps read from the spec, capability
  gating, per-agent policy wiring hints) can use the same registry without a
  second lookup seam.

* :class:`ResidentHost` — the full execution-host surface the driver requires:
  the L0 triple (``event_log`` / ``content_store`` / ``dispatcher``), the
  agent → :class:`~noeta.protocols.engine.EngineProtocol` resolver, the
  host-fixed default model, and the agent registry. An optional
  ``drive_pending_subtasks`` supports multi-agent delegation drains; hosts
  that never delegate (single-agent fakes) omit the attribute and the driver
  treats it as a no-op (S3b drain discipline).

Code-agnostic by contract: this module imports only ``noeta.protocols`` /
``noeta.core`` / ``noeta.agent`` — never ``noeta.agent`` (enforced by the
import-linter ``execution-not-code`` contract).
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

    Implementations MUST raise a well-defined error on unknown names (the
    driver treats ``start(agent=unknown)`` as a hard error before any
    durable write, matching D2). Alias support is
    implementation-defined: the code-product implementation accepts
    ``"main"`` / ``"default"`` as aliases for the same canonical spec;
    product-neutral implementations may reject aliases.

    The ``UnknownAgentError`` raised by the canonical
    :class:`noeta.agent.registry.AgentRegistry` is the conventional error
    type, but implementations may raise any ``Exception`` subclass (the
    driver surfaces it verbatim to the caller).
    """

    def resolve(self, name: str) -> AgentSpec:
        """Return the spec registered under ``name``.

        Unknown names MUST raise — no silent fallback to a default spec.
        """
        ...


@runtime_checkable
class ResidentHost(Protocol):
    """The resident multi-agent execution surface the :class:`InteractionDriver` drives.

    A thin structural seam over the L0 triple + engine resolver + agent
    registry — intentionally a strict subset of the concrete
    :class:`~noeta.agent.execution.resolver.CodeEngineResolver`'s public
    API, bounded by what the driver actually calls (enumerated below).

    Required attributes / methods:

    * ``event_log`` — full read+write event stream (``system_emit`` for
      control-plane writes such as ``TaskCancelled``; ``read`` for
      trace-id recovery on cancel; passed to :func:`noeta.core.fold.fold`
      for task-state reconstruction).
    * ``content_store`` — content-addressed blob store (passed to
      :func:`~noeta.core.fold.fold` and
      :func:`~noeta.policies.control_tools.load_questions_body`).
    * ``dispatcher`` — task lifecycle (``enqueue`` / ``lease`` / ``wake``).
    * ``model`` — host-fixed default model id used when a Task has no
      folded ``ModelBound`` (old recordings, CLI sessions that never
      switched). Recorded as the opening ``ModelBound`` in the driver's
      ``start`` path, so a resumed turn folds and binds the same model.
    * ``agent_registry`` — name → ``AgentSpec`` lookup. Used in ``start`` to
      resolve the named Agent bound into ``TaskCreated`` / ``AgentBound``
      (the provenance lock D3).
    * ``resolve_engine(task)`` — task-keyed engine resolution (every
      drive path *after* Task creation: resume, approve, lifecycle
      writes). The returned Engine MUST be keyed on the Task's folded
      ``(agent_name, model_binding)`` so a resumed turn rebuilds the same
      Engine and composes the same bytes.
    * ``resolve_engine_for_agent(agent_name, *, model=None)`` —
      agent-name-keyed engine resolution used *before* Task creation to
      get the seed Engine that writes ``TaskCreated``.

    Optional attribute (S3b delegation drain):

    * ``drive_pending_subtasks(parent_task)`` — synchronous in-request
      delegation-tree drain. When present, the driver calls it after
      each driven command if the parent task suspended on a delegation
      wake. Absent hosts simply never drain (single-agent hosts,
      control-plane-only lifecycle hosts).
    """

    # -- L0 triple ---------------------------------------------------------
    event_log: EventLogFull
    content_store: ContentStore
    dispatcher: Dispatcher

    # -- host configuration ------------------------------------------------
    model: str
    agent_registry: AgentRegistryProtocol

    # -- engine resolution -------------------------------------------------
    def resolve_engine(self, task: Any) -> EngineProtocol:
        """Resolve the Engine driving ``task`` by its folded state.

        Implementations typically key on ``(agent_name, model_binding)``
        read from the Task's folded governance + ``TaskCreated`` payload.
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
        ``agent_name``) exists, so it cannot go through
        :meth:`resolve_engine`. ``model`` overrides the host-fixed
        default (used when the opening selector binds a specific model).
        ``workspace`` (I3) is the per-session workspace **name**
        the seed Engine runs its fs/skill tools under (``None`` ⇒ host default
        dir). ``provider`` (I4) is the per-session provider
        **name** the seed Engine runs its LLM round-trips on (``None`` ⇒ host
        default provider). ``exec_env_ref`` (T6) is the per-session sandbox
        container ``base_url`` the seed Engine's fs / shell tools target
        (``None`` ⇒ local host), passed explicitly so the seed matches the ref
        the driver is about to weld into ``TaskHostBound``. ``permission_mode`` /
        ``mcp_aliases`` / ``effort`` are per-turn, non-durable selectors that
        must shape the seed Engine before the task exists. These are passed
        explicitly because the seed Engine writes ``TaskCreated`` *before* the
        durable binding is folded back.
        """
        ...

    # -- optional S3b delegation drain ------------------------------------
    # def drive_pending_subtasks(self, parent_task: Any) -> Any: ...

    # -- optional provider registry (I4) --------------------
    # A host that downsinks provider to session-level exposes the
    # provider→model-list table the driver pair-checks against. Absent /
    # empty ⇒ the single-provider path (no pair check; byte-equal pre-I4).
    # provider_models: Mapping[str, tuple[str, ...]]
