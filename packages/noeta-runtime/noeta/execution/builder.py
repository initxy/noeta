"""The generic SDK builder is the single construction point that live runs and resume share.

This is the one fixed construction site (the old code-product shim was
deleted with the roster). The product side (noeta-agent) calls it
directly via :class:`noeta.client.host.SdkHost` (the old
``noeta.agent.host.session`` runner was deleted). The parameterized
"product defaults" (agent fields, budget, compaction, plan-mode tool set)
are passed in explicitly by the caller; the roster/wiring layer is gone,
so no second code path exists.

Byte-stable construction is the headline constraint: a resumed turn rebuilds
the SAME tool set / composer / guards from the same inputs, so the prefix it
composes stays byte-stable (the stable-prefix prompt cache only hits when the
prefix is byte-stable).

Internal shape (microkernel phase 3): the builder enumerates no capability.
:func:`build_session_inputs`:

* freezes the operator inputs into a :class:`_BuildSpec` (read-only),
* builds the containment :class:`WorkspaceRoot` and the generic
  :class:`~noeta.execution.session_pack.SessionBuildContext`,
* runs every :class:`~noeta.execution.session_pack.SessionPackEntry` —
  manifest-contributed packs plus the two kernel-owned injections (mcp /
  custom) — through ONE ``(priority, name)``-sorted loop, merging tools
  (later-wins), content kinds (their own priority) and named exports,
* then runs the post-tools phases (control schemas → content channels →
  composer → policy factory → guards) as named helpers reading from the
  assembly.

The priority bands are the construction-order contract (fs=100 → web=200 →
memory=300 → instructions=400 → environment=500 → skills=600 → browser=700
→ mcp=800 → custom=900 → app=1000), load-bearing for byte-equality (tool
dict insertion order feeds the Engine's deterministic ToolSchemaRecorded
emission) and locked by ``tests/test_session_pack_goldens.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, cast

from noeta.context.composer import ThreeSegmentComposer
from noeta.context.content_channel import ContentChannelRegistry, ContentKindSpec
from noeta.context.reminders import ReminderRegistry, ReminderSpec
from noeta.context.environment import EnvironmentSnapshot
from noeta.context.instructions import InstructionsSnapshot
from noeta.context.memory import MemoryEntries
from noeta.core.hooks import HookManager
from noeta.execution.session_pack import (
    EMPTY_CONTRIBUTION,
    EXPORT_CONTENT_DISCOVERY,
    EXPORT_CONTENT_PRELOADER,
    EXPORT_ENVIRONMENT_SNAPSHOT,
    EXPORT_INSTRUCTIONS_SNAPSHOT,
    EXPORT_INSTRUCTIONS_SNAPSHOTS,
    EXPORT_MEMORY_ENTRIES,
    EXPORT_MEMORY_STORE,
    EXPORT_SKILLS_KIT,
    InitHook,
    PackContribution,
    SessionBuildContext,
    SessionPackEntry,
)
from noeta.execution.skills import SkillsKit
from noeta.runtime.governance import (
    Budget,
    PreToolUseRule,
    RepetitionAction,
    SkillEnforcementMode,
)
from noeta.execution.control_tool import (
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


# ---------------------------------------------------------------------------
# Compaction config (③ finding 1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompactionConfig:
    """The deterministic compaction knobs for one ``(agent, model)`` session.

    ``context_window is None`` ⇒ compaction OFF (legacy behaviour). When set,
    the policy's available window is
    ``context_window - max_output_tokens - compaction_buffer`` and the composer
    protects / the policy summarises against ``tail_token_budget``.
    """

    context_window: Optional[int]
    max_output_tokens: int
    compaction_buffer: int
    tail_token_budget: Optional[int]
    composer_version: str


#: Compaction disabled — the byte-equal-safe default for any model the catalog
#: does not describe (``stub-model`` and friends).
COMPACTION_OFF = CompactionConfig(
    context_window=None,
    max_output_tokens=0,
    compaction_buffer=0,
    tail_token_budget=None,
    composer_version="",
)


# NOTE (microkernel M2): ``derive_compaction_config`` — the catalog-driven
# derivation of these knobs — moved into the ``providers`` built-in plugin
# (``noeta.builtins.providers.impl.catalog``), reachable SDK-side through
# :func:`noeta.client.parts.derive_compaction_config`. The kernel keeps the
# ``CompactionConfig`` TYPE and takes the derived knobs pre-resolved
# (``build_session_inputs(compaction=…)``) — it holds no model opinions.


# ---------------------------------------------------------------------------
# SessionInputs + build_session_inputs — the single construction point (D9)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SessionInputs:
    """Composer + Policy factory + tools bundle for a generic agent
    session (live run or resume).

    Returned by :func:`build_session_inputs`. Carries the five pieces an
    Engine needs: the filtered-and-ordered tool dict, the composer (with
    skill rendering and control-action schemas wired in), the policy factory
    bound to the same ``(tools, model, compaction)`` triple, the guard
    HookManager (budget / permission / repetition / hook in the same
    deterministic order the live session registered them), and the loaded
    skill registry (for pre-loop activation + provenance).
    """

    tools: dict[str, Tool]
    composer: ThreeSegmentComposer
    #: The default factory builds :class:`ReActPolicy`; a custom
    #: ``policy_factory_override`` (SDK ``Options.policy`` extension point)
    #: substitutes any :class:`~noeta.protocols.policy.Policy`, hence the
    #: widened return type.
    policy_factory: Callable[[Any], Policy]
    #: Issue A — the guard shape the live session ran (BudgetGuard +
    #: PermissionGuard with the same allow-list + ``require_approval``
    #: set). A session recording that suspended for approval (or that a
    #: guard denied) carries its guard-origin events.
    hooks: HookManager
    #: Exposed to the runner for the pre-loop :func:`activate_skills`
    #: call and for content provenance. The three-tier merge: built-in
    #: + global tiers under the workspace-local pack
    #: (``skills_dir`` override or ``<workspace>/.noeta/skills``), workspace
    #: wins — the same registry both live and resume wire into the composer.
    skill_registry: Any
    #: The generic ``(kind, name) → (version, hash)``
    #: resolver derived from the content-channel registry the composer
    #: renders from (one source of truth). Hosts wire this into
    #: ``Engine(content_hashes=…)`` so mid-loop activations emit the
    #: generic ``ContextContentRecorded`` with the same fingerprints the
    #: composer's kinds declare.
    content_hashes: Callable[[str, str], Optional[tuple[str, str]]]
    #: Memory v1 wiring surface. ``memory_store`` is
    #: the session's file store as an OPAQUE handle (microkernel M3: the
    #: concrete ``MemoryStore`` lives in the memory built-in; the kernel
    #: passes it through, never calls it) — ``None`` when ``memory_enabled``
    #: was off; ``memory_entries`` is the load-time index snapshot the
    #: composer's renderer AND the pre-loop ``record_memory_index`` must
    #: share (one snapshot, one fingerprint — record time equals compose
    #: time by construction).
    memory_store: Optional[Any] = None
    memory_entries: MemoryEntries = ()
    #: Instructions file wiring surface. ``instructions_snapshot`` is the
    #: load-time snapshot (``None`` when ``instructions_enabled`` is off
    #: or no instructions file exists) shared by the composer's renderer
    #: AND the pre-loop ``record_instructions`` call — one snapshot, one
    #: fingerprint.
    instructions_snapshot: Optional[InstructionsSnapshot] = None
    #: Workspace environment wiring surface. ``environment_snapshot`` is
    #: the load-time snapshot (always present — a workspace always exists)
    #: shared by the composer's renderer AND the pre-loop
    #: ``record_environment`` call — one snapshot, one fingerprint.
    environment_snapshot: Optional[EnvironmentSnapshot] = None
    #: microcompact — host-level inline char cap for tool
    #: results before they are appended as messages. ``None`` ⇒ no
    #: truncation (default, backward-compatible). The value is forwarded
    #: verbatim to :class:`Engine` (which validates it). A resuming host must
    #: wire the same value so the rebuilt messages match the recording.
    tool_output_inline_limit: Optional[int] = None
    #: Anchored-content seams (docs/adr/anchored-content-placement.md), built
    #: only when ``instructions_discovery`` is on. ``content_discovery`` is the
    #: post-tool ``(task, call, result) → activation payloads`` hook the host
    #: wires into ``Engine(content_discovery=…)``; ``content_preloader`` is the
    #: per-step ``(task) → None`` resume re-read the host wires into
    #: ``Engine(content_preloader=…)``. Both ``None`` by default so every
    #: existing caller is byte-identical.
    content_discovery: Optional[Any] = None
    content_preloader: Optional[Any] = None
    #: The contributed pre-loop ``init`` hooks (spec §4.5), folded and
    #: priority-ordered from the pack loop. The host wires them onto the
    #: session's Engine; the driver runs them through a
    #: :class:`~noeta.execution.recorder.SeedRecorder` at seed time — the
    #: generic successor of the three feature-named kernel seed recorders. Empty
    #: for a session whose packs activate no pre-loop residents.
    init_hooks: tuple[InitHook, ...] = ()
    #: The control tools' collected mount exports (control-tool-surface S2, D8) —
    #: a closed-vocabulary mapping the host threads to the kernel seams that
    #: consume it. The only S2 tenant is the ``ask_user_question`` answer codec
    #: (:data:`~noeta.execution.control_tool.CONTROL_EXPORT_ASK_ANSWER_CODEC`),
    #: which the host puts on the session's Engine for the driver's ``answer``
    #: path. Empty for a session that mounts no exporting control tool.
    control_exports: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# C02 deepening — frozen build spec + mutable tool-assembly accumulator
# ---------------------------------------------------------------------------


class GuardsFactory(Protocol):
    """Loader-resolved constructor of the default guard stack (microkernel M2).

    The kernel never imports a guard class: the SDK host resolves the
    ``governance`` built-in plugin's factory through the plugin loader and
    injects it here. The kernel calls it with the finished tool assembly, the
    skill-derived guard facts, and the operator passthrough fields; it returns
    the registered :class:`~noeta.core.hooks.HookManager`. Signature =
    ``noeta.builtins.governance.impl:build_default_guards``.
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
        skill_tool_enforcement: SkillEnforcementMode,
        skill_allowed_tools: tuple[tuple[str, frozenset[str]], ...],
        allowed_subtask_agents: Optional[frozenset[str]],
        skill_script_tools: frozenset[str],
        skill_scripts: frozenset[tuple[str, str]],
        repetition_threshold: int,
        repetition_action: RepetitionAction,
        repetition_window: int,
        hooks_pre_tool_use: tuple[PreToolUseRule, ...],
        extra_guards: tuple[Guard, ...],
    ) -> HookManager: ...


class PolicyFactoryBuilder(Protocol):
    """Loader-resolved constructor of the default policy factory
    (microkernel phase 2b).

    The kernel never imports the decision-mapping policy: the SDK host
    resolves the ``react`` built-in plugin's factory builder through the
    plugin loader and injects it here. It takes exactly the kernel-computed
    session facts the builder used to close over inline and returns the
    ``(llm) -> Policy`` factory. ``Options.policy`` / the plugin ``policy``
    surface (D10) still override the default. Signature =
    ``noeta.builtins.react.impl:build_react_policy_factory``.
    """

    def __call__(
        self,
        *,
        tools: dict[str, Tool],
        system_prompt: str,
        model: str,
        max_steps: int,
        #: The routing-ordered control-tool translate specs the mount loop
        #: produced (control-tool-surface S1) — replaces the five ``*_enabled``
        #: flags + ``skill_menu_names`` the policy used to rebuild toggles from.
        #: Mounting IS enablement, so a disabled tool simply contributes no spec.
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
    ) -> Callable[[Any], Policy]: ...


@dataclass(frozen=True, slots=True)
class _BuildSpec:
    """All operator inputs to one session build, frozen.

    This is the internal mirror of :func:`build_session_inputs`'s keyword
    parameters: the public function copies its args into one of these so the
    pipeline stages read a single read-only object instead of closing over 30
    locals. Keeping the public signature byte-identical (resume must pass the
    same params to rebuild identically; a test asserts on ``inspect.signature``)
    is the whole reason this is a SEPARATE struct rather than the function
    exposing it.
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
    skill_tool_enforcement: SkillEnforcementMode
    #: The effective capability flags by name (agent activation × host
    #: kill-switch, already ANDed by the host) — the ONE generic bag both the
    #: session packs (via ``SessionBuildContext``) and the control-tool mounts
    #: (via ``ControlToolBuildContext``) self-gate on. The kernel enumerates
    #: no capability here.
    capability_flags: Mapping[str, bool]
    structured_output_schema: Optional[dict[str, Any]]
    mcp_tools_override: Optional[dict[str, Tool]]
    custom_tools: Optional[dict[str, Tool]]
    #: Execution backend for the fs / shell pack. ``None`` ⇒ host
    #: (``LocalExecEnv``, byte-identical); a sandbox ``ExecEnv`` makes the pack
    #: act against a container and switches the workspace to lexical
    #: (container-path) containment. A wiring-only runtime injection, never part
    #: of session identity — the tool schemas are unchanged either way.
    exec_env: Optional[ExecEnv]
    hooks_pre_tool_use: tuple[PreToolUseRule, ...]
    #: SDK ``Options`` extension points (T3). Custom Guards registered after
    #: the built-in guard stack; custom ContentKindSpec channels appended
    #: after the built-in content residents. Both default to ``()`` so every
    #: other caller (product host, tests, resume) is byte-identical.
    extra_guards: tuple[Guard, ...]
    extra_content_kinds: tuple[ContentKindSpec, ...]
    #: Per-agent-activated compose-time reminders (D8), appended after the three
    #: built-in reminders in the composer's reminder registry. Default ``()`` so
    #: every existing caller composes byte-identically (the built-in three only).
    extra_reminders: tuple[ReminderSpec, ...]
    repetition_threshold: int
    repetition_action: RepetitionAction
    repetition_window: int
    subtask_agent_directory: tuple[tuple[str, str], ...]
    output_schema: Optional[dict[str, Any]]
    thinking: Optional[str]
    effort: Optional[str]
    tool_output_inline_limit: Optional[int]
    #: Loader-resolved built-in compose-time reminders (microkernel M2, D2):
    #: the three renders the ``reminders`` built-in plugin declares, resolved by
    #: the SDK host and injected here. ``None`` fails loudly at the reminder-
    #: registry phase — the kernel never imports a renderer.
    base_reminders: Optional[tuple[ReminderSpec, ...]] = None
    #: Loader-resolved default guard-stack factory (microkernel M2, D2):
    #: ``noeta.builtins.governance.impl:build_default_guards``, resolved by the
    #: SDK host and injected here. ``None`` fails loudly at the guards phase —
    #: the kernel never imports a guard class.
    guards_factory: Optional[GuardsFactory] = None
    #: The bound model's vendor family (``"anthropic"`` / ``"openai"`` /
    #: ``None``), resolved by the SDK host from the providers built-in's
    #: catalog (microkernel M2) and exposed to packs through the context
    #: (the fs pack keys its own edit-tool mutex on it). ``None``
    #: (kernel-alone / stub) drops neither edit tool — the documented
    #: no-catalog semantic, NOT a silent fallback.
    provider_family: Optional[str] = None
    #: The manifest-contributed session packs (microkernel phase 3): resolved
    #: by the SDK host (``noeta.client.parts.default_session_packs`` + the
    #: external plugins' ``session_pack`` projection) and run by the generic
    #: pack loop in ``(priority, name)`` order. Empty builds a session with
    #: no pack tools at all — the kernel mandates no capability.
    session_packs: tuple[SessionPackEntry, ...] = ()
    #: The contributed control tools (control-tool-surface S2/S2b): resolved by
    #: the SDK host (``noeta.client.parts.default_control_tools`` — the built-in
    #: ``control_tool`` contributions — + the external plugins' ``control_tool``
    #: projection) and run through the dual-priority mount loop. Since S2b the
    #: kernel keeps NO internal control-tool table, so this tuple is the whole
    #: input: empty ⇒ a session with zero control tools.
    control_tools: tuple[ControlToolEntry, ...] = ()


@dataclass(slots=True)
class _ToolAssembly:
    """The mutable accumulator the tool pipeline threads through.

    ``tools`` is the dict each stage mutates (the construction-order contract
    is the ORDER stages append into it). The other fields are the side-outputs
    that one stage produces and a LATER stage (or a post-tools phase) consumes:
    the skill ``registry`` (feeds script / read-fence / menu / content kinds),
    the memory ``store`` + ``entries`` (feed the memory tools + content kind),
    the ``instructions_snapshot`` (feeds its content kind), and the skill-
    script guard fields (feed the PermissionGuard). Capturing them on the
    accumulator is what lets each pipeline stage stay a small self-contained
    ``(spec, asm) -> None``.
    """

    tools: dict[str, Tool] = field(default_factory=dict)
    registry: Any = None
    #: The skill kind's ContentKindSpec + the resolved allowed-tools grants,
    #: produced by the skills stage's kit (microkernel phase 2a) and consumed
    #: by the content-registry phase / the guards phase.
    skill_content_kind: Optional[ContentKindSpec] = None
    skill_allowed_tools: tuple[tuple[str, frozenset[str]], ...] = ()
    memory_store: Optional[Any] = None
    memory_entries: MemoryEntries = ()
    instructions_snapshot: Optional[InstructionsSnapshot] = None
    #: The shared name → snapshot mapping the instructions kind renders from
    #: (root file under its basename; discovered subdirectory files under
    #: their workspace-relative paths). Deliberately MUTABLE and shared with
    #: the discovery hook + resume preloader, which add entries at tool/step
    #: time — the renderer itself only ever looks up in memory.
    instructions_snapshots: dict[str, InstructionsSnapshot] = field(
        default_factory=dict
    )
    environment_snapshot: Optional[EnvironmentSnapshot] = None
    skill_script_tools: frozenset[str] = frozenset()
    skill_scripts: frozenset[tuple[str, str]] = frozenset()
    workspace: WorkspaceRoot = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Session packs — each owns "whether to enable + how to build/filter" and
# returns a PackContribution; the generic loop in ``build_session_inputs``
# merges them in (priority, name) order. The priority BANDS are the
# construction-order contract (fs=100 → web=200 → memory=300 →
# instructions=400 → environment=500 → skills=600 → browser=700 → mcp=800 →
# custom=900 → app=1000), load-bearing for byte-equality (tool dict insertion
# order feeds the Engine's deterministic ToolSchemaRecorded emission) and
# locked by ``tests/test_session_pack_goldens.py``; do not renumber.
#
# Microkernel phase 3, S1: the packs still close over the legacy
# ``_BuildSpec`` factory fields (loader-resolved by the SDK host exactly as
# before); S2+ replaces each with the plugin's own manifest-contributed
# ``session_pack`` factory reading only the SessionBuildContext.
# ---------------------------------------------------------------------------


# NOTE (microkernel phase 3): every capability pack is a ``session_pack``
# manifest contribution (``noeta.builtins.<name>.impl:build_*_session_pack``)
# resolved by the SDK host and passed in as ``session_packs``; the capability
# seam Protocols live in their plugins (``BrowserBackend`` → browser,
# ``AppPreviewGateway`` → app) and their live backing objects ride the
# generic ``backends`` bag under the plugins' own names. The kernel owns only
# the two pre-built injections below (mcp / custom), which ride the same loop
# as fixed-priority internal entries.


# ---------------------------------------------------------------------------
# Post-tools phases — control schemas, content channels, composer, policy,
# guards. Each reads from the finished assembly and the frozen spec.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Control-tool mount loop — a pure MECHANISM (control-tool-surface S1 → S2b).
# The builder enumerates no control tool: every mount arrives as a host-supplied
# ``ControlToolEntry`` (the built-in ``control_tool`` contributions the SDK host
# resolves from the manifests via ``default_control_tools()`` + the external
# plugins' ``control_tool`` projection), each a factory that self-gates on the
# ``ControlToolBuildContext`` and returns a ``ControlToolMount`` or ``None``.
# S2b emptied the last three internal entries (``skill`` → ``skills``,
# ``run_workflow`` / ``structured_output`` → ``react``), so no fixed table
# remains — the loop below runs whatever the host passes. The two priority BANDS
# are the byte-order contract, locked by the S0 golden: schema render order
# spawn=100 → todo=200 → ask=300 → skill=400 → workflow=500 →
# structured_output=600; decision routing order ask=100 → todo=200 → spawn=300 →
# skill=400 → workflow=500. ``structured_output`` carries no translate (react's
# StructuredOutputPolicy intercepts it) so it contributes a schema but no routing
# spec.
# ---------------------------------------------------------------------------


def _mount_control_tools(
    entries: Sequence[ControlToolEntry],
    ctx: ControlToolBuildContext,
) -> tuple[
    Optional[list[dict[str, Any]]],
    tuple[ControlToolSpec, ...],
    dict[str, Any],
]:
    """Run the control-tool entries → ``(schema_list, routing_specs, exports)``.

    The generic dual-priority mount loop, mirroring the session-pack loop: each
    factory self-gates on ``ctx`` and returns ``None`` to opt out (a mount IS
    enablement). Collision is loud — two mounts of one name raise a ``ValueError``
    naming both entries, exactly as the pack loop rejects a re-export (e.g. a
    third-party ``control_tool`` clashing with a built-in one). The two
    output orders come from each mount's OWN bands: the schema list sorts on
    ``schema_priority`` (the composer's ``control_action_schemas`` byte order —
    the S0 golden), the routing specs on ``routing_priority`` (the decision
    dispatch order), ties broken by name in both. A mount with no ``translate``
    (``structured_output``) contributes a schema but no routing spec. An empty
    schema list folds to ``None`` (the composer's "no control schemas" sentinel).

    The third output collects every mount's :attr:`ControlToolMount.exports` into
    one closed-vocabulary mapping (D8) — a mount export is single-writer across
    the loop (a duplicate export key raises), exactly like the session-pack
    export bag; the ask codec is the only S2 tenant.
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
    exports: dict[str, Any] = {}
    for m in mounts:
        for key, value in m.exports.items():
            if key in exports:
                raise RuntimeError(
                    f"control tool {m.name!r} re-exports {key!r} — mount export "
                    f"keys are single-writer across the control-tool loop"
                )
            exports[key] = value
    return (schema_list or None, tuple(routing_specs), exports)


def _run_control_tool_mounts(
    entries: Sequence[ControlToolEntry],
    ctx: ControlToolBuildContext,
) -> tuple[Optional[list[dict[str, Any]]], tuple[ControlToolSpec, ...]]:
    """Two-output view of :func:`_mount_control_tools` (schemas + routing specs).

    The mechanism seam ``tests/test_control_tool_mount_loop.py`` pins; the
    builder itself uses :func:`_mount_control_tools`, which additionally returns
    the collected mount exports (D8).
    """
    schema_list, routing_specs, _exports = _mount_control_tools(entries, ctx)
    return schema_list, routing_specs


def _build_content_registry(
    spec: _BuildSpec,
    pack_kinds: tuple[ContentKindSpec, ...] = (),
) -> ContentChannelRegistry:
    """The content-channel registry — registration order IS semi_stable layout.

    Microkernel phase 3 (S4): every built-in resident arrives as a pack
    contribution — the packs' content kinds are sorted upstream by
    ``(kind priority, pack order)``, which reproduces the historical layout
    (skill=100 → memory=200 → instructions=300 → environment=400). A pack
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
    # SDK ``Options.content_channels`` extension point (T3): user-registered
    # ContentKindSpec channels append LAST, after every built-in resident, so
    # existing sessions (no extra channels) keep their semi_stable byte layout
    # byte-identical. This is the ONLY composer extension seam — the composer
    # itself is not replaceable (stable-prefix cache hard constraint).
    content_kinds.extend(spec.extra_content_kinds)
    return ContentChannelRegistry(content_kinds)


def _build_reminder_registry(spec: _BuildSpec) -> ReminderRegistry:
    """The compose-time reminder registry (D8) — injected base + activated extras.

    Microkernel M2: the three built-in reminders (todo / delegation / read) are
    no longer imported here — the SDK host resolves the ``reminders`` built-in
    plugin's renders through the plugin loader and injects them as
    ``base_reminders`` (their priorities keep the composed dynamic-suffix tail
    byte-identical to the pre-migration append order). Per-agent-activated
    plugin reminders (``extra_reminders``) append after them and interleave by
    priority — the exact mirror of how ``_build_content_registry`` extends the
    built-in content kinds with ``Options.content_channels``. Empty extras ⇒
    byte-identical to the built-in-only composer.
    """
    if spec.base_reminders is None:
        raise RuntimeError(
            "base reminders were not injected — the SDK host resolves the "
            "reminders built-in plugin through the plugin loader and passes "
            "base_reminders (microkernel M2); the kernel builder imports no "
            "reminder renderer"
        )
    return ReminderRegistry((*spec.base_reminders, *spec.extra_reminders))


def _build_guards(spec: _BuildSpec, asm: _ToolAssembly) -> HookManager:
    """The guard HookManager in the live session's registration order.

    Issue A: rebuild the exact guard shape the live session ran so a resumed
    Engine reproduces guard-origin events (the approval suspend +
    ``ToolCallApprovalRequested``, or a guard deny) consistently.

    Microkernel M2: the construction body moved into the ``governance``
    built-in plugin (``build_default_guards``) — this phase pre-shapes the
    kernel-side facts and delegates to the injected factory:

    * Issue B — the raw skill ``allowed-tools`` map is extracted and
      sdk-resolved HERE (both halves are kernel machinery) so an enforcement
      recording reproduces byte-equal.
    * Issue C — delegation targets are authorized only while delegation is
      enabled; the caller has already roster-filtered the set through the same
      single-source helper the live runner uses, so an unknown
      ``--delegate-to`` produces the identical (empty) allow-list — live deny
      == resume deny, no SubtaskDenied-vs-SubtaskSpawned divergence.

    The budget and repetition defaults are supplied by the caller (product
    layer) so this phase stays noeta.agent-agnostic.
    """
    if spec.guards_factory is None:
        raise RuntimeError(
            "guards factory was not injected — the SDK host resolves the "
            "governance built-in plugin through the plugin loader and passes "
            "guards_factory (microkernel M2); the kernel builder imports no "
            "guard implementation"
        )
    return spec.guards_factory(
        tools=asm.tools,
        budget=spec.budget,
        require_approval_tools=tuple(spec.require_approval_tools),
        shell_approval_predicate=spec.shell_approval_predicate,
        skill_tool_enforcement=spec.skill_tool_enforcement,
        skill_allowed_tools=asm.skill_allowed_tools,
        allowed_subtask_agents=(
            spec.allowed_subtask_agents
            if spec.capability_flags.get("delegation", False)
            else None
        ),
        skill_script_tools=asm.skill_script_tools,
        skill_scripts=asm.skill_scripts,
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
    #: ``shell_allowlist`` / ``write_path_globs`` / ``write_roots``) are no
    #: longer kernel-signature parameters: they have a single consumer (the fs
    #: pack), so the host supplies them in ``plugin_config["fs"]`` and the fs
    #: pack parses its own entry (mechanism-slots-only context, spec §4.2).
    require_approval_tools: tuple[str, ...] = (),
    #: Per-call conditional approval predicate, forwarded verbatim into
    #: ``PermissionPolicy.conditional_approval``. Built by the SDK host for the
    #: shell allowlist-or-approve gate; ``None`` on every other path.
    shell_approval_predicate: Optional[
        Callable[[str, Mapping[str, Any]], bool]
    ] = None,
    skill_tool_enforcement: SkillEnforcementMode = "off",
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
    #: host once it has provisioned / attached a container, T5/T6) makes the
    #: pack act against that container and switches the workspace to lexical
    #: (container-path) containment. Wiring-only, never session identity.
    exec_env: Optional[ExecEnv] = None,
    #: The named backend bag (microkernel phase 3): the host's live backing
    #: objects for capability packs, keyed by the contributing plugins' own
    #: names (the sandbox-vended ``"browser"`` backend, the product's
    #: ``"app_preview"`` gateway, …). An absent name means the capability has
    #: no live backing — its pack contributes nothing, so resume + every
    #: SDK/test fixture stay byte-identical with an empty bag.
    backends: Optional[Mapping[str, object]] = None,
    #: The agent's effective capability flags by name (``"browser"`` /
    #: ``"memory"`` / ``"delegation"`` / ``"todo_write"`` / …) — the ONE
    #: per-agent truth both the session packs AND the control-tool mounts
    #: self-gate on. The host supplies the already-ANDed values (agent
    #: activation × host kill-switch, plus any cross-capability gates such as
    #: workflow requiring delegation); the kernel never re-derives or
    #: enumerates a flag.
    capability_flags: Optional[Mapping[str, bool]] = None,
    #: Per-plugin config bag (microkernel phase 3): ``plugin name → its own
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
    #: Per-agent-activated compose-time reminders (D8, track B). Default ``()`` so
    #: every existing caller composes the built-in three only, byte-identically.
    extra_reminders: tuple[ReminderSpec, ...] = (),
    #: The manifest-contributed session packs (microkernel phase 3): the SDK
    #: host resolves every ``session_pack`` contribution (built-ins via
    #: ``noeta.client.parts.default_session_packs``, external plugins via the
    #: PluginSet projection) and injects the merged, priority-ordered entry
    #: tuple here. The generic loop is the whole tool-assembly pipeline;
    #: an empty tuple builds a session with no pack tools at all.
    session_packs: tuple[SessionPackEntry, ...] = (),
    #: The contributed control tools (control-tool-surface S2/S2b): the SDK host
    #: resolves every ``control_tool`` contribution (built-ins via
    #: ``noeta.client.parts.default_control_tools`` — todo_write / ask_user_question
    #: / delegation / skill / run_workflow / structured_output — external plugins
    #: via the PluginSet projection) and injects them here. Since S2b the kernel
    #: holds no internal control-tool table, so this tuple is the whole mount-loop
    #: input; a name clash between two contributions raises loudly. Empty ⇒ a
    #: session with zero control tools.
    control_tools: tuple[ControlToolEntry, ...] = (),
    #: Loader-resolved built-in compose-time reminders (microkernel M2, D2):
    #: the three renders declared by the ``reminders`` built-in plugin,
    #: resolved by the SDK host and injected here; ``None`` fails loudly.
    base_reminders: Optional[tuple[ReminderSpec, ...]] = None,
    #: Loader-resolved default guard-stack factory (microkernel M2, D2):
    #: the ``governance`` built-in plugin's ``build_default_guards``, resolved
    #: by the SDK host and injected here; ``None`` fails loudly.
    guards_factory: Optional[GuardsFactory] = None,
    #: The bound model's vendor family, resolved by the SDK host from the
    #: providers built-in's catalog (``provider_family(model)``) and consumed
    #: by the edit-tool mutex. ``None`` ⇒ both edit tools stay (the documented
    #: unrecognised-model semantic — byte-identical for stub/test builds).
    provider_family: Optional[str] = None,
    #: Loader-resolved default policy factory builder (microkernel phase 2b):
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
    * ``allowed_subtask_agents`` — already roster-filtered set of
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

    Internally (C02 deepening) the body is an explicit tool pipeline
    (:data:`_TOOL_PIPELINE`) plus the named post-tools phases below; the
    keyword interface and every produced byte are unchanged.
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
        skill_tool_enforcement=skill_tool_enforcement,
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

    # The generic pack context (microkernel phase 3): every session pack
    # reads THIS, never the spec.
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

    # The two kernel-owned injections (D1: pre-built objects, not packs) ride
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
    exports: dict[str, object] = {}
    init_hooks: list[InitHook] = []
    for seq, entry in enumerate(entries):
        contrib = entry.factory(ctx)
        for name, tool in contrib.tools.items():
            tools[name] = tool
        for ck in contrib.content_kinds:
            pack_kinds.append((ck.priority, seq, ck.spec))
        # Pre-loop activation hooks fold in pack-loop order (priority, name),
        # so the driver's SeedRecorder records residents deterministically.
        if contrib.init is not None:
            init_hooks.append(contrib.init)
        for key, value in contrib.exports.items():
            if key in exports:
                raise RuntimeError(
                    f"session pack {entry.name!r} re-exports {key!r} — "
                    f"export keys are single-writer across the pack loop"
                )
            exports[key] = value

    # Populate the assembly the post-tools phases read. The exports adapter
    # is S1-transitional: S4/S5 teach the phases to read contributions
    # directly and this block shrinks with them.
    asm = _ToolAssembly()
    asm.tools = tools
    asm.workspace = workspace
    _kit = exports.get(EXPORT_SKILLS_KIT)
    if _kit is not None:
        skills_kit = cast(SkillsKit, _kit)
        asm.registry = skills_kit.registry
        asm.skill_content_kind = skills_kit.content_kind
        asm.skill_allowed_tools = skills_kit.allowed_tools
        asm.skill_script_tools = skills_kit.skill_script_tools
        asm.skill_scripts = skills_kit.skill_scripts
    asm.memory_store = exports.get(EXPORT_MEMORY_STORE)
    asm.memory_entries = cast(
        MemoryEntries, exports.get(EXPORT_MEMORY_ENTRIES, ())
    )
    asm.instructions_snapshot = cast(
        Optional[InstructionsSnapshot],
        exports.get(EXPORT_INSTRUCTIONS_SNAPSHOT),
    )
    asm.instructions_snapshots = cast(
        "dict[str, InstructionsSnapshot]",
        exports.get(EXPORT_INSTRUCTIONS_SNAPSHOTS, {}),
    )
    asm.environment_snapshot = cast(
        Optional[EnvironmentSnapshot],
        exports.get(EXPORT_ENVIRONMENT_SNAPSHOT),
    )
    # Control-tool mounts (control-tool-surface S1 → S2b): build the control-
    # specific context (the generic capability-flag bag + the packs' exports),
    # then run the host-supplied ``control_tools`` (every ``control_tool``
    # contribution the SDK host resolves — the built-in six + any external
    # plugin's) through the generic dual-priority mount loop. Since S2b the
    # kernel keeps no internal control-tool table, so this tuple IS the whole
    # input; each mount self-gates on its own flag / export (the skills mount
    # derives its menu from ``EXPORT_SKILLS_KIT``). It yields the composer's
    # ``control_action_schemas`` (schema-band order, the S0 golden), the
    # routing-ordered translate specs the policy factory binds, and the
    # collected mount exports (D8 — the ask answer codec). The loop re-sorts by
    # ``(priority, name)``; the collision check guards a third-party clash with
    # a built-in control-tool name.
    control_ctx = ControlToolBuildContext(
        capability_flags=spec.capability_flags,
        subtask_agent_directory=spec.subtask_agent_directory,
        structured_output_schema=spec.structured_output_schema,
        exports=exports,
    )
    (
        control_action_schemas,
        control_translate_specs,
        control_exports,
    ) = _mount_control_tools(spec.control_tools, control_ctx)
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
    # Microkernel phase 2a: the composer is constructed directly — the old
    # ``build_skill_composer`` wrapper's only skill-specific behaviour was a
    # single-kind registry fallback this call never used (the multi-kind
    # ``content_registry`` is always passed explicitly).
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

    # SDK ``Options.policy`` extension point (T3): a custom decision policy
    # factory fully replaces the default. ``None`` ⇒ the loader-resolved
    # default (microkernel phase 2b: the ReAct construction lives in the
    # ``react`` built-in; the injected builder receives exactly the
    # kernel-computed facts the old inline closure captured — byte-identical
    # prompts and schemas). Wiring-only LLM request overrides
    # (output_schema / thinking / effort) ride through; omitted from
    # canonical bytes when unset so legacy recordings resume byte-equal.
    policy_factory: Callable[[Any], Policy]
    if policy_factory_override is not None:
        policy_factory = policy_factory_override
    else:
        if default_policy_factory is None:
            raise RuntimeError(
                "default policy factory was not injected — the SDK host "
                "resolves the react built-in plugin through the plugin "
                "loader and passes default_policy_factory (microkernel "
                "phase 2b); the kernel builder imports no policy "
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
        )

    hooks = _build_guards(spec, asm)

    # Anchored-content seams (docs/adr/anchored-content-placement.md):
    # exported by the workspace plugin's instructions pack when discovery is
    # armed — both hooks close over the SAME snapshot mapping the composer's
    # instructions kind renders from, so a discovered (or resume-preloaded)
    # file is renderable the moment its activation folds.
    content_discovery = exports.get(EXPORT_CONTENT_DISCOVERY)
    content_preloader = exports.get(EXPORT_CONTENT_PRELOADER)

    return SessionInputs(
        tools=tools,
        composer=composer,
        policy_factory=policy_factory,
        hooks=hooks,
        skill_registry=asm.registry,
        content_hashes=content_registry.content_hashes(),
        memory_store=asm.memory_store,
        memory_entries=asm.memory_entries,
        instructions_snapshot=asm.instructions_snapshot,
        environment_snapshot=asm.environment_snapshot,
        tool_output_inline_limit=tool_output_inline_limit,
        content_discovery=content_discovery,
        content_preloader=content_preloader,
        init_hooks=tuple(init_hooks),
        control_exports=control_exports,
    )
