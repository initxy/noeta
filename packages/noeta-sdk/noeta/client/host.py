"""SDK-side resident host.

:class:`SdkHost` implements the
:class:`~noeta.execution.host.ResidentHost` Protocol via the
:class:`~noeta.execution.resolver.GenericEngineResolver` skeleton. It owns
the canonical engine-construction path for SDK callers: compiled
:class:`~noeta.agent.spec.AgentSpec` s → per-(agent, model, ask) Engines
with catalog pricing and the deterministic tool pack the generic
:func:`~noeta.execution.builder.build_session_inputs` produces.

Product-neutral by design: this module imports only ``noeta.execution`` /
``noeta.agent`` / ``noeta.protocols`` / ``noeta.tools`` — never ``noeta.agent``
(enforced by import-linter).
:data:`_catalog_pricing` is the SDK's only implementation (the former
``noeta.agent.wiring.engine._pricing_callback`` was deleted with the roster);
the product references this module's constant directly — no second copy.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from noeta.agent.registry import AgentRegistry, UnknownAgentError
from noeta.agent.spec import AgentSpec, BudgetSpec, ToolRef
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.protocols.canonical import to_canonical_bytes
from noeta.context.content_channel import ContentKindSpec
from noeta.context.environment import EnvironmentSnapshot
from noeta.context.instructions import InstructionsSnapshot
from noeta.context.memory import MemoryEntries
from noeta.execution import memory as execution_memory
from noeta.execution.builder import build_session_inputs, derive_compaction_config
from noeta.execution.environment import load_environment
from noeta.execution.host import AgentRegistryProtocol
from noeta.execution.instructions import load_instructions
from noeta.execution.resolver import GenericEngineResolver
from noeta.policies.orchestration import (
    OrchestrationPolicy,
    StructuredOutputPolicy,
    WORKFLOW_SYSTEM_PROMPT,
)
from noeta.guards.budget import Budget
from noeta.guards.hook import PreToolUseRule
from noeta.guards.permission import SkillEnforcementMode
from noeta.guards.repetition import RepetitionAction
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
from noeta.providers.catalog import price as catalog_price
from noeta.execution.background_subagent import (
    DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_SESSION,
    BackgroundSubagentRegistry,
)
from noeta.runtime.background_shell import (
    DEFAULT_MAX_BACKGROUND_JOBS_PER_SESSION,
    ProcessRegistry,
)
from noeta.runtime.cancellation import CancellationRegistry
from noeta.runtime.file_checkpoint import FileCheckpointRegistry
from noeta.tools.app import AppPreviewGateway
from noeta.runtime.llm import RuntimeLLMClient
from noeta.tools.fs import FsWriteMode, ShellMode
from noeta.tools.memory import MemoryStore
from noeta.tools.fs.shell import (
    build_allowlist,
    command_in_allowlist,
    load_project_shell_allowlist,
)
from noeta.tools.mcp import (
    HttpPostFn,
    McpAnyServerSpec,
    build_mcp_tools,
    mcp_provenance_from_specs,
)



__all__ = ["SdkHost"]

_log = logging.getLogger(__name__)

#: #13 — upper bound on the in-process Engine cache shared by all sessions on
#: this host. LRU eviction (OrderedDict.popitem(last=False)) keeps the pool
#: from growing without bound in long-lived server processes.
_MAX_CACHED_ENGINES: int = 256


#: An ``OrderedDict`` for the Engine LRU that also REAPS the live MCP clients
#: an evicted Engine owns. ``_build_engine`` connects MCP servers (each an
#: :class:`McpStdioClient` subprocess / an :class:`McpHttpClient`) and those
#: clients are retained only via the cached Engine's tools; the plain
#: ``popitem(last=False)`` eviction in :meth:`GenericEngineResolver._engine_for_agent`
#: drops the Engine and would orphan the subprocess + leak its fds. We can't
#: change that eviction site (it lives in the base resolver), so we make the
#: cache *value* carry its clients: ``_build_engine`` stages them on the host
#: via :meth:`SdkHost._stage_mcp_clients`; ``__setitem__`` adopts the staged
#: list for the new key, and every removal path (``__delitem__`` / ``pop`` /
#: ``popitem``) calls ``client.shutdown()`` (idempotent, never raises) on the
#: removed entry's clients. One bad client can't break eviction: shutdown is
#: swallowed + logged.
class _McpReapingEngineCache(OrderedDict):  # type: ignore[type-arg]
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # key → live MCP clients owned by that key's Engine.
        self._clients_by_key: dict[Any, list[Any]] = {}
        # Set by the host right before ``__setitem__`` runs (the base resolver
        # assigns ``self._engines[key] = engine`` straight after ``_build_engine``
        # returns). Consumed-and-cleared on adoption. THREAD-LOCAL (item 3):
        # with the per-key build locks, Engine builds for different keys run
        # concurrently; stage → adopt pairs on the build thread (the build and
        # its put always run on the same thread), so concurrent builds can no
        # longer adopt each other's clients.
        self._staging = threading.local()

    def stage(self, clients: list[Any]) -> None:
        self._staging.pending = list(clients)

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


#: The synthetic registry name a single convenience
#: ``provider`` is folded under in :meth:`SdkHost.__post_init__`. On the
#: single-provider path a bound ``ModelBound`` never carries a provider name
#: (the driver passes no selector), so this name is purely an internal table
#: key — it never enters a durable write and changing it does not affect
#: historical recordings.
_SINGLE_PROVIDER_NAME = "default"


# ---------------------------------------------------------------------------
# Pure helpers — permission_mode → require_approval_tools (unit-testable)
# ---------------------------------------------------------------------------

#: The three "edit" tool names (``edit`` / ``write`` / ``apply_patch``) that
#: ``acceptEdits`` exempts from the default non-low risk gate. Kept as a
#: module-level set so tests and future permission-mode additions can refer to
#: one canonical list.
_EDIT_TOOL_NAMES: frozenset[str] = frozenset(
    {"edit", "write", "apply_patch"}
)


def _make_shell_approval_predicate(
    rules: tuple[Any, ...]
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


def _approval_set_for(
    mode: str, tool_refs: Sequence[ToolRef]
) -> tuple[str, ...]:
    """Return a sorted tuple of tool names to gate via ``require_approval_tools``.

    Pure function — no runtime access, no side effects — so unit tests can
    exercise every ``permission_mode`` directly without spinning up a
    :class:`SdkHost`.

    Modes (simplified to three; plan removed)
    -----
    ``"default"``
        Every tool whose declared ``risk_level != "low"``.
    ``"acceptEdits"``
        Same rule as ``default`` but the three edit-class tools
        (``edit`` / ``write`` / ``apply_patch``) are exempted
        even when declared high-risk.
    ``"bypassPermissions"``
        Empty — the legacy "no tool is gated" behaviour.
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
# Catalog-pricing callback — the SDK's only implementation
# (noeta.agent.wiring.engine._pricing_callback
# was deleted with the roster; the product does `from noeta.client.host import
# _catalog_pricing` directly — no second copy). If catalog.price's KeyError
# semantics change, edit only this function.
# ---------------------------------------------------------------------------

def _catalog_pricing(model: str, usage: Usage) -> float:
    """Price one LLM round-trip from the sdk catalog; unknown models → 0.0.

    Rationale (the KeyError-or-zero policy):
      1. Pricing comes from the sdk catalog; any model not in the catalog
         (stub-model, an unpriced real id) counts as 0.0. KeyError-or-zero
         lives here in the code layer, not in catalog.price (which raises
         loudly to surface a priced model someone typed wrong) and not in
         RuntimeLLMClient (which stays provider-neutral, seeing only the
         injected callback).
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

    A spec may carry ``metadata["write_path_globs"]`` (e.g. ``"plans/*.md"``)
    (metadata is excluded from the AgentSpec identity — a host-binding hint, not identity).
    The SdkHost reads it here and forwards it into ``build_session_inputs`` so
    that spec's ``write`` tool is built path-restricted (physically confined to
    the whitelisted paths), while every other agent (no such metadata) keeps the
    unrestricted ``write``. The value is a comma-separated glob list;
    ``()`` ⇒ unrestricted. Mirrors the noeta-agent product copy in
    ``apps/noeta-agent/.../session.py``.
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
      :func:`~noeta.execution.builder.build_session_inputs` with the
      :func:`~noeta.execution.builder.build_session_inputs` with catalog-priced
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
        :class:`~noeta.agent.spec.ToolRef` references. D3 contract: the
        ``AgentSpec.tools`` identity list controls which closures are
        actually included — not the dict's keyset.
    delegation_allowed:
        Host kill-switch; when ``False`` an agent's
        ``capabilities.delegation=True`` is masked off. Mirrors the
        code-product ``CodeEngineResolver`` semantics exactly.
    policy_wrapper:
        Applied to every Engine's policy. ``None`` ⇒ one-shot behaviour
        (``query``); :func:`~noeta.execution.driver.multi_turn_policy_wrapper`
        ⇒ interactive sessions suspend on the next-goal handle (``Client``).
    unnamed_fallback:
        Passed to the skeleton so legacy recordings labelled
        ``agent_name="unnamed"`` can still resolve (SDK-host v1 is
        greenfield — always ``None`` — but the field is here for
        skeleton parity).
    """

    event_log: EventLogFull
    content_store: ContentStore
    dispatcher: Dispatcher
    # Provider moves from a "startup single instance" to a
    # "per-session optional set". ``provider`` (single instance) is the
    # convenience input for single-provider callers (oneshot/lifecycle/tests +
    # Client) — given it, ``__post_init__`` folds it into a one-entry
    # ``providers`` table + a same-named ``default_provider`` (the name is
    # :data:`_SINGLE_PROVIDER_NAME`). A multi-provider agent-layer deployment
    # passes the ``providers`` table + ``default_provider`` and leaves
    # ``provider=None``. Supply exactly one (both / neither hard-errors in
    # ``__post_init__``).
    provider: Optional[LLMProvider] = None
    model: str = "stub-model"
    workspace_dir: Path = field(default_factory=Path.cwd)
    registry: AgentRegistry = field(default_factory=AgentRegistry)
    custom_tools: dict[str, Tool] = field(default_factory=dict)
    delegation_allowed: bool = True
    #: Host kill-switch for the ``run_workflow`` control tool.
    #: Default off; the deployment opts in (``HostConfig.workflow_enabled``).
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
    # Filesystem write mode; DRY_RUN is the safe default, matching today's
    # default behaviour.
    write_mode: FsWriteMode = FsWriteMode.DRY_RUN
    # shell_run allowlist / allow-all switch; ALLOWLIST by default, matching the
    # build_session_inputs default.
    shell_mode: ShellMode = ShellMode.ALLOWLIST
    # Operator shell rules added on top of the built-in safe allowlist (extend
    # semantics); each like {"program": "npm", "subcommand": "start"}. Empty =
    # built-in defaults only (git/pytest/npm test etc.). Effective only when
    # shell_mode=ALLOWLIST; ignored under ARBITRARY/OFF.
    shell_allowlist: Sequence[Mapping[str, Any]] = ()
    # Per-session background-job concurrency cap. Over the
    # cap, ``ProcessRegistry`` **rejects** (does not queue) a
    # ``shell_run(background)`` spawn. Configurable via HostConfig; default 8
    # (``DEFAULT_MAX_BACKGROUND_JOBS_PER_SESSION``). Injected into the process
    # registry built in ``__post_init__`` below.
    max_background_jobs_per_session: int = DEFAULT_MAX_BACKGROUND_JOBS_PER_SESSION
    # Per-session background SUB-AGENT concurrency cap
    # (docs/adr/background-subagent.md). Over the cap, a
    # ``spawn_subagent(background=True)`` is rejected (not queued) before any
    # durable write. Configurable via HostConfig; default 8.
    max_background_subagents_per_session: int = (
        DEFAULT_MAX_BACKGROUND_SUBAGENTS_PER_SESSION
    )
    # Skills dir overriding workspace_dir/.noeta/skills (workspace-local tier);
    # None uses the default load location.
    skills_dir: Optional[Path] = None
    # Lower-priority skill tiers below the workspace-local tier,
    # ordered built-in < global (workspace-local always wins). The product layer
    # passes the built-in tier (the SDK doesn't know noeta-agent's
    # BUILTIN_SKILLS_DIR); global_skills_dir defaults to None ⇒ no global tier.
    # Empty = workspace-local tier only (byte-identical to the historical
    # single-tier behaviour).
    builtin_skills_dirs: Tuple[Path, ...] = ()
    global_skills_dir: Optional[Path] = None
    # Explicit memory-dir override; None uses global_memory_dir / the
    # SDK global default. Memory is pinned to one global directory
    # (it does not drift with the per-session workspace); the memory switch
    # itself reads spec.capabilities.memory (the SDK host treats capabilities as
    # the source of truth, same discipline as skill_invocation).
    memory_dir: Optional[Path] = None
    # Global memory root (agent-layer config; defaults to the SDK's
    # ~/.noeta/memories). An explicit memory_dir override takes priority over this
    # field.
    global_memory_dir: Optional[Path] = None
    #: Project-instructions-file switch. Like memory, this is workspace
    #: environment material (not agent identity), so Capabilities carries no
    #: flag and SdkHost configures it directly. Default False. When True, looks
    #: for NOETA.md → AGENTS.md in order; an explicit instructions_file override
    #: reads only that path. Missing/empty file = no accounting (no instructions
    #: event).
    instructions_enabled: bool = False
    instructions_file: Optional[Path] = None
    # Skill-tool enforcement level (off/warn/enforce); "off" by default, no
    # intervention.
    skill_tool_enforcement: SkillEnforcementMode = "off"
    # Whether to load script-style run_skill_script tools under .noeta/skills;
    # off by default.
    allow_skill_scripts: bool = False
    # Alias resolver for remote/local MCP servers (product-injected).
    # Given an enabled alias, returns its full spec (incl. url/credentials) from
    # the host-side config store, or None (unconfigured/skip). The SDK does not
    # hold the config store (import-linter ``sdk-not-agent`` forbids the SDK
    # depending back on noeta-agent); credentials live only in the product layer's
    # store. Here we take only a **callback**: each turn it resolves the enabled
    # aliases into connectable specs, then the SDK's ``build_mcp_tools`` actually
    # connects them. ``None`` (default / no MCP config) ⇒ no enabled alias
    # resolves to a spec ⇒ no live MCP is connected, byte-identical to pre-0042.
    mcp_server_resolver: Optional[
        Callable[[str], Optional["McpAnyServerSpec"]]
    ] = None
    # Injectable HTTP POST transport for the remote MCP client. Tests
    # pass a fake (a local stub server) so list/call run without real network;
    # production leaves it ``None`` (the client uses stdlib ``urllib``). Pure
    # wiring — never recorded.
    mcp_http_post: Optional[HttpPostFn] = None
    # HookGuard rules for the pre-tool-use phase; an empty tuple registers no
    # extra hook.
    hooks_pre_tool_use: tuple[PreToolUseRule, ...] = ()
    # Repeated-action detection threshold; 0 disables it, equivalent to not
    # registering a RepetitionGuard today.
    repetition_threshold: int = 0
    # Action taken when repetition_threshold is exceeded; defaults to
    # "require_approval", matching the guard default.
    repetition_action: RepetitionAction = "require_approval"
    # Repetition-detection sliding window size; defaults to 8, matching the
    # RepetitionPolicy default.
    repetition_window: int = 8
    # Session-level budget override; None derives from AgentSpec.default_budget
    # (today's path).
    budget: Optional[Budget] = None
    # Explicit approval-required tool names; when None, derived from
    # permission_mode — an explicit value wins.
    require_approval_tools: Optional[tuple[str, ...]] = None
    # Map from legacy recording name → canonical registered name (e.g.
    # {"default": "main"}); used for resuming historical recordings
    # and product-facing alias
    # support.
    aliases: Mapping[str, str] = field(default_factory=dict)
    # Wiring-only LLM request overrides. Excluded from the AgentSpec identity;
    # forwarded into every in-session LLMRequest via the policy.
    output_schema: Optional[dict[str, Any]] = None
    thinking: Optional[str] = None
    effort: Optional[str] = None
    # Microcompact — positive int or None; engine-level inline char
    # cap for tool output. None = no truncation. A resumed session must reuse
    # the value the original run used, or it re-derives different tool-output bytes.
    tool_output_inline_limit: Optional[int] = None
    # SDK Options extension
    # points threaded into every Engine this host builds. All default to inert
    # values; the SDK host feeds the SAME values to the live and resume paths
    # (single _build_engine call site), so a resumed turn rebuilds the
    # identical policy / guard stack / content layout by construction.
    #   * policy_override: a custom decision-policy factory ``(llm) -> Policy``
    #     that fully replaces ReActPolicy (``None`` ⇒ ReAct).
    #   * extra_guards: custom Guards registered after the built-in stack.
    #   * extra_content_kinds: custom ContentKindSpec channels appended after
    #     the built-in residents (the ONLY composer extension seam).
    policy_override: Optional[Callable[[Any], Policy]] = None
    extra_guards: tuple[Guard, ...] = ()
    extra_content_kinds: tuple[ContentKindSpec, ...] = ()
    # The agent-layer base pool root for **bare sessions** (no
    # workspace_id). The agent layer does ``mkdir <workspace_base>/session-<uuid>``
    # and passes the resulting absolute path as ``workspace_dir`` to the driver.
    # ``None`` ⇒ no base pool: every session uses the host-fixed
    # ``workspace_dir`` (the single-workspace behaviour).
    # Named workspaces are no longer subdirs of ``workspace_base``;
    # they are arbitrary paths in the agent-layer registry.
    workspace_base: Optional[Path] = None
    # The provider **registry**: a name→instance table. A
    # multi-provider agent-layer deployment passes it directly (+
    # ``default_provider``); a single-provider caller leaves it empty and passes
    # the convenience ``provider`` single instance instead, which
    # ``__post_init__`` folds into a one-entry table. After folding, ``providers``
    # is always non-empty and ``default_provider`` is always one of its keys
    # (resolver / ``_provider_for`` read this folded table and never touch the
    # ``provider`` convenience field).
    providers: Mapping[str, LLMProvider] = field(default_factory=dict)
    # Default provider name: bound when no per-turn provider selector is given.
    # On the single-provider convenience path ``__post_init__`` pins it to
    # :data:`_SINGLE_PROVIDER_NAME`; a multi-provider deployment sets it
    # explicitly in the agent layer (the explicit ``default`` flag = the
    # conclusion of Open Question Q1).
    default_provider: str = ""
    # The provider→model-list table; the half read by the
    # (provider, model) pair legality check: a session may bind ``model`` only on
    # a pair where ``model ∈ provider_models[name]``. The agent layer builds this
    # table from the provider registry; empty default ⇒ no restriction (the old
    # single-provider path doesn't pass it, so the driver does no pair check —
    # identical to that path).
    provider_models: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    # Optional per-LLM-call HTTP headers derived from the current StepContext.
    # Product hosts use this for deployment gateway correlation headers; the SDK
    # default is None, so generic callers and resume fixtures keep plain provider
    # calls.
    provider_headers: Optional[Callable[[StepContext], Mapping[str, str]]] = None
    # Token-streaming sink (host wiring, like ``provider_headers`` — never part
    # of agent identity): forwarded into every session's RuntimeLLMClient so a
    # streaming-capable provider's in-flight deltas reach the product's delta
    # hub. ``None`` ⇒ providers are called exactly as before.
    delta_sink: Optional[
        Callable[[StepContext, str, StreamDelta], None]
    ] = None
    # The host's live HTML-app preview gateway. A runtime injection
    # (like ``provider_headers`` / ``_process_registry``), NOT part of the host
    # identity. When set, ``_build_engine`` threads it into
    # ``build_session_inputs`` so the agent gets the ``open_app`` tool. ``None``
    # (oneshot / lifecycle / tests / resume) ⇒ no open_app, so the prompt's tool
    # list is unchanged.
    app_gateway: Optional[AppPreviewGateway] = None
    # The cache key has a ``workspace`` dimension
    # (the bound **absolute path**, or ``None`` for the host default) and a
    # ``provider`` dimension — so two sessions on different directories or
    # providers never share an Engine.
    # #13: bounded LRU via OrderedDict (cap = _MAX_CACHED_ENGINES) + a threading
    # Lock to serialise get-or-build-put under ThreadingHTTPServer concurrency.
    _engines: OrderedDict[
        tuple[
            str, str, bool, Optional[str], Optional[str], Optional[str],
            tuple[str, ...], Optional[str],
        ],
        Engine,
    ] = field(
        default_factory=_McpReapingEngineCache, init=False, repr=False, compare=False
    )
    _engines_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )
    # item 3 — per-key Engine-build locks (see
    # ``GenericEngineResolver._engine_for_agent``): builds run outside the
    # global ``_engines_lock`` so one session's slow/hanging MCP connect no
    # longer serialises every other Engine build.
    _engine_builds: dict[Any, threading.Lock] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Per-session permission_mode is a NON-durable,
    # per-turn knob — the frontend sends it each turn; it is never written to the
    # event log. The async HTTP transport seeds a turn on the request thread but
    # resolves the Engine later on a background thread, so the per-turn mode is
    # stashed here keyed by task_id (set by the driver before resolution, read in
    # ``resolve_engine`` to key + build the Engine). Single writer per task (turns
    # are serial under the dispatcher lease); overwritten each turn, never evicted
    # (one short string per task — negligible) so a turn that suspends on approval
    # still resolves the same mode when it resumes.
    _turn_permission_mode: dict[str, Optional[str]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Per-turn, NON-durable enabled-MCP-alias carrier keyed by
    # task_id. Mirrors ``_turn_permission_mode``: the driver records the turn's
    # enabled aliases (clean list, no url/token) here before the Engine is
    # resolved; ``resolve_engine`` reads it to thread the aliases into the cache
    # key + ``_build_engine`` (which resolves each alias → spec via
    # ``mcp_server_resolver`` → live MCP tools). Never written to the event log.
    _turn_mcp_aliases: dict[str, tuple[str, ...]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # Per-turn, NON-durable reasoning-effort override. Same carrier pattern as
    # permission_mode / enabled_mcp: the driver records the selector before the
    # turn resolves its Engine; resolver reads it into the cache key and policy.
    _turn_effort: dict[str, Optional[str]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    # cancel-cascade — process-local registry of cancelled root task ids.
    # The driver's ``cancel`` marks the root here (alongside the durable
    # ``TaskCancelled`` event); ``drive_pending_subtasks`` polls it per tree
    # so an in-flight child abandons its result at the next turn boundary.
    # Per-host singleton; never written to the event log → no resume effect.
    _cancellation: CancellationRegistry = field(
        default_factory=CancellationRegistry, init=False, repr=False, compare=False
    )
    # Background-shell process registry: a per-host runtime
    # accelerator (mirrors ``_cancellation``) owning live ``Popen`` handles +
    # watcher threads for ``shell_run(background=true)``. Constructed in
    # ``__post_init__`` because it needs the host's shared event_log +
    # content_store; never written to the event log (the BackgroundShell*
    # events are the durable record) → no resume effect. Injected into every
    # built Engine so the background shell tools reach it.
    _process_registry: Optional[ProcessRegistry] = field(
        default=None, init=False, repr=False, compare=False
    )
    # Background SUB-AGENT registry (docs/adr/background-subagent.md): a per-host
    # runtime accelerator (mirrors ``_process_registry``) holding the live drive
    # futures + per-session cap for ``spawn_subagent(background=True)``.
    # Constructed in ``__post_init__`` (it needs the host's L0 triple + the
    # resolver's ``_build_drain_host``); never written to the event log (the
    # ``BackgroundSubagent*`` events are the durable record) → no resume effect.
    # Threaded into a top-level interactive Engine as the launch+capacity seam.
    _background_subagents: Optional[BackgroundSubagentRegistry] = field(
        default=None, init=False, repr=False, compare=False
    )
    # Per-turn file-checkpoint gate: a per-host runtime
    # accelerator (mirrors ``_cancellation`` / ``_process_registry``) recording
    # "which workspace files already have a rewind baseline stashed THIS turn",
    # keyed by the session root task id. Needs no event_log/content_store (it is
    # a pure in-memory path set), so a plain default_factory suffices. Injected
    # into every built Engine so an AI ``edit`` / ``write`` stashes its baseline;
    # never written to the event log (the ``file_baselines`` on
    # ``ToolResultRecorded`` are the durable record) → no resume effect.
    _file_checkpoint: FileCheckpointRegistry = field(
        default_factory=FileCheckpointRegistry, init=False, repr=False,
        compare=False,
    )
    # The background-completion notifier (an
    # ``InteractionDriver``, duck-typed to avoid a noeta.client → noeta.execution
    # import cycle). The product sets it AFTER constructing the driver (the
    # driver wraps this host, so the host can't construct it). ``None`` ⇒ no
    # push: a background exit still records ``BackgroundShellExited`` durably,
    # but no wake-and-notify turn is driven (oneshot / lifecycle / tests).
    _background_notifier: Optional[Any] = field(
        default=None, init=False, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        """Fold the single-provider convenience field into a ``providers`` table.

        Supply exactly one: given the convenience ``provider`` single instance
        (oneshot/lifecycle/tests + Client), fold it into a one-entry
        ``providers`` table + a same-named ``default_provider``; a multi-provider
        agent-layer deployment passes the ``providers`` table +
        ``default_provider`` instead (leaving ``provider`` as ``None``). Both /
        neither is a deployment error — hard-error. After folding, downstream
        (the resolver cache key, ``_provider_for``, the read-only ``provider``
        property) sees only ``providers`` / ``default_provider`` and runs the
        same code as the multi-provider path.
        """
        if self.provider is not None:
            if self.providers:
                raise ValueError(
                    "SdkHost: pass EITHER provider (single, convenience) OR "
                    "providers (registry table) — not both"
                )
            object.__setattr__(
                self, "providers", {_SINGLE_PROVIDER_NAME: self.provider}
            )
            object.__setattr__(self, "default_provider", _SINGLE_PROVIDER_NAME)
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
        # Build the background-shell registry once on the host's
        # shared L0 triple. Lazily here (not a default_factory) because it
        # needs event_log + content_store. issue 02 (Mechanism C): inject the
        # dispatcher (the wake seam; 03's kill push reuses it) + the host's own
        # ``_on_background_exit`` hook so a job that exits while the session is
        # idle is pushed back as a next-goal notice. The hook hands off to a
        # daemon drive thread; it is inert until the product wires a notifier
        # via :meth:`set_background_notifier` (oneshot / lifecycle / tests never
        # do, so they stay byte-identical — no push).
        object.__setattr__(
            self,
            "_process_registry",
            ProcessRegistry(
                event_log=self.event_log,
                content_store=self.content_store,
                # Per-session concurrency cap (HostConfig
                # threads it here; default 8). Reject (not queue) over the cap.
                max_jobs_per_session=self.max_background_jobs_per_session,
                dispatcher=self.dispatcher,
                on_background_exit=self._on_background_exit,
            ),
        )
        # Background sub-agent registry (docs/adr/background-subagent.md). Mirrors
        # the process registry: built lazily here on the shared L0 triple. The
        # ``build_host`` callback hands it the resolver's ``_build_drain_host`` so
        # it drives one background child on the same delegation host the
        # foreground drain uses; ``deliver`` is the Mechanism-C hook fired once a
        # child reaches terminal. Inert until ``set_background_notifier`` wires a
        # driver (then ``deliver`` drives the wake-and-notify turn).
        object.__setattr__(
            self,
            "_background_subagents",
            BackgroundSubagentRegistry(
                event_log=self.event_log,
                content_store=self.content_store,
                dispatcher=self.dispatcher,
                build_host=self._drain_host_for_id,
                deliver=self._on_background_subagent_exit,
                max_per_session=self.max_background_subagents_per_session,
            ),
        )

    # -- file-checkpoint per-turn gate reset ----------------

    def reset_file_checkpoint_turn(self, root_task_id: str) -> None:
        """Clear the per-turn rewind-baseline gate at a turn boundary.

        "clear each turn": the driver calls this when a NEW user goal opens
        a turn (``start`` / ``send_goal``) so the next turn re-stashes a fresh
        baseline for any file it touches — which is what lets a rewind restore to
        ANY turn boundary. Idempotent; a never-edited root is a clean no-op."""
        self._file_checkpoint.reset_turn(root_task_id)

    # -- background-shell emergency-stop ---------------

    def kill_background_session(self, session_root_task_id: str) -> list[Any]:
        """Human emergency-stop — kill ALL background jobs of one session.

        The control-plane ``cancel`` (and issue 04's session-close
        cascade) call this so a cancelled / closed conversation does not leave
        its long-running ``shell_run(background)`` processes orphaned. Reuses the
        ``ProcessRegistry`` per-job kill primitive (``kill_session`` → SIGTERM→
        SIGKILL per job; the watchers reap + record ``BackgroundShellKilled``).
        Safe no-op when the registry is unbuilt (returns ``[]``)."""
        registry = self._process_registry
        if registry is None:
            return []
        return registry.kill_session(session_root_task_id)

    def purge_background_session(self, session_root_task_id: str) -> None:
        """Drop a *deleted* session's retained job handles (memory reclaim).

        Called by ``Client.delete_task`` when a conversation is hard-deleted.
        Unlike ``kill_background_session`` (which reaps the OS processes but
        keeps handles pollable for a still-inspectable closed conversation),
        this reclaims the handles that would otherwise leak for the process
        lifetime. Safe no-op when the registry is unbuilt."""
        registry = self._process_registry
        if registry is not None:
            registry.purge_session(session_root_task_id)

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
        background exit records ``BackgroundShellExited`` durably but drives no
        wake-and-notify turn (oneshot / lifecycle / tests). Idempotent."""
        object.__setattr__(self, "_background_notifier", notifier)

    def _on_background_exit(
        self, session_id: str, job_id: str, summary: str, ref: ContentRef
    ) -> None:
        """ProcessRegistry watcher hook — hand the completion to a drive thread.

        Mechanism C. Runs on the watcher's daemon thread, so
        it MUST NOT block: it spawns a short-lived daemon drive thread that runs
        the three-state push and returns immediately. No-op when no notifier is
        wired (the push is opt-in; the durable ``BackgroundShellExited`` is the
        authoritative record regardless)."""
        notifier = self._background_notifier
        if notifier is None:
            return
        threading.Thread(
            target=self._drive_background_exit,
            args=(notifier, session_id, job_id, summary, ref),
            name=f"noeta-bg-notify-{job_id}",
            daemon=True,
        ).start()

    def _drive_background_exit(
        self,
        notifier: Any,
        session_id: str,
        job_id: str,
        summary: str,
        ref: ContentRef,
    ) -> None:
        """Three-state completion push (turn-boundary drain).

        Runs on a dedicated daemon thread off the watcher:

        * **terminal** — the session reached a terminal state (cancelled /
          failed); there is no turn to wake. Drop the drive — the
          ``BackgroundShellExited`` stays for audit / the read model (05).
        * **idle-suspended on NEXT_GOAL** — wake + drive a fresh turn now via
          ``notify_background_exit`` (the notice folds into the agent's view).
        * **mid-turn / any other suspend** — ``notify_background_exit`` raises
          (``_require_human_suspend`` rejects a non-next-goal-suspended task). We
          swallow it as a no-op: the notice is NOT injected mid-turn (non-
          preemptive), and the durable Exited event will be picked up when the
          model next polls — v1 does not auto-re-arm a deferred push (a follow-up
          enhancement; tracked in the issue-02 report). Either way the fact is
          durably recorded, so nothing is lost."""
        task = fold(self.event_log, self.content_store, session_id)
        if task.status == "terminal":
            # No turn to wake — the session is done; leave the event for audit.
            return
        try:
            notifier.notify_background_exit(
                session_id, summary=summary, ref=ref, job_id=job_id
            )
        except Exception:  # noqa: BLE001 — non-preemptive: mid-turn raises here
            # Not suspended on the next-goal handle (mid-turn, or on an
            # approval/subtask wake). The notice is deferred — the durable
            # Exited event already records the completion; the model surfaces it
            # on its next poll. A background backstop must never crash.
            _log.debug(
                "background-exit notice for job %s deferred (session %s not "
                "idle-suspended on next-goal)",
                job_id,
                session_id,
            )

    # -- background sub-agent (docs/adr/background-subagent.md) -------------

    #: How long the Mechanism-C delivery thread keeps re-attempting while the
    #: parent session is still mid-turn (the background child finished before the
    #: parent's spawning turn settled to its next-goal suspend). Bounded so a
    #: never-settling parent does not leak a daemon thread; a settled parent
    #: delivers on the first attempt (no wait). Determinism-safe — retrying only
    #: changes WHEN the notice turn is injected, never the recorded bytes.
    _BG_SUBAGENT_DELIVER_TIMEOUT_S = 30.0
    _BG_SUBAGENT_DELIVER_POLL_S = 0.05

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

    def forget_background_subagents(self, session_root_task_id: str) -> None:
        """Drop a session's background sub-agent tracking (cancel/close cascade).

        The in-flight drives are torn down cooperatively by the cancel registry
        (``request_cancellation`` → the ``DrainHost.cancel_check`` each child step
        polls); this frees the per-session cap table so a reopened session starts
        clean. Safe no-op when the registry is unbuilt."""
        registry = self._background_subagents
        if registry is not None:
            registry.forget_session(session_root_task_id)

    def _on_background_subagent_exit(
        self, parent_task_id: str, child_task_id: str
    ) -> None:
        """Registry deliver hook — hand a finished background child to a thread.

        Mechanism C, mirroring :meth:`_on_background_exit`. Runs on the executor
        worker (the drive's done-callback), so it MUST NOT block: it spawns a
        short-lived daemon thread that runs the deferred-tolerant delivery and
        returns at once. No-op until a notifier is wired (the durable child
        terminal + the parent's ``BackgroundSubagentStarted`` are the record)."""
        notifier = self._background_notifier
        if notifier is None:
            return
        threading.Thread(
            target=self._drive_background_subagent_exit,
            args=(notifier, parent_task_id, child_task_id),
            name=f"noeta-bg-subagent-notify-{child_task_id}",
            daemon=True,
        ).start()

    def _drive_background_subagent_exit(
        self, notifier: Any, parent_task_id: str, child_task_id: str
    ) -> None:
        """Deliver a background sub-agent's result at the parent's turn boundary.

        Reads the child's REAL terminal disposition from its own EventLog
        (dropping a cancelled child — the session is being torn down), then waits
        for the parent to be idle-suspended on the next-goal handle and drives the
        Mechanism-C notice turn. The wait is bounded: a parent still mid-turn
        (its spawning turn outran the child) is retried until it settles; a
        terminal parent (cancelled / failed session) drops the delivery."""
        result = self._background_subagent_result(child_task_id)
        if result is None:
            return  # cancelled / nothing to deliver
        status, ref, summary = result
        deadline = time.monotonic() + self._BG_SUBAGENT_DELIVER_TIMEOUT_S
        while True:
            task = fold(self.event_log, self.content_store, parent_task_id)
            if task.status == "terminal":
                return  # no turn to wake — session is done; child terminal stands
            try:
                notifier.notify_background_subagent_exit(
                    parent_task_id,
                    subtask_id=child_task_id,
                    summary=summary,
                    ref=ref,
                    status=status,
                )
                return
            except Exception:  # noqa: BLE001 — parent mid-turn: retry until idle
                if time.monotonic() >= deadline:
                    _log.debug(
                        "background sub-agent %s notice deferred (parent %s never "
                        "settled to next-goal within %.0fs)",
                        child_task_id, parent_task_id,
                        self._BG_SUBAGENT_DELIVER_TIMEOUT_S,
                    )
                    return
                time.sleep(self._BG_SUBAGENT_DELIVER_POLL_S)

    def _background_subagent_result(
        self, child_task_id: str
    ) -> Optional[Tuple[str, ContentRef, str]]:
        """Project a background child's terminal into ``(status, ref, summary)``.

        ``ref`` is a ContentStore snapshot of the full result (the model derefs
        it); ``summary`` is the one-line notice body. Returns ``None`` for a
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
                f'Background sub-agent "{agent_name}" finished. '
                "Its full result is referenced below — read it and continue.",
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
            f'Background sub-agent "{agent_name}" did not complete: {detail[:200]}',
        )

    # -- ResidentHost requirement ------------------------------------------

    @property
    def agent_registry(self) -> AgentRegistryProtocol:
        """Satisfy :class:`ResidentHost.agent_registry` (structural seam)."""
        return self.registry

    # -- provider registry ------------------------------

    @property
    def default_provider_instance(self) -> LLMProvider:
        """The host default provider instance (back-compat single-provider read).

        After ``provider`` (single instance) folds into the
        ``providers`` table, this accessor returns the **default** provider
        instance from the folded table — a stable entry point for readers that
        care only about a single provider. ``_build_engine`` goes through
        :meth:`_provider_for` to fetch the instance by bound name, not this.
        (The name avoids the convenience input field ``provider``.)
        """
        return self.providers[self.default_provider]

    def _provider_for(self, name: Optional[str]) -> LLMProvider:
        """Resolve a bound provider **name** → its instance.

        ``None`` (no provider bound — an old recording, or a turn that only
        switched the model) ⇒ the host :attr:`default_provider`, byte-identical
        to the pre-I4 single-provider path. The driver/server validated the
        ``(provider, model)`` pair against this same registry *before* any
        durable write, so a bound name is always a configured key here.

        #7 resume fallback: if the name is non-empty but not found in the current
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
        # Historical recordings +
        # product-facing aliases: map a
        # legacy recording name (e.g. "default") to the canonical registered name
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
        config store — credentials stay product-side, D3), then connect them all
        with :func:`noeta.tools.mcp.build_mcp_tools` and return the discovered
        ``mcp__{alias}__{tool}`` ``McpTool`` dict. Aliases are sorted (D7
        determinism: alias alphabetical order → tool-name order inside
        ``build_mcp_tools``), so the tool dict order → schema order → stable hash
        is reproducible.

        **D7 connection lifecycle** — this runs once per BUILT Engine (the
        resolver caches on the 7-tuple incl. ``mcp_aliases``), so the connect +
        the tool-set freeze happen exactly once at task start, never mid-turn.
        Failure is **skip-on-failure (option B)**: one enabled server that cannot
        connect / handshake / ``tools/list`` is dropped, recorded as one
        ``McpServerSkipped`` observer event on ``task_id``'s stream (the
        front-end surface), and the build continues with the surviving servers'
        tools — a single bad connector never sinks the task. The alias is a clean
        name; no url/token ever enters the event (D3).

        Returns ``None`` when no live MCP tools resulted — empty aliases, no
        resolver wired, no alias resolved to a spec, OR every server was skipped —
        so the caller passes ``None`` as ``mcp_tools_override`` and the builder
        merges no MCP tools (tool set unchanged from pre-0042; resume passes empty
        aliases and so never reaches here). A returned dict takes the live override
        path.
        """
        # Clear any clients staged by a prior build that never reached the
        # cache put (so a no-MCP build can never adopt stale clients). The cache
        # consumes-and-clears on each ``__setitem__``; this guards the rare
        # build-without-put path (e.g. an exception after staging).
        self._stage_mcp_clients([])
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
        # Record the per-task MCP provenance (enabled aliases +
        # tool subsets, names only, NO credentials) the moment we know which
        # servers resolved, BEFORE the connect (so a server that then fails to
        # connect — and gets a McpServerSkipped — still shows in the provenance as
        # "enabled this run"). Emitted in the pre-loop window (resolve_engine runs
        # before the first step), origin observer, so the fold rebuilds the same
        # GovernanceState.mcp_provenance from the event on resume.
        # Only on the live connect path (task_id present); the seed/by-name build
        # passes task_id=None and never reaches here, and resume passes empty
        # aliases — so no provenance event, identical to pre-0042.
        if task_id:
            self.event_log.system_emit(
                task_id=task_id,
                type="McpProvenanceRecorded",
                payload=McpProvenanceRecordedPayload(
                    servers=mcp_provenance_from_specs(specs)
                ),
                actor="mcp",
                origin="observer",
            )
        tools, clients, skipped = build_mcp_tools(
            tuple(specs), http_post=self.mcp_http_post, skip_on_failure=True
        )
        # Stage the live clients so the engine cache adopts them when the base
        # resolver puts the just-built Engine (``self._engines[key] = engine``),
        # and shuts them down when that Engine is evicted from the LRU. Without
        # this the McpStdioClient subprocess + its fds would leak on eviction.
        self._stage_mcp_clients(clients)
        # D7: record one observer event per skipped server (front-end surface +
        # audit trail). Only possible once the task exists; the seed/by-name build
        # passes ``task_id=None`` and never connects MCP (see driver.start), so a
        # skip without a task_id is not reachable on the live path. Defensive:
        # only emit when we have a stream to write to.
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

    def _build_engine(
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
        structured_output_schema: Optional[dict[str, Any]] = None,
    ) -> Engine:
        spec = agent
        # ``workspace`` is now the per-session workspace **absolute
        # path** (welded into durable state, expanded by the agent layer; the
        # driver receives only the final path). ``None`` (no session workspace)
        # keeps the host-fixed default dir, byte-identical to the
        # single-workspace path.
        workspace_dir = Path(workspace) if workspace else self.workspace_dir
        # D3: only a custom tool explicitly named by spec.tools enters the engine.
        spec_tool_names = frozenset(r.name for r in spec.tools)
        filtered_custom = {
            n: t for n, t in self.custom_tools.items() if n in spec_tool_names
        }
        # Derive the approval gate set from the three permission modes.
        # A per-turn ``permission_mode`` (the frontend
        # selector, threaded in NON-durably) has the HIGHEST priority — it must
        # win even over an explicit host ``require_approval_tools`` (the code
        # product wires ``()`` by default), else the per-session switch is a
        # no-op. ``None`` (no per-turn selection: resume / daemon / CLI / every
        # pre-#4 path) falls through to the unchanged precedence below, so those
        # paths behave exactly as before.
        # An explicit require_approval_tools wins over the host permission_mode.
        if permission_mode is not None:
            require_approval_tools = _approval_set_for(permission_mode, spec.tools)
        elif self.require_approval_tools is not None:
            require_approval_tools = self.require_approval_tools
        else:
            require_approval_tools = _approval_set_for(
                self.permission_mode, spec.tools
            )
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
        shell_approval_predicate: Optional[
            Callable[[str, Mapping[str, Any]], bool]
        ] = None
        if self.shell_mode is not ShellMode.OFF:
            shell_mode = ShellMode.ARBITRARY
            if effective_permission != "bypassPermissions":
                effective_rules = build_allowlist(
                    tuple(self.shell_allowlist)
                    + load_project_shell_allowlist(workspace_dir)
                )
                shell_approval_predicate = _make_shell_approval_predicate(
                    effective_rules
                )
                # The predicate owns shell_run's gate now; drop it from the
                # static set so an allowlisted command is NOT also force-gated.
                require_approval_tools = tuple(
                    n for n in require_approval_tools if n != "shell_run"
                )
        # Build a sorted (name, description) directory of
        # the delegation-allowed sub-agents so spawn_subagent's JSON schema
        # surfaces the roster to the model. Keep the schema unchanged from
        # legacy sessions when no description is available (or the set is empty)
        # by passing the empty-tuple default.
        directory: tuple[tuple[str, str], ...] = ()
        if delegation_enabled and allowed_subtask_agents:
            entries = []
            for n in sorted(allowed_subtask_agents):
                try:
                    child = self.registry.resolve(n)
                except Exception:
                    continue
                entries.append(
                    (n, str(child.metadata.get("description", "")))
                )
            if any(d for _, d in entries):
                directory = tuple(entries)
        # Resolve the turn's enabled MCP aliases → host-side
        # server specs (with url/credentials) → LIVE ``McpTool``s, connecting each
        # server now (deterministic alias-sorted order, following F2's
        # fs→script→MCP→control append order). The alias list arrived NON-durably
        # (no url/token in any request,
        # D3); the specs come from the product-injected ``mcp_server_resolver`` (the
        # SDK never holds the config store). ``()`` aliases / ``None`` resolver ⇒
        # ``build_mcp_tools(())`` builds nothing (tool set unchanged from pre-0042). The
        # connected clients are owned by the cached Engine for this session
        # (mirroring the CLI ``AgentSessionRunner`` path, which holds them for the
        # session's life). R-1 keeps resume reconnect-free: the recorded tool spec
        # is the durable truth, and the resume path passes empty aliases.
        mcp_tools_override = self._resolve_live_mcp_tools(mcp_aliases, task_id=task_id)
        inputs = build_session_inputs(
            workspace_dir=workspace_dir,
            system_prompt=spec.instructions,
            allowed_tools=spec_tool_names,
            content_store=self.content_store,
            model=model,
            compaction=derive_compaction_config(model),
            # Session-level budget override; None uses today's spec-derived path.
            budget=self.budget
            if self.budget is not None
            else self._budget_for(spec.default_budget),
            allowed_subtask_agents=allowed_subtask_agents,
            max_steps=self.max_steps,
            write_mode=self.write_mode,
            shell_mode=shell_mode,
            shell_allowlist=self.shell_allowlist,
            shell_approval_predicate=shell_approval_predicate,
            # A spec carrying metadata["write_path_globs"]
            # gets its ``write`` built path-restricted (e.g. plans/*.md); other specs ⇒ ().
            write_path_globs=_spec_write_path_globs(spec),
            skills_dir=self.skills_dir,
            builtin_skills_dirs=self.builtin_skills_dirs,
            global_skills_dir=self.global_skills_dir,
            skill_tool_enforcement=self.skill_tool_enforcement,
            delegation_enabled=delegation_enabled,
            allow_skill_scripts=self.allow_skill_scripts,
            todo_write_enabled=spec.capabilities.todo_write,
            ask_user_question_enabled=ask_user_question_enabled,
            # The SDK host treats spec.capabilities as the source of truth; the
            # noeta-agent product treats CodeSessionConfig as the source of truth
            # and does not read capabilities (see apps/noeta-agent session.py), so
            # migrating a custom spec across hosts requires aligning the two by
            # hand.
            skill_invocation_enabled=spec.capabilities.skill_invocation,
            # Expose run_workflow only when the host
            # enabled workflow AND this agent can delegate. A workflow's
            # agent()/parallel() spawn real sub-agents into the same delegation
            # allow-list, so a non-delegating agent could never run one — gating
            # run_workflow on delegation keeps the tool surface honest (only a
            # delegation-capable agent ever sees it). The reserved __workflow__
            # child is intercepted in _build_orchestration_engine, so it never
            # reaches this builder.
            workflow_enabled=self.workflow_allowed and delegation_enabled,
            # Per-helper structured output (port of the deleted runner's
            # ``_build_child_engine`` wiring): a workflow helper spawned via
            # ``agent(goal, schema=...)`` mounts the ``structured_output``
            # control schema (its ``parameters`` = the declared JSON Schema).
            # ``None`` (every non-helper build) keeps the tool set + View
            # stable hash byte-identical.
            structured_output_schema=structured_output_schema,
            memory_enabled=spec.capabilities.memory,
            memory_dir=self.memory_dir,
            global_memory_dir=self.global_memory_dir,
            instructions_enabled=self.instructions_enabled,
            instructions_file=self.instructions_file,
            # When live MCP tools were resolved for this turn, they are passed as
            # the override. ``None`` override ⇒ the builder merges no MCP tools —
            # so resume, which passes empty aliases, gets the same tool set as
            # before 0042.
            mcp_tools_override=mcp_tools_override,
            custom_tools=filtered_custom,
            # Thread the host's live preview gateway so this engine's
            # tool set gains ``open_app``. ``None`` (oneshot / tests / resume) ⇒
            # no open_app, so the tool set is unchanged.
            app_gateway=self.app_gateway,
            hooks_pre_tool_use=self.hooks_pre_tool_use,
            repetition_threshold=self.repetition_threshold,
            repetition_action=self.repetition_action,
            repetition_window=self.repetition_window,
            require_approval_tools=require_approval_tools,
            subtask_agent_directory=directory,
            # Wiring-only LLM controls: session-wide override propagated
            # to every LLMRequest the ReActPolicy builds.
            output_schema=self.output_schema,
            thinking=self.thinking,
            effort=effort if effort is not None else self.effort,
            # Microcompact — engine-level inline truncation cap.
            tool_output_inline_limit=self.tool_output_inline_limit,
            # SDK Options
            # extension points. Fed to live + resume from the same host fields.
            policy_factory_override=self.policy_override,
            extra_guards=self.extra_guards,
            extra_content_kinds=self.extra_content_kinds,
        )
        # Route to the bound provider's adapter instance
        # (``None`` ⇒ host default), so a session on a different provider runs
        # its LLM round-trips on that adapter — the registry, not a host-fixed
        # single instance, is the source.
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
        # Per-helper structured output: the "structured receipt"
        # wrapper intercepts the helper's decisions — a ``structured_output``
        # call becomes the helper's final answer; an end_turn without one is
        # nudged (at most twice), then failed. Wrapped OUTERMOST; only the
        # subtask drain ever passes a schema, and a child engine never carries
        # the multi-turn ``policy_wrapper``, so the two wrappers never stack.
        if structured_output_schema is not None:
            policy = StructuredOutputPolicy(
                inner=policy, schema=structured_output_schema
            )
        return Engine(
            event_log=self.event_log,
            content_store=self.content_store,
            composer=inputs.composer,
            policy=policy,
            tools=inputs.tools,
            hooks=inputs.hooks,
            # Generation switch: mid-loop activations
            # emit the generic ContextContentRecorded (kind="skill",
            # policy="pinned") through the registry-derived generic seam.
            # The pre-loop path (activate_skills helper) fires its own
            # generic event before the patch; the engine guards against
            # first-only re-emission so exactly one event per (task, skill).
            content_hashes=inputs.content_hashes,
            tool_output_inline_limit=inputs.tool_output_inline_limit,
            # The host's background-shell registry, so a session's
            # ``shell_run(background=true)`` reaches it through the ToolContext.
            background_runner=self._process_registry,
            # The host's per-turn file-checkpoint gate, so an AI
            # ``edit`` / ``write`` stashes its rewind baseline.
            file_checkpoint_registry=self._file_checkpoint,
            # background sub-agent launch+capacity seam, wired ONLY on a
            # top-level interactive Engine (``policy_wrapper`` is the multi-turn
            # wrapper — present only there). A child engine / oneshot host gets
            # ``None`` so ``spawn_subagent(background=True)`` degrades to the
            # foreground barrier spawn — which is exactly the wanted "no nested
            # background" behaviour (docs/adr/background-subagent.md).
            background_subagent_launcher=(
                self._background_subagents if policy_wrapper is not None else None
            ),
        )

    def _build_orchestration_engine(
        self, task_id: str, *, allowed_subtask_agents: frozenset[str]
    ) -> Engine:
        """Build the ``__workflow__`` child's Engine.

        Mirrors the runner path's ``_build_orchestration_engine``: read the
        script/args off the child's durable ``TaskCreated.inputs`` and build an
        Engine whose Policy is :class:`OrchestrationPolicy`. Its ``agent()`` calls
        delegate into ``allowed_subtask_agents`` (the inherited worker set, filtered
        to roster names); ``workflow_enabled`` is OFF (no nested workflows, v1).
        """
        wf_inputs = self._read_task_inputs(task_id)
        script = str(wf_inputs.get("script", ""))
        raw_args = wf_inputs.get("args")
        wf_args = dict(raw_args) if isinstance(raw_args, dict) else {}
        known = self._spawnable_set(allowed_subtask_agents)
        directory: tuple[tuple[str, str], ...] = ()
        if known:
            entries = []
            for n in sorted(known):
                try:
                    child = self.registry.resolve(n)
                except Exception:
                    continue
                entries.append((n, str(child.metadata.get("description", ""))))
            if any(d for _, d in entries):
                directory = tuple(entries)
        inputs = build_session_inputs(
            workspace_dir=self.workspace_dir,
            system_prompt=WORKFLOW_SYSTEM_PROMPT,
            allowed_tools=frozenset(),
            content_store=self.content_store,
            model=self.model,
            compaction=derive_compaction_config(self.model),
            budget=self.budget
            if self.budget is not None
            else self._budget_for(BudgetSpec()),
            allowed_subtask_agents=known,
            subtask_agent_directory=directory,
            max_steps=self.max_steps,
            write_mode=self.write_mode,
            shell_mode=self.shell_mode,
            shell_allowlist=self.shell_allowlist,
            skills_dir=self.skills_dir,
            skill_tool_enforcement=self.skill_tool_enforcement,
            delegation_enabled=True,
            workflow_enabled=False,
            allow_skill_scripts=self.allow_skill_scripts,
            todo_write_enabled=False,
            ask_user_question_enabled=False,
            skill_invocation_enabled=False,
            memory_enabled=False,
            memory_dir=self.memory_dir,
            instructions_enabled=self.instructions_enabled,
            instructions_file=self.instructions_file,
            tool_output_inline_limit=self.tool_output_inline_limit,
        )
        policy: Policy = OrchestrationPolicy(script=script, args=wf_args)
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

        ``workspace`` is now the **absolute path** welded into durable state (see
        ``TaskHostBoundPayload.workspace_dir``); this method simply converts it to
        a ``Path``.  ``None`` (no session workspace bound, or an old
        legacy name-style recording that folds to ``None`` per the D7 break) ⇒ the
        host-fixed :attr:`workspace_dir`, byte-identical to the
        single-workspace path.

        Called by product-layer callers (e.g. the HTTP approval handler) that
        need the SAME fs root the engine used — now trivially ``Path(workspace)``
        since the path is already absolute in durable state.
        """
        if not workspace:
            return self.workspace_dir
        p = Path(workspace)
        if p.is_absolute():
            return p
        # Fallback for any residual non-absolute value: treat as host default
        # (defensive; should not happen once the per-session workspace path is fully wired).
        return self.workspace_dir

    def session_content_snapshots(
        self, workspace: Optional[str]
    ) -> tuple[Optional[EnvironmentSnapshot], Optional[InstructionsSnapshot]]:
        """The (environment, instructions) snapshots for a session's workspace.

        The seam the :class:`~noeta.execution.driver.InteractionDriver`'s
        ``seed_start`` reads to pre-loop-activate the environment / instructions
        content channels (the same activation the resident
        ``AgentSessionRunner.prepare()`` does via ``build_session_inputs``). Until
        this existed, the server seed path appended only the goal + activated
        skills, so server-created tasks emitted **no**
        ``ContextContentRecorded(kind=environment|instructions)`` — the model
        never saw the working dir / git / platform block.

        Snapshots are loaded from the SAME ``(workspace_dir, instructions_file)``
        resolution :meth:`_build_engine` feeds ``build_session_inputs``, via the
        SAME pure loaders (:func:`load_environment` /
        :func:`load_instructions`), so the snapshot the driver records the
        fingerprint of is byte-equal to the one this session's composer renders
        from. Environment is always present (a workspace always exists);
        instructions is ``None`` when the file is missing/empty OR the host has
        ``instructions_enabled`` off (zero footprint, byte-equal to a host that
        never configured a project instructions file).
        """
        workspace_dir = self.workspace_dir_for(workspace)
        environment = load_environment(workspace_dir)
        instructions: Optional[InstructionsSnapshot] = None
        if self.instructions_enabled:
            instructions = load_instructions(
                workspace_dir, override_path=self.instructions_file
            )
        return environment, instructions

    def memory_recall_context(
        self, agent: str
    ) -> Optional[tuple[MemoryStore, MemoryEntries]]:
        """The (store, entries-snapshot) pair for ``agent``'s memory recall.

        The seam the :class:`~noeta.execution.driver.InteractionDriver`'s seed
        path (``seed_start`` / ``seed_send_goal``) reads to run the deleted
        runner's prepare-time memory wiring: record the index resident
        (``ContextContentRecorded`` kind=memory, policy=evolving) and route the
        incoming goal through ``append_user_message_with_recall`` so hits land
        as one ``origin="memory"`` turn. Retrieval therefore happens on the
        WRITE side (at recording time), never at compose time — the composer
        stays a pure function of folded state.

        Returns ``None`` when the agent's spec lacks ``Capabilities.memory``
        (only the ``main`` preset enables it), so a memory-off agent's stream
        stays byte-identical to the pre-seam path. The store root resolution is
        the SAME precedence :func:`~noeta.execution.builder.build_session_inputs`
        uses for the tools + resident index (``memory_dir`` override >
        ``global_memory_dir`` > the SDK global default), so recall reads exactly
        the store the session's ``memory_write`` / ``memory_read`` tools use.
        The global default is read late off the module (not from-imported) so a
        test pinning ``noeta.execution.memory.DEFAULT_GLOBAL_MEMORY_DIR`` stays
        hermetic. An empty / missing directory is a valid empty store
        (``entries == ()``): the index record no-ops and recall never hits, so
        the default flow pays zero bytes.
        """
        if agent == "unnamed" and self.unnamed_fallback is not None:
            spec = self.unnamed_fallback
        else:
            spec = self._lookup_agent(agent, task_id="<unbound>")
        if not spec.capabilities.memory:
            return None
        memory_root = (
            self.memory_dir
            if self.memory_dir is not None
            else (
                self.global_memory_dir
                if self.global_memory_dir is not None
                else execution_memory.DEFAULT_GLOBAL_MEMORY_DIR
            )
        )
        store = execution_memory.load_memory_store(root=memory_root)
        return store, store.entries()

    @staticmethod
    def _budget_for(spec_budget: BudgetSpec) -> Budget:
        """Translate a declared :class:`BudgetSpec` into a live :class:`Budget`.

        Per-slice 4b decision (design notes §4b "when BudgetSpec is all None…"):
        SDK path uses :class:`Budget`'s own field defaults (all
        ``None`` caps) — not the coding-product's
        ``default_coding_budget()``. A ``BudgetSpec`` field that is
        ``None`` delegates to the ``Budget()`` default so the SDK path
        can independently tighten caps without re-implementing the
        guard.
        """
        overrides = {
            k: v
            for k, v in dataclasses.asdict(spec_budget).items()
            if v is not None
        }
        return dataclasses.replace(Budget(), **overrides)
