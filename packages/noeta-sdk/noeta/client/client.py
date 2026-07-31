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
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, TypedDict

from noeta.agent.registry import AgentRegistry
from noeta.context.reminders import ReminderSpec
from noeta.execution.control_tool import ControlToolEntry
from noeta.execution.session_pack import SessionPackEntry
from noeta.core.wiring import wire_default_observers
from noeta.client.otlp import make_otlp_trace_observer
from noeta.observers.trace_export import TraceExportObserver
from noeta.execution import (
    InteractionDriver,
    multi_turn_policy_wrapper,
)
from noeta.client.messages import ViewItem, as_messages
from noeta.client.parts import resolve_model_alias
from noeta.execution.driver import DriveOutcome, SeededTurn
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
from noeta.runtime.workspace import FsWriteMode

from noeta.client.host import SdkHost
from noeta.client.host_config import HostConfig
from noeta.client.options import (
    AgentDefinition,
    Options,
    PluginActivation,
    compile_options,
    effective_root_policy,
)
from noeta.client.plugin_set import PluginSet


__all__ = ["Client", "DeleteTaskResult", "QueryFailedError", "QueryResult", "query"]


class DeleteTaskResult(TypedDict, total=False):
    """The typed shape :meth:`Client.delete_task` returns.

    ``ok`` and ``task_id`` are always present; ``deleted`` lists the purged
    task ids (the root plus its subtask tree) and ``reason`` appears only on a
    refusal (``"running"`` / ``"not_found"``). Declared so a caller mapping
    the result onto an HTTP status gets the keys checked rather than guessing
    at a bare ``dict[str, Any]``.
    """

    ok: bool
    task_id: str
    deleted: list[str]
    reason: str


#: Deployment model-selector allowlist the SDK applies when the host config
#: sets no ``allowed_models``: the friendly aliases of the providers
#: builtin's catalog (mirrors Claude Code's ``/model`` names). A product
#: default, SDK-side since phase 2c — the kernel driver takes the allowlist
#: purely as an injection (``None`` there means "no deployment bound").
DEFAULT_MODEL_ALLOWLIST: frozenset[str] = frozenset({"opus", "sonnet", "haiku"})


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


def _collect_custom_tools(
    root: Options,
    activations: Optional[Mapping[str, PluginActivation]] = None,
) -> dict[str, Tool]:
    """Gather every ``DecoratedTool`` closure referenced from ``root``.

    Scans (in order):

    * Every ``root.allowed_tools`` entry (when not ``None``).
    * Every ``AgentDefinition.tools`` entry in ``root.agents`` (when not
      ``None``).
    * Every activated plugin's contributed tools — ``compile_options`` puts their
      ``ToolRef`` in the spec, so the closure has to reach the host too or the
      agent carries a tool name the runtime cannot build.

    The agents tree is flat — there is no recursive nesting, so no tree
    walk is needed.
    """
    gathered: dict[str, Tool] = {}
    if root.allowed_tools is not None:
        _scan_entries(root.allowed_tools, gathered)
    for defn in root.agents.values():
        if isinstance(defn, AgentDefinition) and defn.tools is not None:
            _scan_entries(defn.tools, gathered)
    for act in (activations or {}).values():
        _scan_entries(tuple(act.tools), gathered)
        for _name, defn in act.agents:
            if isinstance(defn, AgentDefinition) and defn.tools is not None:
                _scan_entries(defn.tools, gathered)
    # In-process MCP servers (Options.mcp_servers): their bundled @tool
    # closures are custom tools too. Duck-typed by ``.tools`` (the SdkMcpServer
    # value object) so noeta.client takes no upward import on noeta.sdk.
    for server in root.mcp_servers:
        _scan_entries(tuple(getattr(server, "tools", ())), gathered)
    return gathered


# ---------------------------------------------------------------------------
# Per-agent activation folding (D6 — feature surfaces follow activation)
# ---------------------------------------------------------------------------


def _activated_names(
    options: Options, plugins: Optional["PluginSet"]
) -> Optional[frozenset[str]]:
    """Every plugin name some agent activates — the resolution scope (D5).

    Resolving a plugin imports its refs and runs its module body, so that step is
    restricted to plugins an agent actually opted into. The set is computed in two
    passes because an activated plugin may itself contribute a child agent with
    its own activation list: pass one covers the base recipe, and if resolving it
    contributes children naming further plugins, the scope widens and resolves
    again. Resolution is memoised on the ``PluginSet``, so the second pass only
    pays for the plugins the first one did not already reach.

    ``None`` (no loaded set) means there is nothing to scope.
    """
    if plugins is None:
        return None
    names = {*options.plugins}
    for defn in options.agents.values():
        if isinstance(defn, AgentDefinition):
            names.update(defn.plugins)
    seen: set[str] = set()
    while True:
        pending = names - seen
        if not pending:
            return frozenset(names)
        seen |= pending
        for act in plugins.identity_activations(only=frozenset(names)).values():
            for _child, defn in act.agents:
                if isinstance(defn, AgentDefinition):
                    names.update(defn.plugins)


def _agent_activations(
    options: Options,
    plugin_agents: Mapping[str, AgentDefinition],
) -> dict[str, tuple[str, ...]]:
    """``agent name -> its activation list``, over the effective agent roster.

    The roster is the base ``Options.agents`` plus the child agents the activated
    plugins contribute — the same set ``compile_options`` compiles — so a
    plugin-contributed child gets its own per-agent wiring rather than silently
    running with none.
    """
    out: dict[str, tuple[str, ...]] = {options.name: tuple(options.plugins)}
    for name, defn in {**dict(options.agents), **dict(plugin_agents)}.items():
        if isinstance(defn, AgentDefinition):
            out[name] = tuple(defn.plugins)
    return out


def _ordered_stages(
    source: Mapping[str, tuple[tuple[Any, str, Any], ...]],
    activation: tuple[str, ...],
) -> list[tuple[Any, str, str, Any]]:
    """One agent's contributions to a priority-ordered surface.

    Collects ``(priority, plugin, contribution name, value)`` across every plugin
    the agent activates and sorts by that triple — the ordering every
    priority-ordered surface in the D3 table shares.
    """
    entries = [
        (priority, plugin, cname, value)
        for plugin in activation
        for priority, cname, value in source.get(plugin, ())
    ]
    entries.sort(key=lambda e: (e[0], e[1], e[2]))
    return entries


def _seam_providers(
    source: Mapping[str, tuple[tuple[tuple[str, ...], str, Any], ...]],
    activation: tuple[str, ...],
) -> dict[str, tuple[Any, ...]]:
    """One agent's ``reminder_provider`` s, grouped by recording seam (track A, D7).

    A provider may declare several seams; within a seam the order is
    ``(plugin, contribution name)`` — the order the recording path runs them in.
    """
    by_seam: dict[str, list[tuple[str, str, Any]]] = {}
    for plugin in activation:
        for seams, cname, provider in source.get(plugin, ()):
            for seam in seams:
                by_seam.setdefault(seam, []).append((plugin, cname, provider))
    return {
        seam: tuple(p for _pl, _n, p in sorted(entries, key=lambda e: (e[0], e[1])))
        for seam, entries in sorted(by_seam.items())
    }


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class Client:
    """High-level conversation driver over an ``Options`` recipe.

    Typical use — as a context manager, so ``shutdown`` (observer teardown,
    worker stop, sandbox reap) cannot be forgotten::

        with Client(my_options, provider=my_provider) as client:
            outcome = client.start(goal="fix my tests", agent="main")
            # read events, or follow up with send_goal / approve / deny / answer / …

    The explicit form is equivalent::

        client = Client(my_options, provider=my_provider)
        try:
            outcome = client.start(goal="fix my tests")
        finally:
            client.shutdown()

    Or the one-shot sugar::

        result = query(my_options, goal="fix my tests", provider=my_provider)

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
        plugins: Optional["PluginSet"] = None,
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

        # 0b. Resolve workspace_dir: explicit kwarg > Options.cwd > CWD.
        #     (Both fields are wiring-only, never inspected by compile_options.)
        #     Falling back to the process working directory matches the
        #     ``SdkHost.workspace_dir`` field default: an agent that never
        #     touches the filesystem should not have to be handed a directory
        #     to answer a question, and the two layers disagreeing about
        #     whether a default exists was the only reason this raised.
        effective_workspace_dir: Path
        if workspace_dir is not None:
            effective_workspace_dir = Path(workspace_dir)
        elif options.cwd is not None:
            effective_workspace_dir = Path(options.cwd)
        else:
            effective_workspace_dir = Path.cwd()

        # 1. Compile + register (including child agents).
        #    Manifest-level collision detection first (zero execution): duplicate
        #    tool / agent / content-kind names, a duplicate mcp alias, a second
        #    single-valued policy or provider. Raises PluginError naming BOTH
        #    plugins — there is no override, and running it here means a bad set
        #    fails at build with a manifest-level message rather than surfacing
        #    later as a contribution that silently went missing.
        if plugins is not None:
            plugins.merged()
        #    Activation (D5): a loaded PluginSet supplies the identity-plane
        #    contributions each activated external plugin carries; compile
        #    validates every activation name against the built-in vocabulary +
        #    this loaded set, failing loudly on an unknown name. Resolution is
        #    scoped to the activated names, so loading a plugin no agent activates
        #    never runs its module body (D5: a set stays auditable until something
        #    opts in).
        activated = _activated_names(options, plugins)
        activation_map = (
            plugins.identity_activations(only=activated) if plugins is not None else None
        )
        main_spec, descendant_specs = compile_options(options, plugins=activation_map)
        # D10 policy: the runtime half of the (identity ref already baked by
        # compile) single-valued policy surface — the base Options.policy OR the
        # single active plugin policy (a collision already failed the compile
        # above). Wired as the host's process-wide policy_override, replacing the
        # old ``options.policy`` pass-through.
        effective_policy = effective_root_policy(options, activation_map)
        # The per-agent wiring surfaces (D6: feature surfaces follow activation).
        # Each resolves the activated plugins' contributions, then folds them into
        # an ``agent name -> ordered values`` map the SdkHost selects from when it
        # builds that agent's Engine:
        #   * tool_result_transform (D9) — ToolResult stages inside the ToolRuntime
        #   * reminder             (D8, track B) — pure compose-time renders
        #   * reminder_provider    (D7, track A) — recorded, per recording seam
        #   * content_kind         (D6) — semi-stable composer residents
        # An agent that activates none of them gets an empty entry, which is
        # byte-identical to the pre-plugin construction.
        agent_activations = _agent_activations(
            options,
            {
                name: defn
                for act in (activation_map or {}).values()
                for name, defn in act.agents
            },
        )
        empty: dict[str, tuple[Any, ...]] = {}
        transform_map = (
            plugins.activation_transforms(only=activated) if plugins is not None else empty
        )
        reminder_map = (
            plugins.activation_reminders(only=activated) if plugins is not None else empty
        )
        provider_map = (
            plugins.activation_reminder_providers(only=activated)
            if plugins is not None
            else empty
        )
        session_pack_map = (
            plugins.activation_session_packs(only=activated)
            if plugins is not None
            else empty
        )
        control_tool_map = (
            plugins.activation_control_tools(only=activated)
            if plugins is not None
            else empty
        )
        tool_result_transforms: dict[str, tuple[Any, ...]] = {}
        extra_reminders: dict[str, tuple[ReminderSpec, ...]] = {}
        reminder_providers: dict[str, Mapping[str, tuple[Any, ...]]] = {}
        activated_content_kinds: dict[str, tuple[Any, ...]] = {}
        activated_session_packs: dict[str, tuple[Any, ...]] = {}
        activated_control_tools: dict[str, tuple[Any, ...]] = {}
        for _agent, _activation in agent_activations.items():
            _stages = _ordered_stages(transform_map, _activation)
            if _stages:
                tool_result_transforms[_agent] = tuple(v for _p, _pl, _n, v in _stages)
            _rems = _ordered_stages(reminder_map, _activation)
            if _rems:
                extra_reminders[_agent] = tuple(
                    ReminderSpec(name=_n, priority=_p, render=_v)
                    for _p, _pl, _n, _v in _rems
                )
            _seams = _seam_providers(provider_map, _activation)
            if _seams:
                reminder_providers[_agent] = _seams
            _packs = _ordered_stages(session_pack_map, _activation)
            if _packs:
                activated_session_packs[_agent] = tuple(
                    SessionPackEntry(name=_n, priority=_p, factory=_v)
                    for _p, _pl, _n, _v in _packs
                )
            _ctools = _ordered_stages(control_tool_map, _activation)
            if _ctools:
                activated_control_tools[_agent] = tuple(
                    ControlToolEntry(name=_n, priority=_p, factory=_v)
                    for _p, _pl, _n, _v in _ctools
                )
            _kinds = tuple(
                kind
                for _plugin in sorted(_activation)
                for kind in getattr(
                    (activation_map or {}).get(_plugin), "content_kinds", ()
                )
            )
            if _kinds:
                activated_content_kinds[_agent] = _kinds
        registry: AgentRegistry = AgentRegistry()
        registry.add(main_spec)
        for d in descendant_specs:
            registry.add(d)

        # 2. Collect custom tools (all nodes, including descendants and the
        #    activated plugins' contributions).
        custom_tools = _collect_custom_tools(options, activation_map)

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
        # D6 effect scoping: a loaded plugin's guard / observer contributions are
        # governance authority — in force process-wide for EVERY agent regardless
        # of which plugins that agent activates. Resolved here from the loaded set
        # and folded into the process guard stack + observer subscriptions.
        plugin_guards: tuple[Any, ...] = ()
        plugin_observers: tuple[Any, ...] = ()
        if plugins is not None:
            plugin_guards, plugin_observers = plugins.process_hooks()
        # (T3) — custom Observer
        # extension point: subscribe each user-supplied post-commit callback
        # alongside the defaults (and the process-wide plugin observers) and
        # collect their unsubscribes for shutdown.
        self._unsubscribe_observers: list[Callable[[], None]] = [
            event_log.subscribe(obs)
            for obs in tuple(options.observers) + plugin_observers
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
            policy_override=effective_policy,
            # D6: options.guards (agent wiring) + process-wide plugin guards.
            extra_guards=tuple(options.guards) + plugin_guards,
            extra_content_kinds=tuple(options.content_channels),
            # D9: per-agent tool_result_transform stages (empty ⇒ byte-identical).
            tool_result_transforms=tool_result_transforms,
            # The other three per-agent activation surfaces. All empty by default,
            # so a host with no plugins builds exactly the same engine as before.
            extra_reminders=extra_reminders,
            reminder_providers=reminder_providers,
            activated_content_kinds=activated_content_kinds,
            # Microkernel phase 3: external plugins' session packs, per agent —
            # appended after the built-in packs and interleaved by priority in
            # the kernel builder's generic loop. Empty ⇒ byte-identical.
            activated_session_packs=activated_session_packs,
            # Control-tool-surface S2: external plugins' control tools, per agent
            # — merged after the built-in control tools and re-sorted with the
            # kernel's internal entries in the builder's mount loop. Empty ⇒
            # byte-identical to the built-in-only session.
            activated_control_tools=activated_control_tools,
            # (D3) — host-level runtime
            # injections (NOT agent identity): the HTML-app preview gateway
            # (open_app) and the live-MCP alias resolver + transport. All default
            # to absent, so a bare HostConfig() leaves the tool list / wire
            # byte-identical to today.
            app_gateway=hc.app_gateway,
            write_roots=hc.write_roots,
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
            # Execution-tier per-session sandbox opt-out (D-C). ``None`` (default)
            # ⇒ provision as before; a policy returning False keeps a session on
            # the local backend even while a provider is configured.
            sandbox_policy=hc.sandbox_policy,
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
            # Per-session background concurrency caps (shell jobs / sub-agents).
            # Both default to 8, so a bare HostConfig() is unchanged.
            max_background_jobs_per_root_task=hc.max_background_jobs_per_root_task,
            max_background_subagents_per_root_task=(
                hc.max_background_subagents_per_root_task
            ),
            # Workspace instruction files: the root NOETA.md / AGENTS.md at
            # session start, and the subdirectory files discovered as the model
            # reads (anchored-content placement ADR). Both off by default; a
            # product opts in through HostConfig.
            instructions_enabled=hc.instructions_enabled,
            instructions_file=hc.instructions_file,
            instructions_discovery=hc.instructions_discovery,
            # Process fs write policy (D3 host config): "apply" performs real
            # writes, anything else stages a dry-run diff (the safe default).
            write_mode=(
                FsWriteMode.APPLY if hc.write_mode == "apply" else FsWriteMode.DRY_RUN
            ),
            # The one built-in disable a manifest contribution cannot express:
            # ``skills`` supplies no per-agent contribution, so dropping its
            # catalogue entry would otherwise leave the capability wired. Read
            # the RECORDED disable, never membership — ``builtins=False`` (the
            # usual "load only my own plugin" form) drops every built-in name
            # without meaning to drop any capability.
            skills_enabled=(
                plugins is None or "skills" not in plugins.disabled_builtins
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
        # config. Absent it, pass the SDK's DEFAULT_MODEL_ALLOWLIST (phase 2c:
        # the triple is a product default, injected — the kernel driver holds
        # no allowlist) → byte-identical to every pre-widening caller.
        self._driver: InteractionDriver = InteractionDriver(
            self._host,
            # Note: do not pass model_selector — let host.model become the
            # default naturally, avoiding allowlist friction.
            # default_model=None makes driver.__init__ fall back to host.model.
            default_model=None,
            # ``is not None``, not truthiness: an explicitly EMPTY
            # ``allowed_models`` means "no per-turn model selector is
            # authorized" (every selector rejected; ``None`` still binds the
            # host default). Falling back to the stub allowlist there would
            # silently widen a deliberate lockdown.
            model_allowlist=(
                frozenset(allowed_models)
                if allowed_models is not None
                else DEFAULT_MODEL_ALLOWLIST
            ),
            # Microkernel M2: the kernel driver holds no model catalog; the
            # friendly-alias table lives in the providers built-in and is
            # injected here (identity for non-alias selectors).
            alias_resolver=resolve_model_alias,
        )
        # Wire the driver back into the host as the background-completion
        # notifier (Mechanism C). The driver wraps the host, so the host cannot
        # construct it — we set it here, after construction. This activates the
        # turn-boundary completion push for BOTH a ``shell_run(background=true)``
        # job and a ``spawn_subagent(background=True)`` sub-agent: when one
        # finishes while the session is idle, the host's drive thread wakes the
        # session and injects an ``origin="system"`` notice. Called directly:
        # ``self._host`` is the SdkHost this constructor just built, so probing
        # for the seam with ``getattr`` only made a typo'd method name look
        # like a disabled feature.
        self._host.set_background_notifier(self._driver)
        # Crash recovery (docs/adr/background-subagent.md): now that the notifier
        # is wired, re-activate background sub-agents orphaned by a prior host
        # crash — re-drive any ``spawn_subagent(background=True)`` child with a
        # ``BackgroundSubagentStarted`` but no ``BackgroundSubagentDelivered``
        # (it resumes from its own EventLog), or re-deliver a terminal one whose
        # turn-boundary notice was lost. A one-shot startup side effect (never
        # resumed); an internal no-op for an in-memory ``query()`` Client (no
        # prior streams) and when the registry is unbuilt (no policy wrapper),
        # which the host itself handles — the caller needs no guard.
        self._host.recover_background_subagents()
        self._main_agent_name = main_spec.name
        self._registry = registry
        # can_use_tool callback (wiring-only, not part of the AgentSpec identity).
        # ``Options.can_use_tool`` now carries its real callable type, so this
        # is a plain assignment — it used to need a ``type: ignore`` purely
        # because the field was annotated ``object``.
        self._can_use_tool: Optional[Callable[[str, dict[str, Any]], bool]] = (
            options.can_use_tool
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
        activations: tuple[str, ...] = (),
    ) -> DriveOutcome:
        """Create a Task and drive the first turn (driver ``start``).

        ``agent`` defaults to the Options-compiled main spec's name
        (``"main"`` unless the recipe changed it). Passing a specific
        ``model_selector`` is subject to the deployment
        :data:`~noeta.client.client.DEFAULT_MODEL_ALLOWLIST`; to set a
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

        ``activations`` are built-in skill names to pin (pre-loop) for this
        turn — see :meth:`seed_start`. ``()`` keeps the start byte-identical to
        the no-skill path.
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
            activations=activations,
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
        activations: tuple[str, ...] = (),
    ) -> DriveOutcome:
        """Append a new user turn (driver ``send_goal``).

        ``images`` rides the appended user turn alongside the goal text
        (additive — empty keeps the append byte-identical to the text-only path).

        ``permission_mode`` / ``enabled_mcp`` are per-turn host knobs (see
        :meth:`start`); inert defaults keep this byte-identical to the bare path.

        ``effort`` is the per-turn, NON-durable reasoning-effort selector. No
        ``workspace_dir`` here: a follow-up turn fold-resolves the workspace the
        session was created with, so the workspace is bound once at
        :meth:`start` and never re-passed.

        ``activations`` are built-in skill names to pin (pre-loop) for this
        turn — see :meth:`seed_start`. This is the channel a mid-conversation
        ``/skill-name`` slash command rides; ``()`` keeps the append
        byte-identical to the no-skill path.
        """
        outcome = self._driver.send_goal(
            task_id=task_id,
            goal=goal,
            model_selector=model_selector,
            images=images,
            permission_mode=permission_mode,
            enabled_mcp=enabled_mcp,
            effort=effort,
            activations=activations,
        )
        return self._drain_approvals(task_id, outcome)

    def approve(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "client",
    ) -> DriveOutcome:
        """Approve a pending gated tool call (driver ``approve``).

        The resumed turn drains through ``can_use_tool`` like every other
        turn-driving verb: if it stops on a *further* gated call, the configured
        callback resolves that one too. Without the drain the same session
        behaved differently depending on which verb resumed it — auto-resolving
        after ``send_goal`` but stalling after ``approve``.
        """
        outcome = self._driver.approve(
            task_id=task_id, call_id=call_id, reason=reason, resolver=resolver
        )
        return self._drain_approvals(task_id, outcome)

    def deny(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "client",
    ) -> DriveOutcome:
        """Deny a pending gated tool call (driver ``deny``).

        The resumed turn drains through ``can_use_tool`` (see :meth:`approve`).
        """
        outcome = self._driver.deny(
            task_id=task_id, call_id=call_id, reason=reason, resolver=resolver
        )
        return self._drain_approvals(task_id, outcome)

    def answer(
        self,
        task_id: str,
        *,
        question_id: str,
        answers: dict[str, Any],
        answered_by: str = "client",
    ) -> DriveOutcome:
        """Answer a pending structured user question (driver ``answer``).

        The resumed turn drains through ``can_use_tool`` (see :meth:`approve`).
        """
        outcome = self._driver.answer(
            task_id=task_id,
            question_id=question_id,
            answers=answers,
            answered_by=answered_by,
        )
        return self._drain_approvals(task_id, outcome)

    def deliver_event(
        self,
        task_id: str,
        *,
        event_kind: str,
        payload: Any = None,
    ) -> DriveOutcome:
        """Deliver an external event to a ``wait_external``-suspended task
        (driver ``deliver_event``).

        Wakes a task suspended on ``ExternalEvent(event_kind)`` and drives the
        resumed turn. ``payload`` (an optional JSON value) rides the resumed
        turn as an ``origin="system"`` message — never the wake event itself.
        A task not waiting on this ``event_kind`` (including a repeat delivery
        after the wake was consumed) raises the typed ``NotResumableError``.

        The resumed turn drains through ``can_use_tool`` (see :meth:`approve`).
        """
        outcome = self._driver.deliver_event(
            task_id=task_id, event_kind=event_kind, payload=payload
        )
        return self._drain_approvals(task_id, outcome)

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
        activations: tuple[str, ...] = (),
    ) -> SeededTurn:
        """Create + validate + lease a first turn WITHOUT driving it
        (driver ``seed_start``); pass the result to :meth:`drive_seeded`.

        ``activations`` are built-in skill names to pin (pre-loop) for this
        task — the same forced-preload channel a ``/skill-name`` slash command
        uses (``TaskStatePatched(activate_skills=…)``). A thin forward to the
        driver's existing ``activations`` parameter; ``()`` keeps the seed
        byte-identical to the no-skill path."""
        return self._driver.seed_start(
            goal=goal,
            agent=agent if agent is not None else self._main_agent_name,
            model_selector=model_selector,
            images=images,
            permission_mode=permission_mode,
            enabled_mcp=enabled_mcp,
            workspace_dir=workspace_dir,
            effort=effort,
            activations=activations,
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
        activations: tuple[str, ...] = (),
    ) -> SeededTurn:
        """Validate + seed a follow-up user turn WITHOUT driving it
        (driver ``seed_send_goal``).

        ``activations`` are built-in skill names to pin (pre-loop) for this
        turn — see :meth:`seed_start`. This is the async-transport counterpart
        of :meth:`send_goal`'s activations, i.e. the path a product's HTTP
        command endpoint uses for a mid-conversation ``/skill-name``."""
        return self._driver.seed_send_goal(
            task_id=task_id,
            goal=goal,
            model_selector=model_selector,
            images=images,
            permission_mode=permission_mode,
            enabled_mcp=enabled_mcp,
            effort=effort,
            activations=activations,
        )

    def seed_approve(
        self,
        task_id: str,
        *,
        call_id: str,
        reason: Optional[str] = None,
        resolver: str = "client",
    ) -> SeededTurn:
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
    ) -> SeededTurn:
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
    ) -> SeededTurn:
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
    ) -> SeededTurn:
        """Validate + seed an external-event resume turn (driver
        ``seed_deliver_event``)."""
        return self._driver.seed_deliver_event(
            task_id, event_kind=event_kind, payload=payload
        )

    def drive_seeded(self, seeded: SeededTurn) -> DriveOutcome:
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
    ) -> DriveOutcome:
        """Cancel a conversation (driver ``cancel``)."""
        return self._driver.cancel(task_id=task_id, reason=reason, cascade=cascade)

    def interrupt(
        self,
        task_id: str,
        *,
        reason: Optional[str] = None,
        interrupted_by: str = "user",
    ) -> DriveOutcome:
        """Stop an in-flight turn, keeping the conversation live (driver
        ``interrupt``).

        The middle ground between :meth:`cancel` (kills the conversation) and
        :meth:`close` (archives it): the turn stops at its next boundary and
        the task lands back on its next-goal suspend, so a following
        :meth:`send_goal` just continues. The interrupted turn's events stay on
        the stream as history — use :meth:`rewind` to discard them.

        Safe to call from another thread while a turn is being driven: the
        cancel registry it marks is thread-safe, and the Engine polls it at
        every turn boundary.
        """
        return self._driver.interrupt(
            task_id=task_id, reason=reason, interrupted_by=interrupted_by
        )

    def close(
        self,
        task_id: str,
        *,
        closed_by: str = "user",
        reason: Optional[str] = None,
    ) -> DriveOutcome:
        """Close / archive a conversation (driver ``close``)."""
        return self._driver.close(task_id=task_id, closed_by=closed_by, reason=reason)

    def rewind(self, task_id: str, *, message_seq: int) -> DriveOutcome:
        """Rewind the conversation to before the user message at ``message_seq``
        (driver ``rewind``).

        ``message_seq`` is the seq of the user-goal ``MessagesAppended`` being
        undone — the bubble the user clicked "undo" on. That message, the
        output it triggered and every later turn become dead history (nothing
        is deleted: a re-base marker names a new fold baseline, append-only
        intact), and workspace files the undone span edited are restored.

        The conversation lands back at the turn boundary before that message
        and is immediately live. See :meth:`fork` to keep both branches instead.
        """
        return self._driver.rewind(task_id, message_seq=message_seq)

    def fork(self, task_id: str, *, message_seq: int) -> DriveOutcome:
        """Branch the conversation into a new task at ``message_seq`` (driver
        ``fork``).

        Same anchor as :meth:`rewind`, opposite retention: instead of re-basing
        this conversation, it mints a **new** task that inherits the history up
        to the turn boundary before ``message_seq`` and leaves the source
        untouched. "Edit that message and try again, keeping both."

        The returned ``DriveOutcome.task_id`` is the **fork's** — it rests at a
        next-goal suspend, so :meth:`send_goal` on it drives the new branch.

        Both branches share the source's workspace: fork branches the
        conversation, not the files on disk. Only a root task can be forked.
        """
        return self._driver.fork(task_id, message_seq=message_seq)

    def reopen(
        self,
        task_id: str,
        *,
        reopened_by: str = "user",
        reason: Optional[str] = None,
    ) -> DriveOutcome:
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

    def delete_task(self, task_id: str) -> DeleteTaskResult:
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
        # ``purge_task`` stays ``getattr``-probed: the event log and dispatcher
        # are Protocol-typed INJECTIONS, and a storage backend legitimately may
        # not implement a hard purge. The host accelerators below are not —
        # they are methods on the SdkHost this Client built, so they are called
        # directly.
        #
        # In-memory host accelerators keyed by task/session id (per-turn
        # carriers, retained background-shell job handles, background sub-agent
        # tracking) are NOT storage, so the storage purge above leaves them
        # resident — a leak for the lifetime of the process across many deleted
        # conversations. Reclaim them here too, per subtree target (each seam
        # no-ops for a tid it holds nothing for).
        for tid in targets:
            if callable(purge_events):
                purge_events(tid)
            if callable(purge_disp):
                purge_disp(tid)
            self._host.forget_turn_carriers(tid)
            self._host.purge_background_shells(tid)
            self._host.forget_background_subagents(tid)
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

        On a **timeout** the pool state is deliberately NOT cleared: the
        stragglers are still running and still hold their leases, so forgetting
        them would let the next :meth:`start_workers` stack a second pool on top
        of the first (double the workers, both pulling the same ready queue) and
        would leave the survivors unjoinable. Returning False with the pool still
        tracked keeps a retry — ``stop_workers`` again — able to finish the job.
        """
        if not self._workers_started:
            return True
        for loop in self._worker_loops:
            loop.stop()
        deadline = None if timeout is None else (time.monotonic() + timeout)
        for th in self._worker_threads:
            remaining = None
            if deadline is not None:
                remaining = max(0.0, deadline - time.monotonic())
            th.join(timeout=remaining)
        all_joined = all(not t.is_alive() for t in self._worker_threads)
        if not all_joined:
            # Keep the pool tracked (see above) — the loops are already stopped,
            # so a later retry only has to re-join.
            return False
        self._worker_loops.clear()
        self._worker_threads.clear()
        self._workers_started = False
        return True

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

    # -- context manager ---------------------------------------------------

    def __enter__(self) -> "Client":
        """Enter a ``with`` block; the client is already fully constructed."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Always :meth:`shutdown` on the way out — including on an exception.

        ``shutdown`` is what unsubscribes observers, stops the worker pool and
        reaps a sandbox container, so leaking it leaks a container per Client.
        Never suppresses the exception (returns ``None``).
        """
        self.shutdown()

    def shutdown(self) -> None:
        """Stop resident workers (if any), then unsubscribe observers.

        Idempotent, so it is safe to call explicitly inside a ``with`` block.
        Does **not** explicitly close in-memory stores (they are
        process-owned).
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
            try:
                # Unsubscribes, drains the async worker, flushes the sink.
                self._trace_export.stop()
            except Exception:
                # A dead OTLP endpoint must not abort shutdown: the sandbox
                # teardown below is the step that actually releases a remote
                # container, and letting an exporter flush failure skip it
                # leaked a live container per Client.
                pass
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

    def __repr__(self) -> str:
        """A compact summary, not the whole envelope stream.

        ``QueryResult`` *is* a ``list[EventEnvelope]``, so the inherited repr
        dumps every envelope — unreadable at a REPL, which is exactly where a
        one-shot result gets printed. This reports what the caller actually
        wants to see; the stream is still there by iteration.
        """
        state = "failed" if self._failure is not None else "completed"
        return (
            f"QueryResult(task_id={self.task_id!r}, status={state!r}, "
            f"events={len(self)}, messages={len(self._view)})"
        )


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
    plugins: Optional[PluginSet] = None,
    host_config: Optional[HostConfig] = None,
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

    Parameters match the ``Client`` constructor + a ``goal`` string —
    including ``host_config``, so the sugar path is **not** limited to
    in-memory storage: ``query(..., host_config=HostConfig(storage_path=
    "noeta.sqlite"))`` records the run durably, and the same one-shot call can
    opt into any other host wiring (preview gateway, MCP resolver, memory
    roots). Callers who need multi-turn interactions (``send_goal`` /
    ``approve`` / …) or access to the compiled registry should instantiate
    ``Client`` directly instead of going through ``query``.
    """
    client = Client(
        options,
        provider=provider,
        workspace_dir=workspace_dir,
        model=model,
        multi_turn=False,
        plugins=plugins,
        host_config=host_config,
    )
    try:
        outcome = client.start(goal=goal, images=images)
        return _materialize_query_result(client, outcome)
    finally:
        client.shutdown()
