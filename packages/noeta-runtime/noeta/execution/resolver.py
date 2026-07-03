"""Code-agnostic per-task agent→Engine resolver skeleton.

Hoisted verbatim from :class:`noeta.agent.execution.resolver.CodeEngineResolver`.
The three domain seams (agent lookup, spawnable-set parsing, engine build) are
left as abstract hooks; a coding-product subclass (``CodeEngineResolver``) and
any future product-specific resolver fill them in.

Code-agnostic by contract: this module imports only ``noeta.protocols`` /
``noeta.core`` / ``noeta.agent`` — never ``noeta.agent`` (enforced by the
import-linter ``execution-not-code`` contract).
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from typing import Any, Callable, Optional

from noeta.agent.registry import UnknownAgentError
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.execution.environment import record_environment
from noeta.execution.instructions import record_instructions
from noeta.execution.subtask_drain import (
    DrainHost,
    drive_pending_subtasks,
    resume_woken_parent,
)
from noeta.policies.control_tools import WORKFLOW_AGENT_NAME
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.policy import Policy
from noeta.runtime.cancellation import CancellationRegistry


__all__ = [
    "GenericEngineResolver",
    "agent_name_of",
]

#: #13 — upper bound on the in-process Engine cache. Mirrors the constant in
#: ``noeta.client.host`` so both sides of the resolver hierarchy use the same cap.
_MAX_CACHED_ENGINES: int = 256


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


class GenericEngineResolver:
    """Hoisted per-task agent→Engine resolver skeleton.

    The common engine-resolution logic lives here; concrete subclasses (e.g.
    :class:`~noeta.agent.execution.resolver.CodeEngineResolver`) implement the
    three abstract seams below. Designed as a **plain class** (not a
    ``@dataclass``) so a dataclass subclass can keep its full field table
    **byte-identical** — fields are declared here as pure annotations for
    type-checker visibility, and the subclass's ``@dataclass`` machinery
    supplies the real storage + ``__init__``.

    Semantics preserved byte-for-byte from the original
    ``CodeEngineResolver``: the cache key, the ask_user_question masks, the
    delegation/spawnable inheritance rule, and the
    :func:`drive_pending_subtasks` shape are all lifted without change.
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
    # D3 (I4): the cache key gained TWO session-scoped
    # dimensions — ``workspace`` (per-session fs-root name) then ``provider``
    # (bound provider name) — each ``None`` for the host-fixed default. The key
    # stays a flat tuple so both extend it without a structural change.
    # #13: bounded LRU via OrderedDict (cap = _MAX_CACHED_ENGINES) + a threading
    # Lock to serialise get-or-build-put under ThreadingHTTPServer concurrency.
    _engines: OrderedDict[
        tuple[
            str, str, bool, Optional[str], Optional[str], Optional[str],
            tuple[str, ...], Optional[str],
        ],
        Engine,
    ]
    _engines_lock: threading.Lock
    #: item 3 — per-key Engine-build locks. ``_engines_lock`` used to be
    #: held for the FULL ``_build_engine`` (including a live MCP connect),
    #: serialising every session's Engine build behind one slow/hanging
    #: connector. Builds now run outside the global lock, one-per-key via
    #: these locks (storage supplied by the @dataclass subclass; lazily
    #: created in ``_engine_for_agent`` for older test doubles).
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
    #: each turn (NO url / token — those live host-side, D3); the driver records
    #: it here via :meth:`note_turn_mcp` before resolution, read in
    #: :meth:`resolve_engine` to thread the aliases into the cache key + build.
    #: ``()`` (default / no enabled servers / every pre-0042 path) ⇒ no live MCP
    #: tools, byte-identical to before.
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

    # --- abstract seams ---------------------------------------------------
    def _lookup_agent(self, name: str, *, task_id: str) -> Any:
        """Resolve ``name`` → an agent object, or raise ``UnknownAgentError``.

        Contract for implementations:
          * The returned object must expose ``.name`` and ``.capabilities``;
            ``capabilities`` must have boolean members ``todo_write``,
            ``ask_user_question``, ``delegation`` and a
            ``spawnable`` member parseable by :meth:`_spawnable_set`.
            (``plan_mode`` was removed)
          * An unknown ``name`` **must** raise ``UnknownAgentError`` carrying
            the supplied ``task_id``, the bad ``name``, and a sorted
            ``available`` list of legal names.
          * The ``"unnamed"`` case is NOT handled here — callers branch on it
            before invoking this hook (using ``self.unnamed_fallback``).
        """
        raise NotImplementedError

    def _spawnable_set(self, spawnable: Any) -> frozenset[str]:
        """Parse ``agent.capabilities.spawnable`` into a set of known agent names.

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
        for reading the remaining fields off ``self`` and/or
        ``agent.capabilities`` (e.g. ``todo_write_enabled``) and forwarding
        them to its engine factory.

        ``workspace`` is the per-session workspace **absolute path**
        (``None`` ⇒ the host-fixed default dir). An implementation uses it
        directly as the Engine's fs/skill tools root; the generic skeleton only
        threads the path string through the cache key.

        ``provider`` is the per-session provider **name**
        (``None`` ⇒ the host default provider). An implementation resolves it
        to a configured LLM adapter instance for this Engine's round-trips; the
        generic skeleton only threads the name through the cache key.
        """
        raise NotImplementedError

    def _build_orchestration_engine(
        self, task_id: str, *, allowed_subtask_agents: frozenset[str]
    ) -> Engine:
        """Build the reserved ``__workflow__`` child's Engine.

        Routed from :meth:`drive_pending_subtasks` when a child's recorded
        ``agent_name`` is :data:`WORKFLOW_AGENT_NAME` (not a roster agent). The
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

        (hoisted from ``CodeEngineResolver.engine``.)
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
        selection" → :meth:`_build_engine` falls back to the host-fixed default,
        byte-identical to every pre-#4 path. Never written to the event log
        (resume re-derives nothing from it — the recorded approval decisions are
        resumed directly). Overwritten each turn, never evicted, so a turn that
        suspends on approval resolves the same mode on resume.
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
        host-side, D3); the driver records them here keyed by ``task_id`` before
        the Engine is resolved so both the synchronous seed-time resolve AND the
        later background-thread drive read the SAME set. ``()`` means "no enabled
        MCP servers" → :meth:`_build_engine` builds no live MCP tools,
        byte-identical to every pre-0042 path. Never written to the event log
        (the recorded tool schema — R-1 — is the durable truth; the alias list is
        only the runtime selector that decides which servers to connect this
        turn). Overwritten each turn; a turn that suspends on approval resolves
        the same set on resume."""
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

    def resolve_engine(self, task: Any) -> Engine:
        """Hoisted Engine resolver.

        Folds the Task's ``TaskCreated.agent_name`` → :meth:`_lookup_agent` →
        cached :meth:`_build_engine`. An unknown ``agent_name`` is a hard
        :class:`UnknownAgentError` at lease time, not a silent
        no-op. ``"unnamed"`` resolves to ``unnamed_fallback`` when one was
        supplied, else also hard-errors.

        Issue 06: the resolver key is the full
        ``(agent_name, model binding, ask_user_question_enabled)``. The bound
        model is read from the Task's latest ``ModelBound`` fold
        (``governance.model_binding``); an old recording with **no**
        ``ModelBound`` folds to ``None`` → the host-fixed default
        :attr:`model` is used, so resume re-records the same
        ``LLMRequestStartedPayload.model`` and stays byte-equal. A per-turn
        switch (a later ``ModelBound`` with a different model) resolves a
        distinct Engine for that model.
        """
        task_id = str(getattr(task, "task_id", ""))
        name = agent_name_of(self.event_log, task_id)
        model = self._bound_model_for(task)
        # the per-session workspace absolute path is welded into the durable record,
        # folded from the Task's ``TaskHostBound`` (``governance.workspace``);
        # ``None`` on an old / non-session recording → the host-fixed default
        # dir, byte-equal.
        workspace = self._bound_workspace_for(task)
        # the per-session provider name folded from the latest
        # ``ModelBound`` (``governance.provider_binding``); ``None`` on an old /
        # pre-I4 recording → the host default provider, byte-equal.
        provider = self._bound_provider_for(task)
        # the per-turn, NON-durable permission_mode the
        # driver stashed for this task. ``None`` (no per-turn selection — resume /
        # daemon / CLI / every pre-#4 path) ⇒ the host-fixed default, byte-equal.
        permission_mode = self._turn_permission_mode.get(task_id)
        # the per-turn, NON-durable enabled-MCP-alias list the driver
        # stashed for this task. ``()`` (no enabled servers — resume / daemon /
        # CLI / every pre-0042 path) ⇒ no live MCP tools, byte-equal.
        mcp_aliases = getattr(self, "_turn_mcp_aliases", {}).get(task_id, ())
        effort = getattr(self, "_turn_effort", {}).get(task_id)
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
            )
        agent = self._lookup_agent(name, task_id=task_id)
        # S1: ask_user_question comes from agent identity, masked to depth-0
        # root tasks (a delegated child never inherits it). Preserves the exact
        # root/parent/subtask_depth gate; only the source of the bool changed.
        return self._engine_for_agent(
            agent,
            model=model,
            ask_user_question_enabled=(
                agent.capabilities.ask_user_question
                and getattr(task, "parent_task_id", None) is None
                and int(getattr(task, "subtask_depth", 0) or 0) == 0
            ),
            workspace=workspace,
            provider=provider,
            permission_mode=permission_mode,
            mcp_aliases=mcp_aliases,
            effort=effort,
            task_id=task_id,
        )

    def _bound_model_for(self, task: Any) -> str:
        """Hoisted model-binding reader.

        The latest ``ModelBound`` the Engine folded into
        ``GovernanceState.model_binding``; ``None`` (no ``ModelBound`` —
        e.g. an old recording or a CLI session that never switched) falls
        back to the host-fixed default :attr:`model` so the recorded
        ``LLMRequestStartedPayload.model`` is unchanged and resume is
        byte-equal.
        """
        bound = getattr(getattr(task, "governance", None), "model_binding", None)
        return bound if isinstance(bound, str) and bound else self.model

    def _bound_workspace_for(self, task: Any) -> Optional[str]:
        """The per-session workspace **absolute path** the Task is bound to.

        Read from the ``TaskHostBound`` fold (``governance.workspace``, which
        now stores the absolute path welded into the durable record); ``None``
        (no binding — an old / non-session recording, or the legacy name-style
        records that fold to None per the D7 clean break) means
        "use the host-fixed default dir", so the recorded fs root is unchanged
        and resume is byte-equal.
        """
        bound = getattr(getattr(task, "governance", None), "workspace", None)
        return bound if isinstance(bound, str) and bound else None

    def _bound_provider_for(self, task: Any) -> Optional[str]:
        """The per-session provider name the Task is bound to.

        Read from the latest ``ModelBound`` fold
        (``governance.provider_binding``); ``None`` (no binding — an old /
        pre-I4 recording, or a session that only ever bound a model) means "use
        the host default provider", so the recorded provider is unchanged and
        resume is byte-equal.
        """
        bound = getattr(getattr(task, "governance", None), "provider_binding", None)
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
    ) -> Engine:
        """Hoisted by-name Engine resolver.

        The :class:`InteractionDriver` (or equivalent task-creating surface)
        needs the seed Engine that writes ``TaskCreated`` *before* a Task
        (and therefore its recorded ``agent_name``) exists, so it cannot go
        through the Task-keyed :meth:`resolve_engine`. This shares the same
        registry lookup + per-(agent, model, ask) cache: an unknown
        ``agent_name`` is the same hard :class:`UnknownAgentError`
        (``agent_name`` is load-bearing), so a caller can never create a
        Task naming an unresolvable Agent. ``"unnamed"`` resolves to
        ``unnamed_fallback`` when supplied, else also hard-errors.

        ``model`` (issue 06) overrides the host-fixed default for the seed
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
                    self.unnamed_fallback.capabilities.ask_user_question
                ),
                workspace=workspace,
                provider=provider,
                permission_mode=permission_mode,
                mcp_aliases=mcp_aliases,
                effort=effort,
            )
        agent = self._lookup_agent(agent_name, task_id="<unbound>")
        # S1: the seed engine is a root resident session — ask_user_question is
        # the agent's own capability (no parent/depth to mask against yet).
        return self._engine_for_agent(
            agent,
            model=model,
            ask_user_question_enabled=agent.capabilities.ask_user_question,
            workspace=workspace,
            provider=provider,
            permission_mode=permission_mode,
            mcp_aliases=mcp_aliases,
            effort=effort,
        )

    def drive_pending_subtasks(self, parent_task: Any) -> Any:
        """Hoisted server-side delegation drain.

        The server mirror of the session-runner's drain: a parent turn that
        suspended on a ``SubtaskCompleted`` / ``SubtaskGroupCompleted`` wake
        is driven to its resumed terminal via the SHARED
        :func:`drive_pending_subtasks` state machine.

        Child inheritance (BYTE-EQUAL gate, mirroring
        ``CodeSessionRunner._build_child_engine``): every child Engine is built
        with delegation INHERITED — ``delegation_enabled=True`` + the **root
        parent's** ``spawnable`` set + the same depth-capped Budget — NOT
        sourced from the leaf child agent's own (possibly delegation-free)
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

    def _build_drain_host(self, parent_task: Any) -> DrainHost:
        """Build the :class:`DrainHost` for a parent's delegation tree.

        Extracted from :meth:`drive_pending_subtasks` so the background-subagent
        driver (docs/adr/background-subagent.md) builds the SAME host — same
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
        inherited_subtasks = self._spawnable_set(root_agent.capabilities.spawnable)
        # children share the root session's fs root — the
        # delegation tree runs in ONE workspace (the root parent's absolute path
        # binding), not each child's host default. ``None`` parent workspace ⇒
        # host default, byte-identical to the pre-decision single-workspace path.
        inherited_workspace = self._bound_workspace_for(parent_task)
        # children likewise run on the root session's bound
        # provider — the whole delegation tree shares ONE provider (the root
        # parent's binding), not each child's host default. ``None`` ⇒ host
        # default, byte-identical to the pre-I4 single-provider path.
        inherited_provider = self._bound_provider_for(parent_task)
        # the whole delegation tree also shares the root session's bound
        # MODEL: a child without its own declared default_model inherits the
        # root parent's ``ModelBound`` binding instead of silently dropping
        # to the host default. Gated to a binding that DIFFERS from the host
        # default — the driver binds every session at open, so a root on the
        # default model keeps children unbound, byte-identical to the
        # pre-inheritance path.
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
        # default, byte-identical to the pre-#4 path.
        inherited_permission = self._turn_permission_mode.get(
            str(parent_task.task_id)
        )
        # the parent task's enabled MCP alias list (NON-durable,
        # the driver stashed it for the spawning turn). A child inherits this
        # set ONLY when its own spec opens the ``mcp`` capability (per-spec
        # opt-in); a child without it gets ``()`` (no MCP tools). The opt-in
        # child connects its OWN independent server sessions (independent
        # recording, R-1 records its own specs — a resume reads them back, never
        # reconnects).
        # ``()`` parent aliases ⇒ no child ever gets MCP, byte-identical to
        # the pre-0042 path.
        inherited_mcp = getattr(self, "_turn_mcp_aliases", {}).get(
            str(parent_task.task_id), ()
        )
        # the whole delegation tree shares the root session's per-turn
        # reasoning-effort override — read from the parent's NON-durable carrier
        # (set by the driver for the spawning turn), same pattern as
        # permission_mode. Without it a child falls back to effort None, which
        # on the Responses provider used to also drop the reasoning-ciphertext
        # include and broke the child's prompt-cache prefix. ``None`` ⇒ host
        # default, byte-identical to the pre-inheritance path.
        inherited_effort = getattr(self, "_turn_effort", {}).get(
            str(parent_task.task_id)
        )

        def _child_mcp_aliases(child_agent: Any) -> tuple[str, ...]:
            # D8 gate: inherit the parent's enabled aliases only when the child
            # spec opts in. ``getattr`` default False keeps a spec without the
            # capability (or a non-AgentSpec like __workflow__) MCP-free.
            return (
                inherited_mcp
                if getattr(child_agent.capabilities, "mcp", False)
                else ()
            )

        def _build_subtask_engine(task_id: str) -> Engine:
            # a child recorded as __workflow__ is the orchestration
            # interpreter, not a roster agent — route it (with the task_id, so the
            # script can be read off its stream) BEFORE the registry lookup that
            # would raise UnknownAgentError for the reserved name.
            if agent_name_of(self.event_log, task_id) == WORKFLOW_AGENT_NAME:
                return self._build_orchestration_engine(
                    task_id, allowed_subtask_agents=inherited_subtasks
                )
            # The child's own agent (its tools / system prompt / read-only
            # allowlist) — but delegation is INHERITED from the root, not read
            # from this leaf agent's identity (gate #2). No policy_wrapper:
            # children are one-shot, never multi-turn wrapped, exactly as
            # ``CodeSessionRunner._build_child_engine``. ``ask_user_question``
            # is OFF for children (depth>0), mirroring the resolve_engine mask.
            # the child runs on its agent's declared
            # default model when one exists, else the root session's inherited
            # bound model, else the host default (each non-default choice is
            # recorded as the child's opening ModelBound by the drain, so a
            # cold resume rebuilds the same binding). CodingAgent carries no
            # ``default_model`` attribute → getattr None; an unbound /
            # default-bound root leaves ``inherited_model`` None → host model,
            # byte-identical to the pre-inheritance behaviour.
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
                permission_mode=inherited_permission,
                # per-spec opt-in MCP inheritance. The opt-in child
                # connects its own server sessions; ``task_id`` so a connect
                # skip records ``McpServerSkipped`` on the CHILD's stream.
                mcp_aliases=_child_mcp_aliases(child_agent),
                effort=inherited_effort,
                task_id=task_id,
            )

        def _child_model_binding(task_id: str) -> Optional[tuple[str, str]]:
            # __workflow__ has no roster spec / declared model → no binding
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

        # Pre-loop activation of a child's instructions + environment content
        # channels — the same parity ``InteractionDriver.seed_start`` gives a
        # top-level session. Snapshots come from the host's
        # ``session_content_snapshots`` over the INHERITED workspace (the whole
        # delegation tree runs in one fs root, so a child sees the root session's
        # workspace block), the same source ``_build_engine`` feeds the child's
        # composer — so the recorded fingerprint matches the bytes the child
        # renders. ``getattr``-guarded so a generic resolver without the SdkHost
        # seam (or a test-double host) leaves the callback ``None`` → no-op.
        _snapshots = getattr(self, "session_content_snapshots", None)

        def _record_child_session_content(
            child_id: str, child_task: Any, lease_id: str
        ) -> Any:
            environment_snapshot, instructions_snapshot = _snapshots(
                inherited_workspace
            )
            child_task = record_instructions(
                self.event_log, self.content_store, child_task,
                snapshot=instructions_snapshot, lease_id=lease_id,
            )
            child_task = record_environment(
                self.event_log, self.content_store, child_task,
                snapshot=environment_snapshot, lease_id=lease_id,
            )
            return child_task

        record_child: Optional[Callable[[str, Any, str], Any]] = (
            _record_child_session_content if callable(_snapshots) else None
        )

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
            record_session_content=record_child,
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
    ) -> Engine:
        """Hoisted per-agent Engine builder + cache.

        Issue 06: the cache key is
        ``(agent_name, model, ask_user_question_enabled, workspace, provider)``
        — the model is part of the binding (a per-turn switch resolves a
        distinct Engine), ``workspace`` is the per-session fs-root **absolute path**
        so two concurrent sessions on different directories never share an Engine
        (and their files never cross), and ``provider`` is the per-session
        provider name so two sessions on different providers never share an Engine.
        ``None`` workspace / provider ⇒ the host-fixed defaults, keeping the key
        byte-equal with the single-workspace/single-provider path.

        S1: ``todo_write`` / ``ask_user_question`` are AGENT identity, not host
        config (``plan_mode`` was removed). ``effective_ask`` is
        the (already depth-masked) value the caller passed; when unspecified it
        falls back to the agent's own capability.

        S3b: delegation is AGENT identity too, gated by the host kill-switch.
        The authorized sub-agent set comes from the agent's own
        ``capabilities.spawnable`` (filtered to known agents) — never a host
        input. When delegation is off (agent declares none, or the deployment
        disabled it) the set is empty so no spawn_subagent schema is exposed.
        """
        # Issue 06: the cache key is ``(agent_name, model)`` —
        # the model is now part of the binding, so a per-turn switch resolves
        # a distinct Engine rather than reusing the opening one.
        resolved_model = model if model else self.model
        # S1: todo_write / ask_user_question are AGENT identity, not host config
        # (plan_mode was removed). ``effective_ask`` is the
        # (already depth-masked) value the caller passed; when unspecified it
        # falls back to the agent's own capability.
        effective_ask = (
            agent.capabilities.ask_user_question
            if ask_user_question_enabled is None
            else ask_user_question_enabled
        )
        # S3b: delegation is AGENT identity too, gated by the host kill-switch.
        # The authorized sub-agent set comes from the agent's own
        # ``capabilities.spawnable`` (filtered to known agents) — never a host
        # input. When delegation is off (agent declares none, or the deployment
        # disabled it) the set is empty so no spawn_subagent schema is exposed.
        eff_delegation = agent.capabilities.delegation and self.delegation_allowed
        eff_subtask_agents = (
            self._spawnable_set(agent.capabilities.spawnable)
            if eff_delegation
            else frozenset()
        )
        # when the host enables workflow, run_workflow may spawn the
        # reserved __workflow__ orchestration child, so it must be in the
        # PermissionGuard allow-list. It is NEVER a roster agent, so it is filtered
        # out of the model-facing spawn_subagent directory by ``_build_engine``
        # (registry.resolve raises → skipped).
        if self.workflow_allowed:
            eff_subtask_agents = eff_subtask_agents | {WORKFLOW_AGENT_NAME}
        # Delegation is a pure function of (agent, delegation_allowed) and the
        # kill-switch is resolver-fixed, so ``agent.name`` already keys it
        # uniquely — no need to widen the cache key with it. ``workspace`` (D2)
        # and ``provider`` (D3) ARE part of the key: a different session fs-root
        # / provider must resolve a distinct Engine so concurrent sessions never
        # share fs tools or LLM adapter.
        # ``permission_mode`` is the 6th dimension — a
        # per-turn, NON-durable knob that drives ``require_approval_tools``, so two
        # turns on different permission modes must NOT share a cached Engine.
        # ``None`` (no per-turn selection) keeps the key byte-equal with the pre-#4
        # 5-tuple semantics (the host-fixed default gating).
        # ``mcp_aliases`` is the 7th dimension — a per-turn,
        # NON-durable enabled-server-alias tuple. Two turns enabling different MCP
        # servers must NOT share a cached Engine (their live tool sets differ), so
        # the alias tuple keys the build. ``()`` (no enabled servers) keeps the key
        # byte-equal with the pre-0042 6-tuple semantics.
        key = (
            agent.name, resolved_model, effective_ask, workspace, provider,
            permission_mode, mcp_aliases, effort,
        )
        # #13 / item 3: the global lock guards only the cache map. The build
        # itself runs OUTSIDE it, guarded by a PER-KEY build lock — one build
        # per key (so the live MCP connect + its McpServerSkipped/observer
        # events still fire exactly once), while builds for DIFFERENT keys run
        # concurrently. Holding the global lock across ``_build_engine`` used
        # to serialise every session behind one slow/hanging MCP connector —
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
                    policy_wrapper=self.policy_wrapper,
                    workspace=workspace,
                    provider=provider,
                    permission_mode=permission_mode,
                    mcp_aliases=mcp_aliases,
                    effort=effort,
                    task_id=task_id,
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
