"""Per-task agent→Engine resolver skeleton.

Three domain seams (agent lookup, spawnable-set parsing, engine build) are left
as abstract hooks; a concrete subclass fills them in while the skeleton owns the
shared resolution logic — the binding dimensions a build receives, the
ask_user_question masks, and the delegation/spawnable inheritance rule.

**The Engine is a per-turn value, not a cached resource.** A turn opens
with a user goal and runs — through every approval, answer and sub-agent
return that resumes it — until the task parks on the next-goal handle or
ends. :meth:`GenericEngineResolver.resolve_engine` builds the turn's Engine
once, from the task's folded bindings (model, workspace, provider, sandbox
container, permission mode, MCP aliases, effort) and whatever the host reads
at build time — skill tiers, the project shell allowlist, the workspace
trust decision, an MCP server's tool list — keeps it in the host's
:class:`~noeta.runtime.task_local.TaskLocalRegistry` for the turn's resumes,
and lets it go when the turn settles. Reading those inputs per turn is what
makes a change to any of them visible on the next turn of every task, with
no invalidation machinery; reusing the build within the turn is what keeps
the turn's tool set fixed. A build is deterministic in its inputs, so the
composed stable prefix moves only when an input actually changed. The one
expensive external resource, the live MCP connection, is pooled by the host
across builds (see ``noeta.builtins.mcp.impl.pool``), not by this resolver.

Declared as a plain class rather than a ``@dataclass`` so a dataclass subclass
supplies the real field storage and ``__init__`` while keeping its field table
byte-identical.
"""

from __future__ import annotations

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
    seed_child_task,
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


def _recorded_mcp_aliases(task: Any) -> tuple[str, ...]:
    """The enabled MCP aliases a Task's folded provenance carries.

    ``GovernanceState.mcp_provenance`` (folded from ``McpProvenanceRecorded``
    — one event per Task and one more each time the enabled set changes) is
    the durable, credential-free record of which connectors the Task was
    given. The per-turn alias carrier is process-local, so a Task resolved in
    another process — a restart, a daemon worker, another machine — has none
    and reads this instead. ``()`` for a Task that never had MCP, which keeps
    that build byte-identical to the carrier-less path.
    """
    rows = getattr(getattr(task, "governance", None), "mcp_provenance", None)
    if not isinstance(rows, list):
        return ()
    return tuple(
        str(row["alias"])
        for row in rows
        if isinstance(row, dict) and row.get("alias")
    )


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
    # per-turn, NON-durable permission_mode carrier
    # keyed by task_id (storage supplied by the @dataclass subclass — see
    # ``SdkHost._turn_permission_mode``). Set via :meth:`note_turn_permission`
    # before resolution, read in :meth:`resolve_engine` to thread the mode into
    # the build.
    _turn_permission_mode: dict[str, Optional[str]]
    #: per-turn, NON-durable enabled-MCP-alias carrier keyed by
    #: task_id (storage supplied by the @dataclass subclass — see
    #: ``SdkHost._turn_mcp_aliases``). The frontend sends the alias clean list
    #: each turn (NO url / token — those live host-side); the driver records
    #: it here via :meth:`note_turn_mcp` before resolution, read in
    #: :meth:`resolve_engine` to thread the aliases into the build.
    #: An entry — including an explicit ``()`` (every server off this turn) —
    #: is this turn's choice; NO entry (a resume in another process) falls
    #: back to the Task's durable MCP provenance.
    _turn_mcp_aliases: dict[str, tuple[str, ...]]
    #: Per-turn, NON-durable reasoning-effort carrier keyed by task_id. Mirrors
    #: permission/MCP: set before Engine resolution, read into the build
    #: inputs. ``None`` ⇒ host/provider default.
    _turn_effort: dict[str, Optional[str]]
    #: The task-local registry (``noeta.runtime.task_local``): the turn's
    #: Engine, the read-first record, and the slots the built-ins keep per
    #: task. Storage supplied by the @dataclass subclass (see
    #: ``SdkHost._task_locals``); read through ``getattr`` so an older test
    #: double without it builds per resolve and keeps nothing.
    _task_locals: Any
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

        Called once per turn: the Engine is a per-turn value (module
        docstring), so everything an implementation reads here — skill tiers,
        the shell allowlist, an MCP server's tool list — is read fresh for
        the turn, and a resume within the turn never reaches here. ``task_id`` is the task whose stream MCP provenance / skip
        observer events are recorded on (``None`` for the seed/by-name path
        where no task exists yet — that path is built without live MCP).

        ``GenericEngineResolver`` itself never inspects the product-specific
        knobs (write modes, shell modes, workspace dir, provider, hooks,
        budget, MCP specs, skill settings, …). The hook receives the four
        cross-product arguments it computed; an implementation is responsible
        for reading the remaining fields off ``self`` and/or the agent's
        activation tuple (e.g. ``agent_activates(agent, "todo_write")``) and
        forwarding them to its engine factory.

        ``workspace`` is the per-session workspace **absolute path**
        (``None`` ⇒ the host-fixed default dir). An implementation uses it
        directly as the Engine's fs/skill tools root.

        ``provider`` is the per-session provider **name**
        (``None`` ⇒ the host default provider). An implementation resolves it
        to a configured LLM adapter instance for this Engine's round-trips.

        ``exec_env_ref`` is the per-session sandbox container ``base_url``
        (``None`` ⇒ the local host / the host-default sandbox config). An
        implementation resolves it to a live sandbox backend the Engine's fs /
        shell tools run their IO against — a resumed / reclaimed session
        reconnects to THIS container by its address.

        ``structured_output_schema`` is a workflow helper's per-helper JSON
        Schema (read off its durable ``TaskCreated.inputs.output_schema`` by
        :meth:`drive_pending_subtasks`' child-engine builder and by
        :meth:`resolve_engine` for a helper a resident worker claimed). An
        implementation mounts the ``structured_output`` control schema (its
        ``parameters`` = this schema) AND wraps the built policy in
        ``StructuredOutputPolicy`` so the helper's call becomes its final
        answer. ``None`` (every other build) leaves the build unchanged.
        """
        raise NotImplementedError

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
        Agent's Engine, built afresh on every read like any other. A resident
        host normally drives via :meth:`resolve_engine`; this is the
        degenerate single-Agent view.
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
        Never written to the event log (the alias list is only the runtime
        selector that decides which servers to connect this turn; the durable
        record is the ``McpProvenanceRecorded`` the connecting build writes).
        Overwritten each turn; a turn that suspends on approval resolves the
        same set on resume — and a resume in ANOTHER process, which has no
        entry here, falls back to that durable record rather than to no MCP at
        all."""
        carrier = getattr(self, "_turn_mcp_aliases", None)
        if carrier is not None:
            carrier[str(task_id)] = tuple(aliases)

    def forget_turn_engine(self, task_id: str) -> None:
        """Let go of the Engine kept for ``task_id``'s turn, so the next
        :meth:`resolve_engine` builds afresh. The driver calls it when a new
        user goal opens a turn (a stale Engine from an earlier turn must not
        serve it), the worker when a turn settles — terminal, or parked on
        the next-goal handle — so a parked conversation holds no tool set and
        no MCP lease. Idempotent; a no-op without the registry."""
        locals_ = getattr(self, "_task_locals", None)
        if locals_ is not None:
            locals_.drop_engine(str(task_id))

    def forget_turn_carriers(self, task_id: str) -> None:
        """Drop a task's per-turn carrier entries (permission_mode / effort /
        mcp aliases) and its task-local state (the turn Engine, the read-first
        record, the built-ins' slots). Called from the conversation-end
        control verbs (``cancel`` / ``close``) — mirrors
        :meth:`forget_background_subagents`.

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
        locals_ = getattr(self, "_task_locals", None)
        if locals_ is not None:
            locals_.forget(key)

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

        The turn's Engine, built once: a resume within the turn (an approval,
        an answer, a sub-agent return, the driver reading the ask codec off
        it) gets the Engine the turn opened with, from the task-local
        registry; the first resolve of a turn folds the Task's
        ``TaskCreated.agent_name`` → :meth:`_lookup_agent` →
        :meth:`_build_engine` and keeps the result until
        :meth:`forget_turn_engine`. An unknown ``agent_name`` is a hard
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

        A **subtask** (a task with a parent) that carries no binding of its
        own inherits its delegation tree's ROOT session instead of the host
        defaults — bound model, workspace, provider, sandbox container,
        per-turn effort / permission mode / MCP selection, and the root's
        spawn set — the same choices ``_build_drain_host``'s child-engine
        builder makes, so a child claimed by a resident worker ahead of the
        drain (``subtask_drain._ChildNotReady``) resolves the same Engine.
        """
        task_id = str(getattr(task, "task_id", ""))
        locals_ = getattr(self, "_task_locals", None)
        if locals_ is not None:
            held: Optional[Engine] = locals_.held_engine(task_id)
            if held is not None:
                return held
        engine = self._build_turn_engine(task, task_id)
        if locals_ is not None:
            locals_.hold_engine(task_id, engine)
        return engine

    def _build_turn_engine(self, task: Any, task_id: str) -> Engine:
        """The build half of :meth:`resolve_engine`: fold the bindings, pick
        the agent, build. Never consults the task-local registry."""
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
        # same tree layer. Built the way the drain path builds it.
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
        # stashed for this task. An entry — including an explicit ``()`` (the
        # frontend turned every server off for this turn) — WINS: a new human
        # turn is what changes the enabled set. NO entry means this process
        # never saw the turn open (resume after a restart, a daemon worker,
        # another machine), so the durable provenance is read back and its
        # servers are reconnected through the host's resolver — without it the
        # rebuilt Engine would silently drop every ``mcp__`` tool the Task was
        # given (a host with no resolver still builds none).
        carrier = getattr(self, "_turn_mcp_aliases", {})
        mcp_aliases = (
            carrier[task_id]
            if task_id in carrier
            else _recorded_mcp_aliases(task)
        )
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
        # ``"unnamed"`` resolves to the supplied fallback; without one the
        # registry lookup hard-errors on it, per ``_lookup_agent``'s contract.
        is_unnamed = name == "unnamed" and self.unnamed_fallback is not None
        agent = (
            self.unnamed_fallback
            if is_unnamed
            else self._lookup_agent(name, task_id=task_id)
        )
        # Subtasks carry no TaskHostBound / opening ModelBound of their own
        # until a driver opens them, and their per-turn carriers are never
        # noted — the fold leaves governance.exec_env_ref / workspace /
        # provider / model_binding None and the carriers empty. A delegation
        # tree runs in ONE container / fs root / provider, on the ROOT
        # session's bound model and per-turn effort / permission mode / MCP
        # selection; a child whose own spec activates ``delegation`` spawns
        # from the root's spawnable set, any other child gets no ``Task``
        # tool (:meth:`_child_may_delegate`) — exactly the choices
        # ``_build_drain_host``'s child-engine builder makes for the foreground
        # drain path. This branch makes the resident-worker path (an idle
        # worker's untargeted ``tick()`` claiming a child ahead of the drain's
        # targeted descent — ``subtask_drain._ChildNotReady``) resolve the SAME
        # Engine, so a child is driven identically whichever driver won its
        # lease. Read off the tree's ROOT (walked up from the child's
        # ``TaskCreated.parent_task_id``), not the direct parent: a depth ≥ 2
        # child's parent is itself a subtask carrying none of these.
        delegation_enabled: Optional[bool] = None
        allowed_subtask_agents: Optional[frozenset[str]] = None
        if is_subtask:
            root_id = self.root_task_id_of(task_id)
            root = fold(self.event_log, self.content_store, root_id)
            if exec_env_ref is None:
                exec_env_ref = self._bound_exec_env_ref_for(root)
            if workspace is None:
                workspace = self._bound_workspace_for(root)
            if provider is None:
                provider = self._bound_provider_for(root)
            # An unbound child runs on its agent's declared default model, else
            # the root's non-default binding, else the host default — the same
            # binding :meth:`seed_claimed_subtask` records when the worker
            # opens it, so Engine and recording agree.
            if self._own_model_binding(task) is None:
                binding = self._child_binding_for(
                    task_id, self._inherited_model_of(root)
                )
                model = binding[0] if binding else self._canonical_model(self.model)
            if effort is None:
                effort = getattr(self, "_turn_effort", {}).get(root_id)
            if permission_mode is None:
                permission_mode = self._turn_permission_mode.get(root_id)
            if not mcp_aliases:
                mcp_aliases = self._child_mcp_aliases(agent, root_id, root=root)
            # The root's spawn set needs the root's recorded agent; a root
            # stream with no genesis (hand-emitted child, purged parent) has
            # none to inherit, so the child keeps its own delegation identity.
            if self._task_created_of(root_id) is not None:
                delegation_enabled = self._child_may_delegate(agent)
                allowed_subtask_agents = (
                    self._inherited_spawnable_of(root_id)
                    if delegation_enabled
                    else frozenset()
                )
        # A workflow helper spawned via ``agent(goal, schema=...)`` carries its
        # per-helper JSON Schema in the durable ``TaskCreated.inputs`` — thread
        # it so a child claimed HERE (a resident worker's untargeted ``tick()``,
        # ahead of the drain's targeted descent) still mounts the
        # ``structured_output`` control schema + receipt wrapper the drain path
        # builds. Without this the claimed helper silently loses its schema
        # contract. ``None`` for every root / plain child keeps the build
        # byte-identical.
        subtask_schema = (
            _subtask_output_schema(self.event_log, task_id)
            if is_subtask
            else None
        )
        return self._engine_for_agent(
            agent,
            model=model,
            # ask_user_question comes from agent identity, masked to depth-0
            # root tasks (a delegated child never inherits it); the unnamed
            # fallback never gets it.
            ask_user_question_enabled=(
                not is_unnamed
                and agent_activates(agent, "ask_user_question")
                and not is_subtask
            ),
            workspace=workspace,
            provider=provider,
            permission_mode=permission_mode,
            mcp_aliases=mcp_aliases,
            effort=effort,
            task_id=task_id,
            exec_env_ref=exec_env_ref,
            policy_wrapper=subtask_wrapper,
            structured_output_schema=subtask_schema,
            delegation_enabled=delegation_enabled,
            allowed_subtask_agents=allowed_subtask_agents,
        )

    def _child_may_delegate(self, child_agent: Any) -> bool:
        """Whether a sub-agent gets the ``Task`` tool: only when its own spec
        activates ``delegation`` (and the host allows delegation at all). A
        child that may spawn spawns from the ROOT's roster; a leaf child —
        every official subagent, every ``AgentDefinition`` without the
        activation — gets no spawn tool rather than one it must not use."""
        return bool(
            self.delegation_allowed and agent_activates(child_agent, "delegation")
        )

    def _canonical_model(self, model: str) -> str:
        """``model`` as the real id the provider is sent.

        Identity here — the kernel owns no alias table. A host that has one
        (the SDK's model catalog) overrides this, so a friendly alias from any
        source — an agent's declared ``default_model``, a ``ModelBound`` an
        older release recorded unresolved — reaches the build, the child
        binding and the inheritance comparison as the same id.
        """
        return model

    def _bound_model_for(self, task: Any) -> str:
        """The model binding the Task resolves on.

        The latest ``ModelBound`` the Engine folded into
        ``GovernanceState.model_binding``; ``None`` (a recording that never
        switched) falls back to the host-fixed default :attr:`model` so the
        recorded ``LLMRequestStartedPayload.model`` is unchanged. Resolved
        through :meth:`_canonical_model`, so a legacy alias binding replays
        on the real id.
        """
        return self._canonical_model(self._own_model_binding(task) or self.model)

    @staticmethod
    def _own_model_binding(task: Any) -> Optional[str]:
        """The model the Task's OWN latest ``ModelBound`` folded to
        (``governance.model_binding``); ``None`` when it never bound one."""
        bound = getattr(getattr(task, "governance", None), "model_binding", None)
        return bound if isinstance(bound, str) and bound else None

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

    # -- delegation-tree inheritance (shared by the drain and the worker path) --

    def _task_created_of(self, task_id: str) -> Optional[Any]:
        """The ``TaskCreated`` payload recorded on ``task_id``'s stream — its
        genesis (agent, parent, goal) — or ``None`` when the stream records
        none (never opened, or purged)."""
        for env in self.event_log.read(task_id):
            if env.type == "TaskCreated":
                return env.payload
        return None

    def root_task_id_of(self, task_id: str) -> str:
        """The root of ``task_id``'s delegation tree, walked up through each
        stream's ``TaskCreated.parent_task_id`` (the same walk
        :meth:`_resume_woken_ancestors` makes); a root returns itself. The
        walk stops at a parent whose stream records no genesis (a hand-emitted
        child, a purged parent) and returns THAT id — its fold is empty, so
        the child inherits nothing from it rather than failing to resolve.

        Public because the tree's root is also what the cancel registry is
        marked with: the worker reaches this as a duck-typed seam (L2 cannot
        import ``noeta.execution``) to bind a claimed child's cooperative-cancel
        poll to its root, the way the in-request drain already does."""
        current = str(task_id)
        while True:
            created = self._task_created_of(current)
            parent_id = getattr(created, "parent_task_id", None) if created else None
            if not parent_id:
                return current
            current = str(parent_id)

    def _agent_of(self, task_id: str) -> Any:
        """The agent object a recorded task resolves to: its
        ``TaskCreated.agent_name`` through :meth:`_lookup_agent`, with
        ``"unnamed"`` routed to ``unnamed_fallback`` when one is supplied (the
        lookup hook's contract leaves that case to callers)."""
        name = agent_name_of(self.event_log, task_id)
        if name == "unnamed" and self.unnamed_fallback is not None:
            return self.unnamed_fallback
        return self._lookup_agent(name, task_id=task_id)

    def _inherited_model_of(self, root_task: Any) -> Optional[str]:
        """The model a delegation tree inherits from its root session: the
        root's bound model when it DIFFERS from the host default, else
        ``None`` — the driver binds every session at open, so a root on the
        default model keeps its children unbound."""
        bound = self._own_model_binding(root_task)
        if not bound:
            return None
        bound = self._canonical_model(bound)
        return bound if bound != self._canonical_model(self.model) else None

    def _child_binding_for(
        self, task_id: str, inherited_model: Optional[str]
    ) -> Optional[tuple[str, str]]:
        """The opening ``ModelBound`` a sub-agent child runs on, as
        ``(model, principal_identity)``: the child agent's declared default
        model (``"agent-default"``) wins, else the root session's inherited
        non-default binding (``"inherited"``), else ``None`` — the
        host-default model, which writes no event. ``__workflow__`` has no
        agent spec / declared model → no binding (the orchestration
        interpreter makes no LLM calls of its own; the workers it spawns
        inherit through this same rule). The ONE rule behind both the drain's
        ``child_model_binding`` callback and the resident worker's
        :meth:`seed_claimed_subtask`."""
        if agent_name_of(self.event_log, task_id) == WORKFLOW_AGENT_NAME:
            return None
        declared = getattr(self._agent_of(task_id), "default_model", None)
        if declared:
            return (self._canonical_model(str(declared)), "agent-default")
        if inherited_model:
            return (inherited_model, "inherited")
        return None

    def _child_mcp_aliases(
        self, child_agent: Any, root_id: str, *, root: Any = None
    ) -> tuple[str, ...]:
        """The MCP aliases a child inherits: the root session's per-turn
        enabled set — or, when this process never saw that turn open (a resume
        after a restart, a daemon worker claiming the child), the root's
        durable provenance — ONLY when the child's own spec opens the ``mcp``
        capability (per-spec opt-in); ``()`` otherwise. ``agent_activates``
        tolerates a spec without the activation (or a non-AgentSpec like
        ``__workflow__`` carrying no ``plugins``) — both stay MCP-free.
        ``root`` is the caller's already-folded root, so the fallback costs no
        second fold on the paths that have one."""
        if not agent_activates(child_agent, "mcp"):
            return ()
        inherited = getattr(self, "_turn_mcp_aliases", {}).get(str(root_id), ())
        if inherited:
            return tuple(inherited)
        if root is None:
            root = fold(self.event_log, self.content_store, str(root_id))
        return _recorded_mcp_aliases(root)

    def _inherited_spawnable_of(self, root_id: str) -> frozenset[str]:
        """The spawn set every child of a delegation tree runs with: the ROOT
        agent's ``spawnable`` (filtered to known agents) — delegation is
        inherited from the root, never read from a leaf child's own (possibly
        delegation-free) identity."""
        return self._spawnable_set(self._agent_of(str(root_id)).spawnable)

    def seed_claimed_subtask(
        self, task: Any, *, engine: Any, lease_id: str
    ) -> Any:
        """Open a sub-agent child a resident worker claimed ahead of the drain.

        The worker's ``run_leased_task`` reaches this as a duck-typed seam
        (L2 cannot import ``noeta.execution``) when it leases a child that
        has a parent, a goal and no messages yet — the num_workers>=2 race
        ``subtask_drain._ChildNotReady`` documents. It runs the SAME
        :func:`seed_child_task` the drain's targeted descent runs, with the
        same binding choice (the child agent's declared default model, else
        the root session's non-default bound model, on the root's bound
        provider), so the child's recording is identical whichever driver won
        the lease. ``engine`` is the child's already-resolved Engine —
        :meth:`resolve_engine` inherits the root's bindings by the same rule,
        so the Engine and the binding it records agree.
        """
        root = fold(
            self.event_log,
            self.content_store,
            self.root_task_id_of(str(task.task_id)),
        )
        return seed_child_task(
            self.event_log,
            self.content_store,
            task,
            engine,
            lease_id=lease_id,
            model_binding=self._child_binding_for(
                str(task.task_id), self._inherited_model_of(root)
            ),
            provider=self._bound_provider_for(root),
        )

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
        """Build an Engine **by agent name** — for Task creation.

        The :class:`InteractionDriver` (or equivalent task-creating surface)
        needs the seed Engine that writes ``TaskCreated`` *before* a Task
        (and therefore its recorded ``agent_name``) exists, so it cannot go
        through the Task-keyed :meth:`resolve_engine`. This shares the same
        registry lookup and build: an unknown
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

        Child inheritance (mirroring the child-engine build): a child whose own
        spec activates ``delegation`` is built with the **root parent's**
        ``spawnable`` set + the same depth-capped Budget; any other child gets
        no spawn tool (:meth:`_child_may_delegate`). Recursion is bounded by the
        depth-capped Budget (``BudgetGuard.max_subtask_depth``); the rule reads
        only durable identity, so resume rebuilds the same tool set.
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
        inherited_subtasks = self._inherited_spawnable_of(root_id)
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
        inherited_model = self._inherited_model_of(parent_task)
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
        # opt-in — ``_child_mcp_aliases``); a child without it gets ``()`` (no
        # MCP tools). The opt-in child connects its OWN independent server
        # sessions (independent recording — a resume reads its own recorded
        # specs back, never reconnects). ``()`` parent aliases ⇒ no child ever
        # gets MCP.
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
            # allowlist); delegation only when its own spec activates it, and
            # then from the ROOT's spawnable set. No policy_wrapper:
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
            declared_model = getattr(child_agent, "default_model", None)
            child_model = self._canonical_model(
                declared_model or inherited_model or self.model
            )
            may_delegate = self._child_may_delegate(child_agent)
            return self._build_engine(
                child_agent,
                child_model,
                delegation_enabled=may_delegate,
                allowed_subtask_agents=(
                    inherited_subtasks if may_delegate else frozenset()
                ),
                ask_user_question_enabled=False,
                policy_wrapper=None,
                workspace=inherited_workspace,
                provider=inherited_provider,
                exec_env_ref=inherited_exec_env_ref,
                permission_mode=inherited_permission,
                # per-spec opt-in MCP inheritance. The opt-in child
                # connects its own server sessions; ``task_id`` so a connect
                # skip records ``McpServerSkipped`` on the CHILD's stream.
                mcp_aliases=self._child_mcp_aliases(
                    child_agent, root_id, root=parent_task
                ),
                effort=inherited_effort,
                task_id=task_id,
                # Per-helper structured output: a workflow helper spawned via
                # ``agent(goal, schema=...)`` carries the declared JSON Schema
                # in its durable ``TaskCreated.inputs.output_schema`` — thread
                # it so the child mounts the ``structured_output`` control
                # schema + the ``StructuredOutputPolicy`` receipt wrapper.
                # ``None`` (every plain child) leaves the build unchanged.
                structured_output_schema=_subtask_output_schema(
                    self.event_log, task_id
                ),
            )

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
            # the child agent's declared default model, else the root's
            # inherited non-default binding — the one rule the worker-claimed
            # path (``seed_claimed_subtask``) applies too.
            child_model_binding=lambda child_id: self._child_binding_for(
                child_id, inherited_model
            ),
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
        structured_output_schema: Optional[dict[str, Any]] = None,
        delegation_enabled: Optional[bool] = None,
        allowed_subtask_agents: Optional[frozenset[str]] = None,
    ) -> Engine:
        """Build ``agent``'s Engine for one turn.

        A per-turn value, never cached (module docstring): every call builds
        afresh, so two tasks with equal bindings hold distinct Engines that
        compose byte-identical schemas, and nothing in an Engine outlives its
        turn except what the host pools deliberately (live MCP connections).

        ``todo_write`` / ``ask_user_question`` are AGENT identity, not host
        config. ``effective_ask`` is the (already depth-masked) value the caller
        passed; when unspecified it falls back to the agent's own capability.

        Delegation is AGENT identity too, gated by the host kill-switch.
        The authorized sub-agent set comes from the agent's own
        ``spawnable`` (filtered to known agents) — never a host
        input. When delegation is off (agent declares none, or the deployment
        disabled it) the set is empty so no spawn_subagent schema is exposed.
        ``delegation_enabled`` / ``allowed_subtask_agents`` override that
        identity read for a SUBTASK — :meth:`resolve_engine` passes the root's
        inherited spawn set, verbatim, the way the drain's child-engine builder
        does.

        ``policy_wrapper``: the multi-turn wrapper is a TOP-LEVEL-session
        concern (it turns a ``FinishDecision`` into a next-goal suspend for
        ``noeta code chat``). A delegated child is one-shot and must finish
        with a real ``TaskCompleted``, so the resident worker's per-task
        :meth:`resolve_engine` passes ``None`` for a subtask (mirroring the
        drain's ``_build_subtask_engine``) while the root keeps
        ``self.policy_wrapper``. The ``_POLICY_WRAPPER_UNSET`` sentinel
        distinguishes "caller did not pass it" (⇒ ``self.policy_wrapper``)
        from "caller passed ``None``" (⇒ build unwrapped); a plain ``None``
        default would conflate the two and re-wrap an explicit-unwrapped
        child.

        ``structured_output_schema`` (a workflow helper's per-helper JSON
        Schema) shapes this one build: the ``structured_output`` control mount
        plus the ``StructuredOutputPolicy`` receipt wrapper.
        """
        resolved_model = self._canonical_model(model if model else self.model)
        effective_ask = (
            agent_activates(agent, "ask_user_question")
            if ask_user_question_enabled is None
            else ask_user_question_enabled
        )
        if delegation_enabled is None:
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
            # PermissionGuard allow-list. It is NEVER a named agent, so it is
            # filtered out of the model-facing spawn_subagent directory by
            # ``_build_engine`` (registry.resolve raises → skipped).
            if self.workflow_allowed:
                eff_subtask_agents = eff_subtask_agents | {WORKFLOW_AGENT_NAME}
        else:
            # a subtask's inherited delegation (the root's spawn set), taken
            # verbatim — byte-equal to the drain's ``_build_subtask_engine``.
            eff_delegation = delegation_enabled
            eff_subtask_agents = (
                allowed_subtask_agents
                if allowed_subtask_agents is not None
                else frozenset()
            )
        effective_wrapper = (
            self.policy_wrapper
            if policy_wrapper is _POLICY_WRAPPER_UNSET
            else policy_wrapper
        )
        return self._build_engine(
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
            structured_output_schema=structured_output_schema,
        )
