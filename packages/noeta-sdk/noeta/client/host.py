"""SDK-side resident host.

:class:`SdkHost` implements the
:class:`~noeta.execution.host.ResidentHost` Protocol via the
:class:`~noeta.execution.resolver.GenericEngineResolver` skeleton. It owns
the canonical engine-construction path for SDK callers: compiled
:class:`~noeta.agent.spec.AgentSpec` s → per-(agent, model, ask) Engines
with catalog pricing and the deterministic tool pack the generic
:func:`~noeta.execution.builder.build_session_inputs` produces.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from noeta.agent.registry import AgentRegistry, UnknownAgentError
from noeta.agent.spec import AgentSpec, BudgetSpec, ToolRef, agent_activates
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.protocols.canonical import to_canonical_bytes
from noeta.context.content_channel import ContentKindSpec
from noeta.context.reminders import ReminderSpec
from noeta.execution.builder import build_session_inputs
from noeta.execution.host import AgentRegistryProtocol
from noeta.execution.reminders import TURN_INTAKE
from noeta.execution.resolver import GenericEngineResolver
from noeta.runtime.governance import (
    Budget,
    PreToolUseRule,
    RepetitionAction,
    SkillEnforcementMode,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.dispatcher import Dispatcher
from noeta.protocols.event_log import EventLogFull
from noeta.protocols.events import (
    McpProvenanceRecordedPayload,
    McpServerSkippedPayload,
)
from noeta.protocols.hooks import Guard
from noeta.protocols.messages import LLMProvider, StreamDelta, Usage
from noeta.protocols.policy import Policy
from noeta.protocols.step_context import StepContext
from noeta.protocols.tool import Tool
from noeta.protocols.values import ContentRef
from noeta.execution.background_delivery import BackgroundDelivery, DeliverFn
from noeta.execution.background_subagent import (
    DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_ROOT_TASK,
    BackgroundSubagentRegistry,
)
from noeta.runtime.background_shell import (
    DEFAULT_MAX_BACKGROUND_JOBS_PER_ROOT_TASK,
    ProcessRegistry,
)
from noeta.runtime.cancellation import CancellationRegistry
from noeta.runtime.injection import InjectionInbox
from noeta.runtime.file_checkpoint import FileCheckpointRegistry
from noeta.client.parts import (
    browser_tool_names,
    catalog_price,
    mcp_impl,
    default_control_tools,
    default_guards_factory,
    default_policy_factory,
    default_session_packs,
    react_impl,
    default_reminder_specs,
    default_shell_rules,
    derive_compaction_config,
    memory_impl,
    provider_family,
)
from noeta.client.host_config import SandboxExecEnvConfig
from noeta.client.sandbox import (
    BackendFactory,
    BrowserBackendFactory,
    SandboxExecEnvManager,
    provider_for_config,
)
from noeta.client.sandbox_provider import SandboxProvider, SandboxSpec
from noeta.client.host_config import AppPreviewGateway
from noeta.runtime.llm import RuntimeLLMClient
from noeta.runtime.exec_env import ExecEnv
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.runtime.shell_policy import (
    build_allowlist,
    command_in_allowlist,
    load_project_shell_allowlist,
)
from noeta.runtime.mcp import HttpPostFn, McpAnyServerSpec


__all__ = ["SdkHost"]

_log = logging.getLogger(__name__)

#: Upper bound on the in-process Engine cache shared by all sessions on this
#: host. LRU eviction (OrderedDict.popitem(last=False)) keeps the pool from
#: growing without bound in long-lived server processes.
_MAX_CACHED_ENGINES: int = 256


#: An ``OrderedDict`` for the Engine LRU that also REAPS the live MCP clients
#: an evicted Engine owns. ``_build_engine`` connects MCP servers (each an
#: :class:`McpStdioClient` subprocess / an :class:`McpHttpClient`) and those
#: clients are retained only via the cached Engine's tools; the plain
#: ``popitem(last=False)`` eviction in :meth:`GenericEngineResolver._engine_for_agent`
#: drops the Engine and would orphan the subprocess + leak its fds. That eviction
#: site lives in the base resolver, so the cache *value* carries its clients:
#: ``_build_engine`` stages them on the host via
#: :meth:`SdkHost._stage_mcp_clients`; ``__setitem__`` adopts the staged list for
#: the new key, and every removal path (``__delitem__`` / ``pop`` / ``popitem``)
#: calls ``client.shutdown()`` (idempotent, never raises) on the removed entry's
#: clients. One bad client can't break eviction: shutdown is swallowed + logged.
class _McpReapingEngineCache(OrderedDict):  # type: ignore[type-arg]
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # key → live MCP clients owned by that key's Engine.
        self._clients_by_key: dict[Any, list[Any]] = {}
        # Set by the host right before ``__setitem__`` runs (the base resolver
        # assigns ``self._engines[key] = engine`` straight after ``_build_engine``
        # returns). Consumed-and-cleared on adoption. THREAD-LOCAL: with the
        # per-key build locks, Engine builds for different keys run concurrently;
        # stage → adopt pairs on the build thread (the build and its put always
        # run on the same thread), so concurrent builds cannot adopt each other's
        # clients.
        self._staging = threading.local()

    def stage(self, clients: list[Any]) -> None:
        self._staging.pending = list(clients)

    def discard_staged(self) -> None:
        """Reap + clear anything staged on THIS thread that was never adopted.

        Staging pairs with adoption on the build thread, so a pending list still
        sitting here means its build never reached the cache put — the clients
        are orphaned and must be shut down, not merely dropped (an
        ``McpStdioClient`` drop leaks its subprocess + fds for the life of the
        process)."""
        pending = getattr(self._staging, "pending", [])
        self._staging.pending = []
        if pending:
            self._reap(pending)

    def _reap(self, clients: list[Any]) -> None:
        for client in clients:
            try:
                client.shutdown()
            except Exception:  # noqa: BLE001 — one bad client can't break eviction
                _log.warning("MCP client shutdown failed on eviction", exc_info=True)

    def __setitem__(self, key: Any, value: Any) -> None:
        pending = getattr(self._staging, "pending", [])
        self._staging.pending = []
        # Replacing an existing key (rare — only an identical key rebuild): reap
        # the old key's clients first so they aren't orphaned by the overwrite.
        if key in self._clients_by_key:
            self._reap(self._clients_by_key.pop(key))
        if pending:
            self._clients_by_key[key] = pending
        super().__setitem__(key, value)

    def __delitem__(self, key: Any) -> None:
        clients = self._clients_by_key.pop(key, None)
        super().__delitem__(key)
        if clients:
            self._reap(clients)

    def popitem(self, last: bool = True) -> Any:
        key, value = super().popitem(last=last)
        clients = self._clients_by_key.pop(key, None)
        if clients:
            self._reap(clients)
        return key, value

    def pop(self, key: Any, *args: Any) -> Any:
        had = key in self
        value = super().pop(key, *args)
        if had:
            clients = self._clients_by_key.pop(key, None)
            if clients:
                self._reap(clients)
        return value

    def clear(self) -> None:
        all_clients = list(self._clients_by_key.values())
        self._clients_by_key.clear()
        super().clear()
        for clients in all_clients:
            self._reap(clients)


#: The synthetic registry name a single convenience ``provider`` is folded under
#: in :meth:`SdkHost.__post_init__`. On the single-provider path a bound
#: ``ModelBound`` never carries a provider name (the driver passes no selector),
#: so this name is purely an internal table key — it never enters a durable write.
_SINGLE_PROVIDER_NAME = "default"


# ---------------------------------------------------------------------------
# Pure helpers — permission_mode → require_approval_tools (unit-testable)
# ---------------------------------------------------------------------------

#: The "edit" tool names (``edit`` / ``write`` / ``apply_patch``) that
#: ``acceptEdits`` exempts from the default non-low risk gate. One canonical
#: module-level set so tests and permission-mode logic refer to the same list.
_EDIT_TOOL_NAMES: frozenset[str] = frozenset({"edit", "write", "apply_patch"})


def _make_shell_approval_predicate(
    rules: tuple[Any, ...],
) -> Callable[[str, Mapping[str, Any]], bool]:
    """Build the per-call shell gate: ``True`` ⇒ this shell_run needs approval.

    A ``shell_run`` whose command is already in ``rules`` (the effective
    allowlist) runs silently; anything else (or a malformed command) is gated.
    Non-shell tools are never affected.
    """

    def _needs_approval(tool_name: str, arguments: Mapping[str, Any]) -> bool:
        if tool_name != "shell_run":
            return False
        command = arguments.get("command")
        if not isinstance(command, str) or not command.strip():
            return True
        return not command_in_allowlist(command, rules)

    return _needs_approval


def _approval_set_for(mode: str, tool_refs: Sequence[ToolRef]) -> tuple[str, ...]:
    """Return a sorted tuple of tool names to gate via ``require_approval_tools``.

    Pure function — no runtime access, no side effects — so unit tests can
    exercise every ``permission_mode`` directly without spinning up a
    :class:`SdkHost`.

    Modes
    -----
    ``"default"``
        Every tool whose declared ``risk_level != "low"``.
    ``"acceptEdits"``
        Same rule as ``default`` but the three edit-class tools
        (``edit`` / ``write`` / ``apply_patch``) are exempted
        even when declared high-risk.
    ``"bypassPermissions"``
        Empty — no tool is gated.
    """
    if mode == "bypassPermissions":
        return ()
    refs = list(tool_refs)
    if mode == "default":
        names = sorted({r.name for r in refs if r.risk_level != "low"})
        return tuple(names)
    if mode == "acceptEdits":
        names = sorted(
            {
                r.name
                for r in refs
                if r.risk_level != "low" and r.name not in _EDIT_TOOL_NAMES
            }
        )
        return tuple(names)
    # Guard: caller must have validated permission_mode against the
    # Options._PERMISSION_MODES set before calling here.
    raise ValueError(f"Unsupported permission_mode: {mode!r}")


# ---------------------------------------------------------------------------
# Catalog-pricing callback. If catalog.price's KeyError semantics change, edit
# only this function.
# ---------------------------------------------------------------------------


def _catalog_pricing(model: str, usage: Usage) -> float:
    """Price one LLM round-trip from the sdk catalog; unknown models → 0.0.

    The KeyError-or-zero policy:
      1. Any model not in the catalog (stub-model, an unpriced real id) counts
         as 0.0. This lives here in the code layer, not in catalog.price (which
         raises loudly to surface a priced model someone typed wrong) and not in
         RuntimeLLMClient (which stays provider-neutral, seeing only the injected
         callback).
      2. cost is written into the event body; resume reads back the value
         recorded at the time — so updating the price table never rewrites
         historical recordings.
    """
    try:
        return catalog_price(model, usage)
    except KeyError:
        return 0.0


def _spec_write_path_globs(spec: AgentSpec) -> tuple[str, ...]:
    """Read a spec's restricted-write path whitelist.

    A spec may carry ``metadata["write_path_globs"]`` (e.g. ``"plans/*.md"``);
    metadata is excluded from the AgentSpec identity — a host-binding hint. The
    SdkHost forwards it into ``build_session_inputs`` so that spec's ``write`` tool
    is built path-restricted (physically confined to the whitelisted paths), while
    every other agent (no such metadata) keeps the unrestricted ``write``. The
    value is a comma-separated glob list; ``()`` ⇒ unrestricted.
    """
    raw = spec.metadata.get("write_path_globs")
    if not raw:
        return ()
    return tuple(p.strip() for p in raw.split(",") if p.strip())


@dataclass
class SdkHost(GenericEngineResolver):
    """SDK-side :class:`ResidentHost` over a compiled :class:`AgentRegistry`.

    The three GenericEngineResolver seams are implemented as:

    * :meth:`_lookup_agent` — thin wrapper over the wired
      :class:`AgentRegistry`, re-raising :class:`UnknownAgentError` with
      the caller-supplied ``task_id`` so resolver-level error messages
      carry the task context.
    * :meth:`_spawnable_set` — filters a spec's declared ``spawnable`` to
      names actually present in the registry (so a stale spec reference
      that was never registered cannot resolve).
    * :meth:`_build_engine` — wires the generic
      :func:`~noeta.execution.builder.build_session_inputs` with a catalog-priced
      :class:`RuntimeLLMClient`. ``custom_tools`` is filtered to only
      those whose names appear in the agent's declared ``spec.tools``
      (the spec is the identity authority — a tool closure supplied but
      never referenced is silently dropped rather than leaking into the
      composer schema).

    Parameters
    ----------
    event_log, content_store, dispatcher:
        The L0 triple (typically in-memory instances created by
        :class:`~noeta.client.client.Client`).
    provider:
        The single-provider **convenience** input
        (oneshot / lifecycle / tests / ``Client``). Given it,
        :meth:`__post_init__` folds it into a one-entry ``providers`` table
        named :data:`_SINGLE_PROVIDER_NAME` + a same-named ``default_provider``.
        Mutually exclusive with ``providers`` (supply exactly one).
    providers:
        The provider **registry**: a name→instance table
        the agent layer built (each name a configured ``(adapter, base_url,
        key, models)`` provider). A session binds a provider **name** (folded
        into the model binding); the resolver looks it up here for the bound
        Engine. ``default_provider`` names the one bound when no per-turn
        provider selector is given. A multi-provider deployment passes this +
        ``default_provider`` and leaves ``provider=None``.
    default_provider:
        The provider name bound when no per-turn provider selector is given —
        the deployment's own default, not caller input. Must be a key of
        ``providers`` (on the single-provider convenience path ``__post_init__``
        pins it to :data:`_SINGLE_PROVIDER_NAME`).
    model:
        Host-fixed default model id; used when no per-turn selector is
        given. Recorded as the opening ``ModelBound`` in
        :meth:`InteractionDriver.start`.
    workspace_dir:
        Root passed to every Engine's fs-tool pack and skill loader.
    registry:
        Compiled specs (main + descendants); ``_lookup_agent`` reads
        straight out of here.
    custom_tools:
        Run-time closures keyed by the tool name the spec's
        :class:`~noeta.agent.spec.ToolRef` references. The ``AgentSpec.tools``
        identity list controls which closures are actually included — not the
        dict's keyset.
    delegation_allowed:
        Host kill-switch; when ``False`` an agent's own ``"delegation"``
        activation is masked off.
    policy_wrapper:
        Applied to every Engine's policy. ``None`` ⇒ one-shot behaviour
        (``query``); :func:`~noeta.execution.driver.multi_turn_policy_wrapper`
        ⇒ interactive sessions suspend on the next-goal handle (``Client``).
    unnamed_fallback:
        Passed to the skeleton so a recording labelled ``agent_name="unnamed"``
        can still resolve — always ``None`` for SDK callers, but the field is
        here for skeleton parity.
    """

    event_log: EventLogFull
    content_store: ContentStore
    dispatcher: Dispatcher
    # ``provider`` (single instance) is the convenience input for single-provider
    # callers (oneshot / lifecycle / tests + Client) — given it, ``__post_init__``
    # folds it into a one-entry ``providers`` table + a same-named
    # ``default_provider`` (the name is :data:`_SINGLE_PROVIDER_NAME`). A
    # multi-provider deployment passes the ``providers`` table + ``default_provider``
    # and leaves ``provider=None``. Supply exactly one (both / neither hard-errors
    # in ``__post_init__``).
    provider: Optional[LLMProvider] = None
    model: str = "stub-model"
    workspace_dir: Path = field(default_factory=Path.cwd)
    registry: AgentRegistry = field(default_factory=AgentRegistry)
    custom_tools: dict[str, Tool] = field(default_factory=dict)
    delegation_allowed: bool = True
    #: Host kill-switch for the ``run_workflow`` control tool.
    #: Default off; the deployment opts in (``HostConfig.workflow_allowed``).
    #: When on, agents expose run_workflow and the reserved ``__workflow__``
    #: orchestration child resolves to :class:`OrchestrationPolicy`.
    workflow_allowed: bool = False
    policy_wrapper: Optional[Callable[[Policy], Policy]] = None
    unnamed_fallback: Optional[Any] = None
    permission_mode: str = "default"
    # Max steps per ReActPolicy turn; the SDK host default matches the coding
    # budget's max_iterations so a long session doesn't hit the inner ReAct cap
    # first.
    max_steps: int = 200
    # Filesystem write mode; DRY_RUN is the safe default.
    write_mode: FsWriteMode = FsWriteMode.DRY_RUN
    # shell_run allowlist / allow-all switch; ALLOWLIST by default, matching the
    # build_session_inputs default.
    shell_mode: ShellMode = ShellMode.ALLOWLIST
    # Operator shell rules added on top of the built-in safe allowlist (extend
    # semantics); each like {"program": "npm", "subcommand": "start"}. Empty =
    # built-in defaults only (git/pytest/npm test etc.). Effective only when
    # shell_mode=ALLOWLIST; ignored under ARBITRARY/OFF.
    shell_allowlist: Sequence[Mapping[str, Any]] = ()
    # Per-session background-job concurrency cap. Over the cap, ``ProcessRegistry``
    # **rejects** (does not queue) a ``shell_run(background)`` spawn. Injected into
    # the process registry built in ``__post_init__`` below.
    max_background_jobs_per_root_task: int = DEFAULT_MAX_BACKGROUND_JOBS_PER_ROOT_TASK
    # Per-session background SUB-AGENT concurrency cap
    # (docs/adr/background-subagent.md). Over the cap, a
    # ``spawn_subagent(background=True)`` is rejected (not queued) before any
    # durable write.
    max_background_subagents_per_root_task: int = (
        DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_ROOT_TASK
    )
    #: Whether the ``skills`` built-in is wired at all. ``False`` (the host
    #: honouring ``load_plugins(disabled_builtins=["skills"])``) passes NO
    #: ``skills_factory`` to the kernel builder: nothing is indexed, no skill
    #: content kind is registered, no skill tool is grown, and the script
    #: wiring stays empty. Unlike ``react``, skills has a defined empty state —
    #: the agent simply has none — so the disable is real rather than refused.
    #: The other skills fields below become inert when this is ``False``.
    skills_enabled: bool = True
    # Skills dir overriding workspace_dir/.noeta/skills (workspace-local tier);
    # None uses the default load location.
    skills_dir: Optional[Path] = None
    # Lower-priority skill tiers below the workspace-local tier, ordered
    # built-in < global (workspace-local always wins). The product layer passes
    # the built-in tier (the SDK doesn't know noeta-agent's BUILTIN_SKILLS_DIR);
    # global_skills_dir defaults to None ⇒ no global tier. Empty = workspace-local
    # tier only.
    builtin_skills_dirs: Tuple[Path, ...] = ()
    global_skills_dir: Optional[Path] = None
    # Explicit memory-dir override; None uses global_memory_dir / the SDK global
    # default. Memory is pinned to one global directory (it does not drift with the
    # per-session workspace); the memory switch reads the spec's ``"memory"``
    # activation (the activation tuple is the source of truth, same discipline as
    # skill_invocation).
    memory_dir: Optional[Path] = None
    # Global memory root; defaults to the SDK's ~/.noeta/memories. An explicit
    # memory_dir override takes priority over this field.
    global_memory_dir: Optional[Path] = None
    # Per-task memory-root resolution seam: given a task id, the host-injected
    # callable returns that task's memory root, or ``None`` to fall back to the
    # ``memory_dir`` > ``global_memory_dir`` > default chain. Lets a multi-tenant
    # product split the store per tenant while the SDK stays tenancy-agnostic (it
    # hands over task ids, never users). Must be cheap, total, and deterministic
    # per task id — a resumed task must resolve the same store. ``None`` ⇒ the
    # host-level chain.
    memory_root_resolver: Optional[Callable[[str], Optional[Path]]] = None
    #: Project-instructions-file switch. Like memory, this is workspace environment
    #: material (not agent identity), so the activation tuple carries no flag and
    #: SdkHost configures it directly. When True, looks for NOETA.md → AGENTS.md in
    #: order; an explicit instructions_file override reads only that path.
    #: Missing/empty file = no accounting (no instructions event).
    instructions_enabled: bool = False
    instructions_file: Optional[Path] = None
    #: `read`-triggered discovery of subdirectory ``NOETA.md``/``AGENTS.md`` files
    #: (docs/adr/anchored-content-placement.md). Like ``instructions_enabled`` this
    #: is workspace environment material, not agent identity. When True, a
    #: successful ``read`` inside the workspace activates the instruction files
    #: between the read file's directory and the workspace root; they render
    #: anchored at the point of discovery.
    instructions_discovery: bool = False
    # Skill-tool enforcement level (off/warn/enforce); "off" ⇒ no intervention.
    skill_tool_enforcement: SkillEnforcementMode = "off"
    # Whether to load script-style run_skill_script tools under .noeta/skills;
    # off by default.
    allow_skill_scripts: bool = False
    # Alias resolver for remote/local MCP servers (product-injected). Given an
    # enabled alias, returns its full spec (incl. url/credentials) from the
    # host-side config store, or None (unconfigured/skip). The SDK does not hold
    # the config store (import-linter ``sdk-not-agent`` forbids the SDK depending
    # back on noeta-agent); credentials live only in the product layer's store.
    # This is a **callback**: each turn it resolves the enabled aliases into
    # connectable specs, then the SDK's ``build_mcp_tools`` connects them. ``None``
    # ⇒ no enabled alias resolves to a spec ⇒ no live MCP is connected.
    mcp_server_resolver: Optional[Callable[[str], Optional["McpAnyServerSpec"]]] = None
    # Injectable HTTP POST transport for the remote MCP client. Tests pass a fake
    # (a local stub server) so list/call run without real network; production
    # leaves it ``None`` (the client uses stdlib ``urllib``). Pure wiring — never
    # recorded.
    mcp_http_post: Optional[HttpPostFn] = None
    # HookGuard rules for the pre-tool-use phase; an empty tuple registers no
    # extra hook.
    hooks_pre_tool_use: tuple[PreToolUseRule, ...] = ()
    # Repeated-action detection threshold; 0 disables it.
    repetition_threshold: int = 0
    # Action taken when repetition_threshold is exceeded; defaults to
    # "require_approval", matching the guard default.
    repetition_action: RepetitionAction = "require_approval"
    # Repetition-detection sliding window size; defaults to 8, matching the
    # RepetitionPolicy default.
    repetition_window: int = 8
    # Session-level budget override; None derives from AgentSpec.default_budget.
    budget: Optional[Budget] = None
    # Explicit approval-required tool names; when None, derived from
    # permission_mode — an explicit value wins.
    require_approval_tools: Optional[tuple[str, ...]] = None
    # Map from a recording name → canonical registered name (e.g.
    # {"default": "main"}); used for resuming recordings and product-facing alias
    # support.
    aliases: Mapping[str, str] = field(default_factory=dict)
    # Wiring-only LLM request overrides. Excluded from the AgentSpec identity;
    # forwarded into every in-session LLMRequest via the policy.
    output_schema: Optional[dict[str, Any]] = None
    thinking: Optional[str] = None
    effort: Optional[str] = None
    # Positive int or None; engine-level inline char cap for tool output. None = no
    # truncation. A resumed session must reuse the value the original run used, or
    # it re-derives different tool-output bytes.
    tool_output_inline_limit: Optional[int] = None
    # SDK Options extension points threaded into every Engine this host builds. All
    # default to inert values; the SDK host feeds the SAME values to the live and
    # resume paths (single _build_engine call site), so a resumed turn rebuilds the
    # identical policy / guard stack / content layout by construction.
    #   * policy_override: a custom decision-policy factory ``(llm) -> Policy``
    #     that fully replaces ReActPolicy (``None`` ⇒ ReAct).
    #   * extra_guards: custom Guards registered after the built-in stack.
    #   * extra_content_kinds: custom ContentKindSpec channels appended after
    #     the built-in residents (the ONLY composer extension seam).
    policy_override: Optional[Callable[[Any], Policy]] = None
    #: Host authorization for out-of-workspace writes (HostConfig.write_roots):
    #: ``task_id -> extra writable directories``, forwarded verbatim into the
    #: fs pack. ``None`` ⇒ the single-root wall.
    write_roots: Optional[Callable[[str], Sequence[str]]] = None
    extra_guards: tuple[Guard, ...] = ()
    extra_content_kinds: tuple[ContentKindSpec, ...] = ()
    #: Per-agent ``tool_result_transform`` stages — ``agent name -> ordered
    #: (priority, plugin, name) ``ToolResult -> ToolResult`` callables. A
    #: wiring-plane surface that follows per-agent activation: the Client resolves
    #: the map from the activated plugin set, and ``_build_engine`` picks the stages
    #: for the agent it is building. Empty default ⇒ the untransformed result is
    #: recorded.
    tool_result_transforms: Mapping[str, tuple[Callable[[Any], Any], ...]] = field(
        default_factory=dict
    )
    #: Per-agent compose-time reminders — ``agent name -> ReminderSpec`` s,
    #: appended after the three built-ins in that agent's composer reminder
    #: registry and interleaved by priority. Same per-agent activation scoping as
    #: ``tool_result_transforms``.
    extra_reminders: Mapping[str, tuple[ReminderSpec, ...]] = field(
        default_factory=dict
    )
    #: Per-agent recorded reminder providers — ``agent name -> seam name ->
    #: ordered providers``. Read by the recording path through
    #: :meth:`intake_reminder_providers`; a provider's output is *recorded*, so
    #: resume folds it back from the ledger and never re-invokes it.
    reminder_providers: Mapping[str, Mapping[str, tuple[Any, ...]]] = field(
        default_factory=dict
    )
    #: Per-agent content-kind residents contributed by activated plugins — the
    #: activation-scoped sibling of ``extra_content_kinds`` (which is host-wide,
    #: carrying ``Options.content_channels``). Both are appended after the
    #: built-in residents; this one only for the agent that activated the plugin.
    activated_content_kinds: Mapping[str, tuple[ContentKindSpec, ...]] = field(
        default_factory=dict
    )
    #: Per-agent session packs contributed by activated external plugins — appended
    #: after the built-in packs and interleaved by priority in the kernel builder's
    #: generic loop.
    activated_session_packs: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    #: Per-agent control tools contributed by activated external plugins — merged
    #: after the built-in control tools and re-sorted by ``(priority, name)`` with
    #: the internal entries in the builder's mount loop.
    activated_control_tools: Mapping[str, tuple[Any, ...]] = field(default_factory=dict)
    # The agent-layer base pool root for **bare sessions** (no workspace_id). The
    # agent layer does ``mkdir <workspace_base>/session-<uuid>`` and passes the
    # resulting absolute path as ``workspace_dir`` to the driver. ``None`` ⇒ no
    # base pool: every session uses the host-fixed ``workspace_dir``. Named
    # workspaces are arbitrary paths in the agent-layer registry, not subdirs of
    # ``workspace_base``.
    workspace_base: Optional[Path] = None
    # The provider **registry**: a name→instance table. A multi-provider deployment
    # passes it directly (+ ``default_provider``); a single-provider caller leaves
    # it empty and passes the convenience ``provider`` single instance instead,
    # which ``__post_init__`` folds into a one-entry table. After folding,
    # ``providers`` is always non-empty and ``default_provider`` is always one of
    # its keys (resolver / ``_provider_for`` read this folded table and never touch
    # the ``provider`` convenience field).
    providers: Mapping[str, LLMProvider] = field(default_factory=dict)
    # Default provider name: bound when no per-turn provider selector is given. On
    # the single-provider convenience path ``__post_init__`` pins it to
    # :data:`_SINGLE_PROVIDER_NAME`; a multi-provider deployment sets it explicitly.
    default_provider: str = ""
    # The provider→model-list table; the half read by the (provider, model) pair
    # legality check: a session may bind ``model`` only on a pair where
    # ``model ∈ provider_models[name]``. The agent layer builds this table from the
    # provider registry; empty default ⇒ no restriction (the single-provider path
    # passes nothing, so the driver does no pair check).
    provider_models: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Optional per-LLM-call HTTP headers derived from the current StepContext.
    # Product hosts use this for deployment gateway correlation headers; the SDK
    # default is None, so generic callers and resume fixtures keep plain provider
    # calls.
    provider_headers: Optional[Callable[[StepContext], Mapping[str, str]]] = None
    # Token-streaming sink (host wiring, like ``provider_headers`` — never part of
    # agent identity): forwarded into every session's RuntimeLLMClient so a
    # streaming-capable provider's in-flight deltas reach the product's delta hub.
    # ``None`` ⇒ deltas are never emitted.
    delta_sink: Optional[Callable[[StepContext, str, StreamDelta], None]] = None
    # The host's live HTML-app preview gateway. A runtime injection (like
    # ``provider_headers`` / ``_process_registry``), NOT part of the host identity.
    # When set, ``_build_engine`` threads it into ``build_session_inputs`` so the
    # agent gets the ``open_app`` tool. ``None`` ⇒ no open_app.
    app_gateway: Optional[AppPreviewGateway] = None
    # Sandbox execution backend addressing. ``None`` ⇒ the local host
    # (``LocalExecEnv``). When set, ``__post_init__`` builds a
    # ``SandboxExecEnvManager`` and ``_build_engine`` routes every session's fs /
    # shell IO into the container (the tool schemas — and thus the stable prefix —
    # are unchanged). A host runtime injection (like ``app_gateway``), never part
    # of any agent identity. ``exec_env`` attaches one shared container;
    # ``sandbox_provider`` + ``sandbox_spec`` provision a fresh container per
    # root-task tree and take precedence over ``exec_env`` when both are set.
    exec_env: Optional[SandboxExecEnvConfig] = None
    sandbox_provider: Optional[SandboxProvider] = None
    sandbox_spec: Optional[SandboxSpec] = None
    # Per-session shell preamble source ``(exec_env_ref, argv) -> prefix``,
    # threaded into the ``SandboxExecEnvManager``: minted fresh per container exec
    # so a product can inject per-user credentials that expire mid-session. A host
    # runtime injection, never recorded; ``None`` ⇒ no preamble.
    sandbox_exec_preamble: Optional[Callable[[str, Sequence[str]], str]] = None
    # Optional per-session backend factories, threaded into the
    # ``SandboxExecEnvManager``. ``None`` ⇒ the SDK defaults (``AioSandboxExecEnv``
    # / ``AioBrowserBackend``). The product injects these to swap in an alternative
    # wire without touching the seam; the adapters implement the same ``ExecEnv`` /
    # ``BrowserBackend`` surface, so the tool schemas — and the stable prefix — are
    # unchanged.
    sandbox_backend_factory: Optional[BackendFactory] = None
    sandbox_browser_factory: Optional[BrowserBackendFactory] = None
    # Per-session sandbox opt-out (execution tiers): ``(root_task_id,
    # workspace_dir) -> provision?``. Consulted at the top of ``allocate_exec_env``;
    # ``False`` ⇒ this session gets NO container (return ``None``), so the driver
    # records no ``exec_env_ref`` and the build falls back to ``LocalExecEnv`` +
    # the host ``WorkspaceRoot`` fence — the ``local`` execution tier, reachable
    # even while a sandbox provider is configured for other sessions. ``None`` ⇒
    # every session provisions when a provider is present. A host runtime injection,
    # never part of any agent identity.
    sandbox_policy: Optional[Callable[[str, Optional[str]], bool]] = None
    # The cache key has a ``workspace`` dimension (the bound **absolute path**, or
    # ``None`` for the host default) and a ``provider`` dimension — so two sessions
    # on different directories or providers never share an Engine. Bounded LRU via
    # OrderedDict (cap = _MAX_CACHED_ENGINES) + a threading Lock to serialise
    # get-or-build-put under ThreadingHTTPServer concurrency.
    _engines: OrderedDict[
        tuple[
            str,
            str,
            bool,
            Optional[str],
            Optional[str],
            Optional[str],
            tuple[str, ...],
            Optional[str],
            Optional[str],
        ],
        Engine,
    ] = field(
        default_factory=_McpReapingEngineCache, init=False, repr=False, compare=False
    )
    _engines_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )
    # Per-key Engine-build locks (see
    # ``GenericEngineResolver._engine_for_agent``): builds run outside the global
    # ``_engines_lock`` so one session's slow/hanging MCP connect does not
    # serialise every other Engine build.
    _engine_builds: dict[Any, threading.Lock] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Per-session permission_mode is a NON-durable, per-turn knob — the frontend
    # sends it each turn; it is never written to the event log. The async HTTP
    # transport seeds a turn on the request thread but resolves the Engine later on
    # a background thread, so the per-turn mode is stashed here keyed by task_id
    # (set by the driver before resolution, read in ``resolve_engine`` to key +
    # build the Engine). Single writer per task (turns are serial under the
    # dispatcher lease); overwritten each turn, never evicted (one short string per
    # task) so a turn that suspends on approval still resolves the same mode when
    # it resumes.
    _turn_permission_mode: dict[str, Optional[str]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Per-turn, NON-durable enabled-MCP-alias carrier keyed by task_id. Mirrors
    # ``_turn_permission_mode``: the driver records the turn's enabled aliases
    # (clean list, no url/token) here before the Engine is resolved;
    # ``resolve_engine`` reads it to thread the aliases into the cache key +
    # ``_build_engine`` (which resolves each alias → spec via ``mcp_server_resolver``
    # → live MCP tools). Never written to the event log.
    _turn_mcp_aliases: dict[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Per-turn, NON-durable reasoning-effort override. Same carrier pattern as
    # permission_mode / enabled_mcp: the driver records the selector before the
    # turn resolves its Engine; resolver reads it into the cache key and policy.
    _turn_effort: dict[str, Optional[str]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # cancel-cascade — process-local registry of cancelled root task ids. The
    # driver's ``cancel`` marks the root here (alongside the durable
    # ``TaskCancelled`` event); ``drive_pending_subtasks`` polls it per tree so an
    # in-flight child abandons its result at the next turn boundary. Per-host
    # singleton; never written to the event log → no resume effect.
    _cancellation: CancellationRegistry = field(
        default_factory=CancellationRegistry, init=False, repr=False, compare=False
    )
    # mid-turn injection — process-local inbox of pending injections keyed by
    # task id (mirrors ``_cancellation``). The driver's ``inject_goal`` submits
    # here alongside the durable ``InjectionRequested`` event; the worker's drain
    # reads it each turn boundary. Per-host singleton; never written to the event
    # log → no resume effect (the durable ``InjectionRequested`` is the record).
    _injection_inbox: InjectionInbox = field(
        default_factory=InjectionInbox, init=False, repr=False, compare=False
    )
    # Background-shell process registry: a per-host runtime accelerator (mirrors
    # ``_cancellation``) owning live ``Popen`` handles + watcher threads for
    # ``shell_run(background=true)``. Constructed in ``__post_init__`` because it
    # needs the host's shared event_log + content_store; never written to the event
    # log (the BackgroundShell* events are the durable record) → no resume effect.
    # Injected into every built Engine so the background shell tools reach it.
    _process_registry: Optional[ProcessRegistry] = field(
        default=None, init=False, repr=False, compare=False
    )
    # Background SUB-AGENT registry (docs/adr/background-subagent.md): a per-host
    # runtime accelerator (mirrors ``_process_registry``) holding the live drive
    # futures + per-session cap for ``spawn_subagent(background=True)``. Constructed
    # in ``__post_init__`` (it needs the host's L0 triple + the resolver's
    # ``_build_drain_host``); never written to the event log (the
    # ``BackgroundSubagent*`` events are the durable record) → no resume effect.
    # Threaded into a top-level interactive Engine as the launch+capacity seam.
    _background_subagents: Optional[BackgroundSubagentRegistry] = field(
        default=None, init=False, repr=False, compare=False
    )
    # Per-turn file-checkpoint gate: a per-host runtime accelerator (mirrors
    # ``_cancellation`` / ``_process_registry``) recording "which workspace files
    # already have a rewind baseline stashed THIS turn", keyed by the session root
    # task id. Needs no event_log/content_store (it is a pure in-memory path set),
    # so a plain default_factory suffices. Injected into every built Engine so an AI
    # ``edit`` / ``write`` stashes its baseline; never written to the event log (the
    # ``file_baselines`` on ``ToolResultRecorded`` are the durable record) → no
    # resume effect.
    _file_checkpoint: FileCheckpointRegistry = field(
        default_factory=FileCheckpointRegistry,
        init=False,
        repr=False,
        compare=False,
    )
    # Sandbox backend lifecycle: built in ``__post_init__`` ONLY when a sandbox is
    # configured (``None`` otherwise ⇒ the local host). Owns the host's shared
    # ``AioSandboxExecEnv`` and its teardown seam; ``_build_engine`` pulls the
    # backend from it. A runtime accelerator like ``_process_registry`` — never
    # written to the event log.
    _sandbox: Optional[SandboxExecEnvManager] = field(
        default=None, init=False, repr=False, compare=False
    )
    # The shared background-delivery glue (docs/adr/background-subagent.md): the
    # daemon-thread hop + parent-fold terminal check + mid-turn-deferral retry loop
    # that BOTH the background-shell and background-sub-agent exit paths funnel
    # through. Built in ``__post_init__`` on the host's shared L0 read seams; the
    # completion notifier (an ``InteractionDriver``) is wired late via
    # :meth:`set_background_notifier` (the driver wraps this host, so the host can't
    # construct it). ``None`` notifier ⇒ no push: a background exit still records
    # its durable event, but drives no wake-and-notify turn.
    _delivery: BackgroundDelivery = field(init=False, repr=False, compare=False)
    # Pending woken-prelude inbox for the resident worker pool. When a ``seed_*``
    # method returns a SeededTurn with a non-durable prelude (ResolveApprovalPrelude),
    # the client stashes it here before yielding the seed's lease back to the ready
    # queue; a worker pops it at the start of its step and threads it into
    # ``run_leased_task`` so the prelude runs on the worker thread (not the HTTP
    # request thread). Process-local: a crash between seed and worker pick-up loses
    # the prelude, the same benign loss mode the per-command drive thread has
    # (re-approve).
    _pending_preludes: dict[str, Any] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _pending_preludes_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        """Fold the single-provider convenience field into a ``providers`` table.

        Supply exactly one: given the convenience ``provider`` single instance,
        fold it into a one-entry ``providers`` table + a same-named
        ``default_provider``; a multi-provider deployment passes the ``providers``
        table + ``default_provider`` instead (leaving ``provider`` as ``None``).
        Both / neither hard-errors. After folding, downstream (the resolver cache
        key, ``_provider_for``, the read-only ``provider`` property) sees only
        ``providers`` / ``default_provider``.
        """
        if self.provider is not None:
            if self.providers:
                raise ValueError(
                    "SdkHost: pass EITHER provider (single, convenience) OR "
                    "providers (registry table) — not both"
                )
            self.providers = {_SINGLE_PROVIDER_NAME: self.provider}
            self.default_provider = _SINGLE_PROVIDER_NAME
        elif not self.providers:
            raise ValueError(
                "SdkHost: one of provider (single) or providers (registry "
                "table) must be supplied"
            )
        elif not self.default_provider:
            raise ValueError(
                "SdkHost: a providers registry requires a default_provider name"
            )
        if self.default_provider not in self.providers:
            raise ValueError(
                f"SdkHost: default_provider {self.default_provider!r} is not a "
                f"key of the providers registry {sorted(self.providers)!r}"
            )
        # Build the shared background-delivery glue first: both the
        # background-shell and background-sub-agent exit hooks route their
        # completion push through it. It stays inert (records the durable exit,
        # drives no turn) until the product wires a notifier via
        # :meth:`set_background_notifier`.
        self._delivery = BackgroundDelivery(
            event_log=self.event_log, content_store=self.content_store
        )
        # Build the background-shell registry on the host's shared L0 triple
        # (lazily here, not a default_factory, because it needs event_log +
        # content_store). The dispatcher is the wake seam; the host's own
        # ``_on_background_exit`` hook pushes a job that exits while the session is
        # idle back as a next-goal notice. The hook hands off to a daemon drive
        # thread; it is inert until the product wires a notifier via
        # :meth:`set_background_notifier`.
        self._process_registry = ProcessRegistry(
            event_log=self.event_log,
            content_store=self.content_store,
            # Per-session concurrency cap. Reject (not queue) over the cap.
            max_jobs_per_root_task=self.max_background_jobs_per_root_task,
            dispatcher=self.dispatcher,
            on_background_exit=self._on_background_exit,
        )
        # Background sub-agent registry (docs/adr/background-subagent.md). Mirrors
        # the process registry: built here on the shared L0 triple. The
        # ``build_host`` callback hands it the resolver's ``_build_drain_host`` so
        # it drives one background child on the same delegation host the foreground
        # drain uses; ``deliver`` fires once a child reaches terminal. Inert until
        # ``set_background_notifier`` wires a driver.
        self._background_subagents = BackgroundSubagentRegistry(
            event_log=self.event_log,
            content_store=self.content_store,
            dispatcher=self.dispatcher,
            build_host=self._drain_host_for_id,
            deliver=self._on_background_subagent_exit,
            max_per_root_task=self.max_background_subagents_per_root_task,
        )
        # Sandbox backend lifecycle: only when the host configured one. Absent it,
        # ``_sandbox`` stays ``None`` and ``_build_engine`` runs the local host.
        # A ``sandbox_provider`` (per-session provisioning) wins over the
        # ``exec_env`` attach config; the manager drives whichever provider.
        if self.sandbox_provider is not None:
            self._sandbox = SandboxExecEnvManager(
                self.sandbox_provider,
                spec_template=self.sandbox_spec or SandboxSpec(image=""),
                exec_preamble=self.sandbox_exec_preamble,
                backend_factory=self.sandbox_backend_factory,
                browser_factory=self.sandbox_browser_factory,
            )
        elif self.exec_env is not None:
            self._sandbox = SandboxExecEnvManager(
                provider_for_config(self.exec_env),
                spec_template=SandboxSpec(image=""),
                default_workdir=self.exec_env.workdir,
                # The attach path has ONE shared container: a build that carries no
                # session-welded ref still targets it, unlike the per-session
                # provisioning path.
                default_ref=self.exec_env.base_url,
                exec_preamble=self.sandbox_exec_preamble,
                backend_factory=self.sandbox_backend_factory,
                browser_factory=self.sandbox_browser_factory,
            )

    # -- sandbox backend lifecycle -----------------------------------------

    def allocate_exec_env(
        self, root_task_id: str, workspace: Optional[str] = None
    ) -> Optional[str]:
        """Provision this session's container → the ``exec_env_ref`` to weld.

        Called eagerly at ``driver.seed_start`` (which pre-mints ``root_task_id``
        so the container is keyed by the root task id). The provider builds a FRESH
        container mounting ``workspace`` (the session's host workspace path) and the
        fixed skills mounts; the returned ``"{base_url}#{sandbox_id}"`` ref is
        recorded on ``TaskHostBound`` so a resumed / reclaimed session — even on
        another host — reconnects to the SAME container. ``None`` on the local path
        (no sandbox configured). Addressing only — the API key rides on the wire."""
        if self._sandbox is None:
            return None
        # Execution-tier opt-out: a per-session policy may decline a container for
        # THIS session even when a provider is configured. ``False`` ⇒ no ref
        # recorded ⇒ the build falls back to ``LocalExecEnv`` (the ``local`` tier).
        if self.sandbox_policy is not None and not self.sandbox_policy(
            root_task_id, workspace
        ):
            return None
        return self._sandbox.allocate(root_task_id, host_workspace=workspace)

    def exec_env_for_ref(
        self, exec_env_ref: Optional[str]
    ) -> Optional[tuple[ExecEnv, Path]]:
        """The ``(backend, container root)`` for a bound ref, or ``None``.

        ``None`` (a local session, or no host sandbox) means "act against the host
        filesystem" — the driver keeps its pathlib path (the rewind restore). When
        a sandbox session recorded an ``exec_env_ref`` and this host has a sandbox,
        returns the container backend + its lexical root, reconnecting to the
        recorded container by address (creds from this host's env)."""
        if self._sandbox is None or not exec_env_ref:
            return None
        backend, workdir = self._sandbox.resolve(exec_env_ref)
        return (backend, Path(workdir))

    def release_exec_env(self, root_task_id: str) -> None:
        """Tear down a session's container at its root-task terminal.

        Idempotent; a no-op on the local path (no manager) or for a
        ``root_task_id`` that never allocated (a subtask, or a non-sandbox
        session). The driver calls this when a ROOT task reaches a terminal; the
        process-shutdown backstop is :meth:`teardown_exec_env`."""
        if self._sandbox is not None:
            self._sandbox.release(root_task_id)

    def teardown_exec_env(self) -> None:
        """Reap every still-open session container (if any). Idempotent; safe on
        the local path (no manager ⇒ no-op). The Client calls this on shutdown so
        no container outlives the process — the backstop for interactive sessions
        that rest at ``suspended`` and never reach a root terminal."""
        if self._sandbox is not None:
            self._sandbox.teardown()

    def add_sandbox_lifecycle_listener(
        self,
        on_allocate: Any,
        on_release: Any,
    ) -> None:
        """Register product-side ``(on_allocate, on_release)`` listeners.

        ``on_allocate(root_id, handle)`` fires after a container is provisioned;
        ``on_release(root_id)`` fires before teardown. Safe on the local path
        (no sandbox manager ⇒ no-op). Used by the product backend to wire
        sandbox preview mounts and similar lifecycle-tracked side effects."""
        if self._sandbox is not None:
            self._sandbox.add_lifecycle_listener(on_allocate, on_release)

    # -- file-checkpoint per-turn gate reset ----------------

    def reset_file_checkpoint_turn(self, root_task_id: str) -> None:
        """Clear the per-turn rewind-baseline gate at a turn boundary.

        "clear each turn": the driver calls this when a NEW user goal opens
        a turn (``start`` / ``send_goal``) so the next turn re-stashes a fresh
        baseline for any file it touches — which is what lets a rewind restore to
        ANY turn boundary. Idempotent; a never-edited root is a clean no-op."""
        self._file_checkpoint.reset_turn(root_task_id)

    # -- pending woken-prelude inbox (worker-pool handoff) -------------

    def put_pending_prelude(self, task_id: str, prelude: Any) -> None:
        """Stash a non-durable woken prelude (ResolveApprovalPrelude) for
        ``task_id`` so a resident worker can apply it between note_woken
        and run_one_step. One prelude per task (a second stashed prelude
        for the same task replaces the first)."""
        with self._pending_preludes_lock:
            self._pending_preludes[task_id] = prelude

    def take_pending_prelude(self, task_id: str) -> Any:
        """Pop and return the stashed prelude for ``task_id``, or ``None``."""
        with self._pending_preludes_lock:
            return self._pending_preludes.pop(task_id, None)

    # -- background-shell emergency-stop ---------------

    def kill_background_shells(self, root_task_id: str) -> list[Any]:
        """Human emergency-stop — kill ALL background jobs of one session.

        The control-plane ``cancel`` (and the session-close cascade) call this so
        a cancelled / closed conversation does not leave its long-running
        ``shell_run(background)`` processes orphaned. Reuses the ``ProcessRegistry``
        per-job kill primitive (``kill_root_task`` → SIGTERM → SIGKILL per job; the
        watchers reap + record ``BackgroundShellKilled``). Safe no-op when the
        registry is unbuilt (returns ``[]``)."""
        registry = self._process_registry
        if registry is None:
            return []
        return registry.kill_root_task(root_task_id)

    def purge_background_shells(self, root_task_id: str) -> None:
        """Drop a *deleted* session's retained job handles (memory reclaim).

        Called by ``Client.delete_task`` when a conversation is hard-deleted.
        Unlike ``kill_background_shells`` (which reaps the OS processes but
        keeps handles pollable for a still-inspectable closed conversation),
        this reclaims the handles that would otherwise leak for the process
        lifetime. Safe no-op when the registry is unbuilt."""
        registry = self._process_registry
        if registry is not None:
            registry.purge_root_task(root_task_id)

    # -- background-shell crash recovery ---------------

    def recover_background_orphans(self) -> list[str]:
        """Reap orphan background jobs at host startup.

        A host crash/restart loses the in-memory ``ProcessRegistry``; the OS
        processes it spawned are reparented to ``init`` (orphans) and the event
        log holds their ``BackgroundShellStarted`` with NO terminal. This scans
        the persisted streams (via the event log's task index) and, per orphan,
        emits the MANDATORY ``BackgroundShellLost`` so the read model / model
        stop showing it as forever-"running", plus a CONSERVATIVE best-effort PID
        kill (only when the recorded PID's identity can be verified — never an
        unverified / reused PID). Called ONCE on the live SSE product's startup
        (``build_code_server``); it is a startup side effect, never resumed.
        Safe no-op when the registry is unbuilt (returns ``[]``). Returns the
        ``job_id``s newly marked Lost."""
        registry = self._process_registry
        if registry is None:
            return []
        return registry.recover_orphans()

    # -- background-shell completion push --------------

    def set_background_notifier(self, notifier: Any) -> None:
        """Wire the :class:`InteractionDriver` that drives a completion notice.

        Called by the product AFTER it builds the driver over this host (the
        driver wraps the host, so the host cannot construct it). Until set, a
        background exit records its durable event (``BackgroundShellExited`` /
        the child terminal) but drives no wake-and-notify turn (oneshot /
        lifecycle / tests). Idempotent. Both background paths share the one
        :class:`~noeta.execution.background_delivery.BackgroundDelivery`."""
        self._delivery.set_notifier(notifier)

    def _on_background_exit(
        self, root_task_id: str, job_id: str, summary: str, ref: ContentRef
    ) -> None:
        """ProcessRegistry watcher hook — project a shell exit, hand it to delivery.

        Runs on the watcher's daemon thread, so it MUST NOT block:
        :meth:`BackgroundDelivery.on_exit` spawns the drive thread and returns at
        once. The shell notice rides a pointer (``summary`` + the final
        :class:`ContentRef` + the ``job_id`` the model can ``shell_poll``); no
        stream read is needed, so the projection just closes over them."""

        def _plan() -> DeliverFn:
            def _deliver(notifier: Any) -> None:
                notifier.notify_background_exit(
                    root_task_id, summary=summary, ref=ref, job_id=job_id
                )

            return _deliver

        self._delivery.on_exit(
            task_id=root_task_id,
            plan=_plan,
            thread_name=f"noeta-bg-notify-{job_id}",
        )

    # -- background sub-agent (docs/adr/background-subagent.md) -------------

    def _drain_host_for_id(self, parent_task_id: str) -> Any:
        """Build the delegation :class:`DrainHost` for a parent id (registry seam).

        The registry hands ``launch`` / ``recover`` a parent id; the resolver
        owns ``_build_drain_host`` (which takes a folded Task), so this folds and
        delegates. Read-only over the parent's stream — safe to call while the
        parent holds its own lease (the launch happens mid-turn)."""
        parent_task = fold(self.event_log, self.content_store, parent_task_id)
        return self._build_drain_host(parent_task)

    def recover_background_subagents(self) -> list[str]:
        """Re-drive / re-deliver background sub-agents orphaned by a crash.

        Mirrors :meth:`recover_background_orphans` for the sub-agent path: scans
        persisted streams for a ``BackgroundSubagentStarted`` with no matching
        ``BackgroundSubagentDelivered`` and re-drives a non-terminal child (it
        resumes from its own EventLog) or re-delivers a terminal one whose notice
        was lost. Startup side effect, never resumed. Safe no-op when the
        registry is unbuilt. Returns the recovered child ids."""
        registry = self._background_subagents
        if registry is None:
            return []
        return registry.recover()

    def forget_background_subagents(self, root_task_id: str) -> None:
        """Drop a session's background sub-agent tracking (cancel/close cascade).

        The in-flight drives are torn down cooperatively by the cancel registry
        (``request_cancellation`` → the ``DrainHost.cancel_check`` each child step
        polls); this frees the per-session cap table so a reopened session starts
        clean. Safe no-op when the registry is unbuilt."""
        registry = self._background_subagents
        if registry is not None:
            registry.forget_root_task(root_task_id)

    def _on_background_subagent_exit(
        self, parent_task_id: str, child_task_id: str
    ) -> None:
        """Registry deliver hook — project a finished child, hand it to delivery.

        Mirroring :meth:`_on_background_exit`. Runs on the executor worker (the
        drive's done-callback), so it MUST NOT block:
        :meth:`BackgroundDelivery.on_exit` spawns the drive thread. The projection
        reads the child's REAL terminal from its own EventLog and dereferences its
        result into an inlined notice (:meth:`_background_subagent_result`); it runs
        ONCE on the delivery thread (not the retry loop) and returns ``None`` for a
        cancelled child (session teardown — nothing to push)."""

        def _plan() -> Optional[DeliverFn]:
            result = self._background_subagent_result(child_task_id)
            if result is None:
                return None  # cancelled / nothing to deliver
            status, ref, summary = result

            def _deliver(notifier: Any) -> None:
                notifier.notify_background_subagent_exit(
                    parent_task_id,
                    subtask_id=child_task_id,
                    summary=summary,
                    ref=ref,
                    status=status,
                )

            return _deliver

        self._delivery.on_exit(
            task_id=parent_task_id,
            plan=_plan,
            thread_name=f"noeta-bg-subagent-notify-{child_task_id}",
        )

    def _background_subagent_result(
        self, child_task_id: str
    ) -> Optional[Tuple[str, ContentRef, str]]:
        """Project a background child's terminal into ``(status, ref, summary)``.

        ``ref`` is a ContentStore snapshot of the full result (dereferenced and
        inlined into the delivery notice); ``summary`` is the one-line notice
        header. Returns ``None`` for a
        cancelled child (session teardown — nothing to push). A child whose drive
        ended without a terminal (it suspended on an unsupported mid-flight
        interaction — background children have no human to answer) is reported as
        a ``failed`` delivery so the parent is not left waiting silently."""
        agent_name = "sub-agent"
        terminal_type: Optional[str] = None
        answer: Any = None
        answer_ref: Optional[ContentRef] = None
        reason: Optional[str] = None
        for env in self.event_log.read(child_task_id):
            if env.type == "TaskCreated":
                agent_name = getattr(env.payload, "agent_name", agent_name)
            elif env.type == "TaskCompleted":
                terminal_type = "completed"
                answer = getattr(env.payload, "answer", None)
                answer_ref = getattr(env.payload, "answer_ref", None)
            elif env.type == "TaskFailed":
                terminal_type = "failed"
                reason = getattr(env.payload, "reason", None)
            elif env.type == "TaskCancelled":
                return None
        if terminal_type == "completed":
            ref = answer_ref or self.content_store.put(
                to_canonical_bytes(answer if answer is not None else ""),
                media_type="application/json",
            )
            return (
                "completed",
                ref,
                f'Background sub-agent "{agent_name}" finished. Here is its result:',
            )
        # failed, or no terminal (stuck on an unsupported suspend).
        detail = reason or (
            "the sub-agent stopped without completing (it needed an interaction "
            "that is unavailable to a background agent)"
        )
        ref = self.content_store.put(
            to_canonical_bytes(detail), media_type="application/json"
        )
        return (
            "failed",
            ref,
            # Summary stays a clean one-liner; the full ``detail`` is the ref
            # body, inlined into the notice below it — don't repeat it here.
            f'Background sub-agent "{agent_name}" did not complete.',
        )

    # -- ResidentHost requirement ------------------------------------------

    @property
    def agent_registry(self) -> AgentRegistryProtocol:
        """Satisfy :class:`ResidentHost.agent_registry` (structural seam)."""
        return self.registry

    # -- provider registry ------------------------------

    @property
    def default_provider_instance(self) -> LLMProvider:
        """The host default provider instance (single-provider read).

        After ``provider`` (single instance) folds into the ``providers`` table,
        this accessor returns the **default** provider instance from the folded
        table — a stable entry point for readers that care only about a single
        provider. ``_build_engine`` goes through :meth:`_provider_for` to fetch the
        instance by bound name, not this.
        """
        return self.providers[self.default_provider]

    def _provider_for(self, name: Optional[str]) -> LLMProvider:
        """Resolve a bound provider **name** → its instance.

        ``None`` (no provider bound — a recording, or a turn that only switched the
        model) ⇒ the host :attr:`default_provider`. The driver/server validated the
        ``(provider, model)`` pair against this same registry *before* any durable
        write, so a bound name is always a configured key here.

        Resume fallback: if the name is non-empty but not found in the current
        providers table (a resume / trimmed host may not have configured the
        secondary provider used at recording time), fall back to the default
        provider rather than raising KeyError.
        """
        if not name:
            return self.providers[self.default_provider]
        provider = self.providers.get(name)
        if provider is None:
            # A resume / trimmed host may not have configured the secondary
            # provider used at recording time — fall back to default, not KeyError.
            return self.providers[self.default_provider]
        return provider

    def models_for_provider(self, name: str) -> tuple[str, ...]:
        """The model list configured for provider ``name``.

        The legality half of a ``(provider, model)`` selector: a session may
        bind ``model`` only when it is in this provider's declared list. The
        agent layer populates :attr:`provider_models`; an unconfigured provider
        name returns the empty tuple (so the pair check rejects it loud)."""
        return tuple(self.provider_models.get(name, ()))

    # -- three resolver seams ----------------------------------------------

    def _lookup_agent(self, name: str, *, task_id: str) -> AgentSpec:
        # Map a recording name (e.g. "default") to the canonical registered name
        # (e.g. "main"). A name not in aliases passes through unchanged.
        name = self.aliases.get(name, name)
        try:
            return self.registry.resolve(name)
        except UnknownAgentError as exc:
            # skeleton contract: must carry task_id and the sorted available set
            raise UnknownAgentError(
                agent_name=exc.agent_name,
                available=self.registry.names(),
                task_id=task_id,
            ) from exc

    def _spawnable_set(self, spawnable: Any) -> frozenset[str]:
        return frozenset(n for n in spawnable if n in self.registry)

    def _resolve_live_mcp_tools(
        self, mcp_aliases: tuple[str, ...], *, task_id: Optional[str] = None
    ) -> Optional[dict[str, Any]]:
        """Connect the turn's enabled MCP servers → live tools.

        Resolve each enabled **alias** to its host-side server spec via the
        product-injected :attr:`mcp_server_resolver` (the SDK never holds the
        config store — credentials stay product-side), then connect them all with
        :func:`noeta.tools.mcp.build_mcp_tools` and return the discovered
        ``mcp__{alias}__{tool}`` ``McpTool`` dict. Aliases are sorted (alias
        alphabetical order → tool-name order inside ``build_mcp_tools``), so the
        tool dict order → schema order → stable hash is reproducible.

        Connection lifecycle: this runs once per BUILT Engine (the resolver caches
        on the cache-key tuple incl. ``mcp_aliases``), so the connect + the tool-set
        freeze happen exactly once at task start, never mid-turn. Failure is
        skip-on-failure: one enabled server that cannot connect / handshake /
        ``tools/list`` is dropped, recorded as one ``McpServerSkipped`` observer
        event on ``task_id``'s stream, and the build continues with the surviving
        servers' tools — a single bad connector never sinks the task. The alias is a
        clean name; no url/token ever enters the event.

        Returns ``None`` when no live MCP tools resulted — empty aliases, no
        resolver wired, no alias resolved to a spec, OR every server was skipped —
        so the caller passes ``None`` as ``mcp_tools_override`` and the builder
        merges no MCP tools (resume passes empty aliases and so never reaches here).
        A returned dict takes the live override path.
        """
        # Reap any clients staged by a prior build that never reached the cache put
        # (so a no-MCP build can never adopt stale clients, and an orphaned stdio
        # subprocess is shut down rather than dropped). The cache consumes-and-clears
        # on each ``__setitem__``; this guards the rare build-without-put path (e.g.
        # an exception after staging).
        self._discard_staged_mcp_clients()
        resolver = self.mcp_server_resolver
        if not mcp_aliases or resolver is None:
            return None
        specs: list[McpAnyServerSpec] = []
        for alias in sorted(set(mcp_aliases)):
            spec = resolver(alias)
            if spec is not None:
                specs.append(spec)
        if not specs:
            return None
        # Record the per-task MCP provenance (enabled aliases + tool subsets, names
        # only, NO credentials) the moment we know which servers resolved, BEFORE
        # the connect (so a server that then fails to connect — and gets a
        # McpServerSkipped — still shows in the provenance as "enabled this run").
        # Emitted in the pre-loop window (resolve_engine runs before the first step),
        # origin observer, so the fold rebuilds the same GovernanceState.mcp_provenance
        # from the event on resume. Only on the live connect path (task_id present);
        # the seed/by-name build passes task_id=None and never reaches here, and
        # resume passes empty aliases.
        if task_id:
            self.event_log.system_emit(
                task_id=task_id,
                type="McpProvenanceRecorded",
                payload=McpProvenanceRecordedPayload(
                    servers=mcp_impl().mcp_provenance_from_specs(specs)
                ),
                actor="mcp",
                origin="observer",
            )
        tools, clients, skipped = mcp_impl().build_mcp_tools(
            tuple(specs), http_post=self.mcp_http_post, skip_on_failure=True
        )
        # Stage the live clients so the engine cache adopts them when the base
        # resolver puts the just-built Engine (``self._engines[key] = engine``), and
        # shuts them down when that Engine is evicted from the LRU. Without this the
        # McpStdioClient subprocess + its fds would leak on eviction.
        self._stage_mcp_clients(clients)
        # Record one observer event per skipped server (front-end surface + audit
        # trail). Only possible once the task exists; the seed/by-name build passes
        # ``task_id=None`` and never connects MCP, so a skip without a task_id is not
        # reachable on the live path. Defensive: only emit when we have a stream to
        # write to.
        if skipped and task_id:
            for skip in skipped:
                self.event_log.system_emit(
                    task_id=task_id,
                    type="McpServerSkipped",
                    payload=McpServerSkippedPayload(
                        alias=skip.alias, reason=skip.reason
                    ),
                    actor="mcp",
                    origin="observer",
                )
        if not tools:
            return None
        # The connected clients are retained by the live ``McpTool`` objects (each
        # holds its client); the cached Engine owns those tools for the session's
        # life, and the staged clients (above) are shut down by the engine cache
        # when that Engine is evicted from the LRU.
        return dict(tools)

    def _stage_mcp_clients(self, clients: list[Any]) -> None:
        """Hand the just-connected MCP clients to the engine cache so it adopts
        them on the next ``self._engines[key] = engine`` put and reaps them on
        eviction. A no-op for a plain ``OrderedDict`` cache (e.g. a test that
        swapped it out), so the staging contract is best-effort."""
        stage = getattr(self._engines, "stage", None)
        if stage is not None:
            stage(clients)

    def _discard_staged_mcp_clients(self) -> None:
        """Shut down + clear staged-but-never-adopted MCP clients (see
        :meth:`_McpReapingEngineCache.discard_staged`). Best-effort, like
        :meth:`_stage_mcp_clients`: a swapped-in plain cache has nothing to
        discard."""
        discard = getattr(self._engines, "discard_staged", None)
        if discard is not None:
            discard()

    def _build_engine(self, *args: Any, **kwargs: Any) -> Engine:
        """Build this turn's Engine, never leaking a connected MCP client.

        :meth:`_assemble_engine` connects the turn's enabled MCP servers partway
        through and *stages* the live clients for the engine cache to adopt on
        the put that follows a successful return. Every step after that connect
        can still raise (a bad session-inputs build, an unknown provider, a
        failing policy factory) — and the base resolver then never puts, so the
        staged clients would be adopted by nobody: an ``McpStdioClient``
        subprocess and its fds would survive, per failed build, for the life of
        the process. Reaping them here makes the connect failure-atomic."""
        try:
            return self._assemble_engine(*args, **kwargs)
        except BaseException:
            self._discard_staged_mcp_clients()
            raise

    def _assemble_engine(
        self,
        agent: AgentSpec,
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
        spec = agent
        # ``workspace`` is the per-session workspace **absolute path** (welded into
        # durable state, expanded by the agent layer; the driver receives only the
        # final path). ``None`` (no session workspace) keeps the host-fixed default
        # dir.
        workspace_dir = Path(workspace) if workspace else self.workspace_dir
        # Sandbox backend: when the host configured one, every session's fs / shell
        # IO routes into the container instead of the host. The backend is fed to
        # ``build_session_inputs`` (which builds the pack's tools against it AND
        # swaps the host ``WorkspaceRoot`` for a lexical container root); the host
        # ``workspace_dir`` is meaningless inside the container, so it is replaced by
        # the container's working directory. The SAME backend is handed to the seed
        # build (``task_id is None``) and every driving turn, which keeps the
        # seed/drive Engine-cache entry from silently pinning the local backend.
        # Absent a manager, this is a no-op and the local path is unaffected.
        bound_exec_env: Optional[ExecEnv] = None
        bound_exec_env_ref = exec_env_ref
        if self._sandbox is not None and not bound_exec_env_ref:
            # No session-welded ref: the attach path falls back to its single shared
            # container (``default_ref``); the per-session provisioning path has no
            # default (``None``) so a ref-less build keeps the local backend — a real
            # per-session build always carries the ref the driver allocated.
            bound_exec_env_ref = self._sandbox.default_ref
        if self._sandbox is not None and bound_exec_env_ref:
            # A session bound to a specific container (``exec_env_ref``, the
            # welded/folded ``"{base_url}#{sandbox_id}"``) resolves THAT one — the
            # multi-machine reconnect criterion: a task reclaimed on another host
            # reads its recorded ref, not this host's config default, and the manager
            # reconnects via ``provider.attach`` when the handle is not local. The API
            # key comes from THIS host's env, never the ref.
            bound_exec_env, container_workdir = self._sandbox.resolve(
                bound_exec_env_ref
            )
            workspace_dir = Path(container_workdir)
        # Browser backend (sandbox-only): when this agent opens the browser
        # capability AND the session is bound to a container, vend the per-session
        # browser backend off the SAME sandbox handle ``resolve`` used. Gated on the
        # capability so a non-browser session never builds it (and its tool set /
        # stable prefix are untouched); ``None`` on every local / non-browser /
        # no-sandbox path.
        browser_enabled = agent_activates(spec, "browser")
        browser_backend = None
        if browser_enabled and self._sandbox is not None and bound_exec_env_ref:
            browser_backend = self._sandbox.resolve_browser(bound_exec_env_ref)
        # Only a custom tool explicitly named by spec.tools enters the engine.
        spec_tool_names = frozenset(r.name for r in spec.tools)
        filtered_custom = {
            n: t for n, t in self.custom_tools.items() if n in spec_tool_names
        }
        # Derive the approval gate set from the permission mode. A per-turn
        # ``permission_mode`` (the frontend selector, threaded in NON-durably) has
        # the HIGHEST priority — it must win even over an explicit host
        # ``require_approval_tools``, else the per-session switch is a no-op.
        # ``None`` (no per-turn selection: resume / daemon / CLI) falls through to
        # the precedence below. An explicit require_approval_tools wins over the
        # host permission_mode.
        if permission_mode is not None:
            require_approval_tools = _approval_set_for(permission_mode, spec.tools)
        elif self.require_approval_tools is not None:
            require_approval_tools = self.require_approval_tools
        else:
            require_approval_tools = _approval_set_for(self.permission_mode, spec.tools)
        # Shell permission model (allowlist-or-approve). The effective
        # permission_mode drives shell_run's gate; this is independent of the
        # engine/event/resume path (the allowlist is external governance config,
        # never recorded into the conversation):
        #   * bypassPermissions -> ARBITRARY, no gate (unrestricted).
        #   * default / acceptEdits -> the tool runs ARBITRARY (no self-refusal)
        #     and a per-call predicate gates it: a command in the EFFECTIVE
        #     allowlist (built-in + host config + this project's remembered
        #     rules) runs silently; an unknown one routes through HITL approval.
        #   * shell_mode OFF stays off (tool absent) regardless of permission.
        effective_permission = (
            permission_mode if permission_mode is not None else self.permission_mode
        )
        shell_mode = self.shell_mode
        shell_approval_predicate: Optional[Callable[[str, Mapping[str, Any]], bool]] = (
            None
        )
        if self.shell_mode is not ShellMode.OFF:
            shell_mode = ShellMode.ARBITRARY
            if effective_permission != "bypassPermissions":
                effective_rules = build_allowlist(
                    tuple(self.shell_allowlist)
                    # Sandbox mode reads the project allowlist from INSIDE the
                    # container (``workspace_dir`` is the container workdir here);
                    # ``bound_exec_env`` is ``None`` on the local path.
                    + load_project_shell_allowlist(
                        workspace_dir, exec_env=bound_exec_env
                    ),
                    # The curated base is the fs built-in's table — the same rules
                    # the shell_run tool enforces.
                    base_rules=default_shell_rules(),
                )
                shell_approval_predicate = _make_shell_approval_predicate(
                    effective_rules
                )
                # The predicate owns shell_run's gate; drop it from the static set
                # so an allowlisted command is NOT also force-gated.
                require_approval_tools = tuple(
                    n for n in require_approval_tools if n != "shell_run"
                )
        # Browser tools are flag-gated (never in ``spec.tools``), so
        # ``_approval_set_for`` never sees them. Force-gate the whole high-risk
        # browser pack here when the capability is enabled, unless the session
        # bypasses permissions. ``build_session_inputs`` filters
        # ``require_approval_tools`` to the tools actually present, so this is a
        # clean no-op when browser is off (no backend ⇒ no browser tools).
        if browser_enabled and effective_permission != "bypassPermissions":
            require_approval_tools = tuple(require_approval_tools) + tuple(
                n for n in browser_tool_names() if n not in require_approval_tools
            )
        directory = (
            self._subagent_directory(allowed_subtask_agents)
            if delegation_enabled
            else ()
        )
        # Resolve the turn's enabled MCP aliases → host-side
        # server specs (with url/credentials) → LIVE ``McpTool``s, connecting each
        # server now (deterministic alias-sorted order, following the
        # fs→script→MCP→control append order). The alias list arrived NON-durably
        # (no url/token in any request); the specs come from the product-injected
        # ``mcp_server_resolver`` (the SDK never holds the config store). ``()``
        # aliases / ``None`` resolver ⇒ ``build_mcp_tools(())`` builds nothing. The
        # connected clients are owned by the cached Engine for this session. Resume
        # stays reconnect-free: the recorded tool spec is the durable truth, and the
        # resume path passes empty aliases.
        mcp_tools_override = self._resolve_live_mcp_tools(mcp_aliases, task_id=task_id)
        memory_override = self._memory_root_override(task_id)
        # The named backend bag: live backing objects for the capability packs,
        # keyed by the plugins' own names. Absent names mean "no live backing" —
        # the pack contributes nothing.
        pack_backends: dict[str, object] = {}
        if browser_backend is not None:
            pack_backends["browser"] = browser_backend
        if self.app_gateway is not None:
            pack_backends["app_preview"] = self.app_gateway
        inputs = build_session_inputs(
            session_packs=self._session_packs(agent.name),
            # The built-in + this agent's activated control tools, merged with the
            # kernel's internal entries in the builder's mount loop.
            control_tools=self._control_tools(agent.name),
            base_reminders=default_reminder_specs(),
            guards_factory=default_guards_factory(),
            default_policy_factory=default_policy_factory(),
            workspace_dir=workspace_dir,
            system_prompt=spec.instructions,
            allowed_tools=spec_tool_names,
            content_store=self.content_store,
            model=model,
            compaction=derive_compaction_config(model),
            # The catalog's vendor-family judgment (the kernel holds no model
            # opinions); the edit-tool mutex table lives inside the fs pack.
            provider_family=provider_family(model),
            # Session-level budget override; None uses the spec-derived path.
            budget=self.budget
            if self.budget is not None
            else self._budget_for(spec.default_budget),
            allowed_subtask_agents=allowed_subtask_agents,
            max_steps=self.max_steps,
            # The write/shell safety inputs ride ``plugin_config["fs"]`` below — the
            # fs pack is their sole consumer, so they left the kernel signature.
            shell_approval_predicate=shell_approval_predicate,
            # Per-helper structured output: a workflow helper spawned via
            # ``agent(goal, schema=...)`` mounts the ``structured_output`` control
            # schema (its ``parameters`` = the declared JSON Schema). ``None`` (every
            # non-helper build) leaves the tool set + View stable hash unchanged.
            structured_output_schema=structured_output_schema,
            # When live MCP tools were resolved for this turn, they are passed as the
            # override. ``None`` override ⇒ the builder merges no MCP tools — so
            # resume, which passes empty aliases, gets the same tool set.
            mcp_tools_override=mcp_tools_override,
            custom_tools=filtered_custom,
            # The sandbox backend for this session's fs / shell tools. ``None`` (no
            # host sandbox) ⇒ the builder uses ``LocalExecEnv`` and the host
            # ``WorkspaceRoot``.
            exec_env=bound_exec_env,
            # The backend bag + this agent's effective capability flags: the ONE
            # generic bag both the session packs and the control-tool mounts
            # self-gate on (the browser pack merges only with a live ``"browser"``
            # backend AND the flag; the app pack only with a live ``"app_preview"``
            # gateway; each control-tool mount reads its own name). The host computes
            # every already-ANDed value here — the SDK host treats the spec's
            # activation tuple as the source of truth. ``workflow`` mounts
            # run_workflow only when the host enabled workflow AND this agent can
            # delegate: a workflow's agent()/parallel() spawn real sub-agents into
            # the same delegation allow-list, so gating on delegation keeps the tool
            # surface honest. The reserved __workflow__ child is intercepted in
            # _build_orchestration_engine, so it never reaches this builder.
            backends=pack_backends,
            capability_flags={
                "browser": browser_enabled,
                "memory": agent_activates(spec, "memory"),
                "delegation": delegation_enabled,
                "todo_write": agent_activates(spec, "todo_write"),
                "ask_user_question": ask_user_question_enabled,
                "skill_invocation": agent_activates(spec, "skill_invocation"),
                "workflow": self.workflow_allowed and delegation_enabled,
            },
            # The per-plugin config bag: each pack parses only its own entry. The
            # per-task memory root rides the top-precedence ``memory_dir`` slot so
            # the builder's tool pack +
            # resident index target that tenant's store; ``None`` override
            # (single-tenant / resolver fallback) keeps the host fields.
            plugin_config=self._plugin_config(
                shell_mode=shell_mode,
                spec=spec,
                memory_override=memory_override,
            ),
            hooks_pre_tool_use=self.hooks_pre_tool_use,
            repetition_threshold=self.repetition_threshold,
            repetition_action=self.repetition_action,
            repetition_window=self.repetition_window,
            require_approval_tools=require_approval_tools,
            subtask_agent_directory=directory,
            # Wiring-only LLM controls: session-wide override propagated to every
            # LLMRequest the ReActPolicy builds.
            output_schema=self.output_schema,
            thinking=self.thinking,
            effort=effort if effort is not None else self.effort,
            # Engine-level inline truncation cap.
            tool_output_inline_limit=self.tool_output_inline_limit,
            # SDK Options extension points. Fed to live + resume from the same host
            # fields.
            policy_factory_override=self.policy_override,
            extra_guards=self.extra_guards,
            # Host-wide channels (Options.content_channels) plus the ones this
            # agent's activated plugins contribute.
            extra_content_kinds=self.extra_content_kinds
            + tuple(self.activated_content_kinds.get(agent.name, ())),
            # Compose-time reminders this agent's activated plugins contribute,
            # interleaved by priority with the built-in three.
            extra_reminders=tuple(self.extra_reminders.get(agent.name, ())),
        )
        # Route to the bound provider's adapter instance (``None`` ⇒ host default),
        # so a session on a different provider runs its LLM round-trips on that
        # adapter — the registry, not a host-fixed single instance, is the source.
        llm = RuntimeLLMClient(
            provider=self._provider_for(provider),
            event_log=self.event_log,
            content_store=self.content_store,
            pricing=_catalog_pricing,
            provider_headers=self.provider_headers,
            delta_sink=self.delta_sink,
        )
        policy: Policy = inputs.policy_factory(llm)
        if policy_wrapper is not None:
            policy = policy_wrapper(policy)
        # Per-helper structured output: the "structured receipt" wrapper intercepts
        # the helper's decisions — a ``structured_output`` call becomes the helper's
        # final answer; an end_turn without one is nudged (at most twice), then
        # failed. Wrapped OUTERMOST; only the subtask drain ever passes a schema, and
        # a child engine never carries the multi-turn ``policy_wrapper``, so the two
        # wrappers never stack.
        if structured_output_schema is not None:
            policy = react_impl().StructuredOutputPolicy(
                inner=policy, schema=structured_output_schema
            )
        return Engine(
            event_log=self.event_log,
            content_store=self.content_store,
            composer=inputs.composer,
            policy=policy,
            tools=inputs.tools,
            hooks=inputs.hooks,
            # Mid-loop activations emit the generic ContextContentRecorded
            # (kind="skill", policy="pinned") through the registry-derived seam. The
            # pre-loop path (activate_skills helper) fires its own generic event
            # before the patch; the engine guards against first-only re-emission so
            # exactly one event per (task, skill).
            content_hashes=inputs.content_hashes,
            tool_output_inline_limit=inputs.tool_output_inline_limit,
            # The host's background-shell registry, so a session's
            # ``shell_run(background=true)`` reaches it through the ToolContext.
            background_runner=self._process_registry,
            # The host's per-turn file-checkpoint gate, so an AI ``edit`` / ``write``
            # stashes its rewind baseline.
            file_checkpoint_registry=self._file_checkpoint,
            # background sub-agent launch+capacity seam, wired ONLY on a top-level
            # interactive Engine (``policy_wrapper`` is the multi-turn wrapper —
            # present only there). A child engine / oneshot host gets ``None`` so
            # ``spawn_subagent(background=True)`` degrades to the foreground barrier
            # spawn — the wanted "no nested background" behaviour
            # (docs/adr/background-subagent.md).
            background_subagent_launcher=(
                self._background_subagents if policy_wrapper is not None else None
            ),
            # Anchored-content seams (docs/adr/anchored-content-placement.md):
            # ``None`` unless the host armed ``instructions_discovery``.
            content_discovery=inputs.content_discovery,
            content_preloader=inputs.content_preloader,
            # The contributed pre-loop ``init`` hooks: the driver reads them off this
            # resolved Engine and records each pack's residents at seed time. ``()``
            # when no pack activates a pre-loop resident.
            content_init_hooks=inputs.init_hooks,
            # tool_result_transform stages for THIS agent — selected by the agent's
            # name from the per-agent map the Client resolved from the activated
            # plugin set. ``()`` for an agent that activated no transform-bearing
            # plugin.
            tool_result_transforms=tuple(
                self.tool_result_transforms.get(agent.name, ())
            ),
            # The ask answer codec (the ask mount's typed ``answer_codec``, threaded
            # through the builder) put on the Engine so the driver's ``answer`` path
            # can decode a submitted answer. ``None`` for a session that did not
            # mount ``ask_user_question`` — the driver then fails loudly on an answer.
            answer_codec=inputs.answer_codec,
            # mid-turn goal injection — the per-host inbox the driver's
            # ``inject_goal`` submits to and this Engine's step loop drains at
            # each turn boundary. Same singleton every built Engine shares
            # (mirrors ``background_runner``); never resumed from.
            injection_inbox=self._injection_inbox,
        )

    def _build_orchestration_engine(
        self, task_id: str, *, allowed_subtask_agents: frozenset[str]
    ) -> Engine:
        """Build the ``__workflow__`` child's Engine.

        Reads the script/args off the child's durable ``TaskCreated.inputs`` and
        builds an Engine whose Policy is :class:`OrchestrationPolicy`. Its
        ``agent()`` calls delegate into ``allowed_subtask_agents`` (the inherited
        worker set, filtered to roster names); ``workflow_allowed`` is OFF (no
        nested workflows).
        """
        wf_inputs = self._read_task_inputs(task_id)
        script = str(wf_inputs.get("script", ""))
        raw_args = wf_inputs.get("args")
        wf_args = dict(raw_args) if isinstance(raw_args, dict) else {}
        known = self._spawnable_set(allowed_subtask_agents)
        directory = self._subagent_directory(known)
        inputs = build_session_inputs(
            session_packs=self._session_packs(),
            # The built-in control tools (no per-agent activation on the
            # orchestration engine); ask/todo self-gate off below, so only
            # delegation could mount when workflow enables it.
            control_tools=self._control_tools(),
            base_reminders=default_reminder_specs(),
            guards_factory=default_guards_factory(),
            default_policy_factory=default_policy_factory(),
            workspace_dir=self.workspace_dir,
            system_prompt=react_impl().WORKFLOW_SYSTEM_PROMPT,
            allowed_tools=frozenset(),
            content_store=self.content_store,
            model=self.model,
            compaction=derive_compaction_config(self.model),
            provider_family=provider_family(self.model),
            budget=self.budget
            if self.budget is not None
            else self._budget_for(BudgetSpec()),
            allowed_subtask_agents=known,
            subtask_agent_directory=directory,
            max_steps=self.max_steps,
            # Delegation only: the orchestration engine spawns workers but mounts no
            # other control tool (no nested workflows, and ask/todo/skill self-gate
            # off on their absent flags).
            capability_flags={"delegation": True},
            # The orchestration engine runs memory off (no "memory" flag) and keeps
            # the host's fs/skills/instructions config through the same per-plugin
            # bag as the session path. ``spec=None`` selects the reduced
            # orchestration environment — see :meth:`_plugin_config` for what it
            # deliberately omits.
            plugin_config=self._plugin_config(shell_mode=self.shell_mode),
            tool_output_inline_limit=self.tool_output_inline_limit,
        )
        policy: Policy = react_impl().OrchestrationPolicy(script=script, args=wf_args)
        return Engine(
            event_log=self.event_log,
            content_store=self.content_store,
            composer=inputs.composer,
            policy=policy,
            tools=inputs.tools,
            hooks=inputs.hooks,
            content_hashes=inputs.content_hashes,
            tool_output_inline_limit=inputs.tool_output_inline_limit,
            background_runner=self._process_registry,
            file_checkpoint_registry=self._file_checkpoint,
            content_init_hooks=inputs.init_hooks,
        )

    def _read_task_inputs(self, task_id: str) -> dict[str, Any]:
        """Read a task's recorded ``TaskCreated.inputs`` (durable, resume-safe)."""
        for env in self.event_log.read(task_id):
            if env.type == "TaskCreated":
                return dict(getattr(env.payload, "inputs", {}) or {})
        raise RuntimeError(f"workflow: child {task_id!r} has no TaskCreated")

    # -- helpers -----------------------------------------------------------

    def workspace_dir_for(self, workspace: Optional[str]) -> Path:
        """Resolve a per-session workspace → fs root.

        ``workspace`` is the **absolute path** welded into durable state (see
        ``TaskHostBoundPayload.workspace_dir``); this converts it to a ``Path``.
        ``None`` (no session workspace bound) ⇒ the host-fixed
        :attr:`workspace_dir`.

        Called by product-layer callers (e.g. the HTTP approval handler) that need
        the SAME fs root the engine used.
        """
        if not workspace:
            return self.workspace_dir
        p = Path(workspace)
        if p.is_absolute():
            return p
        # Fallback for any residual non-absolute value: treat as host default
        # (defensive; the per-session workspace path is always absolute).
        return self.workspace_dir

    def intake_reminder_providers(
        self, agent: str, task_id: Optional[str] = None
    ) -> tuple[Any, ...]:
        """``agent``'s composed ``turn_intake`` providers — the ONE intake seam.

        The driver asks for the FULL ordered tuple for a turn and never
        distinguishes one provider from another; the composition rule lives HERE:
        the built-in memory auto-recall FIRST (its recorded position is pinned by
        the characterization goldens), then the agent's activated
        ``reminder_provider`` plugins in their ``(plugin, name)`` order. The
        recording path runs them before the incoming turn enters the ledger and
        records what they return as follow-up turns — retrieval happens on the WRITE
        side (at recording time), never at compose time, so the composer stays a
        pure function of folded state and resume folds the reminders back without
        re-invoking a provider.

        The recall provider joins only for an agent whose spec activates
        ``"memory"``. It arrives already bound to the live store (the memory
        built-in's ``memory_reminder_provider`` — the kernel driver never sees a
        store), and the store root resolution is the SAME precedence
        :func:`~noeta.execution.builder.build_session_inputs` uses for the tools +
        resident index (:meth:`memory_root`). ``task_id`` is the task the goal is
        being recorded on; the driver passes it so a multi-tenant host recalls from
        that tenant's store (``None`` — a caller without a task — keeps the
        host-level chain). The global default is read late off the impl module
        (never from-imported) so a test pinning
        ``noeta.builtins.memory.impl.store.DEFAULT_GLOBAL_MEMORY_DIR`` stays
        hermetic. An empty / missing directory is a valid empty store: recall never
        hits, so the default flow pays zero bytes.

        Empty for a memory-off agent that activated no provider-bearing plugin. Read
        defensively by the driver (``getattr``), so a host without this seam is a
        clean no-op.
        """
        providers: list[Any] = []
        if agent == "unnamed" and self.unnamed_fallback is not None:
            spec = self.unnamed_fallback
        else:
            spec = self._lookup_agent(agent, task_id="<unbound>")
        if agent_activates(spec, "memory"):
            impl = memory_impl()
            store = impl.load_memory_store(root=self.memory_root(task_id))
            providers.append(impl.memory_reminder_provider(store))
        providers.extend(self.reminder_providers.get(agent, {}).get(TURN_INTAKE, ()))
        return tuple(providers)

    def memory_root(self, task_id: Optional[str] = None) -> Path:
        """The resolved memory-store root directory (pure wiring, no IO).

        ONE resolution chain for every consumer — the session builder's tool pack +
        resident index, :meth:`intake_reminder_providers`, and a product's
        host-side memory material (e.g. the consolidation debounce marker, which
        must sit NEXT TO the store the memory tools mutate): the per-task
        ``memory_root_resolver`` when set AND ``task_id`` is given AND it returns a
        root, else ``memory_dir`` override > ``global_memory_dir`` > the SDK global
        default (``~/.noeta/memories``). ``task_id=None`` (or no resolver) keeps the
        host-level chain. The default is read late off the impl's store module
        (never from-imported) so a test pinning
        ``noeta.builtins.memory.impl.store.DEFAULT_GLOBAL_MEMORY_DIR`` stays
        hermetic.
        """
        override = self._memory_root_override(task_id)
        if override is not None:
            return override
        if self.memory_dir is not None:
            return self.memory_dir
        if self.global_memory_dir is not None:
            return self.global_memory_dir
        default_root: Path = memory_impl().DEFAULT_GLOBAL_MEMORY_DIR
        return default_root

    def _subagent_directory(
        self, agents: frozenset[str]
    ) -> tuple[tuple[str, str], ...]:
        """The sorted ``(name, description)`` roster ``spawn_subagent`` renders.

        Surfaces the delegation-allowed children to the model inside the tool's
        JSON schema. Returns ``()`` for an empty set, or when no child carries a
        description (a roster of blank descriptions tells the model nothing and
        would only churn the schema). A name that no longer resolves is skipped
        rather than failing the build: a stale spec reference must not sink an
        otherwise-valid session. Shared by the session and workflow-orchestration
        build paths.
        """
        if not agents:
            return ()
        entries: list[tuple[str, str]] = []
        for name in sorted(agents):
            try:
                child = self.registry.resolve(name)
            except Exception:
                continue
            entries.append((name, str(child.metadata.get("description", ""))))
        if not any(description for _name, description in entries):
            return ()
        return tuple(entries)

    def _plugin_config(
        self,
        *,
        shell_mode: ShellMode,
        spec: Optional[AgentSpec] = None,
        memory_override: Optional[Path] = None,
    ) -> dict[str, dict[str, Any]]:
        """The per-plugin config bag a build path hands the kernel builder.

        Each pack parses only its own entry. The two build paths — a session Engine
        and the ``__workflow__`` orchestration Engine — share ONE builder with ONE
        explicit switch, ``spec``:

        * ``spec`` given (a session) — the full environment: the agent's ``write``
          fence (``metadata["write_path_globs"]``), the host's out-of-workspace
          ``write_roots`` grant, the full skills tier list, read-triggered
          instruction discovery, and the ``memory`` entry.
        * ``spec is None`` (the orchestration engine) — the **reduced** environment.
          A workflow driver spawns workers and calls no fs tool of consequence: it
          has no agent spec to read a write fence off, runs memory off, and mounts
          neither the lower skills tiers nor instruction discovery. Omitting an
          entry (rather than passing one the pack would self-gate away) is what
          keeps its session correct.
        """
        reduced = spec is None
        config: dict[str, dict[str, Any]] = {
            # The fs pack's own write/shell safety inputs. ``shell_mode`` is the
            # permission-derived effective value on the session path and the raw
            # host mode on the orchestration path (which runs no approval gate).
            "fs": {
                "write_mode": self.write_mode,
                "shell_mode": shell_mode,
                "shell_allowlist": self.shell_allowlist,
            },
            "skills": {
                "skills_dir": self.skills_dir,
                "allow_skill_scripts": self.allow_skill_scripts,
                "tool_enforcement": self.skill_tool_enforcement,
            },
            "workspace": {
                "instructions_enabled": self.instructions_enabled,
                "instructions_file": self.instructions_file,
            },
        }
        if reduced:
            return config
        config["fs"]["write_path_globs"] = _spec_write_path_globs(spec)
        config["fs"]["write_roots"] = self.write_roots
        config["skills"]["builtin_skills_dirs"] = tuple(self.builtin_skills_dirs)
        config["skills"]["global_skills_dir"] = self.global_skills_dir
        config["workspace"]["instructions_discovery"] = self.instructions_discovery
        # The per-task memory root rides the top-precedence ``memory_dir`` slot so
        # the builder's tool pack + resident index target that tenant's store;
        # ``None`` override (single-tenant / resolver fallback) keeps the host
        # fields.
        config["memory"] = {
            "memory_dir": (
                memory_override if memory_override is not None else self.memory_dir
            ),
            "global_memory_dir": self.global_memory_dir,
        }
        return config

    def _session_packs(self, agent_name: Optional[str] = None) -> tuple[Any, ...]:
        """The kernel builder's ``session_packs`` injection.

        The built-in manifests' ``session_pack`` contributions, loader-resolved and
        ``(priority, plugin, name)``-ordered, plus — when ``agent_name`` is given —
        the packs this agent's activated external plugins contribute (the Client
        folds them into ``activated_session_packs``). The kernel builder's generic
        loop re-sorts the merged list by ``(priority, name)``, so an external pack
        interleaves at its declared band. A disabled ``skills`` built-in simply
        drops its pack. Both build paths (session + workflow orchestration) go
        through here so they can never disagree about which packs are wired.
        """
        disabled = frozenset() if self.skills_enabled else frozenset({"skills"})
        packs: tuple[Any, ...] = default_session_packs(disabled=disabled)
        if agent_name is not None:
            packs = packs + tuple(self.activated_session_packs.get(agent_name, ()))
        return packs

    def _control_tools(self, agent_name: Optional[str] = None) -> tuple[Any, ...]:
        """The kernel builder's ``control_tools`` injection.

        The built-in manifests' ``control_tool`` contributions (``todo_write`` /
        ``ask_user_question`` / ``delegation``), loader-resolved and
        ``(priority, plugin, name)``-ordered, plus — when ``agent_name`` is given —
        the control tools this agent's activated external plugins contribute (the
        Client folds them into ``activated_control_tools``). The builder MERGES
        these with its remaining internal entries (skill / run_workflow /
        structured_output) and re-sorts the union by ``(priority, name)`` in the
        mount loop, so a contributed control tool mounts at its declared band. Both
        build paths (session + workflow orchestration) go through here so they can
        never disagree.
        """
        tools: tuple[Any, ...] = default_control_tools()
        if agent_name is not None:
            tools = tools + tuple(self.activated_control_tools.get(agent_name, ()))
        return tools

    def _memory_root_override(self, task_id: Optional[str]) -> Optional[Path]:
        """The per-task root the injected resolver maps ``task_id`` to, or ``None``.

        ``None`` — no resolver wired (single-tenant), no task id in hand (the
        by-name seed path), or the resolver declining this task — means "fall
        back to the host-level chain". Centralised so :meth:`memory_root`,
        :meth:`_build_engine`, and :meth:`_engine_cache_scope` can never
        disagree on which store a task resolves.
        """
        if self.memory_root_resolver is None or not task_id:
            return None
        return self.memory_root_resolver(task_id)

    def _engine_cache_scope(
        self, agent: AgentSpec, task_id: Optional[str]
    ) -> Optional[str]:
        """Partition the Engine cache by resolved per-task memory root.

        The Engine cache key deliberately omits ``task_id`` (engines are shared
        across tasks with equal bindings), but a memory-enabled Engine bakes its
        :class:`MemoryStore` into the tool closures + resident index — so two tasks
        whose ``memory_root_resolver`` maps to DIFFERENT roots must never share a
        cached Engine (tenant A's store would serve tenant B). Scope = the resolved
        override root; ``None`` (memory-off agent, no resolver, no task id, or
        resolver fallback) keeps the shared slot.
        """
        if not agent_activates(agent, "memory"):
            return None
        override = self._memory_root_override(task_id)
        return str(override) if override is not None else None

    def declared_skill_activations(self, agent: str) -> tuple[str, ...]:
        """The agent spec's declared ``skills`` (``Options.skills``), as plain names.

        Read at ``InteractionDriver.seed_start`` (mirrors the ``getattr``-guarded
        seam pattern of :meth:`intake_reminder_providers`) so ``Options(skills=[...])``
        pre-activates through the SAME pre-loop ``TaskStatePatch(activate_skills=...)``
        channel a slash-command-resolved ``activations`` selector rides — one
        activation mechanism, two sources, merged + deduped at the call site. Uses
        the same alias / ``"unnamed"`` fallback resolution
        :meth:`resolve_engine_for_agent` applies (via :meth:`_lookup_agent`), so the
        returned names always match the spec that built the seed Engine. An agent
        with no declared skills returns ``()``.
        """
        if agent == "unnamed" and self.unnamed_fallback is not None:
            spec = self.unnamed_fallback
        else:
            spec = self._lookup_agent(agent, task_id="<unbound>")
        return tuple(ref.name for ref in spec.skills)

    @staticmethod
    def _budget_for(spec_budget: BudgetSpec) -> Budget:
        """Translate a declared :class:`BudgetSpec` into a live :class:`Budget`.

        The SDK path uses :class:`Budget`'s own field defaults (all ``None`` caps),
        not a product-specific default budget. A ``BudgetSpec`` field that is
        ``None`` delegates to the ``Budget()`` default, so the SDK path can tighten
        caps independently without re-implementing the guard.
        """
        overrides = {
            k: v for k, v in dataclasses.asdict(spec_budget).items() if v is not None
        }
        return dataclasses.replace(Budget(), **overrides)
