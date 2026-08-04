"""The single construction point a live run and a resume share: it assembles one
session's tools, composer, policy factory and guards from explicit inputs.

Byte-stable construction is the constraint that shapes everything here — a
resumed turn must rebuild the SAME tool set, composer and guards from the same
inputs, because the stable-prefix prompt cache only hits on a byte-stable prefix.
The builder therefore enumerates no capability: every contribution arrives as a
session pack or a control-tool entry and merges through one ``(priority,
name)``-ordered loop. Those priority bands (fs=100 → web=200 → memory=300 →
instructions=400 → environment=500 → skills=600 → browser=700 → mcp=800 →
custom=900 → app=1000) are the construction-order contract, because tool dict
insertion order feeds the Engine's ``ToolSchemaRecorded`` emission; they are
locked by ``tests/test_session_pack_goldens.py``, so do not renumber them.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from noeta.context.composer import ThreeSegmentComposer
from noeta.context.content_channel import ContentChannelRegistry, ContentKindSpec
from noeta.context.reminders import ReminderRegistry, ReminderSpec
from noeta.core.hooks import HookManager
from noeta.execution.session_pack import (
    EMPTY_CONTRIBUTION,
    InitHook,
    PackContribution,
    SessionBuildContext,
    SessionPackEntry,
)
from noeta.runtime.governance import (
    Budget,
    PreToolUseRule,
    RepetitionAction,
)
from noeta.execution.control_tool import (
    AskAnswerCodec,
    ControlToolBuildContext,
    ControlToolEntry,
    ControlToolMount,
)
from noeta.policies.control_semantics import ControlToolSpec
from noeta.protocols.content_store import ContentStore
from noeta.protocols.hooks import Guard
from noeta.protocols.policy import Policy
from noeta.protocols.tool import Tool
from noeta.runtime.exec_env import ExecEnv
from noeta.runtime.workspace import WorkspaceRoot
from noeta.runtime.mcp import MCP_PREFIX, McpConfigError


__all__ = [
    "CompactionConfig",
    "COMPACTION_OFF",
    "SessionInputs",
    "build_session_inputs",
]


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    """The deterministic compaction knobs for one ``(agent, model)`` session.

    ``context_window is None`` ⇒ compaction OFF. When set, the policy's available
    window is ``context_window - max_output_tokens - compaction_buffer`` and the
    composer protects / the policy summarises against ``tail_token_budget``.
    """

    context_window: Optional[int]
    max_output_tokens: int
    compaction_buffer: int
    tail_token_budget: Optional[int]
    composer_version: str


#: Compaction disabled — the default for any model the catalog does not describe.
COMPACTION_OFF = CompactionConfig(
    context_window=None,
    max_output_tokens=0,
    compaction_buffer=0,
    tail_token_budget=None,
    composer_version="",
)


#: The catalog-driven derivation of these knobs lives in the ``providers``
#: built-in plugin; the kernel takes them pre-resolved and holds no model
#: opinions of its own.


@dataclass(frozen=True, slots=True)
class SessionInputs:
    """Everything an Engine needs for one session, live or resumed.

    The pieces are mutually consistent by construction: the policy factory is
    bound to the same ``(tools, model, compaction)`` triple the composer sees,
    and the guards are registered in the same deterministic order every time, so
    a resumed turn reproduces the recording's guard-origin events.
    """

    tools: dict[str, Tool]
    composer: ThreeSegmentComposer
    #: Widened return type because ``policy_factory_override`` may substitute
    #: any :class:`~noeta.protocols.policy.Policy` for the default.
    policy_factory: Callable[[Any], Policy]
    hooks: HookManager
    #: The ``(kind, name) → (version, hash)`` resolver derived from the same
    #: content-channel registry the composer renders from, so a mid-loop
    #: activation records the fingerprints the composer's kinds declare.
    content_hashes: Callable[[str, str], Optional[tuple[str, str]]]
    #: Inline char cap for tool results before they become messages; ``None`` ⇒
    #: no truncation. A resuming host MUST wire the same value, or the rebuilt
    #: messages diverge from the recording.
    tool_output_inline_limit: Optional[int] = None
    #: Anchored-content seams, built only when instructions discovery is armed:
    #: the post-tool activation hook and the per-step resume re-read the host
    #: wires onto the Engine.
    content_discovery: Optional[Any] = None
    content_preloader: Optional[Any] = None
    #: The contributed pre-loop ``init`` hooks as ``(plugin, hook)`` pairs in
    #: pack-loop order; the driver runs each through a recorder bound to its
    #: plugin name at seed time.
    init_hooks: tuple[tuple[str, InitHook], ...] = ()
    #: The ``ask_user_question`` mount's answer codec, which the host puts on the
    #: Engine for the driver's ``answer`` path.
    answer_codec: Optional[AskAnswerCodec] = None


class GuardsFactory(Protocol):
    """Loader-resolved constructor of the default guard stack.

    The kernel never imports a guard class: the host resolves the ``governance``
    built-in plugin's factory through the plugin loader and injects it here. The
    kernel calls it with the finished tool assembly, the packs' opaque
    ``guard_facts`` bundle, and the operator passthrough fields.
    """

    def __call__(
        self,
        *,
        tools: dict[str, Tool],
        budget: Budget,
        require_approval_tools: tuple[str, ...],
        shell_approval_predicate: Optional[
            Callable[[str, Mapping[str, Any]], bool]
        ],
        guard_facts: Optional[Any],
        allowed_subtask_agents: Optional[frozenset[str]],
        repetition_threshold: int,
        repetition_action: RepetitionAction,
        repetition_window: int,
        hooks_pre_tool_use: tuple[PreToolUseRule, ...],
        extra_guards: tuple[Guard, ...],
    ) -> HookManager: ...


class PolicyFactoryBuilder(Protocol):
    """Loader-resolved constructor of the default policy factory.

    The kernel never imports the decision-mapping policy: the host resolves the
    ``react`` built-in plugin's factory builder through the plugin loader and
    injects it here, and it returns the ``(llm) -> Policy`` factory. A
    ``policy_factory_override`` still replaces the default outright.
    """

    def __call__(
        self,
        *,
        tools: dict[str, Tool],
        system_prompt: str,
        model: str,
        max_steps: int,
        #: The routing-ordered translate specs the mount loop produced. Mounting
        #: IS enablement, so a disabled tool simply contributes no spec.
        control_translate_specs: tuple[ControlToolSpec, ...],
        content_store: ContentStore,
        context_window: Optional[int],
        max_output_tokens: Optional[int],
        compaction_buffer: Optional[int],
        tail_token_budget: int,
        composer_version: Optional[str],
        output_schema: Optional[dict[str, Any]],
        thinking: Optional[str],
        effort: Optional[str],
        compaction_model: Optional[str],
        compaction_max_output_tokens: Optional[int],
    ) -> Callable[[Any], Policy]: ...


@dataclass(frozen=True, slots=True)
class _BuildSpec:
    """All operator inputs to one session build, frozen.

    The internal mirror of :func:`build_session_inputs`'s keyword parameters, so
    the pipeline stages read one read-only object instead of closing over thirty
    locals. It stays a SEPARATE struct because the public signature is itself a
    contract — resume must pass the same params to rebuild identically, and a
    test asserts on ``inspect.signature``.
    """

    workspace_dir: Path
    system_prompt: str
    allowed_tools: frozenset[str]
    content_store: ContentStore
    model: str
    compaction: CompactionConfig
    budget: Budget
    allowed_subtask_agents: frozenset[str]
    max_steps: int
    require_approval_tools: tuple[str, ...]
    shell_approval_predicate: Optional[Callable[[str, Mapping[str, Any]], bool]]
    #: The effective capability flags by name (agent activation × host
    #: kill-switch, ANDed by the host) — the ONE generic bag both the
    #: session packs and the control-tool mounts self-gate on.
    capability_flags: Mapping[str, bool]
    structured_output_schema: Optional[dict[str, Any]]
    mcp_tools_override: Optional[dict[str, Tool]]
    custom_tools: Optional[dict[str, Tool]]
    #: Execution backend for the fs / shell pack. ``None`` ⇒ the host; a sandbox
    #: ``ExecEnv`` makes the pack act against a container and switches the
    #: workspace to lexical (container-path) containment. Wiring-only, never part
    #: of session identity — the tool schemas are the same either way.
    exec_env: Optional[ExecEnv]
    hooks_pre_tool_use: tuple[PreToolUseRule, ...]
    #: Custom Guards register after the built-in guard stack; custom content
    #: channels append after the built-in residents.
    extra_guards: tuple[Guard, ...]
    extra_content_kinds: tuple[ContentKindSpec, ...]
    #: Per-agent-activated compose-time reminders, interleaved by priority with
    #: the injected base reminders.
    extra_reminders: tuple[ReminderSpec, ...]
    repetition_threshold: int
    repetition_action: RepetitionAction
    repetition_window: int
    subtask_agent_directory: tuple[tuple[str, str], ...]
    output_schema: Optional[dict[str, Any]]
    thinking: Optional[str]
    effort: Optional[str]
    compaction_model: Optional[str]
    compaction_max_output_tokens: Optional[int]
    tool_output_inline_limit: Optional[int]
    #: Loader-resolved built-in compose-time reminders: the renders the
    #: ``reminders`` built-in plugin declares, resolved by the SDK host and
    #: injected here. ``None`` fails loudly at the reminder-registry phase — the
    #: kernel never imports a renderer.
    base_reminders: Optional[tuple[ReminderSpec, ...]] = None
    #: Loader-resolved default guard-stack factory:
    #: ``noeta.builtins.governance.impl:build_default_guards``, resolved by the
    #: SDK host and injected here. ``None`` fails loudly at the guards phase —
    #: the kernel never imports a guard class.
    guards_factory: Optional[GuardsFactory] = None
    #: The bound model's vendor family (``"anthropic"`` / ``"openai"`` /
    #: ``None``), resolved by the SDK host from the providers built-in's
    #: catalog and exposed to packs through the context
    #: (the fs pack keys its own edit-tool mutex on it). ``None``
    #: (kernel-alone / stub) drops neither edit tool — the documented
    #: no-catalog semantic, NOT a silent fallback.
    provider_family: Optional[str] = None
    #: The manifest-contributed session packs: resolved
    #: by the SDK host (``noeta.client.parts.default_session_packs`` + the
    #: external plugins' ``session_pack`` projection) and run by the generic
    #: pack loop in ``(priority, name)`` order. Empty builds a session with
    #: no pack tools at all — the kernel mandates no capability.
    session_packs: tuple[SessionPackEntry, ...] = ()
    #: The contributed control tools: resolved by
    #: the SDK host (``noeta.client.parts.default_control_tools`` — the built-in
    #: ``control_tool`` contributions — + the external plugins' ``control_tool``
    #: projection) and run through the dual-priority mount loop. The kernel
    #: keeps no internal control-tool table, so this tuple is the whole
    #: input: empty ⇒ a session with zero control tools.
    control_tools: tuple[ControlToolEntry, ...] = ()


# ---------------------------------------------------------------------------
# Session packs — each owns "whether to enable + how to build/filter" and
# returns a PackContribution; the generic loop in ``build_session_inputs``
# merges them in (priority, name) order. The priority BANDS are the
# construction-order contract (fs=100 → web=200 → memory=300 →
# instructions=400 → environment=500 → skills=600 → browser=700 → mcp=800 →
# custom=900 → app=1000), load-bearing for byte-equality (tool dict insertion
# order feeds the Engine's deterministic ToolSchemaRecorded emission) and
# locked by ``tests/test_session_pack_goldens.py``; do not renumber.
# ---------------------------------------------------------------------------


# Every capability pack is a ``session_pack`` manifest contribution
# (``noeta.builtins.<name>.impl:build_*_session_pack``) resolved by the SDK
# host and passed in as ``session_packs``; the capability seam Protocols live
# in their plugins (``BrowserBackend`` → browser, ``AppPreviewGateway`` → app)
# and their live backing objects ride the generic ``backends`` bag under the
# plugins' own names. The kernel owns only the two pre-built injections below
# (mcp / custom), which ride the same loop as fixed-priority internal entries.


# ---------------------------------------------------------------------------
# Control-tool mount loop — a pure MECHANISM. The builder enumerates no
# control tool: every mount arrives as a host-supplied ``ControlToolEntry``
# (the built-in ``control_tool`` contributions the SDK host resolves from the
# manifests via ``default_control_tools()`` + the external plugins'
# ``control_tool`` projection), each a factory that self-gates on the
# ``ControlToolBuildContext`` and returns a ``ControlToolMount`` or ``None``.
# The two priority BANDS are the byte-order contract, locked by the golden:
# schema render order spawn=100 → todo=200 → ask=300 → skill=400 →
# workflow=500 → structured_output=600; decision routing order ask=100 →
# todo=200 → spawn=300 → skill=400 → workflow=500. ``structured_output``
# carries no translate (react's StructuredOutputPolicy intercepts it) so it
# contributes a schema but no routing spec.
# ---------------------------------------------------------------------------


def _mount_control_tools(
    entries: Sequence[ControlToolEntry],
    ctx: ControlToolBuildContext,
) -> tuple[
    Optional[list[dict[str, Any]]],
    tuple[ControlToolSpec, ...],
    Optional[AskAnswerCodec],
]:
    """Run the control-tool entries → ``(schema_list, routing_specs, answer_codec)``.

    The generic dual-priority mount loop, mirroring the session-pack loop: each
    factory self-gates on ``ctx`` and returns ``None`` to opt out (a mount IS
    enablement). Collision is loud — two mounts of one name raise a ``ValueError``
    naming both entries, exactly as the pack loop rejects a re-export (e.g. a
    third-party ``control_tool`` clashing with a built-in one). The two
    output orders come from each mount's OWN bands: the schema list sorts on
    ``schema_priority`` (the composer's ``control_action_schemas`` byte order),
    the routing specs on ``routing_priority`` (the decision
    dispatch order), ties broken by name in both. A mount with no ``translate``
    (``structured_output``) contributes a schema but no routing spec. An empty
    schema list folds to ``None`` (the composer's "no control schemas" sentinel).

    The third output is the mounts' :attr:`ControlToolMount.answer_codec` (a
    typed field, not a stringly bag) — single-writer across the loop (a
    second mount filling it raises); only the ``ask_user_question`` mount does.
    """
    ordered = sorted(entries, key=lambda e: (e.priority, e.name))
    mounts: list[ControlToolMount] = []
    mounted_by: dict[str, str] = {}
    for entry in ordered:
        mount = entry.factory(ctx)
        if mount is None:
            continue
        if mount.name in mounted_by:
            raise ValueError(
                f"control tool {mount.name!r} mounted twice: entries "
                f"{mounted_by[mount.name]!r} and {entry.name!r} collide on name"
            )
        mounted_by[mount.name] = entry.name
        mounts.append(mount)

    schema_list = [
        m.schema
        for m in sorted(mounts, key=lambda m: (m.schema_priority, m.name))
    ]
    routing_specs: list[ControlToolSpec] = []
    for m in sorted(mounts, key=lambda m: (m.routing_priority, m.name)):
        if m.translate is None:
            continue
        routing_specs.append(ControlToolSpec(name=m.name, translate=m.translate))
    answer_codec: Optional[AskAnswerCodec] = None
    for m in mounts:
        if m.answer_codec is None:
            continue
        if answer_codec is not None:
            raise RuntimeError(
                f"control tool {m.name!r} contributes a second answer_codec — "
                f"it is single-writer across the control-tool loop"
            )
        answer_codec = m.answer_codec
    return (schema_list or None, tuple(routing_specs), answer_codec)


def _run_control_tool_mounts(
    entries: Sequence[ControlToolEntry],
    ctx: ControlToolBuildContext,
) -> tuple[Optional[list[dict[str, Any]]], tuple[ControlToolSpec, ...]]:
    """Two-output view of :func:`_mount_control_tools` (schemas + routing specs).

    The mechanism seam ``tests/test_control_tool_mount_loop.py`` pins; the
    builder itself uses :func:`_mount_control_tools`, which additionally returns
    the mounts' answer codec.
    """
    schema_list, routing_specs, _codec = _mount_control_tools(entries, ctx)
    return schema_list, routing_specs


def _build_content_registry(
    spec: _BuildSpec,
    pack_kinds: tuple[ContentKindSpec, ...] = (),
) -> ContentChannelRegistry:
    """The content-channel registry — registration order IS semi_stable layout.

    Every built-in resident arrives as a pack contribution — the packs' content
    kinds are sorted upstream by ``(kind priority, pack order)``, giving the
    layout skill=100 → memory=200 → instructions=300 → environment=400. A pack
    that does not apply (skills disabled, memory off, no instructions file
    and no discovery) contributes no kind and the later kinds close up
    behind it — correct and self-consistent, because a session built without
    a resident is a different configuration, and a recording made WITH one
    must be resumed with it (the host passes the same switches).

    The same registry feeds the composer's render rules AND the engine's
    generic content_hashes seam so the rendered content and the recorded
    fingerprint come from one source.
    """
    content_kinds: list[ContentKindSpec] = list(pack_kinds)
    # ``Options.content_channels`` extension point: user-registered
    # ContentKindSpec channels append LAST, after every built-in resident, so
    # existing sessions (no extra channels) keep their semi_stable byte layout
    # byte-identical. This is the ONLY composer extension seam — the composer
    # itself is not replaceable (stable-prefix cache hard constraint).
    content_kinds.extend(spec.extra_content_kinds)
    return ContentChannelRegistry(content_kinds)


def _build_reminder_registry(spec: _BuildSpec) -> ReminderRegistry:
    """The compose-time reminder registry — injected base + activated extras.

    The SDK host resolves the ``reminders`` built-in plugin's renders through
    the plugin loader and injects them as ``base_reminders`` (their priorities
    fix the composed dynamic-suffix tail order); the kernel imports no renderer.
    Per-agent-activated plugin reminders (``extra_reminders``) append after them
    and interleave by priority — the exact mirror of how
    ``_build_content_registry`` extends the built-in content kinds with
    ``Options.content_channels``. Empty extras ⇒ byte-identical to the
    built-in-only composer.
    """
    if spec.base_reminders is None:
        raise RuntimeError(
            "base reminders were not injected — the SDK host resolves the "
            "reminders built-in plugin through the plugin loader and passes "
            "base_reminders; the kernel builder imports no "
            "reminder renderer"
        )
    return ReminderRegistry((*spec.base_reminders, *spec.extra_reminders))


def _build_guards(
    spec: _BuildSpec,
    tools: dict[str, Tool],
    guard_facts: Optional[Any],
) -> HookManager:
    """The guard HookManager in the live session's registration order.

    Rebuild the exact guard shape the live session ran so a resumed Engine
    reproduces guard-origin events (the approval suspend +
    ``ToolCallApprovalRequested``, or a guard deny) consistently.

    The construction body lives in the ``governance`` built-in plugin
    (``build_default_guards``) — this phase forwards the finished tool
    assembly, the packs' opaque ``guard_facts`` bundle (the builder never
    reads inside it), and the operator passthrough fields. Delegation targets
    are authorized only while delegation is enabled; the caller has
    filtered the set through the same single-source helper the live driver
    uses, so an unknown ``--delegate-to`` produces the identical (empty)
    allow-list — live deny == resume deny, no SubtaskDenied-vs-SubtaskSpawned
    divergence.

    The budget and repetition defaults are supplied by the caller (product
    layer) so this phase stays noeta.agent-agnostic.
    """
    if spec.guards_factory is None:
        raise RuntimeError(
            "guards factory was not injected — the SDK host resolves the "
            "governance built-in plugin through the plugin loader and passes "
            "guards_factory; the kernel builder imports no "
            "guard implementation"
        )
    return spec.guards_factory(
        tools=tools,
        budget=spec.budget,
        require_approval_tools=tuple(spec.require_approval_tools),
        shell_approval_predicate=spec.shell_approval_predicate,
        guard_facts=guard_facts,
        allowed_subtask_agents=(
            spec.allowed_subtask_agents
            if spec.capability_flags.get("delegation", False)
            else None
        ),
        repetition_threshold=spec.repetition_threshold,
        repetition_action=spec.repetition_action,
        repetition_window=spec.repetition_window,
        hooks_pre_tool_use=spec.hooks_pre_tool_use,
        extra_guards=spec.extra_guards,
    )


def build_session_inputs(
    *,
    workspace_dir: Path,
    system_prompt: str,
    allowed_tools: frozenset[str],
    content_store: ContentStore,
    model: str,
    compaction: CompactionConfig,
    budget: Budget,
    allowed_subtask_agents: frozenset[str] = frozenset(),
    max_steps: int = 20,
    #: The write/shell safety inputs (``write_mode`` / ``shell_mode`` /
    #: ``shell_allowlist`` / ``write_path_globs`` / ``write_roots``) are not
    #: kernel-signature parameters: they have a single consumer (the fs
    #: pack), so the host supplies them in ``plugin_config["fs"]`` and the fs
    #: pack parses its own entry (mechanism-slots-only context).
    require_approval_tools: tuple[str, ...] = (),
    #: Per-call conditional approval predicate, forwarded verbatim into
    #: ``PermissionPolicy.conditional_approval``. Built by the SDK host for the
    #: shell allowlist-or-approve gate; ``None`` on every other path.
    shell_approval_predicate: Optional[
        Callable[[str, Mapping[str, Any]], bool]
    ] = None,
    #: When set, expose a per-helper ``structured_output`` control
    #: schema (its ``parameters`` = this JSON Schema). Set ONLY for a workflow
    #: helper subtask whose ``agent(schema=...)`` declared a schema; the
    #: orchestration's StructuredOutputPolicy wrapper intercepts the call.
    structured_output_schema: Optional[dict[str, Any]] = None,
    mcp_tools_override: Optional[dict[str, Tool]] = None,
    custom_tools: Optional[dict[str, Tool]] = None,
    #: Execution backend for the fs / shell pack. ``None`` (resume + every
    #: SDK/test fixture) ⇒ the host ``LocalExecEnv`` and a host ``WorkspaceRoot``
    #: — byte-identical, and the tool schemas are unchanged so the stable prefix
    #: is unaffected. A sandbox ``ExecEnv`` (supplied per-task by the product
    #: host once it has provisioned / attached a container) makes the
    #: pack act against that container and switches the workspace to lexical
    #: (container-path) containment. Wiring-only, never session identity.
    exec_env: Optional[ExecEnv] = None,
    #: The named backend bag: the host's live backing
    #: objects for capability packs, keyed by the contributing plugins' own
    #: names (the sandbox-vended ``"browser"`` backend, the product's
    #: ``"app_preview"`` gateway, …). An absent name means the capability has
    #: no live backing — its pack contributes nothing, so resume + every
    #: SDK/test fixture stay byte-identical with an empty bag.
    backends: Optional[Mapping[str, object]] = None,
    #: The agent's effective capability flags by name (``"browser"`` /
    #: ``"memory"`` / ``"delegation"`` / ``"todo_write"`` / …) — the ONE
    #: per-agent truth both the session packs AND the control-tool mounts
    #: self-gate on. The host supplies the ANDed values (agent
    #: activation × host kill-switch, plus any cross-capability gates such as
    #: workflow requiring delegation); the kernel never re-derives or
    #: enumerates a flag.
    capability_flags: Optional[Mapping[str, bool]] = None,
    #: Per-plugin config bag: ``plugin name → its own
    #: keys`` (the memory roots, the skills dirs + script switch, the
    #: workspace residents' instructions settings, …). Each pack parses only
    #: its own entry; the kernel never reads a key.
    plugin_config: Optional[Mapping[str, Mapping[str, object]]] = None,
    hooks_pre_tool_use: tuple[PreToolUseRule, ...] = (),
    repetition_threshold: int = 0,
    repetition_action: RepetitionAction = "require_approval",
    repetition_window: int = 8,
    subtask_agent_directory: tuple[tuple[str, str], ...] = (),
    # Wiring-only LLM request overrides (not part of session identity).
    # Propagated verbatim to the policy → each LLMRequest.
    output_schema: Optional[dict[str, Any]] = None,
    thinking: Optional[str] = None,
    effort: Optional[str] = None,
    #: Optional cheaper model for the compaction summarize round-trip ONLY;
    #: decide turns always use ``model``. Already alias-resolved by the caller
    #: (same table ``model`` went through) — the kernel owns no alias table.
    #: ``None`` ⇒ the summarize call uses ``model``, byte-identically to
    #: before this parameter existed.
    compaction_model: Optional[str] = None,
    #: The compaction model's OWN output ceiling (catalog-derived by the
    #: caller, alias-resolved like ``compaction_model`` — the kernel owns no
    #: catalog). The summarize request's ``max_tokens`` must be valid for the
    #: model that serves it: forwarding the MAIN model's cap to a smaller
    #: summarizer is a provider 400 → non-retryable
    #: ``compaction_summary_failed`` on every proactive compaction. ``None``
    #: (no compaction model, or an old caller) keeps the main model's cap.
    compaction_max_output_tokens: Optional[int] = None,
    # microcompact — engine-level truncation limit for inline
    # tool output in messages. ``None`` (default) = no truncation.
    tool_output_inline_limit: Optional[int] = None,
    # SDK Options extension
    # points. All default to inert values so every existing caller (product
    # host, tests, resume) is byte-identical. The SDK host is the single
    # caller that supplies them, and it feeds both the live and resume paths
    # from the same fields, so a resumed turn rebuilds the identical policy /
    # guard stack / content layout by construction.
    policy_factory_override: Optional[Callable[[Any], Policy]] = None,
    extra_guards: tuple[Guard, ...] = (),
    extra_content_kinds: tuple[ContentKindSpec, ...] = (),
    #: Per-agent-activated compose-time reminders. Default ``()`` so
    #: every caller composes the built-in reminders only, byte-identically.
    extra_reminders: tuple[ReminderSpec, ...] = (),
    #: The manifest-contributed session packs: the SDK
    #: host resolves every ``session_pack`` contribution (built-ins via
    #: ``noeta.client.parts.default_session_packs``, external plugins via the
    #: PluginSet projection) and injects the merged, priority-ordered entry
    #: tuple here. The generic loop is the whole tool-assembly pipeline;
    #: an empty tuple builds a session with no pack tools at all.
    session_packs: tuple[SessionPackEntry, ...] = (),
    #: The contributed control tools: the SDK host
    #: resolves every ``control_tool`` contribution (built-ins via
    #: ``noeta.client.parts.default_control_tools`` — todo_write / ask_user_question
    #: / delegation / skill / run_workflow / structured_output — external plugins
    #: via the PluginSet projection) and injects them here. The kernel
    #: holds no internal control-tool table, so this tuple is the whole mount-loop
    #: input; a name clash between two contributions raises loudly. Empty ⇒ a
    #: session with zero control tools.
    control_tools: tuple[ControlToolEntry, ...] = (),
    #: Loader-resolved built-in compose-time reminders:
    #: the renders declared by the ``reminders`` built-in plugin,
    #: resolved by the SDK host and injected here; ``None`` fails loudly.
    base_reminders: Optional[tuple[ReminderSpec, ...]] = None,
    #: Loader-resolved default guard-stack factory:
    #: the ``governance`` built-in plugin's ``build_default_guards``, resolved
    #: by the SDK host and injected here; ``None`` fails loudly.
    guards_factory: Optional[GuardsFactory] = None,
    #: The bound model's vendor family, resolved by the SDK host from the
    #: providers built-in's catalog (``provider_family(model)``) and consumed
    #: by the edit-tool mutex. ``None`` ⇒ both edit tools stay (the documented
    #: unrecognised-model semantic — byte-identical for stub/test builds).
    provider_family: Optional[str] = None,
    #: Loader-resolved default policy factory builder:
    #: the ``react`` built-in plugin's ``build_react_policy_factory``,
    #: resolved by the SDK host and injected here; ``None`` fails loudly at
    #: policy construction unless ``policy_factory_override`` replaces the
    #: default outright.
    default_policy_factory: Optional[PolicyFactoryBuilder] = None,
) -> SessionInputs:
    """Build the generic-session live/resume inputs from explicit
    operator-supplied pieces.

    All five inputs must match the recording's live session:

    * ``workspace_dir`` — same directory the recording was made
      against (or any clean copy; resume never writes).
    * ``system_prompt`` / ``allowed_tools`` — same pair the live
      session used (otherwise the rebuilt tool schema or system segment
      would diverge from the recording).
    * ``content_store`` — the same store the recording lives in (so
      composer can write the rebuilt ``ContextPlan`` body and read
      recorded artifacts back).
    * ``model`` / ``max_steps`` — same constants the live ReActPolicy
      was constructed with.
    * ``compaction`` — pre-derived knobs (window / output cap / buffer /
      tail / composer version); the product determines threshold policy.
    * ``budget`` — pre-parsed session budget (caller supplies default).
    * ``allowed_subtask_agents`` — filtered set of
      delegation targets (``None``-when-disabled semantics handled here).
    * ``plugin_config["fs"]`` — the write/shell safety inputs (``write_mode``
      / ``shell_mode`` / ``shell_allowlist`` / ``write_path_globs`` /
      ``write_roots``). ``write_mode`` must match the recording's mode so the
      rebuilt ``shell_run`` tool's allow-list shape (and thus its
      ``input_schema``) reproduces the recorded ``provider_tool_schemas``
      bytes; the fs pack defaults an absent entry to ``DRY_RUN`` / ``ALLOWLIST``
      (defence in depth on resume).

    **Live-path override**: the runner calls this same helper (single
    construction point) but passes ``mcp_tools_override`` with real
    McpTool instances (spawned stdio servers). ``None`` (the default,
    resume path) means no MCP tools are merged, so existing recordings
    (which carry none) are unaffected.

    **custom_tools**: injected AFTER the MCP segment so a user-supplied
    tool shadows any built-in / local / script / MCP tool of the same
    name. The canonical construction-order contract is
    ``fs → local → script → mcp → custom``.
    ``None`` ⇒ nothing is merged (existing paths unchanged).
    """
    spec = _BuildSpec(
        workspace_dir=workspace_dir,
        system_prompt=system_prompt,
        allowed_tools=allowed_tools,
        content_store=content_store,
        model=model,
        compaction=compaction,
        budget=budget,
        allowed_subtask_agents=allowed_subtask_agents,
        max_steps=max_steps,
        require_approval_tools=require_approval_tools,
        shell_approval_predicate=shell_approval_predicate,
        capability_flags=dict(capability_flags) if capability_flags else {},
        structured_output_schema=structured_output_schema,
        mcp_tools_override=mcp_tools_override,
        custom_tools=custom_tools,
        exec_env=exec_env,
        hooks_pre_tool_use=hooks_pre_tool_use,
        extra_guards=extra_guards,
        extra_content_kinds=extra_content_kinds,
        extra_reminders=extra_reminders,
        session_packs=session_packs,
        control_tools=control_tools,
        base_reminders=base_reminders,
        guards_factory=guards_factory,
        provider_family=provider_family,
        repetition_threshold=repetition_threshold,
        repetition_action=repetition_action,
        repetition_window=repetition_window,
        subtask_agent_directory=subtask_agent_directory,
        output_schema=output_schema,
        thinking=thinking,
        effort=effort,
        compaction_model=compaction_model,
        compaction_max_output_tokens=compaction_max_output_tokens,
        tool_output_inline_limit=tool_output_inline_limit,
    )

    # The kernel-built containment root (safety mechanism — packs consume it,
    # never build their own). Sandbox mode (``exec_env`` set) makes
    # ``workspace_dir`` a CONTAINER path: a host ``realpath`` / existence
    # check is wrong (it lives in the container), so build a lexical
    # containment root and let the ExecEnv do the remote IO.
    if exec_env is None:
        workspace = WorkspaceRoot.from_path(workspace_dir)
    else:
        workspace = WorkspaceRoot.for_container(workspace_dir)

    # The generic pack context: every session pack reads THIS, never the spec.
    ctx = SessionBuildContext(
        workspace=workspace,
        workspace_dir=workspace_dir,
        content_store=content_store,
        exec_env=exec_env,
        model=model,
        provider_family=provider_family,
        allowed_tools=allowed_tools,
        backends=dict(backends) if backends else {},
        capability_flags=spec.capability_flags,
        plugin_config=dict(plugin_config) if plugin_config else {},
    )

    # The two kernel-owned injections (pre-built objects, not packs) ride
    # the same loop as internal fixed-priority entries so ONE sorted iteration
    # is the whole construction-order contract. ``_mcp_entry`` closes over the
    # accumulating tool dict for the reserved-prefix check — by band 800 every
    # built-in name has merged.
    tools: dict[str, Tool] = {}

    def _mcp_entry(_ctx: SessionBuildContext) -> PackContribution:
        if spec.mcp_tools_override is None:
            return EMPTY_CONTRIBUTION
        for existing in tools:
            if existing.startswith(MCP_PREFIX):
                raise McpConfigError(
                    f"built-in tool {existing!r} occupies the reserved "
                    f"{MCP_PREFIX!r} namespace"
                )
        return PackContribution(tools=spec.mcp_tools_override)

    def _custom_entry(_ctx: SessionBuildContext) -> PackContribution:
        # Merged at band 900 so a custom tool intentionally shadows a
        # built-in / MCP tool of the same name (later-wins merge).
        if spec.custom_tools is None:
            return EMPTY_CONTRIBUTION
        return PackContribution(tools=spec.custom_tools)

    entries: list[SessionPackEntry] = [
        *session_packs,
        SessionPackEntry("mcp", 800, _mcp_entry),
        SessionPackEntry("custom", 900, _custom_entry),
    ]
    entries.sort(key=lambda e: (e.priority, e.name))

    pack_kinds: list[tuple[int, int, ContentKindSpec]] = []
    init_hooks: list[tuple[str, InitHook]] = []
    pack_control_tools: list[ControlToolEntry] = []
    # The typed contribution side-state the builder consumes: each
    # is single-writer across the pack loop (a second contributor is a wiring
    # fault, not a merge). ``None`` ⇒ no pack contributed it.
    guard_facts: Optional[Any] = None
    content_discovery: Optional[Any] = None
    content_preloader: Optional[Any] = None

    def _claim(field_name: str, current: Any, value: Any) -> Any:
        if value is None:
            return current
        if current is not None:
            raise RuntimeError(
                f"two session packs contributed {field_name!r} — "
                f"typed side-state is single-writer across the pack loop"
            )
        return value

    for seq, entry in enumerate(entries):
        contrib = entry.factory(ctx)
        for name, tool in contrib.tools.items():
            tools[name] = tool
        for ck in contrib.content_kinds:
            pack_kinds.append((ck.priority, seq, ck.spec))
        # Pre-loop activation hooks fold in pack-loop order (priority, name),
        # paired with the contribution name so the driver's SeedRecorder stamps
        # ``actor="plugin:<name>"`` and records residents deterministically.
        if contrib.init is not None:
            init_hooks.append((entry.name, contrib.init))
        # Pack-contributed control-tool entries (translate closures are
        # factory outputs) — collected in pack-loop order and mounted through
        # the SAME dual-priority loop as the host-supplied entries below.
        pack_control_tools.extend(contrib.control_tools)
        guard_facts = _claim("guard_facts", guard_facts, contrib.guard_facts)
        content_discovery = _claim(
            "content_discovery", content_discovery, contrib.content_discovery
        )
        content_preloader = _claim(
            "content_preloader", content_preloader, contrib.content_preloader
        )

    # Control-tool mounts: build the control-specific context (the generic
    # capability-flag bag), then run the host-supplied ``control_tools`` (every
    # ``control_tool`` contribution the SDK host resolves) PLUS the packs'
    # contributed entries through the generic dual-priority mount loop. Each
    # mount self-gates on its own flag / closed-over state (the skill mount
    # carries its menu in its closure — no kit crosses into kernel code). It
    # yields the composer's ``control_action_schemas`` (schema-band order), the
    # routing-ordered translate specs the policy factory binds, and the
    # collected mount exports (the ask answer codec). The loop re-sorts by
    # ``(priority, name)``; the collision check guards a third-party clash with
    # a built-in control-tool name.
    control_ctx = ControlToolBuildContext(
        capability_flags=spec.capability_flags,
        subtask_agent_directory=spec.subtask_agent_directory,
        structured_output_schema=spec.structured_output_schema,
    )
    (
        control_action_schemas,
        control_translate_specs,
        control_answer_codec,
    ) = _mount_control_tools(
        (*spec.control_tools, *pack_control_tools), control_ctx
    )
    content_registry = _build_content_registry(
        spec,
        tuple(
            kind_spec
            for _p, _s, kind_spec in sorted(
                pack_kinds, key=lambda t: (t[0], t[1])
            )
        ),
    )
    reminder_registry = _build_reminder_registry(spec)
    composer = ThreeSegmentComposer(
        system_prompt=system_prompt,
        tools=tools,
        content_store=content_store,
        content_renderers=content_registry,
        reminders=reminder_registry,
        control_action_schemas=control_action_schemas,
        # 0 ⇒ None (pruning OFF) to match ReActPolicy semantics: policy side
        # uses `tail_token_budget or 0` where both None and 0 mean "no tail
        # budget". Without this conversion composer's _prune_tail would treat
        # budget==0 as "protect zero tokens of tail", nullifying ALL tool-
        # result outputs — the opposite meaning. Positive values pass through
        # unchanged.
        tail_token_budget=compaction.tail_token_budget or None,
        # Relief-valve gate: the usable window (same formula as the Policy's
        # ``_available_window`` and ``derive_compaction_config``'s ``available``)
        # so prune only clears once the history nears the window instead of
        # clamping to the tail every turn. ``None`` when compaction is OFF.
        available_window=(
            max(
                0,
                compaction.context_window
                - compaction.max_output_tokens
                - compaction.compaction_buffer,
            )
            if compaction.context_window is not None
            else None
        ),
    )

    # ``Options.policy`` extension point: a custom decision policy
    # factory fully replaces the default. ``None`` ⇒ the loader-resolved
    # default (the ReAct construction lives in the ``react`` built-in; the
    # injected builder receives exactly the kernel-computed facts). Wiring-only
    # LLM request overrides (output_schema / thinking / effort /
    # compaction_model) ride through; omitted from canonical bytes when unset
    # so recordings resume byte-equal.
    policy_factory: Callable[[Any], Policy]
    if policy_factory_override is not None:
        policy_factory = policy_factory_override
    else:
        if default_policy_factory is None:
            raise RuntimeError(
                "default policy factory was not injected — the SDK host "
                "resolves the react built-in plugin through the plugin "
                "loader and passes default_policy_factory; the kernel "
                "builder imports no policy "
                "implementation"
            )
        policy_factory = default_policy_factory(
            tools=tools,
            system_prompt=system_prompt,
            model=model,
            max_steps=max_steps,
            control_translate_specs=control_translate_specs,
            content_store=content_store,
            context_window=compaction.context_window,
            max_output_tokens=compaction.max_output_tokens,
            compaction_buffer=compaction.compaction_buffer,
            tail_token_budget=compaction.tail_token_budget or 0,
            composer_version=compaction.composer_version,
            output_schema=output_schema,
            thinking=thinking,
            effort=effort,
            compaction_model=compaction_model,
            compaction_max_output_tokens=compaction_max_output_tokens,
        )

    hooks = _build_guards(spec, tools, guard_facts)

    # Anchored-content seams (docs/adr/anchored-content-placement.md):
    # ``content_discovery`` / ``content_preloader`` are the workspace plugin's
    # instructions-pack contributions (claimed in the pack loop above when
    # discovery is armed); both close over the SAME snapshot mapping the
    # composer's instructions kind renders from.
    return SessionInputs(
        tools=tools,
        composer=composer,
        policy_factory=policy_factory,
        hooks=hooks,
        content_hashes=content_registry.content_hashes(),
        tool_output_inline_limit=tool_output_inline_limit,
        content_discovery=content_discovery,
        content_preloader=content_preloader,
        init_hooks=tuple(init_hooks),
        answer_codec=control_answer_codec,
    )
