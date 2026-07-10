"""High-level ``Client`` + one-shot ``query`` (slice 4b).

``Client`` wires an :class:`~noeta.client.options.Options` recipe into a
live :class:`SdkHost` + :class:`~noeta.execution.driver.InteractionDriver`
pair and exposes the full conversation command surface
(``start`` / ``send_goal`` / ``approve`` / ``deny`` / ``answer`` /
``deliver_event`` / ``cancel`` / ``close`` / ``reopen``) as 1:1
pass-throughs.

``query`` is the sugar surface for library users who just want a single
goal driven to its terminal: it creates a temporary ``Client`` with
``multi_turn=False``, drives a single turn to the terminal TaskCompleted,
returns a :class:`QueryResult` (the envelope list + the message view and
terminal answer, folded against the live ContentStore *before* teardown),
and tears everything down.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Optional

from noeta.agent.registry import AgentRegistry
from noeta.core.wiring import wire_default_observers
from noeta.observers.otlp import make_otlp_trace_observer
from noeta.observers.trace_export import TraceExportObserver
from noeta.execution import (
    InteractionDriver,
    multi_turn_policy_wrapper,
)
from noeta.client.messages import ViewItem, as_messages
from noeta.execution.driver import DriveOutcome, STUB_MODEL_ALLOWLIST
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.errors import CodedError
from noeta.protocols.event_log import EventEnvelope, EventLogFull
from noeta.protocols.events import (
    TaskCompletedPayload,
    TaskFailedPayload,
    ToolCallApprovalRequestedPayload,
    ToolCallApprovalResolvedPayload,
    answer_from_payload,
)
from noeta.protocols.messages import ImageBlock, LLMProvider
from noeta.protocols.tool import Tool
from noeta.protocols.tool_args import resolve_tool_call_arguments
from noeta.protocols.values import ContentRef
from noeta.runtime.worker import WorkerLoop
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.tools.decorator import DecoratedTool
from noeta.tools.fs.edit import FsWriteMode

from noeta.client.host import SdkHost
from noeta.client.host_config import HostConfig
from noeta.client.options import (
    AgentDefinition,
    Options,
    compile_options,
)


__all__ = ["Client", "QueryFailedError", "QueryResult", "query"]


# ---------------------------------------------------------------------------
# Custom-tool gatherer
# ---------------------------------------------------------------------------


def _scan_entries(entries: tuple[Any, ...], gathered: dict[str, Tool]) -> None:
    """Append every :class:`DecoratedTool` in ``entries`` to ``gathered``.

    Shared helper for ``_collect_custom_tools`` — entries come from
    ``allowed_tools`` or an ``AgentDefinition.tools`` tuple.
    Raises ``ValueError`` on distinct-closure name collision.
    """
    for entry in entries:
        if isinstance(entry, DecoratedTool):
            existing = gathered.get(entry.name)
            if existing is not None and existing is not entry:
                raise ValueError(
                    f"custom tool name collision: {entry.name!r} is "
                    "registered twice with distinct closures"
                )
            gathered[entry.name] = entry


def _collect_custom_tools(root: Options) -> dict[str, Tool]:
    """Gather every ``DecoratedTool`` closure referenced from ``root``.

    Scans (in order):

    * Every ``root.allowed_tools`` entry (when not ``None``).
    * Every ``AgentDefinition.tools`` entry in ``root.agents`` (when not
      ``None``).

    The agents tree is flat — there is no recursive nesting, so no tree
    walk is needed.
    """
    gathered: dict[str, Tool] = {}
    if root.allowed_tools is not None:
        _scan_entries(root.allowed_tools, gathered)
    for defn in root.agents.values():
        if isinstance(defn, AgentDefinition) and defn.tools is not None:
            _scan_entries(defn.tools, gathered)
    # In-process MCP servers (Options.mcp_servers): their bundled @tool
    # closures are custom tools too. Duck-typed by ``.tools`` (the SdkMcpServer
    # value object) so noeta.client takes no upward import on noeta.sdk.
    for server in root.mcp_servers:
        _scan_entries(tuple(getattr(server, "tools", ())), gathered)
    return gathered


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Client:
    """High-level conversation driver over an ``Options`` recipe.

    Typical use::

        client = Client(my_options, provider=my_provider,
                        workspace_dir=Path("."))
        try:
            outcome = client.start(goal="fix my tests", agent="main")
            # read events, or follow up with send_goal / approve / deny / answer / …
        finally:
            client.shutdown()

    Or the one-shot sugar::

        envelopes = query(my_options, goal="fix my tests",
                          provider=my_provider, workspace_dir=Path("."))

    Storage defaults to in-memory, but a :class:`HostConfig` (the D3 host-level
    wiring surface) can inject an external durable triple plus the host runtime
    injections (HTML-app preview gateway, live-MCP alias resolver) without
    touching the agent identity. ``shutdown`` is idempotent: it unsubscribes the
    default observers wired at construction.
    """

    def __init__(
        self,
        options: Options,
        *,
        provider: Optional[LLMProvider] = None,
        workspace_dir: Optional[Path] = None,
        model: Optional[str] = None,
        multi_turn: bool = True,
        host_config: Optional[HostConfig] = None,
        allowed_models: Optional[Sequence[str]] = None,
    ) -> None:
        # 0. Resolve provider: explicit kwarg first, then Options.provider
        #    (D5: wiring is NOT identity — the AgentSpec identity never sees it).
        effective_provider: LLMProvider
        if provider is not None:
            effective_provider = provider
        elif options.provider is not None:
            effective_provider = options.provider
        else:
            raise ValueError(
                "a provider is required — pass one via the Client(provider=...)"
                " kwarg or set Options.provider"
            )

        # 0b. Resolve workspace_dir: explicit kwarg > Options.cwd
        #     (both treated as wiring-only, never inspected by compile_options).
        effective_workspace_dir: Path
        if workspace_dir is not None:
            effective_workspace_dir = Path(workspace_dir)
        elif options.cwd is not None:
            assert isinstance(options.cwd, (str, Path))
            effective_workspace_dir = Path(options.cwd)
        else:
            raise ValueError(
                "a workspace directory is required — pass one via the "
                "Client(workspace_dir=...) kwarg or set Options.cwd"
            )

        # 1. Compile + register (including child agents)
        main_spec, descendant_specs = compile_options(options)
        registry: AgentRegistry = AgentRegistry()
        registry.add(main_spec)
        for d in descendant_specs:
            registry.add(d)

        # 2. Collect custom tools (all nodes, including descendants)
        custom_tools = _collect_custom_tools(options)

        # 3. Open stores (dispatcher first — the event log needs it as
        #    lease_validator). The durable-storage host config may inject an
        #    external triple (sqlite +
        #    multi-session); absent it, build the in-memory triple (the historical
        #    default, byte-identical for every existing caller).
        hc = host_config if host_config is not None else HostConfig()
        injected = hc.storage_triple()
        dispatcher: Dispatcher
        event_log: EventLogFull
        content_store: ContentStore
        if injected is not None:
            event_log, content_store, dispatcher = injected
        else:
            dispatcher = InMemoryDispatcher()
            event_log = InMemoryEventLog(lease_validator=dispatcher)
            content_store = InMemoryContentStore()
        self._unsubscribe_default: Callable[[], None] = wire_default_observers(
            event_log, dispatcher
        )
        # (T3) — custom Observer
        # extension point: subscribe each user-supplied post-commit callback
        # alongside the defaults and collect their unsubscribes for shutdown.
        self._unsubscribe_observers: list[Callable[[], None]] = [
            event_log.subscribe(obs) for obs in options.observers
        ]
        self._shutdown = False
        # Resident worker pool (lazily started by start_workers). Each loop
        # runs on its own daemon thread; ``_worker_loops`` holds the loop
        # objects so stop_workers / shutdown can signal and join them.
        self._worker_loops: list[WorkerLoop] = []
        self._worker_threads: list[threading.Thread] = []
        self._workers_started = False

        # 4. Assemble host
        host_model = (
            model
            if model is not None
            else main_spec.default_model
            if main_spec.default_model is not None
            else options.model
            if options.model is not None
            else "sonnet"
        )
        self._host = SdkHost(
            event_log=event_log,
            content_store=content_store,
            dispatcher=dispatcher,
            provider=effective_provider,
            model=host_model,
            workspace_dir=effective_workspace_dir,
            registry=registry,
            custom_tools=custom_tools,
            policy_wrapper=(multi_turn_policy_wrapper if multi_turn else None),
            permission_mode=options.permission_mode,
            # Wiring-only LLM controls: live in Options, excluded from
            # the AgentSpec identity (compile_options never reads them), forwarded
            # through the host to ReActPolicy so every in-session
            # LLMRequest inherits the same override.
            output_schema=(
                dict(options.output_schema)
                if options.output_schema is not None
                else None
            ),
            thinking=options.thinking,
            effort=options.effort,
            # (T3) — extension points.
            # policy: the custom Options.policy IS the ``(llm) -> Policy``
            # factory (it also carries the .ref compile_options put in the
            # spec); guards / content_channels pass through verbatim.
            policy_override=options.policy,
            extra_guards=tuple(options.guards),
            extra_content_kinds=tuple(options.content_channels),
            # (D3) — host-level runtime
            # injections (NOT agent identity): the HTML-app preview gateway
            # (open_app) and the live-MCP alias resolver + transport. All default
            # to absent, so a bare HostConfig() leaves the tool list / wire
            # byte-identical to today.
            app_gateway=hc.app_gateway,
            # Sandbox execution backend (D2 host config). ``None`` (default) ⇒
            # the local host; when set, the SdkHost builds a sandbox manager and
            # routes every session's fs / shell IO into the container. ``exec_env``
            # attaches one shared container (v1); ``sandbox_provider`` +
            # ``sandbox_spec`` provision a fresh container per root-task tree (v2).
            exec_env=hc.exec_env,
            sandbox_provider=hc.sandbox_provider,
            sandbox_spec=hc.sandbox_spec,
            sandbox_exec_preamble=hc.sandbox_exec_preamble,
            sandbox_backend_factory=hc.sandbox_backend_factory,
            sandbox_browser_factory=hc.sandbox_browser_factory,
            # Memory store addressing (issue #53): the host-level roots plus the
            # per-task resolver seam for multi-tenant hosts. All default to
            # absent, so a bare HostConfig() keeps the SDK global default root —
            # byte-identical for every single-tenant caller.
            memory_dir=hc.memory_dir,
            global_memory_dir=hc.global_memory_dir,
            memory_root_resolver=hc.memory_root_resolver,
            mcp_server_resolver=hc.mcp_server_resolver,
            mcp_http_post=hc.mcp_http_post,
            delta_sink=hc.delta_sink,
            provider_headers=hc.provider_headers,
            workflow_allowed=hc.workflow_allowed,
            # Process fs write policy (D3 host config): "apply" performs real
            # writes, anything else stages a dry-run diff (the safe default).
            write_mode=(
                FsWriteMode.APPLY if hc.write_mode == "apply" else FsWriteMode.DRY_RUN
            ),
        )

        # OTLP trace export (host config): a lifecycle-owning observer the
        # Client stops on shutdown. Default off. Constructed only after the
        # host assembled successfully — its async worker thread must not
        # outlive a failed __init__ (nothing is emitted before this point,
        # so no event is missed).
        self._trace_export: Optional[TraceExportObserver] = None
        if hc.otlp_traces is not None:
            self._trace_export = make_otlp_trace_observer(
                event_log=event_log,
                config=hc.otlp_traces,
                http_post=hc.otlp_http_post,
            )

        # 5. Interaction driver
        # A local deployment widens the per-turn model-selector allowlist to its
        # configured model list. noeta-agent runs as the ⊤ LOCAL_PRINCIPAL, so the
        # deployment allowlist IS the authorized set (``allowed_models`` =
        # BackendConfig.models) — this lets real model ids (e.g. ``gpt-5.5``) pass
        # the driver's per-turn ``_authorize_selector`` without per-principal
        # config. Absent it, keep the driver's STUB_MODEL_ALLOWLIST default →
        # byte-identical to every pre-widening caller (oneshot / tests / CLI).
        self._driver: InteractionDriver = InteractionDriver(
            self._host,
            # Note: do not pass model_selector — let host.model become the
            # default naturally, avoiding allowlist friction.
            # default_model=None makes driver.__init__ fall back to host.model.
            default_model=None,
            model_allowlist=(
                frozenset(allowed_models) if allowed_models else STUB_MODEL_ALLOWLIST
            ),
        )
        # Wire the driver back into the host as the background-completion
        # notifier (Mechanism C). The driver wraps the host, so the host cannot
        # construct it — we set it here, after construction. This activates the
        # turn-boundary completion push for BOTH a ``shell_run(background=true)``
        # job and a ``spawn_subagent(background=True)`` sub-agent: when one
        # finishes while the session is idle, the host's drive thread wakes the
        # session and injects an ``origin="system"`` notice. ``getattr`` so a host
        # without the seam (a test double) is a clean no-op.
        set_notifier = getattr(self._host, "set_background_notifier", None)
        if callable(set_notifier):
            set_notifier(self._driver)
        # Crash recovery (docs/adr/background-subagent.md): now that the notifier
        # is wired, re-activate background sub-agents orphaned by a prior host
        # crash — re-drive any ``spawn_subagent(background=True)`` child with a
        # ``BackgroundSubagentStarted`` but no ``BackgroundSubagentDelivered``
        # (it resumes from its own EventLog), or re-deliver a terminal one whose
        # turn-boundary notice was lost. A one-shot startup side effect (never
        # resumed); a no-op for an in-memory ``query()`` Client (no prior
        # streams) and for a host without the seam (test double / no policy
        # wrapper → registry unbuilt). ``getattr`` keeps both cases clean.
        recover = getattr(self._host, "recover_background_subagents", None)
        if callable(recover):
            recover()
        self._main_agent_name = main_spec.name
        self._registry = registry
        # can_use_tool callback (wiring-only, not part of the AgentSpec identity)
        self._can_use_tool: Optional[Callable[[str, dict[str, Any]], bool]] = (
            options.can_use_tool  # type: ignore[assignment]
        )

    # -- 1:1 pass-throughs to driver ----------------------------------------

    # -- can_use_tool auto-resolver ------------------------------------------

    def _drain_approvals(self, task_id: str, outcome: DriveOutcome) -> DriveOutcome:
        """Loop-resolve pending tool-call approvals via ``can_use_tool``.

        When the callback is configured and the outcome is a suspend on an
        ``approval-*`` handle (i.e. a gated tool is waiting), scan the
        event log for the newest ``ToolCallApprovalRequested`` that has no
        matching ``ToolCallApprovalResolved``, invoke the user's callback,
        and resume with driver approve/deny. Repeat until the task is no
        longer suspended on an approval handle, then return the final
        outcome.
        """
        callback = self._can_use_tool
        if callback is None:
            return outcome
        while True:
            handle = outcome.wake_handle
            if (
                outcome.status != "suspended"
                or not isinstance(handle, str)
                or not handle.startswith("approval-")
            ):
                return outcome
            # Find the latest unreplied ToolCallApprovalRequested.
            events = self._host.event_log.read(task_id)
            pending: Optional[ToolCallApprovalRequestedPayload] = None
            resolved_call_ids: set[str] = set()
            for e in events:
                if e.type == "ToolCallApprovalResolved":
                    p = e.payload
                    if isinstance(p, ToolCallApprovalResolvedPayload):
                        resolved_call_ids.add(p.call_id)
            for e in reversed(events):
                if e.type == "ToolCallApprovalRequested":
                    p = e.payload
                    if (
                        isinstance(p, ToolCallApprovalRequestedPayload)
                        and p.call_id not in resolved_call_ids
                    ):
                        pending = p
                        break
            if pending is None:
                # No pending request — leave outcome alone.
                return outcome
            args = resolve_tool_call_arguments(pending, self._host.content_store)
            approved = bool(callback(pending.tool_name, args))
            if approved:
                outcome = self._driver.approve(
                    task_id,
                    call_id=pending.call_id,
                    reason=None,
                    resolver="can_use_tool",
                )
            else:
                outcome = self._driver.deny(
                    task_id,
                    call_id=pending.call_id,
                    reason=None,
                    resolver="can_use_tool",
                )

    def start(
        self,
        *,
        goal: str,
        agent: Optional[str] = None,
        model_selector: Optional[str] = None,
        images: Sequence[ImageBlock] = (),
        permission_mode: Optional[str] = None,
        enabled_mcp: tuple[str, ...] = (),
        workspace_dir: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> Any:
        """Create a Task and drive the first turn (driver ``start``).

        ``agent`` defaults to the Options-compiled main spec's name
        (``"main"`` unless the recipe changed it). Passing a specific
        ``model_selector`` is subject to the deployment
        :data:`~noeta.execution.driver.STUB_MODEL_ALLOWLIST`; to set a
        per-Client default without the allowlist check, use the
        constructor ``model`` argument instead.

        ``images`` rides the opening user turn alongside the goal text
        (additive — empty keeps the seed byte-identical to the text-only path).

        ``permission_mode`` / ``enabled_mcp`` are per-turn, NON-durable host
        knobs the product backend forwards from the request (the turn's approval
        mode and the MCP aliases enabled for this conversation); both default to
        inert values (the historical no-MCP, host-default-mode path).

        ``workspace_dir`` is the
        per-session workspace **absolute path** the driver welds into the durable
        ``TaskHostBound`` — pass it once here at session creation and every later
        turn fold-resolves it (zero mapping). ``effort`` is the per-turn,
        NON-durable reasoning-effort selector. Both default to ``None`` ⇒ the
        host-fixed workspace / effort, byte-identical to the pre-widening path.
        """
        outcome = self._driver.start(
            goal=goal,
            agent=agent if agent is not None else self._main_agent_name,
            model_selector=model_selector,
            images=images,
            permission_mode=permission_mode,
            enabled_mcp=enabled_mcp,
            workspace_dir=workspace_dir,
            effort=effort,
        )
        return self._drain_approvals(outcome.task_id, outcome)

    def send_goal(
        self,
        task_id: str,
        *,
        goal: str,
        model_selector: Optional[str] = None,
        images: Sequence[ImageBlock] = (),
        permission_mode: Optional[str] = None,
        enabled_mcp: tuple[str, ...] = (),
        effort: Optional[str] = None,
    ) -> Any:
        """Append a new user turn (driver ``send_goal``).

        ``images`` rides the appended user turn alongside the goal text
        (additive — empty keeps the append byte-identical to the text-only path).

        ``permission_mode`` / ``enabled_mcp`` are per-turn host knobs (see
        :meth:`start`); inert defaults keep this byte-identical to the bare path.

        ``effort`` is the per-turn, NON-durable reasoning-effort selector. No
        ``workspace_dir`` here: a follow-up turn fold-resolves the workspace the
        session was created with, so the workspace is bound once at
        :meth:`start` and never re-passed.
        """
        outcome = self._driver.send_goal(
            task_id=task_id,
            goal=goal,
            model_selector=model_selector,
            images=images,
            permission_mode=permission_mode,
            enabled_mcp=enabled_mcp,
            effort=effort,
        )
        return self._drain_approvals(task_id, outcome)

    def approve(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "client",
    ) -> Any:
        """Approve a pending gated tool call (driver ``approve``)."""
        return self._driver.approve(
            task_id=task_id, call_id=call_id, reason=reason, resolver=resolver
        )

    def deny(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "client",
    ) -> Any:
        """Deny a pending gated tool call (driver ``deny``)."""
        return self._driver.deny(
            task_id=task_id, call_id=call_id, reason=reason, resolver=resolver
        )

    def answer(
        self,
        task_id: str,
        *,
        question_id: str,
        answers: dict[str, Any],
        answered_by: str = "client",
    ) -> Any:
        """Answer a pending structured user question (driver ``answer``)."""
        return self._driver.answer(
            task_id=task_id,
            question_id=question_id,
            answers=answers,
            answered_by=answered_by,
        )

    def deliver_event(
        self,
        task_id: str,
        *,
        event_kind: str,
        payload: Any = None,
    ) -> Any:
        """Deliver an external event to a ``wait_external``-suspended task
        (driver ``deliver_event``).

        Wakes a task suspended on ``ExternalEvent(event_kind)`` and drives the
        resumed turn. ``payload`` (an optional JSON value) rides the resumed
        turn as an ``origin="system"`` message — never the wake event itself.
        A task not waiting on this ``event_kind`` (including a repeat delivery
        after the wake was consumed) raises the typed ``NotResumableError``.
        """
        return self._driver.deliver_event(
            task_id=task_id, event_kind=event_kind, payload=payload
        )

    # -- seed / drive split (async transports) -------------------------------
    #
    # The one-call verbs above run the whole turn on the caller's thread. An
    # async transport (the product backend's HTTP command endpoints) instead
    # seeds on the request thread — every durable, validated step, so a typed
    # rejection (selector / NotResumableError) still surfaces as the same
    # synchronous 4xx — and hands the returned seeded turn to
    # :meth:`drive_seeded` on a background thread, acking immediately while
    # progress rides the committed event stream.

    def seed_start(
        self,
        *,
        goal: str,
        agent: Optional[str] = None,
        model_selector: Optional[str] = None,
        images: Sequence[ImageBlock] = (),
        permission_mode: Optional[str] = None,
        enabled_mcp: tuple[str, ...] = (),
        workspace_dir: Optional[str] = None,
        effort: Optional[str] = None,
    ) -> Any:
        """Create + validate + lease a first turn WITHOUT driving it
        (driver ``seed_start``); pass the result to :meth:`drive_seeded`."""
        return self._driver.seed_start(
            goal=goal,
            agent=agent if agent is not None else self._main_agent_name,
            model_selector=model_selector,
            images=images,
            permission_mode=permission_mode,
            enabled_mcp=enabled_mcp,
            workspace_dir=workspace_dir,
            effort=effort,
        )

    def seed_send_goal(
        self,
        task_id: str,
        *,
        goal: str,
        model_selector: Optional[str] = None,
        images: Sequence[ImageBlock] = (),
        permission_mode: Optional[str] = None,
        enabled_mcp: tuple[str, ...] = (),
        effort: Optional[str] = None,
    ) -> Any:
        """Validate + seed a follow-up user turn WITHOUT driving it
        (driver ``seed_send_goal``)."""
        return self._driver.seed_send_goal(
            task_id=task_id,
            goal=goal,
            model_selector=model_selector,
            images=images,
            permission_mode=permission_mode,
            enabled_mcp=enabled_mcp,
            effort=effort,
        )

    def seed_approve(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "client",
    ) -> Any:
        """Validate + seed an approve-and-resume turn (driver ``seed_approve``)."""
        return self._driver.seed_approve(
            task_id, call_id=call_id, reason=reason, resolver=resolver
        )

    def seed_deny(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "client",
    ) -> Any:
        """Validate + seed a deny-and-resume turn (driver ``seed_deny``)."""
        return self._driver.seed_deny(
            task_id, call_id=call_id, reason=reason, resolver=resolver
        )

    def seed_answer(
        self,
        task_id: str,
        *,
        question_id: str,
        answers: dict[str, Any],
        answered_by: str = "client",
    ) -> Any:
        """Validate + seed an answer-and-resume turn (driver ``seed_answer``)."""
        return self._driver.seed_answer(
            task_id,
            question_id=question_id,
            answers=answers,
            answered_by=answered_by,
        )

    def seed_deliver_event(
        self,
        task_id: str,
        *,
        event_kind: str,
        payload: Any = None,
    ) -> Any:
        """Validate + seed an external-event resume turn (driver
        ``seed_deliver_event``)."""
        return self._driver.seed_deliver_event(
            task_id, event_kind=event_kind, payload=payload
        )

    def drive_seeded(self, seeded: Any) -> Any:
        """Drive a seeded turn to its trailing suspend / terminal (driver
        ``drive_seeded``), then loop-resolve ``can_use_tool`` approvals —
        the same tail the one-call verbs run."""
        outcome = self._driver.drive_seeded(seeded)
        return self._drain_approvals(seeded.task_id, outcome)

    def cancel(
        self,
        task_id: str,
        *,
        reason: str = "cancelled",
        cascade: bool = False,
    ) -> Any:
        """Cancel a conversation (driver ``cancel``)."""
        return self._driver.cancel(task_id=task_id, reason=reason, cascade=cascade)

    def close(
        self,
        task_id: str,
        *,
        closed_by: str = "user",
        reason: Optional[str] = None,
    ) -> Any:
        """Close / archive a conversation (driver ``close``)."""
        return self._driver.close(task_id=task_id, closed_by=closed_by, reason=reason)

    def reopen(
        self,
        task_id: str,
        *,
        reopened_by: str = "user",
        reason: Optional[str] = None,
    ) -> Any:
        """Explicitly reopen a closed conversation (driver ``reopen``)."""
        return self._driver.reopen(
            task_id=task_id, reopened_by=reopened_by, reason=reason
        )

    # -- extras ------------------------------------------------------------

    @property
    def registry(self) -> AgentRegistry:
        """The compiled :class:`AgentRegistry` (main + descendants)."""
        return self._registry

    @property
    def main_agent_name(self) -> str:
        """Convenience: the compiled main spec's name."""
        return self._main_agent_name

    def events(self, task_id: str) -> list[EventEnvelope]:
        """Return the full event-envelope stream for ``task_id``."""
        return list(self._host.event_log.read(task_id))

    def messages(self, task_id: str) -> list[ViewItem]:
        """Fold ``task_id``'s envelope stream into the human-readable view.

        Thin-client convenience: ``as_messages(self.events(task_id), <store>)``
        without the caller having to reach for the content store used to
        deref large blocks. The canonical output is still the envelope stream
        (:meth:`events`); this is the user-facing projection.
        """
        return as_messages(self.events(task_id), self._host.content_store)

    def events_after(
        self, task_id: str, after_seq: Optional[int] = None
    ) -> list[EventEnvelope]:
        """The envelope stream for ``task_id`` strictly past ``after_seq``.

        ``None`` ⇒ the full stream. Used by a streaming bridge (an app's SSE
        layer) to resume one task's sub-stream from a per-task cursor.
        """
        return list(self._host.event_log.read(task_id, after_seq=after_seq))

    def task_streams(self) -> list[Any]:
        """Enumerate every task stream this client has driven.

        Each row carries ``task_id`` + ``last_seq`` (a ``TaskStreamSummary``) —
        enough for a streaming bridge to discover the root's subtask tree and
        catch each sub-stream up from its per-task cursor.
        """
        return list(self._host.event_log.list_task_streams())

    def delete_task(self, task_id: str) -> dict[str, Any]:
        """Hard-delete a task and its subtask tree from storage.

        The conversation *is* the task (D6), so "delete the session" purges each
        task's event stream + dispatcher state, cascaded across the whole subtask
        tree (a subtask rides its root). Refuses with ``reason="running"`` when a
        worker is actively running any task in the tree (the purge never races an
        in-flight turn) and ``reason="not_found"`` when the root is unknown.
        Hash-addressed content blobs are shared across tasks and left for offline
        GC — never touched here. Returns a typed result the caller maps onto a
        status: ``{"ok", "reason"?, "task_id", "deleted": [...]}``.
        """
        event_log = self._host.event_log
        dispatcher = self._host.dispatcher
        # Genesis parent per known task → the subtask tree to cascade across.
        parent_of: dict[str, Optional[str]] = {}
        for summary in event_log.list_task_streams():
            tid = getattr(summary, "task_id", None)
            if isinstance(tid, str):
                parent_of[tid] = self._genesis_parent(tid)
        if task_id not in parent_of:
            return {
                "ok": False,
                "reason": "not_found",
                "task_id": task_id,
                "deleted": [],
            }
        children: dict[str, list[str]] = {}
        for tid, parent in parent_of.items():
            if parent:
                children.setdefault(parent, []).append(tid)
        targets: list[str] = []
        seen: set[str] = set()
        queue = [task_id]
        while queue:
            tid = queue.pop()
            if tid in seen:
                continue
            seen.add(tid)
            targets.append(tid)
            queue.extend(children.get(tid, []))
        # Active guard — never purge a task a worker is actively running. Prefer
        # the expiry-aware lease check so a zombie lease (TTL lapsed after its
        # worker died) never makes a task permanently undeletable.
        active_fn = getattr(dispatcher, "has_active_lease", None)
        status_fn = getattr(dispatcher, "task_status", None)
        for tid in targets:
            if callable(active_fn):
                running = bool(active_fn(tid))
            elif callable(status_fn):
                running = status_fn(tid) == "leased"
            else:
                running = False
            if running:
                return {"ok": False, "reason": "running", "task_id": tid, "deleted": []}
        purge_events = getattr(event_log, "purge_task", None)
        purge_disp = getattr(dispatcher, "purge_task", None)
        # In-memory host accelerators keyed by task/session id (per-turn
        # carriers, retained background-shell job handles, background sub-agent
        # tracking) are NOT storage, so the storage purge above leaves them
        # resident — a leak for the lifetime of the process across many deleted
        # conversations. Reclaim them here too, per subtree target (the seams
        # no-op for a tid they hold nothing for). getattr keeps the purge working
        # against a host/backend that lacks a given accelerator.
        forget_carriers = getattr(self._host, "forget_turn_carriers", None)
        purge_bg_jobs = getattr(self._host, "purge_background_session", None)
        forget_bg_agents = getattr(self._host, "forget_background_subagents", None)
        for tid in targets:
            if callable(purge_events):
                purge_events(tid)
            if callable(purge_disp):
                purge_disp(tid)
            if callable(forget_carriers):
                forget_carriers(tid)
            if callable(purge_bg_jobs):
                purge_bg_jobs(tid)
            if callable(forget_bg_agents):
                forget_bg_agents(tid)
        return {"ok": True, "task_id": task_id, "deleted": targets}

    def _genesis_parent(self, task_id: str) -> Optional[str]:
        """``parent_task_id`` from a task's genesis ``TaskCreated`` (``None`` if root)."""
        for env in self._host.event_log.read(task_id):
            if env.type == "TaskCreated":
                return getattr(env.payload, "parent_task_id", None)
        return None

    def memory_root(self, task_id: Optional[str] = None) -> Path:
        """The host's resolved memory-store root (see :meth:`SdkHost.memory_root`).

        The per-task ``memory_root_resolver`` (when configured and ``task_id``
        is given) first, else ``memory_dir`` override > ``global_memory_dir`` >
        the SDK global default. A product backend reads it to place host-side
        memory material (e.g. the consolidation debounce marker) next to the
        store the memory tools use, without re-deriving the resolution chain —
        a multi-tenant host passes one of the tenant's task ids to land the
        marker next to that tenant's store.
        """
        return self._host.memory_root(task_id)

    def get_content(self, content_hash: str) -> Optional[bytes]:
        """Fetch a stored blob by content hash (``None`` if absent).

        ``ContentStore.get`` is hash-only, so a streaming bridge can deref a
        ``ContentRef`` carried in the envelope stream without re-deriving the
        full ref. The media type is the caller's concern (sniff or default).
        """
        ref = ContentRef(
            hash=content_hash, size=0, media_type="application/octet-stream"
        )
        try:
            return self._host.content_store.get(ref)
        except Exception:
            return None

    def put_content(self, body: bytes, *, media_type: str) -> ContentRef:
        """Store ``body`` and return its stable :class:`ContentRef`.

        The write-side mirror of :meth:`get_content`: a product backend that
        receives raw bytes (e.g. a base64 image attachment) puts them through
        noeta.sdk and gets back a ``ContentRef`` to wrap in an ``ImageBlock`` for
        a user turn — without importing ``noeta.protocols`` (the D2 weld).
        Content-addressed: identical bytes → identical hash.
        """
        return self._host.content_store.put(body, media_type=media_type)

    def subscribe(
        self, callback: Callable[[EventEnvelope], None]
    ) -> Callable[[], None]:
        """Subscribe to the live, post-commit envelope stream (ALL tasks).

        Returns an unsubscribe callable. The callback fires once per committed
        envelope across every task on this client (root + subtasks) — a
        streaming bridge filters to the tree it serves and assigns its own
        stream-level cursor.
        """
        return self._host.event_log.subscribe(callback)

    # -- resident worker pool ---------------------------------------------

    def start_workers(
        self,
        num_workers: int = 1,
        *,
        poll_interval: float = 0.1,
        heartbeat_interval: float = 30.0,
        stale_sweep_interval: float = 10.0,
        timer_poll_interval: float = 1.0,
        lease_seconds: float = 600.0,
        shutdown_grace_s: Optional[float] = 10.0,
    ) -> None:
        """Start ``num_workers`` resident WorkerLoop daemon threads.

        When workers are running, the ``background_drive`` verbs (start /
        send_goal / approve / deny / answer / deliver_event) seed
        durably on the caller thread — same typed 4xx contract — then
        yield the seed's lease back to the ready queue via
        ``dispatcher.release_yield``. A resident worker picks the task
        up and drives it through ``run_leased_task``; progress rides the
        committed event stream (SSE), exactly like the per-command
        ``_spawn_drive`` daemon-thread model but with true concurrency.

        Safe to call once. Subsequent calls raise ``RuntimeError``.
        """
        if self._workers_started:
            raise RuntimeError("start_workers() called more than once")
        if num_workers < 1:
            raise ValueError(f"num_workers must be >= 1, got {num_workers}")
        self._workers_started = True
        # Import NEXT_GOAL_WAKE_HANDLE locally so a runtime-only host
        # never resolving it is fine (start_workers is only called by
        # multi-turn interactive hosts).
        from noeta.protocols.wake import NEXT_GOAL_WAKE_HANDLE

        for i in range(num_workers):
            loop = WorkerLoop(
                self._host,
                worker_id=f"noeta-agent-worker-{i}",
                lease_seconds=lease_seconds,
                poll_interval=poll_interval,
                heartbeat_interval=heartbeat_interval,
                stale_sweep_interval=stale_sweep_interval,
                timer_poll_interval=timer_poll_interval,
                shutdown_grace_s=shutdown_grace_s,
                next_goal_handle=NEXT_GOAL_WAKE_HANDLE,
            )
            self._worker_loops.append(loop)
            th = threading.Thread(
                target=loop.run_forever,
                name=f"noeta-worker-{i}",
                daemon=True,
            )
            th.start()
            self._worker_threads.append(th)

    @property
    def workers_running(self) -> bool:
        return self._workers_started

    def stop_workers(self, timeout: Optional[float] = None) -> bool:
        """Stop every resident worker and wait for them to exit.

        Returns True if all workers exited within ``timeout`` (``None``
        = wait up to each loop's ``shutdown_grace_s`` which is enforced
        inside ``run_forever``'s shutdown path).
        """
        if not self._workers_started:
            return True
        for loop in self._worker_loops:
            loop.stop()
        deadline = (
            None if timeout is None else (__import__("time").monotonic() + timeout)
        )
        for th in self._worker_threads:
            remaining = None
            if deadline is not None:
                remaining = max(0.0, deadline - __import__("time").monotonic())
            th.join(timeout=remaining)
        all_joined = all(not t.is_alive() for t in self._worker_threads)
        self._worker_loops.clear()
        self._worker_threads.clear()
        self._workers_started = False
        return all_joined

    def _yield_seeded_lease(self, seeded: Any) -> None:
        """Hand a seeded lease back to the ready queue for a worker to pick up.

        Used by the background-drive verbs after seed() when a resident
        worker pool is running, in place of spawning a one-off drive
        thread. If the seed produced a non-durable prelude (e.g.
        ResolveApprovalPrelude — executes the approved tool, cannot ride
        the request thread), stash it on the host so the worker that
        picks up the task can apply it between note_woken and
        run_one_step.
        """
        if getattr(seeded, "prelude", None) is not None:
            self._host.put_pending_prelude(seeded.task_id, seeded.prelude)
        self._host.dispatcher.release_yield(seeded.lease.lease_id)

    # -- sandbox lifecycle listeners (product wiring) ----------------------

    def add_sandbox_lifecycle_listener(
        self,
        on_allocate: Any,
        on_release: Any,
    ) -> None:
        """Register ``(on_allocate, on_release)`` on the sandbox manager.

        Delegates to :meth:`SdkHost.add_sandbox_lifecycle_listener`. Safe on
        the local path (no sandbox ⇒ no-op). Used by the product backend to
        wire preview gateway mounts and similar container-tracked side effects.
        """
        self._host.add_sandbox_lifecycle_listener(on_allocate, on_release)

    def shutdown(self) -> None:
        """Stop resident workers (if any), then unsubscribe observers.

        Idempotent. Does **not** explicitly close in-memory stores
        (they are process-owned).
        """
        if self._shutdown:
            return
        self._shutdown = True
        # Stop the worker pool first so no worker is mid-step when we
        # tear down observers / the trace sink below.
        if self._workers_started:
            try:
                self.stop_workers(timeout=10.0)
            except Exception:
                pass
        try:
            self._unsubscribe_default()
        except Exception:
            # Observer unsubscribe must never raise; swallow defensively.
            pass
        for unsub in self._unsubscribe_observers:
            try:
                unsub()
            except Exception:
                pass
        if self._trace_export is not None:
            # Unsubscribes, drains the async worker, flushes the sink.
            self._trace_export.stop()
        # Reap the host's sandbox backend (if any) so an idle container
        # connection does not outlive the process. No-op on the local path.
        try:
            self._host.teardown_exec_env()
        except Exception:
            # Shutdown must never raise from teardown; swallow defensively.
            pass


# ---------------------------------------------------------------------------
# one-shot query
# ---------------------------------------------------------------------------


class QueryFailedError(CodedError):
    """``QueryResult.answer()`` was called but the one-shot task did not
    complete.

    Raised for a ``TaskFailed`` terminal (``status == "failed"``, ``reason`` /
    ``retryable`` from the payload) and for a stream with no terminal at all
    (``status`` is the folded task status, e.g. suspended on an
    ``approval-{call_id}`` handle no one is around to resolve). Keeping the
    failure on the exception path — instead of folding the reason into a
    ``Result.answer`` string — is what stops a caller from mistaking a failure
    reason for a successful answer (issue #5's second footgun).
    """

    code = "query_failed"

    def __init__(
        self,
        *,
        task_id: str,
        status: str,
        reason: str,
        retryable: bool = False,
    ) -> None:
        self.task_id = task_id
        self.status = status
        self.reason = reason
        self.retryable = retryable
        super().__init__(
            f"query task {task_id!r} did not complete (status={status!r}): {reason}"
        )


class QueryResult(list[EventEnvelope]):
    """The return value of :func:`query`: the envelope list + materialized
    projections.

    Still a ``list[EventEnvelope]`` (iteration / indexing / ``isinstance(x,
    list)`` all behave as before), so the canonical record of what the agent
    did remains the envelope stream. On top of that it carries the projections
    a one-shot caller actually wants, **materialized against the temporary
    Client's live ContentStore before shutdown** — raw envelopes reference
    their large bodies by ``ContentRef`` (``answer_ref`` / ``messages_ref`` /
    ``output_ref``), which only the originating host's store can resolve, and
    that store is gone by the time ``query`` returns (issue #5).
    """

    __slots__ = ("task_id", "_view", "_answer", "_failure")

    def __init__(
        self,
        envelopes: Sequence[EventEnvelope],
        *,
        task_id: str,
        view: list[ViewItem],
        answer: Any,
        failure: Optional[QueryFailedError],
    ) -> None:
        super().__init__(envelopes)
        self.task_id = task_id
        self._view = view
        self._answer = answer
        self._failure = failure

    def messages(self) -> list[ViewItem]:
        """The human-readable view of the stream (``as_messages`` output).

        Pre-folded with every ``ContentRef`` already dereferenced, so it stays
        valid for the lifetime of this object — no ContentStore needed.
        """
        return list(self._view)

    def answer(self) -> Any:
        """The full terminal answer (inline or spilled — the spill is
        transparent).

        Strict: raises :class:`QueryFailedError` when the task failed or never
        reached a terminal, so a failure reason can't be mistaken for a
        successful answer. For the lenient view, read the terminal
        ``Result`` item from :meth:`messages` and branch on ``status``.
        """
        if self._failure is not None:
            raise self._failure
        return self._answer


def _materialize_query_result(client: Client, outcome: Any) -> QueryResult:
    """Fold everything ref-carrying against the live host store.

    Runs inside ``query``'s Client lifetime — the last moment the paired
    ContentStore is reachable. After this, the returned ``QueryResult`` is
    self-contained.
    """
    task_id = outcome.task_id
    envelopes = client.events(task_id)
    store = client._host.content_store
    view = as_messages(envelopes, store)

    terminal = next(
        (
            env
            for env in reversed(envelopes)
            if env.type in ("TaskCompleted", "TaskFailed")
        ),
        None,
    )
    answer: Any = None
    failure: Optional[QueryFailedError] = None
    if terminal is not None and isinstance(terminal.payload, TaskCompletedPayload):
        answer = answer_from_payload(terminal.payload, store)
    elif terminal is not None:
        payload = terminal.payload
        assert isinstance(payload, TaskFailedPayload)
        failure = QueryFailedError(
            task_id=task_id,
            status="failed",
            reason=payload.reason,
            retryable=payload.retryable,
        )
    else:
        wake = getattr(outcome, "wake_handle", None)
        detail = f"; waiting on {wake!r}" if wake else ""
        failure = QueryFailedError(
            task_id=task_id,
            status=str(outcome.status),
            reason=f"no terminal event in the stream{detail}",
        )
    return QueryResult(
        envelopes,
        task_id=task_id,
        view=view,
        answer=answer,
        failure=failure,
    )


def query(
    options: Options,
    goal: str,
    *,
    provider: Optional[LLMProvider] = None,
    workspace_dir: Optional[Path] = None,
    model: Optional[str] = None,
    images: Sequence[ImageBlock] = (),
) -> QueryResult:
    """One-shot SDK query: single turn, all envelopes + folded projections.

    Creates a temporary ``Client(multi_turn=False)`` so the policy
    reaches a genuine ``TaskCompleted`` terminal instead of suspending
    on the next-goal handle. The canonical return shape is still the full
    Noeta event-envelope list (:class:`QueryResult` *is* one), but the
    human-facing projections are folded eagerly, **before** the temporary
    Client is torn down: ``result.messages()`` for the message view and
    ``result.answer()`` for the terminal answer. Raw envelopes carry
    ``ContentRef``\\ s (a spilled ``answer_ref``, every ``messages_ref`` /
    ``output_ref``) that only the temporary Client's ContentStore could
    resolve — never hand them to ``as_messages`` with a fresh store.

    Parameters match the ``Client`` constructor + a ``goal`` string.
    Callers who need multi-turn interactions (``send_goal`` /
    ``approve`` / …) or access to the compiled registry should
    instantiate ``Client`` directly instead of going through ``query``.
    """
    client = Client(
        options,
        provider=provider,
        workspace_dir=workspace_dir,
        model=model,
        multi_turn=False,
    )
    try:
        outcome = client.start(goal=goal, images=images)
        return _materialize_query_result(client, outcome)
    finally:
        client.shutdown()
