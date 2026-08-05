"""Per-task agent→Engine resolver skeleton.

Three domain seams (agent lookup, spawnable-set parsing, engine build) are left
as abstract hooks; a concrete subclass fills them in while the skeleton owns the
shared resolution logic — the Engine cache key, the ask_user_question masks, and
the delegation/spawnable inheritance rule. The cache key must reproduce the same
Engine for a resumed turn, so every binding dimension a session can vary (model,
workspace, provider, sandbox container, permission mode, MCP aliases, effort)
extends a flat key tuple. Declared as a plain class rather than a ``@dataclass``
so a dataclass subclass supplies the real field storage and ``__init__`` while
keeping its field table byte-identical.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Callable, Optional

from noeta.agent.registry import UnknownAgentError
from noeta.agent.spec import agent_activates
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.execution.subtask_drain import (
    DrainHost,
    UnsupportedSubtaskSuspend,
    drive_pending_subtasks,
    resume_woken_parent,
)
from noeta.policies.control_semantics import WORKFLOW_AGENT_NAME
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.policy import Policy
from noeta.protocols.wake import SubtaskCompleted, SubtaskGroupCompleted
from noeta.runtime.cancellation import CancellationRegistry
from noeta.runtime.injection import InjectionInbox


__all__ = [
    "GenericEngineResolver",
    "agent_name_of",
]

#: Upper bound on the in-process Engine cache. Mirrors the constant in
#: ``noeta.client.host`` so both sides of the resolver hierarchy use the same cap.
_MAX_CACHED_ENGINES: int = 256

#: Sentinel for ``_engine_for_agent(policy_wrapper=...)`` distinguishing "caller
#: did not pass it" (⇒ the host's ``self.policy_wrapper``) from "caller passed
#: ``None``" (⇒ build unwrapped — a delegated child is one-shot and must NOT
#: suspend on the next-goal handle). A bare ``Optional[...] = None`` default
#: cannot tell the two apart, so a subtask's explicit ``None`` would be
#: overwritten by ``self.policy_wrapper`` and the child would be wrapped.
_POLICY_WRAPPER_UNSET: Any = object()


def agent_name_of(event_log: EventLogFull, task_id: str) -> str:
    """Read a Task's recorded ``TaskCreated.agent_name`` (durable, resume-safe).

    The genesis event self-describes its Agent: this reads the
    authoritative selector straight off the recording, not in-memory state.
    Raises if the Task has no ``TaskCreated`` (a malformed recording).
    """
    for env in event_log.read(task_id):
        if env.type == "TaskCreated":
            return str(getattr(env.payload, "agent_name", ""))
    raise UnknownAgentError(task_id=task_id, agent_name="<no TaskCreated>", available=[])


def _subtask_output_schema(
    event_log: EventLogFull, task_id: str
) -> Optional[dict[str, Any]]:
    """Read a subtask's per-helper ``output_schema`` off its durable
    ``TaskCreated.inputs``.

    Written by the orchestration interpreter's ``agent(goal, schema=...)``
    spawn (see ``noeta.policies.orchestration``); read here so the drain's
    child-engine build mounts the ``structured_output`` control schema + the
    ``StructuredOutputPolicy`` receipt for exactly that helper. ``None``
    (missing / non-dict — every plain child) keeps the build byte-identical
    to the schema-free path. Durable + resume-safe: a cold re-drive re-reads
    the same recorded inputs and rebuilds the same engine shape.
    """
    for env in event_log.read(task_id):
        if env.type == "TaskCreated":
            inputs = getattr(env.payload, "inputs", None) or {}
            schema = inputs.get("output_schema")
            return dict(schema) if isinstance(schema, dict) else None
    return None


class GenericEngineResolver:
    """Per-task agent→Engine resolver skeleton.

    The common engine-resolution logic lives here; concrete subclasses implement
    the three abstract seams below. Designed as a **plain class** (not a
    ``@dataclass``) so a dataclass subclass can keep its full field table
    **byte-identical** — fields are declared here as pure annotations for
    type-checker visibility, and the subclass's ``@dataclass`` machinery
    supplies the real storage + ``__init__``.
    """

    # --- field annotations (storage supplied by the @dataclass subclass) ---
    event_log: EventLogFull
    content_store: ContentStore
    dispatcher: Dispatcher
    model: str
    delegation_allowed: bool
    #: host-level kill-switch for the ``run_workflow`` control tool
    #: (a form of delegation: it spawns subtasks). Default off; the deployment
    #: opts in (e.g. ``HostConfig.workflow_enabled``). Mirrors ``delegation_allowed``.
    workflow_allowed: bool
    policy_wrapper: Optional[Callable[[Policy], Policy]]
    unnamed_fallback: Optional[Any]
    # The cache key carries two session-scoped dimensions — ``workspace``
    # (per-session fs-root name) then ``provider`` (bound provider name) — each
    # ``None`` for the host-fixed default. The key stays a flat tuple so both
    # extend it without a structural change. Bounded LRU via OrderedDict
    # (cap = _MAX_CACHED_ENGINES) + a threading Lock to serialise
    # get-or-build-put under ThreadingHTTPServer concurrency.
    _engines: OrderedDict[
        tuple[
            str, str, bool, Optional[str], Optional[str], Optional[str],
            tuple[str, ...], Optional[str], Optional[str], bool,
        ],
        Engine,
    ]
    _engines_lock: threading.Lock
    #: Per-key Engine-build locks: the global ``_engines_lock`` guards only the
    #: cache map, while a build (including a live MCP connect) runs outside it,
    #: one-per-key via these locks, so a slow/hanging connector cannot serialise
    #: every session's Engine build. Storage supplied by the @dataclass subclass;
    #: lazily created in ``_engine_for_agent`` for older test doubles.
    _engine_builds: dict[Any, threading.Lock]
    # per-turn, NON-durable permission_mode carrier
    # keyed by task_id (storage supplied by the @dataclass subclass — see
    # ``SdkHost._turn_permission_mode``). Set via :meth:`note_turn_permission`
    # before resolution, read in :meth:`resolve_engine` to thread the mode into
    # the cache key + build.
    _turn_permission_mode: dict[str, Optional[str]]
    #: per-turn, NON-durable enabled-MCP-alias carrier keyed by
    #: task_id (storage supplied by the @dataclass subclass — see
    #: ``SdkHost._turn_mcp_aliases``). The frontend sends the alias clean list
    #: each turn (NO url / token — those live host-side); the driver records
    #: it here via :meth:`note_turn_mcp` before resolution, read in
    #: :meth:`resolve_engine` to thread the aliases into the cache key + build.
    #: ``()`` (default / no enabled servers) ⇒ no live MCP tools.
    _turn_mcp_aliases: dict[str, tuple[str, ...]]
    #: Per-turn, NON-durable reasoning-effort carrier keyed by task_id. Mirrors
    #: permission/MCP: set before Engine resolution, read into the cache key +
    #: build inputs. ``None`` ⇒ host/provider default.
    _turn_effort: dict[str, Optional[str]]
    #: cancel-cascade — process-local set of cancelled root task ids. The
    #: driver's ``cancel`` marks the root here (via :meth:`request_cancellation`)
    #: alongside the durable ``TaskCancelled`` event; :meth:`drive_pending_subtasks`
    #: binds a per-tree predicate off it so a child mid-flight abandons its result
    #: at the next turn boundary. Storage supplied by the @dataclass subclass.
    _cancellation: CancellationRegistry
    #: mid-turn injection — process-local inbox of pending injections keyed by
    #: task id. The driver's ``inject_goal`` submits here (via
    #: :meth:`submit_injection`) alongside the durable ``InjectionRequested``
    #: event; the worker's drain reads it at each turn boundary (via
    #: :meth:`pending_injections`) and drops each once its consuming
    #: ``MessagesAppended`` is durable (via :meth:`consume_injection`). Storage
    #: supplied by the @dataclass subclass; never resumed from.
    _injection_inbox: InjectionInbox

    # --- abstract seams ---------------------------------------------------
    def _lookup_agent(self, name: str, *, task_id: str) -> Any:
        """Resolve ``name`` → an agent object, or raise ``UnknownAgentError``.

        Contract for implementations:
          * The returned object must expose ``.name``, ``.plugins`` (the
            activation tuple read through
            :func:`~noeta.agent.spec.agent_activates` — ``"todo_write"`` /
            ``"ask_user_question"`` / ``"delegation"`` / ``"mcp"`` membership),
            and a ``.spawnable`` member parseable by :meth:`_spawnable_set`.
          * An unknown ``name`` **must** raise ``UnknownAgentError`` carrying
            the supplied ``task_id``, the bad ``name``, and a sorted
            ``available`` list of legal names.
          * The ``"unnamed"`` case is NOT handled here — callers branch on it
            before invoking this hook (using ``self.unnamed_fallback``).
        """
        raise NotImplementedError

    def _spawnable_set(self, spawnable: Any) -> frozenset[str]:
        """Parse ``agent.spawnable`` into a set of known agent names.

        Accepts whatever shape the product's agent definitions emit (a list, a
        frozenset, an alias-bearing dict …) and returns a ``frozenset`` of
        concrete agent names that the host's :meth:`_lookup_agent` can resolve.
        Unresolvable names are dropped (the caller never sees them).
        """
        raise NotImplementedError

    def _build_engine(
        self,
        agent: Any,
        model: str,
        *,
        delegation_enabled: bool,
        allowed_subtask_agents: frozenset[str],
        ask_user_question_enabled: bool,
        policy_wrapper: Optional[Callable[[Policy], Policy]],
        workspace: Optional[str] = None,
        provider: Optional[str] = None,
        permission_mode: Optional[str] = None,
        mcp_aliases: tuple[str, ...] = (),
        effort: Optional[str] = None,
        task_id: Optional[str] = None,
        exec_env_ref: Optional[str] = None,
        structured_output_schema: Optional[dict[str, Any]] = None,
    ) -> Engine:
        """Build a real ``Engine`` for ``agent`` on ``model``.

        ``task_id`` is the task whose stream a skipped-MCP-server
        observer event is recorded on (``None`` for the seed/by-name path where
        no task exists yet — that path is built without live MCP). It is NOT part
        of the cache key: a re-resolve of the same (agent, …, mcp_aliases) key
        returns the cached Engine without reconnecting, so the connect + any skip
        event fire exactly once per built Engine.

        ``GenericEngineResolver`` itself never inspects the product-specific
        knobs (write modes, shell modes, workspace dir, provider, hooks,
        budget, MCP specs, skill settings, …). The hook receives the four
        cross-product arguments it computed; an implementation is responsible
        for reading the remaining fields off ``self`` and/or the agent's
        activation tuple (e.g. ``agent_activates(agent, "todo_write")``) and
        forwarding them to its engine factory.

        ``workspace`` is the per-session workspace **absolute path**
        (``None`` ⇒ the host-fixed default dir). An implementation uses it
        directly as the Engine's fs/skill tools root; the generic skeleton only
        threads the path string through the cache key.

        ``provider`` is the per-session provider **name**
        (``None`` ⇒ the host default provider). An implementation resolves it
        to a configured LLM adapter instance for this Engine's round-trips; the
        generic skeleton only threads the name through the cache key.

        ``exec_env_ref`` is the per-session sandbox container ``base_url``
        (``None`` ⇒ the local host / the host-default sandbox config). An
        implementation resolves it to a live sandbox backend the Engine's fs /
        shell tools run their IO against — a resumed / reclaimed session
        reconnects to THIS container by its address; the generic skeleton only
        threads it through the cache key.

        ``structured_output_schema`` is a workflow helper's per-helper JSON
        Schema (read off its durable ``TaskCreated.inputs.output_schema`` by
        :meth:`drive_pending_subtasks`' child-engine builder — the only
        caller that ever passes it). An implementation mounts the
        ``structured_output`` control schema (its ``parameters`` = this
        schema) AND wraps the built policy in ``StructuredOutputPolicy`` so
        the helper's call becomes its final answer. ``None`` (every other
        build, including every cached :meth:`_engine_for_agent` path) leaves
        the build unchanged.
        """
        raise NotImplementedError

    def _engine_cache_scope(
        self, agent: Any, task_id: Optional[str]
    ) -> Optional[str]:
        """Optional host-defined Engine-cache partition for ``(agent, task)``.

        The cache key deliberately omits ``task_id`` — engines are SHARED
        across tasks with equal bindings. A host whose engine material varies
        per task beyond the standard key dimensions (e.g. the SdkHost's
        per-tenant memory root, whose store is baked into the built Engine's
        tool closures and resident index) returns a stable string scope here;
        engines then resolve per ``(key, scope)`` so one task's material can
        never serve another scope's task via the cache. Must be cheap, total,
        and deterministic for a given ``(agent, task_id)`` — it runs on every
        engine resolve. ``None`` (this default — every host without
        task-varying material) keeps the shared slot.
        """
        return None

    def _build_orchestration_engine(
        self, task_id: str, *, allowed_subtask_agents: frozenset[str]
    ) -> Engine:
        """Build the reserved ``__workflow__`` child's Engine.

        Routed from :meth:`drive_pending_subtasks` when a child's recorded
        ``agent_name`` is :data:`WORKFLOW_AGENT_NAME` (not a named agent). The
        implementation reads the child's script/args from its durable
        ``TaskCreated.inputs`` and builds an Engine whose Policy is the
        orchestration interpreter (``OrchestrationPolicy``); ``allowed_subtask_agents``
        is the inherited worker set its ``agent()`` calls may spawn into. Has the
        ``task_id`` (unlike :meth:`_build_engine`) precisely because the script
        lives on that task's stream.
        """
        raise NotImplementedError

    # --- common surface ---------------------------------------------------
    @property
    def engine(self) -> Engine:
        """The single-Engine fallback (Protocol requirement): the default
        Agent's Engine. A resident host normally drives via
        :meth:`resolve_engine`; this is the degenerate single-Agent view.
        """
        return self._engine_for_agent(self._lookup_agent("default", task_id="<default-engine>"))

    def note_turn_permission(
        self, task_id: str, permission_mode: Optional[str]
    ) -> None:
        """Stash a turn's NON-durable permission_mode.

        The frontend sends a per-turn ``permission_mode`` selector; the driver
        records it here (keyed by ``task_id``) before the Engine is resolved, so
        both the synchronous seed-time resolve AND the later background-thread
        drive (async transport) read the SAME mode. ``None`` means "no per-turn
        selection" → :meth:`_build_engine` falls back to the host-fixed default.
        Never written to the event log (resume re-derives nothing from it — the
        recorded approval decisions are resumed directly). Overwritten each turn,
        never evicted, so a turn that suspends on approval resolves the same mode
        on resume.
        """
        self._turn_permission_mode[str(task_id)] = permission_mode

    def note_turn_effort(self, task_id: str, effort: Optional[str]) -> None:
        """Stash a turn's NON-durable reasoning-effort override."""
        carrier = getattr(self, "_turn_effort", None)
        if carrier is not None:
            carrier[str(task_id)] = effort

    def note_turn_mcp(
        self, task_id: str, aliases: tuple[str, ...]
    ) -> None:
        """Stash a turn's NON-durable enabled-MCP-alias list.

        The frontend sends the enabled server **aliases** each turn (a clean
        list like ``("github", "notion")`` — never url / token, which live
        host-side); the driver records them here keyed by ``task_id`` before
        the Engine is resolved so both the synchronous seed-time resolve AND the
        later background-thread drive read the SAME set. ``()`` means "no enabled
        MCP servers" → :meth:`_build_engine` builds no live MCP tools.
        Never written to the event log (the recorded tool schema is the durable
        truth; the alias list is only the runtime selector that decides which
        servers to connect this turn). Overwritten each turn; a turn that
        suspends on approval resolves the same set on resume."""
        carrier = getattr(self, "_turn_mcp_aliases", None)
        if carrier is not None:
            carrier[str(task_id)] = tuple(aliases)

    def forget_turn_carriers(self, task_id: str) -> None:
        """Drop a task's per-turn carrier entries (permission_mode / effort /
        mcp aliases). Called from the conversation-end control verbs
        (``cancel`` / ``close``) — mirrors :meth:`forget_background_subagents`.

        The carriers are written every turn and were otherwise **never evicted**
        (one entry per task, forever), so a long-lived server serving many
        conversations over a long uptime leaked one entry per carrier per task.
        Evicting at conversation end bounds them to live-conversation lifetime.
        Safe against reopen: a subsequent ``send_goal`` re-notes the carriers for
        its new turn before the Engine resolves, so nothing a resume needs is
        lost (the carriers are non-durable runtime selectors, never resumed from
        the event log)."""
        key = str(task_id)
        self._turn_permission_mode.pop(key, None)
        for name in ("_turn_effort", "_turn_mcp_aliases"):
            carrier = getattr(self, name, None)
            if carrier is not None:
                carrier.pop(key, None)

    def request_cancellation(self, task_id: str) -> None:
        """cancel-cascade — mark ``task_id`` cancelled in the process-local
        registry so an in-flight child of this tree abandons its result at
        the next turn boundary. Called by :meth:`InteractionDriver.cancel`
        right after it writes the durable ``TaskCancelled`` event. Guarded
        with ``getattr`` so a subclass that omitted the field is a no-op
        rather than an ``AttributeError``."""
        reg = getattr(self, "_cancellation", None)
        if reg is not None:
            reg.request(task_id)

    def is_cancelled(self, task_id: str) -> bool:
        """cancel-cascade — whether ``task_id``'s tree has been cancelled."""
        reg = getattr(self, "_cancellation", None)
        return reg.is_cancelled(task_id) if reg is not None else False

    def discard_cancellation(self, task_id: str) -> None:
        """Human stop — drop ``task_id``'s registry mark once a stopped turn has
        settled (or an explicit new goal supersedes it), so a later resumed turn
        on the same task is not pre-aborted by a stale mark, and the set does not
        grow unbounded. Idempotent; a host that omitted the field is a no-op."""
        reg = getattr(self, "_cancellation", None)
        if reg is not None:
            reg.discard(task_id)

    def submit_injection(
        self, task_id: str, injection_id: str, descriptor: dict[str, Any]
    ) -> None:
        """mid-turn injection — record a pending injection in the process-local
        inbox so the running drive notices it at the next turn boundary. Called
        by ``inject_goal`` right after it writes the durable ``InjectionRequested``
        event. Guarded so a subclass without the field is a no-op."""
        inbox = getattr(self, "_injection_inbox", None)
        if inbox is not None:
            inbox.submit(task_id, injection_id, descriptor)

    def pending_injections(self, task_id: str) -> dict[str, dict[str, Any]]:
        """mid-turn injection — the pending injections for ``task_id`` (arrival
        order), for the worker's drain. Empty when none / no inbox seam."""
        inbox = getattr(self, "_injection_inbox", None)
        return inbox.snapshot(task_id) if inbox is not None else {}

    def consume_injection(self, task_id: str, injection_id: str) -> None:
        """mid-turn injection — drop one injection from the inbox once its
        consuming ``MessagesAppended`` is durable. Idempotent; no-op without
        the seam."""
        inbox = getattr(self, "_injection_inbox", None)
        if inbox is not None:
            inbox.consume(task_id, injection_id)

    def discard_injections(self, task_id: str) -> None:
        """mid-turn injection — drop every pending injection for ``task_id`` at
        conversation teardown (mirror of :meth:`discard_cancellation`).
        Idempotent; no-op without the seam."""
        inbox = getattr(self, "_injection_inbox", None)
        if inbox is not None:
            inbox.discard(task_id)

    def resolve_engine(self, task: Any) -> Engine:
        """Resolve the Engine driving ``task`` by its folded state.

        Folds the Task's ``TaskCreated.agent_name`` → :meth:`_lookup_agent` →
        cached :meth:`_build_engine`. An unknown ``agent_name`` is a hard
        :class:`UnknownAgentError` at lease time, not a silent
        no-op. ``"unnamed"`` resolves to ``unnamed_fallback`` when one was
        supplied, else also hard-errors.

        The resolver key is the full
        ``(agent_name, model binding, ask_user_question_enabled)``. The bound
        model is read from the Task's latest ``ModelBound`` fold
        (``governance.model_binding``); a recording with **no**
        ``ModelBound`` folds to ``None`` → the host-fixed default
        :attr:`model` is used, so resume re-records the same
        ``LLMRequestStartedPayload.model``. A per-turn switch (a later
        ``ModelBound`` with a different model) resolves a distinct Engine for
        that model.
        """
        task_id = str(getattr(task, "task_id", ""))
        name = agent_name_of(self.event_log, task_id)
        # A ``__workflow__`` child recorded on the stream is the orchestration
        # interpreter, NOT a named agent — route it (with the task_id, so its
        # script is read off its stream) BEFORE the registry lookup that would
        # otherwise raise ``UnknownAgentError`` for the reserved name. Mirrors
        # the drain's ``_build_subtask_engine`` (gate above its ``_lookup_agent``);
        # without this, a ``__workflow__`` child claimed by a resident worker's
        # untargeted ``tick()`` (rather than the drain's targeted descent) hits
        # ``_lookup_agent("__workflow__")`` and hard-errors. The inherited
        # spawnable set comes from the child's DIRECT parent agent (the one that
        # called ``run_workflow``) — equal to what the drain threads at the
        # same tree layer. Returns uncached, exactly as the drain path does.
        if name == WORKFLOW_AGENT_NAME:
            parent_id = getattr(task, "parent_task_id", None)
            inherited: frozenset[str] = frozenset()
            if parent_id is not None:
                parent_name = agent_name_of(self.event_log, str(parent_id))
                parent_agent = self._lookup_agent(parent_name, task_id=str(parent_id))
                inherited = self._spawnable_set(parent_agent.spawnable)
            return self._build_orchestration_engine(
                task_id, allowed_subtask_agents=inherited
            )
        model = self._bound_model_for(task)
        # the per-session workspace absolute path welded into the durable record,
        # folded from the Task's ``TaskHostBound`` (``governance.workspace``);
        # ``None`` on a non-session recording → the host-fixed default dir.
        workspace = self._bound_workspace_for(task)
        # the per-session provider name folded from the latest
        # ``ModelBound`` (``governance.provider_binding``); ``None`` on a
        # recording that never bound a provider → the host default provider.
        provider = self._bound_provider_for(task)
        # the per-session sandbox container base_url folded from
        # ``TaskHostBound`` (``governance.exec_env_ref``); ``None`` on every
        # local / non-sandbox recording → the local host. When set,
        # a resumed / reclaimed task reconnects to THIS container.
        exec_env_ref = self._bound_exec_env_ref_for(task)
        # the per-turn, NON-durable permission_mode the
        # driver stashed for this task. ``None`` (no per-turn selection — resume /
        # daemon / CLI) ⇒ the host-fixed default.
        permission_mode = self._turn_permission_mode.get(task_id)
        # the per-turn, NON-durable enabled-MCP-alias list the driver
        # stashed for this task. ``()`` (no enabled servers — resume / daemon /
        # CLI) ⇒ no live MCP tools.
        mcp_aliases = getattr(self, "_turn_mcp_aliases", {}).get(task_id, ())
        effort = getattr(self, "_turn_effort", {}).get(task_id)
        # The multi-turn wrapper is a TOP-LEVEL-session concern only. A delegated
        # child (has a parent) is one-shot: it must finish with a real
        # ``TaskCompleted`` so the ``ChildLifecycleObserver`` fires the parent's
        # wake. Wrapping a child turns its ``FinishDecision`` into a next-goal
        # suspend → the child never reaches terminal → the parent's
        # ``SubtaskGroupCompleted`` barrier never fires → deadlock (only under a
        # resident worker pool / multi-host, where an idle worker's untargeted
        # ``tick()`` claims the child ahead of the drain's targeted descent).
        # ``None`` here is the SAME gate the drain's ``_build_subtask_engine``
        # uses (``policy_wrapper=None``); pass it through so the per-task
        # resident-worker path matches the in-drain path. Uses the identical
        # parent/depth predicate as the ``ask_user_question`` mask below.
        is_subtask = (
            getattr(task, "parent_task_id", None) is not None
            or int(getattr(task, "subtask_depth", 0) or 0) > 0
        )
        subtask_wrapper: Optional[Callable[[Policy], Policy]] = (
            None if is_subtask else self.policy_wrapper
        )
        # Subtasks carry no TaskHostBound of their own — the fold leaves
        # their governance.exec_env_ref / workspace / provider as None. A
        # delegation tree runs in ONE container / fs root / provider (the
        # root parent's binding), so a subtask must inherit the parent's
        # bound values to resolve the SAME sandbox backend — otherwise the
        # child gets the local host (no browser tools, container-isolated fs
        # visibility). Mirrors _build_drain_host's inheritance for the
        # foreground drain path; this branch covers the resident-worker path
        # (resolve_engine) where an idle worker claims a child task.
        if is_subtask:
            parent_id = getattr(task, "parent_task_id", None)
            if parent_id is not None:
                parent = fold(
                    self.event_log, self.content_store, str(parent_id)
                )
                if exec_env_ref is None:
                    exec_env_ref = self._bound_exec_env_ref_for(parent)
                if workspace is None:
                    workspace = self._bound_workspace_for(parent)
                if provider is None:
                    provider = self._bound_provider_for(parent)
        if name == "unnamed" and self.unnamed_fallback is not None:
            return self._engine_for_agent(
                self.unnamed_fallback,
                model=model,
                ask_user_question_enabled=False,
                workspace=workspace,
                provider=provider,
                permission_mode=permission_mode,
                mcp_aliases=mcp_aliases,
                effort=effort,
                task_id=task_id,
                exec_env_ref=exec_env_ref,
                policy_wrapper=subtask_wrapper,
            )
        agent = self._lookup_agent(name, task_id=task_id)
        # ask_user_question comes from agent identity, masked to depth-0
        # root tasks (a delegated child never inherits it).
        return self._engine_for_agent(
            agent,
            model=model,
            ask_user_question_enabled=(
                agent_activates(agent, "ask_user_question")
                and getattr(task, "parent_task_id", None) is None
                and int(getattr(task, "subtask_depth", 0) or 0) == 0
            ),
            workspace=workspace,
            provider=provider,
            permission_mode=permission_mode,
            mcp_aliases=mcp_aliases,
            effort=effort,
            task_id=task_id,
            exec_env_ref=exec_env_ref,
            policy_wrapper=subtask_wrapper,
        )

    def _bound_model_for(self, task: Any) -> str:
        """The model binding the Task resolves on.

        The latest ``ModelBound`` the Engine folded into
        ``GovernanceState.model_binding``; ``None`` (a recording that never
        switched) falls back to the host-fixed default :attr:`model` so the
        recorded ``LLMRequestStartedPayload.model`` is unchanged.
        """
        bound = getattr(getattr(task, "governance", None), "model_binding", None)
        return bound if isinstance(bound, str) and bound else self.model

    def _bound_workspace_for(self, task: Any) -> Optional[str]:
        """The per-session workspace **absolute path** the Task is bound to.

        Read from the ``TaskHostBound`` fold (``governance.workspace``, which
        stores the absolute path welded into the durable record); ``None``
        (no binding — a non-session recording, or a name-style record that
        folds to None) means "use the host-fixed default dir", so the recorded
        fs root is unchanged.
        """
        bound = getattr(getattr(task, "governance", None), "workspace", None)
        return bound if isinstance(bound, str) and bound else None

    def _bound_provider_for(self, task: Any) -> Optional[str]:
        """The per-session provider name the Task is bound to.

        Read from the latest ``ModelBound`` fold
        (``governance.provider_binding``); ``None`` (no binding — a session
        that only ever bound a model) means "use the host default provider",
        so the recorded provider is unchanged.
        """
        bound = getattr(getattr(task, "governance", None), "provider_binding", None)
        return bound if isinstance(bound, str) and bound else None

    def _bound_exec_env_ref_for(self, task: Any) -> Optional[str]:
        """The sandbox container ``base_url`` the Task is bound to.

        Read from the ``TaskHostBound`` fold (``governance.exec_env_ref``);
        ``None`` (every local / non-sandbox recording) means "use the local host
        / the host-default sandbox config". When present, a resumed /
        **reclaimed** session — possibly on another host — reconnects to THIS
        container address rather than the folding host's own config (the
        multi-machine reconnect criterion). The API key is not here; the
        reconnecting host re-reads it from its env.
        """
        bound = getattr(getattr(task, "governance", None), "exec_env_ref", None)
        return bound if isinstance(bound, str) and bound else None

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
    ) -> Engine:
        """Resolve a (cached) Engine **by agent name** — for Task creation.

        The :class:`InteractionDriver` (or equivalent task-creating surface)
        needs the seed Engine that writes ``TaskCreated`` *before* a Task
        (and therefore its recorded ``agent_name``) exists, so it cannot go
        through the Task-keyed :meth:`resolve_engine`. This shares the same
        registry lookup + per-(agent, model, ask) cache: an unknown
        ``agent_name`` is the same hard :class:`UnknownAgentError`
        (``agent_name`` is load-bearing), so a caller can never create a
        Task naming an unresolvable Agent. ``"unnamed"`` resolves to
        ``unnamed_fallback`` when supplied, else also hard-errors.

        ``model`` overrides the host-fixed default for the seed
        Engine, so a session opened with a model selector seeds and drives
        the first turn on the bound model; ``None`` keeps the host default.

        ``workspace`` is the per-session workspace **absolute path**
        the seed Engine runs its fs/skill tools under (``None`` ⇒ the host-fixed
        default dir). The seed Engine writes ``TaskCreated`` before the
        ``TaskHostBound`` workspace_dir is folded back, so this is passed
        explicitly — it must equal the path the driver records in the binding so
        the first turn and every resumed turn resolve the same fs root.

        ``provider`` is the per-session provider **name** the
        seed Engine runs its LLM round-trips on (``None`` ⇒ the host default
        provider). Like ``model``, it is passed explicitly because the opening
        ``ModelBound`` (carrying the provider) is written *after* the seed Engine
        is built — the explicit name and the durable name must match so the first
        turn and every resumed turn resolve the same adapter.
        """
        if agent_name == "unnamed" and self.unnamed_fallback is not None:
            return self._engine_for_agent(
                self.unnamed_fallback,
                model=model,
                ask_user_question_enabled=(
                    agent_activates(self.unnamed_fallback, "ask_user_question")
                ),
                workspace=workspace,
                provider=provider,
                permission_mode=permission_mode,
                mcp_aliases=mcp_aliases,
                effort=effort,
                exec_env_ref=exec_env_ref,
            )
        agent = self._lookup_agent(agent_name, task_id="<unbound>")
        # the seed engine is a root resident session — ask_user_question is
        # the agent's own capability (no parent/depth to mask against yet).
        return self._engine_for_agent(
            agent,
            model=model,
            ask_user_question_enabled=agent_activates(agent, "ask_user_question"),
            workspace=workspace,
            provider=provider,
            permission_mode=permission_mode,
            mcp_aliases=mcp_aliases,
            effort=effort,
            exec_env_ref=exec_env_ref,
        )

    def drive_pending_subtasks(self, parent_task: Any) -> Any:
        """Server-side delegation drain.

        A parent turn that suspended on a ``SubtaskCompleted`` /
        ``SubtaskGroupCompleted`` wake is driven to its resumed terminal via
        the SHARED :func:`drive_pending_subtasks` state machine.

        Child inheritance (mirroring the child-engine build): every child
        Engine is built with delegation INHERITED — ``delegation_enabled=True``
        + the **root parent's** ``spawnable`` set + the same depth-capped Budget
        — NOT sourced from the leaf child agent's own (possibly delegation-free)
        identity. Recursion is bounded by the depth-capped Budget
        (``BudgetGuard.max_subtask_depth``), never by the absence of a child
        spawn schema, so the child's recorded ``spawn_subagent`` schema matches
        what resume rebuilds.
        """
        host = self._build_drain_host(parent_task)
        return drive_pending_subtasks(host, parent_task)

    def resume_woken_parent(self, parent_task: Any) -> Any:
        """Out-of-band resume of a delegation-suspended parent whose child
        settled through its OWN command turn (approve / deny / answer after
        an :class:`UnsupportedSubtaskSuspend`), so the
        :class:`ChildLifecycleObserver` wake reached the dispatcher with no
        drain descent to consume it. Same :class:`DrainHost` as
        :meth:`drive_pending_subtasks`; returns the settled parent task or
        ``None`` when the parent is not resumable (wake not fired yet)."""
        host = self._build_drain_host(parent_task)
        return resume_woken_parent(host, parent_task)

    def settle_subtasks_after_step(self, task_id: str) -> None:
        """Resident-worker counterpart to ``InteractionDriver.drive_seeded``'s
        delegation tail (``_drain_pending_subtasks`` + ``_resume_woken_ancestors``).

        The in-request driver drains a delegation subtree synchronously after
        every command; the ``background_drive`` resident :class:`WorkerLoop` has
        no such tail. A parent it drove to a ``SubtaskCompleted`` /
        ``SubtaskGroupCompleted`` barrier would strand its FOREGROUND children:
        the :class:`ChildLifecycleObserver` enqueues them, but nothing seeds +
        drives them — only :func:`subtask_drain._descend_to_child` turns a
        child's ``state.goal`` into the opening user message, and the bare
        ``run_leased_task`` step never does (an unseeded child requests the model
        with no messages and the provider rejects it). Calling this after
        ``run_leased_task`` settles the subtree through the SAME
        :meth:`drive_pending_subtasks` state machine (byte-equal child seeding),
        then walks up to resume any ancestor whose wake was delivered
        out-of-band.

        ``UnsupportedSubtaskSuspend`` (a descendant paused for approval / human
        input) is a legitimate suspend, swallowed here; the child's own later
        command re-enters via :meth:`resume_woken_parent`. This is L2-internal so
        the WorkerLoop (which cannot import ``noeta.execution``) reaches it as a
        duck-typed seam on the runtime it drives.
        """
        task = fold(self.event_log, self.content_store, task_id)
        if getattr(task, "status", None) == "suspended" and isinstance(
            getattr(task, "wake_on", None),
            (SubtaskCompleted, SubtaskGroupCompleted),
        ):
            try:
                self.drive_pending_subtasks(task)
            except UnsupportedSubtaskSuspend:
                pass
        self._resume_woken_ancestors(task_id)

    def _resume_woken_ancestors(self, task_id: str) -> None:
        """Walk up the parent chain and resume each delegation-suspended
        ancestor whose wake the :class:`ChildLifecycleObserver` delivered
        out-of-band (mirrors ``InteractionDriver._resume_woken_ancestors``).

        Each ancestor that resumes all the way to terminal wakes ITS parent, so
        the walk continues until an ancestor stays suspended (its own next turn
        / another pending member) or the chain tops out. A deeper descendant
        hitting its own approval suspend leaves the tree durably consistent —
        the next resolution re-enters here.
        """
        current = task_id
        while True:
            events = self.event_log.read(current)
            parent_id = (
                getattr(events[0].payload, "parent_task_id", None)
                if events
                else None
            )
            if not parent_id:
                return
            parent = fold(self.event_log, self.content_store, parent_id)
            try:
                settled = self.resume_woken_parent(parent)
            except UnsupportedSubtaskSuspend:
                return
            if settled is None or getattr(settled, "status", None) != "terminal":
                return
            current = parent_id

    def _build_drain_host(self, parent_task: Any) -> DrainHost:
        """Build the :class:`DrainHost` for a parent's delegation tree.

        The background-subagent driver
        (docs/adr/background-subagent.md) builds the SAME host — same
        child-engine builder, inherited workspace / provider / permission / MCP,
        cancel predicate, and child-session-content activation — to drive a
        single background child on the shared executor. The only difference at
        the call site is whether the host drives a barrier-suspended parent
        (foreground) or one un-barriered child (background).
        """
        # cancel-cascade: the whole delegation tree is keyed by its root
        # (the task the user cancels). Bind a per-tree predicate the drain
        # threads into every child's ``run_one_step`` AND polls between
        # children, so a cancel mid-flight tears the tree down.
        root_id = str(parent_task.task_id)
        cancel_check = lambda: self.is_cancelled(root_id)  # noqa: E731
        root_agent_name = agent_name_of(self.event_log, parent_task.task_id)
        root_agent = self._lookup_agent(root_agent_name, task_id=parent_task.task_id)
        inherited_subtasks = self._spawnable_set(root_agent.spawnable)
        # children share the root session's fs root — the
        # delegation tree runs in ONE workspace (the root parent's absolute path
        # binding), not each child's host default. ``None`` parent workspace ⇒
        # host default.
        inherited_workspace = self._bound_workspace_for(parent_task)
        # children likewise run in the root session's SANDBOX container — a
        # delegation tree shares ONE container (subtasks share the parent's
        # cwd/disk), so a child inherits the root's bound ``exec_env_ref``
        # (subtasks carry no ``TaskHostBound`` of their own; the fold leaves
        # their ``governance.exec_env_ref`` None). ``None`` ⇒ the local host.
        inherited_exec_env_ref = self._bound_exec_env_ref_for(parent_task)
        # children likewise run on the root session's bound
        # provider — the whole delegation tree shares ONE provider (the root
        # parent's binding), not each child's host default. ``None`` ⇒ host
        # default.
        inherited_provider = self._bound_provider_for(parent_task)
        # the whole delegation tree also shares the root session's bound
        # MODEL: a child without its own declared default_model inherits the
        # root parent's ``ModelBound`` binding instead of silently dropping
        # to the host default. Gated to a binding that DIFFERS from the host
        # default — the driver binds every session at open, so a root on the
        # default model keeps children unbound.
        bound = getattr(
            getattr(parent_task, "governance", None), "model_binding", None
        )
        inherited_model = (
            bound
            if isinstance(bound, str) and bound and bound != self.model
            else None
        )
        # the whole delegation tree shares the root
        # session's per-turn permission_mode — read from the parent's NON-durable
        # carrier (set by the driver for the spawning turn). ``None`` ⇒ host
        # default.
        inherited_permission = self._turn_permission_mode.get(
            str(parent_task.task_id)
        )
        # the parent task's enabled MCP alias list (NON-durable,
        # the driver stashed it for the spawning turn). A child inherits this
        # set ONLY when its own spec opens the ``mcp`` capability (per-spec
        # opt-in); a child without it gets ``()`` (no MCP tools). The opt-in
        # child connects its OWN independent server sessions (independent
        # recording — a resume reads its own recorded specs back, never
        # reconnects).
        # ``()`` parent aliases ⇒ no child ever gets MCP.
        inherited_mcp = getattr(self, "_turn_mcp_aliases", {}).get(
            str(parent_task.task_id), ()
        )
        # the whole delegation tree shares the root session's per-turn
        # reasoning-effort override — read from the parent's NON-durable carrier
        # (set by the driver for the spawning turn), same pattern as
        # permission_mode. Without it a child falls back to effort None, which
        # on the Responses provider also drops the reasoning-ciphertext
        # include and breaks the child's prompt-cache prefix. ``None`` ⇒ host
        # default.
        inherited_effort = getattr(self, "_turn_effort", {}).get(
            str(parent_task.task_id)
        )

        def _child_mcp_aliases(child_agent: Any) -> tuple[str, ...]:
            # inherit the parent's enabled aliases only when the child
            # spec opts in. ``agent_activates`` tolerates a spec without the
            # ``mcp`` activation (or a non-AgentSpec like __workflow__ carrying no
            # ``plugins``) — both stay MCP-free.
            return inherited_mcp if agent_activates(child_agent, "mcp") else ()

        def _build_subtask_engine(task_id: str) -> Engine:
            # a child recorded as __workflow__ is the orchestration
            # interpreter, not a named agent — route it (with the task_id, so the
            # script can be read off its stream) BEFORE the registry lookup that
            # would raise UnknownAgentError for the reserved name.
            if agent_name_of(self.event_log, task_id) == WORKFLOW_AGENT_NAME:
                return self._build_orchestration_engine(
                    task_id, allowed_subtask_agents=inherited_subtasks
                )
            # The child's own agent (its tools / system prompt / read-only
            # allowlist) — but delegation is INHERITED from the root, not read
            # from this leaf agent's identity. No policy_wrapper:
            # children are one-shot, never multi-turn wrapped.
            # ``ask_user_question`` is OFF for children (depth>0), mirroring
            # the resolve_engine mask.
            # the child runs on its agent's declared
            # default model when one exists, else the root session's inherited
            # bound model, else the host default (each non-default choice is
            # recorded as the child's opening ModelBound by the drain, so a
            # cold resume rebuilds the same binding). An agent carrying no
            # ``default_model`` attribute → getattr None; an unbound /
            # default-bound root leaves ``inherited_model`` None → host model.
            child_agent = self._lookup_agent(
                agent_name_of(self.event_log, task_id), task_id=task_id
            )
            child_model = (
                getattr(child_agent, "default_model", None)
                or inherited_model
                or self.model
            )
            return self._build_engine(
                child_agent,
                child_model,
                delegation_enabled=True,
                allowed_subtask_agents=inherited_subtasks,
                ask_user_question_enabled=False,
                policy_wrapper=None,
                workspace=inherited_workspace,
                provider=inherited_provider,
                exec_env_ref=inherited_exec_env_ref,
                permission_mode=inherited_permission,
                # per-spec opt-in MCP inheritance. The opt-in child
                # connects its own server sessions; ``task_id`` so a connect
                # skip records ``McpServerSkipped`` on the CHILD's stream.
                mcp_aliases=_child_mcp_aliases(child_agent),
                effort=inherited_effort,
                task_id=task_id,
                # Per-helper structured output: a workflow helper spawned via
                # ``agent(goal, schema=...)`` carries the declared JSON Schema
                # in its durable ``TaskCreated.inputs.output_schema`` — thread
                # it so the child mounts the ``structured_output`` control
                # schema + the ``StructuredOutputPolicy`` receipt wrapper.
                # ``None`` (every plain child) leaves the build unchanged.
                # Built uncached (this direct ``_build_engine`` call never
                # goes through ``_engine_for_agent``), so the schema-shaped
                # engine can never leak to a sibling via the cache key.
                structured_output_schema=_subtask_output_schema(
                    self.event_log, task_id
                ),
            )

        def _child_model_binding(task_id: str) -> Optional[tuple[str, str]]:
            # __workflow__ has no agent spec / declared model → no binding
            # (the orchestration interpreter makes no LLM calls of its own;
            # the workers it spawns inherit through this same callback).
            if agent_name_of(self.event_log, task_id) == WORKFLOW_AGENT_NAME:
                return None
            child_agent = self._lookup_agent(
                agent_name_of(self.event_log, task_id), task_id=task_id
            )
            declared = getattr(child_agent, "default_model", None)
            if declared:
                return (declared, "agent-default")
            if inherited_model:
                return (inherited_model, "inherited")
            return None

        # A child's session-level residents (instructions + environment, plus a
        # memory index when the child's activation carries it) are pre-loop
        # activated by the drain itself, running ``run_content_init`` over the
        # child engine's own ``content_init_hooks`` — the same generic
        # ``init`` seam ``InteractionDriver.seed_start`` uses for a top-level
        # session. The child engine snapshots the INHERITED workspace (the whole
        # delegation tree runs in one fs root), the same source its composer
        # renders from, so no host snapshot callback crosses into the drain.
        host = DrainHost(
            dispatcher=self.dispatcher,
            event_log=self.event_log,
            content_store=self.content_store,
            build_child_engine=_build_subtask_engine,
            # The ROOT parent resumes on the SAME engine (with the
            # MultiTurnReActPolicy wrapper) that drove its spawning turn — so
            # the resumed run_one_step composes byte-identically — while a
            # non-root parent rebuilds its own (child-shaped) agent engine.
            parent_engine=lambda pid, *, is_root: (
                self.resolve_engine(fold(self.event_log, self.content_store, pid))
                if is_root
                else _build_subtask_engine(pid)
            ),
            on_root_release=lambda _lease_id: None,
            child_model_binding=_child_model_binding,
            child_provider=inherited_provider,
            cancel_check=cancel_check,
            discard_cancellation=lambda: self.discard_cancellation(root_id),
        )
        return host

    def _engine_for_agent(
        self,
        agent: Any,
        *,
        model: Optional[str] = None,
        ask_user_question_enabled: Optional[bool] = None,
        workspace: Optional[str] = None,
        provider: Optional[str] = None,
        permission_mode: Optional[str] = None,
        mcp_aliases: tuple[str, ...] = (),
        effort: Optional[str] = None,
        task_id: Optional[str] = None,
        exec_env_ref: Optional[str] = None,
        policy_wrapper: Any = _POLICY_WRAPPER_UNSET,
    ) -> Engine:
        """Per-agent Engine builder + cache.

        The cache key is
        ``(agent_name, model, ask_user_question_enabled, workspace, provider)``
        — the model is part of the binding (a per-turn switch resolves a
        distinct Engine), ``workspace`` is the per-session fs-root **absolute path**
        so two concurrent sessions on different directories never share an Engine
        (and their files never cross), and ``provider`` is the per-session
        provider name so two sessions on different providers never share an Engine.
        ``None`` workspace / provider ⇒ the host-fixed defaults.

        ``todo_write`` / ``ask_user_question`` are AGENT identity, not host
        config. ``effective_ask`` is the (already depth-masked) value the caller
        passed; when unspecified it falls back to the agent's own capability.

        Delegation is AGENT identity too, gated by the host kill-switch.
        The authorized sub-agent set comes from the agent's own
        ``spawnable`` (filtered to known agents) — never a host
        input. When delegation is off (agent declares none, or the deployment
        disabled it) the set is empty so no spawn_subagent schema is exposed.
        """
        resolved_model = model if model else self.model
        effective_ask = (
            agent_activates(agent, "ask_user_question")
            if ask_user_question_enabled is None
            else ask_user_question_enabled
        )
        eff_delegation = (
            agent_activates(agent, "delegation") and self.delegation_allowed
        )
        eff_subtask_agents = (
            self._spawnable_set(agent.spawnable)
            if eff_delegation
            else frozenset()
        )
        # when the host enables workflow, run_workflow may spawn the
        # reserved __workflow__ orchestration child, so it must be in the
        # PermissionGuard allow-list. It is NEVER a named agent, so it is filtered
        # out of the model-facing spawn_subagent directory by ``_build_engine``
        # (registry.resolve raises → skipped).
        if self.workflow_allowed:
            eff_subtask_agents = eff_subtask_agents | {WORKFLOW_AGENT_NAME}
        # Delegation is a pure function of (agent, delegation_allowed) and the
        # kill-switch is resolver-fixed, so ``agent.name`` already keys it
        # uniquely — no need to widen the cache key with it. ``workspace``
        # and ``provider`` ARE part of the key: a different session fs-root
        # / provider must resolve a distinct Engine so concurrent sessions never
        # share fs tools or LLM adapter.
        # ``permission_mode`` is a
        # per-turn, NON-durable knob that drives ``require_approval_tools``, so two
        # turns on different permission modes must NOT share a cached Engine.
        # ``mcp_aliases`` is a per-turn,
        # NON-durable enabled-server-alias tuple. Two turns enabling different MCP
        # servers must NOT share a cached Engine (their live tool sets differ), so
        # the alias tuple keys the build.
        # ``exec_env_ref`` is the per-session
        # sandbox container base_url. Two sessions bound to different containers
        # must NOT share a cached Engine (their fs / shell tools target different
        # backends).
        # ``policy_wrapper``: the multi-turn wrapper is a
        # TOP-LEVEL-session concern (it turns a ``FinishDecision`` into a
        # next-goal suspend for ``noeta code chat``). A delegated child is
        # one-shot and must finish with a real ``TaskCompleted``; the resident
        # worker's per-task ``resolve_engine`` therefore passes ``None`` for a
        # subtask (mirroring the drain's ``_build_subtask_engine``), while the
        # root keeps ``self.policy_wrapper``. Keying on ``wrapper is None`` keeps
        # a wrapped root Engine and an unwrapped child Engine (same agent + model
        # + workspace + ask — the common explorer case) in SEPARATE cache slots,
        # so the root's wrapper never leaks to a child via the cache. The
        # ``_POLICY_WRAPPER_UNSET`` sentinel distinguishes "caller did not pass
        # it" (⇒ ``self.policy_wrapper``) from "caller passed ``None``" (⇒ build
        # unwrapped); a plain ``None`` default would conflate the two and re-wrap
        # an explicit-unwrapped child.
        effective_wrapper = (
            self.policy_wrapper
            if policy_wrapper is _POLICY_WRAPPER_UNSET
            else policy_wrapper
        )
        # ``_engine_cache_scope`` is a host-defined
        # partition for engine material that varies per TASK beyond the
        # standard dimensions (e.g. the SdkHost's per-tenant memory root, whose
        # store is baked into the built Engine's tool closures). ``None`` (the
        # base default, every single-tenant host) keeps the shared slot;
        # the cache is in-memory
        # only (never durable), so widening the tuple has no resume effect.
        key = (
            agent.name, resolved_model, effective_ask, workspace, provider,
            permission_mode, mcp_aliases, effort, exec_env_ref,
            effective_wrapper is None,
            self._engine_cache_scope(agent, task_id),
        )
        # the global lock guards only the cache map. The build
        # itself runs OUTSIDE it, guarded by a PER-KEY build lock — one build
        # per key (so the live MCP connect + its McpServerSkipped/observer
        # events fire exactly once), while builds for DIFFERENT keys run
        # concurrently. Holding the global lock across ``_build_engine`` would
        # serialise every session behind one slow/hanging MCP connector —
        # a delegated child could not even build its Engine until an
        # unrelated session's connect finished.
        with self._engines_lock:
            cached = self._engines.get(key)
            if cached is not None:
                self._engines.move_to_end(key)
                return cached
            builds = getattr(self, "_engine_builds", None)
            if builds is None:
                # Older @dataclass subclasses / test doubles supply no
                # storage — create it lazily under the global lock.
                builds = {}
                self._engine_builds = builds
            build_lock = builds.setdefault(key, threading.Lock())
        with build_lock:
            try:
                # Double-check: a concurrent thread may have finished this key's
                # build while we waited on its lock.
                with self._engines_lock:
                    cached = self._engines.get(key)
                    if cached is not None:
                        self._engines.move_to_end(key)
                        return cached
                engine = self._build_engine(
                    agent,
                    resolved_model,
                    delegation_enabled=eff_delegation,
                    allowed_subtask_agents=eff_subtask_agents,
                    ask_user_question_enabled=effective_ask,
                    policy_wrapper=effective_wrapper,
                    workspace=workspace,
                    provider=provider,
                    permission_mode=permission_mode,
                    mcp_aliases=mcp_aliases,
                    effort=effort,
                    task_id=task_id,
                    exec_env_ref=exec_env_ref,
                )
                with self._engines_lock:
                    self._engines[key] = engine
                    # LRU eviction: drop the oldest entry when over the cap.
                    if len(self._engines) > _MAX_CACHED_ENGINES:
                        self._engines.popitem(last=False)
                return engine
            finally:
                # Always drop the per-key build-lock entry, even if
                # ``_build_engine`` raised — otherwise the Lock leaks in
                # ``_engine_builds`` forever (one per distinct failing key).
                with self._engines_lock:
                    builds.pop(key, None)
