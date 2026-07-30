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
prefix is byte-stable). All commentary, construction order, and literal values
are preserved word-for-word.

Internal shape (C02 deepening): :func:`build_session_inputs` keeps its exact
public interface (the 30+ keyword params resume must pass to rebuild
identically), but its body is no longer one 446-line function. It now:

* freezes the operator inputs into a :class:`_BuildSpec` (read-only),
* threads a single mutable :class:`_ToolAssembly` accumulator through an
  EXPLICIT ordered tool pipeline — ``_TOOL_PIPELINE`` — where each stage
  self-decides "whether to enable + how to build/filter" and owns its
  read-fence side effect,
* then runs the post-tools phases (control schemas → content channels →
  composer → policy factory → guards) as named helpers reading from the
  assembly.

The pipeline list IS the construction-order contract that used to live only
in prose comments + the implicit top-to-bottom statement order. Nothing about
the produced tool set / allowed_tools filter / guard registration / composer
bytes changes — the stages run in the same order and do the same mutations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, cast

from noeta.context.composer import ThreeSegmentComposer
from noeta.context.content_channel import ContentChannelRegistry, ContentKindSpec
from noeta.context.reminders import ReminderRegistry, ReminderSpec
from noeta.context.environment import EnvironmentSnapshot
from noeta.context.instructions import InstructionsSnapshot
from noeta.context.memory import MemoryEntries
from noeta.core.hooks import HookManager
from noeta.execution.environment import EnvironmentKit, load_environment
from noeta.execution.instructions import (
    InstructionsKit,
    build_instructions_discovery,
    build_instructions_preloader,
    load_instructions,
)
from noeta.execution.memory import MemoryIndexKit
from noeta.execution.session_pack import (
    EMPTY_CONTRIBUTION,
    EXPORT_ENVIRONMENT_SNAPSHOT,
    EXPORT_INSTRUCTIONS_SNAPSHOT,
    EXPORT_INSTRUCTIONS_SNAPSHOTS,
    EXPORT_MEMORY_ENTRIES,
    EXPORT_MEMORY_STORE,
    EXPORT_SKILLS_KIT,
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
from noeta.policies.control_tools import (
    ask_user_question_tool_schema,
    run_workflow_tool_schema,
    skill_tool_schema,
    spawn_subagent_tool_schema,
    structured_output_tool_schema,
    todo_write_tool_schema,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.hooks import Guard
from noeta.protocols.policy import Policy
from noeta.protocols.tool import Tool
from noeta.runtime.app_preview import AppPreviewGateway
from noeta.runtime.browser import BrowserBackend
from noeta.runtime.exec_env import ExecEnv
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode, WorkspaceRoot, WriteRootsResolver
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


class AppToolsFactory(Protocol):
    """Loader-resolved constructor of the app-preview pack (microkernel M3).

    The kernel never imports the ``open_app`` tool: the SDK host resolves the
    ``app`` built-in plugin's pack factory through the plugin loader and
    injects it here. Called only when the host wired a live preview gateway.
    Signature = ``noeta.builtins.app.impl:build_app_tools``.
    """

    def __call__(
        self, workspace: WorkspaceRoot, gateway: AppPreviewGateway
    ) -> dict[str, Tool]: ...


class BrowserToolsFactory(Protocol):
    """Loader-resolved constructor of the browser tool pack (microkernel M3).

    The kernel never imports a browser tool: the SDK host resolves the
    ``browser`` built-in plugin's pack factory through the plugin loader and
    injects it here. Called only when the session has a live backend AND the
    agent opens the ``browser`` capability. Signature =
    ``noeta.builtins.browser.impl:build_browser_tools``.
    """

    def __call__(self, backend: BrowserBackend) -> dict[str, Tool]: ...


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
        delegation_enabled: bool,
        todo_write_enabled: bool,
        ask_user_question_enabled: bool,
        skill_invocation_enabled: bool,
        workflow_enabled: bool,
        skill_menu_names: frozenset[str],
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
    write_mode: FsWriteMode
    shell_mode: ShellMode
    shell_allowlist: Sequence[Mapping[str, Any]]
    write_path_globs: tuple[str, ...]
    write_roots: Optional[WriteRootsResolver]
    skills_dir: Optional[Path]
    builtin_skills_dirs: Sequence[Path]
    global_skills_dir: Optional[Path]
    require_approval_tools: tuple[str, ...]
    shell_approval_predicate: Optional[Callable[[str, Mapping[str, Any]], bool]]
    skill_tool_enforcement: SkillEnforcementMode
    delegation_enabled: bool
    allow_skill_scripts: bool
    todo_write_enabled: bool
    ask_user_question_enabled: bool
    skill_invocation_enabled: bool
    workflow_enabled: bool
    structured_output_schema: Optional[dict[str, Any]]
    memory_enabled: bool
    memory_dir: Optional[Path]
    global_memory_dir: Optional[Path]
    instructions_enabled: bool
    instructions_file: Optional[Path]
    #: `read`-triggered discovery of subdirectory instruction files
    #: (docs/adr/anchored-content-placement.md). Default off — byte-identical
    #: for every existing host. Independent of ``instructions_enabled``.
    instructions_discovery: bool
    mcp_tools_override: Optional[dict[str, Tool]]
    custom_tools: Optional[dict[str, Tool]]
    app_gateway: Optional[AppPreviewGateway]
    #: Execution backend for the fs / shell pack. ``None`` ⇒ host
    #: (``LocalExecEnv``, byte-identical); a sandbox ``ExecEnv`` makes the pack
    #: act against a container and switches the workspace to lexical
    #: (container-path) containment. A wiring-only runtime injection, never part
    #: of session identity — the tool schemas are unchanged either way.
    exec_env: Optional[ExecEnv]
    #: Per-session browser backend (sandbox-only). ``None`` ⇒ no browser tools
    #: (byte-identical). A live ``AioBrowserBackend`` (built by the SDK host from
    #: the session's sandbox handle) + ``browser_enabled`` merges the noeta-owned
    #: browser tool pack. Wiring-only runtime injection, never session identity;
    #: the tool schemas are noeta-owned and fixed, so the stable prefix depends
    #: only on ``browser_enabled`` (a capability), never on the backend.
    browser_backend: Optional[BrowserBackend]
    #: Whether this agent's identity opens the browser capability
    #: (``AgentSpec.capabilities.browser``). The browser pack is merged only when
    #: this is ``True`` AND ``browser_backend`` is present.
    browser_enabled: bool
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
    #: catalog (microkernel M2) and consumed by the edit-tool mutex in
    #: ``_stage_fs_pack``. ``None`` (kernel-alone / stub) drops neither edit
    #: tool — the documented no-catalog semantic, NOT a silent fallback.
    provider_family: Optional[str] = None
    #: Loader-resolved browser tool pack factory (microkernel M3, D2):
    #: ``noeta.builtins.browser.impl:build_browser_tools``, resolved by the
    #: SDK host and injected here. ``None`` fails loudly at the browser stage
    #: when a live backend + the capability are both present — the kernel
    #: never imports a browser tool.
    browser_tools_factory: Optional[BrowserToolsFactory] = None
    #: Loader-resolved app-preview pack factory (microkernel M3, D2):
    #: ``noeta.builtins.app.impl:build_app_tools``, resolved by the SDK host
    #: and injected here. ``None`` fails loudly at the app stage when a live
    #: gateway is present — the kernel never imports the tool.
    app_tools_factory: Optional[AppToolsFactory] = None
    #: The manifest-contributed session packs (microkernel phase 3): resolved
    #: by the SDK host (``noeta.client.parts.default_session_packs`` + the
    #: external plugins' ``session_pack`` projection) and run by the generic
    #: pack loop in ``(priority, name)`` order. Empty builds a session with
    #: no pack tools at all — the kernel mandates no capability.
    session_packs: tuple[SessionPackEntry, ...] = ()
    #: Injected memory-index kit (phase 2c):
    #: ``noeta.builtins.memory.impl:build_memory_index_kit``. Carries the
    #: index renderer/hash/kind factory. ``None`` fails loudly at registry
    #: build when ``memory_enabled`` — the kernel ships no index prose.
    memory_index_kit: Optional[MemoryIndexKit] = None
    #: Injected instructions kit (phase 2c):
    #: ``noeta.builtins.workspace.impl:build_instructions_kit``. Carries the
    #: tag renderer/hash/kind factory + the candidate-filename convention.
    #: ``None`` fails loudly when instructions are enabled or discovery is on.
    instructions_kit: Optional[InstructionsKit] = None
    #: Injected environment kit (phase 2c):
    #: ``noeta.builtins.workspace.impl:build_environment_kit``. Carries the
    #: env-block renderer/hash/kind factory. The environment stage is always
    #: on (a workspace always exists), so this kit is effectively required —
    #: ``None`` fails loudly at registry build.
    environment_kit: Optional[EnvironmentKit] = None


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


# NOTE (microkernel phase 3, S2): the fs / web / memory / skills packs are no
# longer kernel-internal — their factories are ``session_pack`` manifest
# contributions (``noeta.builtins.<name>.impl:build_<name>_session_pack``),
# resolved by the SDK host and passed in as ``session_packs``. The kernel
# keeps only the not-yet-migrated packs below (instructions / environment /
# browser / app — S3/S4) plus the two kernel-owned injections (mcp / custom).


def _instructions_pack(
    spec: _BuildSpec, ctx: SessionBuildContext
) -> PackContribution:
    """instructions snapshot load (no tools — feeds the content kind only).

    Read once here at build time so the composer's renderer AND the pre-loop
    ``record_instructions`` share the same snapshot. The shared mutable
    ``name → snapshot`` mapping is ALWAYS exported (empty when the flag is
    off) — the discovery hook and resume preloader add entries to the same
    dict at tool/step time, so its identity is the contract.
    """
    snapshots: dict[str, InstructionsSnapshot] = {}
    exports: dict[str, object] = {EXPORT_INSTRUCTIONS_SNAPSHOTS: snapshots}
    if not spec.instructions_enabled:
        return PackContribution(exports=exports)
    if spec.instructions_kit is None:
        raise RuntimeError(
            "instructions_enabled=True requires an injected instructions_kit "
            "(the workspace built-in's build_instructions_kit — phase 2c); "
            "the kernel ships no filename convention or renderer of its own."
        )
    snapshot = load_instructions(
        ctx.workspace_dir,
        filenames=spec.instructions_kit.filenames,
        override_path=spec.instructions_file,
        exec_env=ctx.exec_env,
    )
    if snapshot is not None:
        # Seed the shared mapping the kind renders from: the root file lives
        # under its basename (resident name unchanged → byte-identical
        # rendering); discovered files join later under relative paths.
        snapshots[snapshot.name] = snapshot
        exports[EXPORT_INSTRUCTIONS_SNAPSHOT] = snapshot
    return PackContribution(exports=exports)


def _environment_pack(
    spec: _BuildSpec, ctx: SessionBuildContext
) -> PackContribution:
    """Workspace environment snapshot load (no tools — feeds the content kind).

    Always on (a workspace always exists): capture the session-static
    workspace facts once here at build time so the composer's renderer AND
    the pre-loop ``record_environment`` share the same snapshot.
    """
    return PackContribution(
        exports={
            EXPORT_ENVIRONMENT_SNAPSHOT: load_environment(
                ctx.workspace_dir, exec_env=ctx.exec_env
            )
        }
    )


def _browser_pack(spec: _BuildSpec, ctx: SessionBuildContext) -> PackContribution:
    """browser tools — sandbox-only, flag-gated (NOT whitelist-filtered).

    Merged when the session both has a live ``"browser"`` backend in the
    context bag (the SDK host built one from the sandbox handle) AND this
    agent opens the ``browser`` capability. The tool schemas are noeta-owned
    and fixed, so a browser session's stable prefix depends only on the
    capability flag, never on the backend or the AIO image. Absent backend OR
    flag off (resume with no sandbox / every non-browser agent) ⇒ nothing
    merged, byte-identical tool set.
    """
    backend = cast(Optional[BrowserBackend], ctx.backends.get("browser"))
    if backend is None or not ctx.flag("browser"):
        return EMPTY_CONTRIBUTION
    if spec.browser_tools_factory is None:
        raise RuntimeError(
            "browser tool pack factory was not injected — the SDK host "
            "resolves the browser built-in plugin through the plugin "
            "loader and passes browser_tools_factory (microkernel M3); "
            "the kernel builder imports no tool implementation"
        )
    return PackContribution(tools=spec.browser_tools_factory(backend))


def _app_pack(spec: _BuildSpec, ctx: SessionBuildContext) -> PackContribution:
    """open_app pack — gateway-injected, merged after custom_tools (band 1000)
    so the host's open_app is authoritative.

    Gated on a live ``"app_preview"`` gateway in the context bag — absent
    (resume + every SDK/test fixture) ⇒ empty, keeping the tool set + stable
    hash byte-identical (a resumed turn that wires no gateway rebuilds the
    identical tool schemas).
    """
    gateway = cast(
        Optional[AppPreviewGateway], ctx.backends.get("app_preview")
    )
    if gateway is None:
        return EMPTY_CONTRIBUTION
    if spec.app_tools_factory is None:
        raise RuntimeError(
            "app tool pack factory was not injected — the SDK host "
            "resolves the app built-in plugin through the plugin loader "
            "and passes app_tools_factory (microkernel M3); the kernel "
            "builder imports no tool implementation"
        )
    return PackContribution(
        tools=spec.app_tools_factory(ctx.workspace, gateway)
    )


# ---------------------------------------------------------------------------
# Post-tools phases — control schemas, content channels, composer, policy,
# guards. Each reads from the finished assembly and the frozen spec.
# ---------------------------------------------------------------------------


def _build_control_action_schemas(
    spec: _BuildSpec, asm: _ToolAssembly
) -> Optional[list[dict[str, Any]]]:
    """The ordered control-action schema list (the composer's extra schemas).

    Issue C: when delegation is enabled, the parent's composer exposes
    the `spawn_subagent` control schema (so it lands in View.provider_tool_schemas
    + the stable hash) and the policy translates it into a
    SpawnSubtaskDecision. A resumed turn rebuilds the SAME schemas → the
    View stable hash matches the recording. CW18b/CW18c: control action
    schemas are appended in one stable order. All default off; a resuming
    host must pass the same flags the recording used or the rebuilt View
    stable hash no longer matches.
    """
    control_action_list: list[dict[str, Any]] = []
    if spec.delegation_enabled:
        control_action_list.append(
            spawn_subagent_tool_schema(spec.subtask_agent_directory)
        )
    if spec.todo_write_enabled:
        control_action_list.append(todo_write_tool_schema())
    if spec.ask_user_question_enabled:
        control_action_list.append(ask_user_question_tool_schema())
    # Skill tool is grown only when the flag is on AND the
    # registry contains at least one indexed skill. The sorted
    # ``(name, description)`` menu is built here from the registry — single
    # source of truth so callers never pass a divergent menu. No registry at
    # all (the ``skills`` built-in disabled) reads the same as an empty one:
    # a capability the agent declares but the host never wired grows no tool.
    if spec.skill_invocation_enabled and asm.registry is not None:
        skill_names = asm.registry.names()
        if skill_names:
            menu = tuple(
                (name, desc.description)
                for name in sorted(skill_names)
                if (desc := asm.registry.get(name)) is not None
            )
            control_action_list.append(skill_tool_schema(menu))
    # The run_workflow control schema is appended LAST (matching
    # the translation routing order ask→plan→todo→spawn→skill→workflow). Off by
    # default ⇒ View stable hash unchanged; a resuming host must pass the same flag.
    if spec.workflow_enabled:
        control_action_list.append(run_workflow_tool_schema())
    # A workflow helper with a declared agent() schema exposes a
    # per-helper structured_output control schema (appended last, opt-in). Off by
    # default ⇒ View stable hash unchanged; the orchestration wrapper intercepts.
    if spec.structured_output_schema is not None:
        control_action_list.append(
            structured_output_tool_schema(spec.structured_output_schema)
        )
    return control_action_list or None


def _skill_menu_names(spec: _BuildSpec, asm: _ToolAssembly) -> frozenset[str]:
    """The skill-tool menu names the policy factory binds (matches the schema).

    Mirrors the gate in :func:`_build_control_action_schemas`: non-empty only
    when ``skill_invocation_enabled`` AND a registry exists AND it has indexed
    skills, so the policy's ``skill_menu_names`` and the composer's skill
    schema agree — including when the ``skills`` built-in is off and neither
    grows anything.
    """
    if spec.skill_invocation_enabled and asm.registry is not None:
        skill_names = asm.registry.names()
        if skill_names:
            return frozenset(skill_names)
    return frozenset()


def _build_content_registry(
    spec: _BuildSpec,
    asm: _ToolAssembly,
    pack_kinds: tuple[ContentKindSpec, ...] = (),
) -> ContentChannelRegistry:
    """The content-channel registry — registration order IS semi_stable layout.

    The content-channel registry is built HERE —
    registration order IS the semi_stable layout (skill first; further
    kinds, e.g. memory, append behind it). The same registry feeds the
    composer's render rules AND the engine's generic content_hashes seam
    so the rendered content and the recorded fingerprint come from one source.

    The skill kind is the first resident but no longer a required one: with
    the ``skills`` built-in off the kit never ran, so the channel simply has
    no skill resident and the later kinds close up behind it. That shifts the
    semi_stable layout — which is correct and self-consistent, because a
    session built without skills is a different configuration, and a recording
    made WITH skills must be resumed with skills (the host passes the same
    switch, exactly as it must for ``memory_enabled`` and the rest).
    """
    content_kinds: list[ContentKindSpec] = []
    if asm.skill_content_kind is not None:
        content_kinds.append(asm.skill_content_kind)
    if spec.memory_enabled:
        # The second resident: renders the index snapshot
        # into semi_stable when activated; policy "evolving".
        if spec.memory_index_kit is None:
            raise RuntimeError(
                "memory_enabled=True requires an injected memory_index_kit "
                "(the memory built-in's build_memory_index_kit — phase 2c); "
                "the kernel ships no index renderer of its own."
            )
        content_kinds.append(spec.memory_index_kit.content_kind(asm.memory_entries))
    # Instructions (third resident): append AFTER memory so existing
    # semi_stable byte layout is unchanged for memory-only sessions.
    # Registered when a root snapshot loaded (rendering byte-identical to the
    # single-snapshot kind — same resident name, same bytes) OR when
    # discovery is armed (an empty mapping renders nothing until the first
    # discovered activation, zero footprint). Neither → no kind registered,
    # same as never adding the feature.
    if asm.instructions_snapshots or spec.instructions_discovery:
        if spec.instructions_kit is None:
            raise RuntimeError(
                "instructions require an injected instructions_kit "
                "(the workspace built-in's build_instructions_kit — phase 2c); "
                "the kernel ships no instructions renderer of its own."
            )
        content_kinds.append(
            spec.instructions_kit.content_kind_from(asm.instructions_snapshots)
        )
    # Environment (fourth resident): append LAST so the semi_stable byte
    # layout is unchanged for sessions that never activate it. Always
    # registered (a workspace always exists); the renderer is a zero-
    # footprint no-op until the pre-loop ``record_environment`` activates
    # it, so existing recordings without an environment event resume
    # byte-equal.
    if asm.environment_snapshot is not None:
        if spec.environment_kit is None:
            raise RuntimeError(
                "the environment resident requires an injected environment_kit "
                "(the workspace built-in's build_environment_kit — phase 2c); "
                "the kernel ships no environment renderer of its own."
            )
        content_kinds.append(
            spec.environment_kit.content_kind(asm.environment_snapshot)
        )
    # Pack-contributed kinds (microkernel phase 3): sorted upstream by
    # ``(priority, pack order)``, appended after the built-in residents.
    # Empty until S4 moves the resident kits into their packs, so every
    # existing session keeps its semi_stable byte layout.
    content_kinds.extend(pack_kinds)
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
            spec.allowed_subtask_agents if spec.delegation_enabled else None
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
    write_mode: FsWriteMode = FsWriteMode.DRY_RUN,
    shell_mode: ShellMode = ShellMode.ALLOWLIST,
    shell_allowlist: Sequence[Mapping[str, Any]] = (),
    #: Injected path whitelist for the ``write`` tool. Empty
    #: ⇒ unrestricted (default, byte-equal); non-empty ⇒ ``write`` refuses any
    #: path outside the globs. The host derives this from the spec's
    #: ``metadata["write_path_globs"]`` (e.g. ``plans/*.md``).
    write_path_globs: tuple[str, ...] = (),
    #: Host authorization for writes OUTSIDE the workspace:
    #: ``task_id -> the extra directories that task may write``, consulted
    #: per call by ``edit`` / ``write`` / ``apply_patch``. ``None`` (default)
    #: keeps the single-root wall. Deliberately a resolver, not a fixed
    #: tuple: the product grows this set mid-session (the owner approves an
    #: out-of-workspace write while the task is paused), and rebuilding the
    #: tool set to take it would move the stable prefix.
    write_roots: Optional[WriteRootsResolver] = None,
    skills_dir: Optional[Path] = None,
    # The lower skill tiers below the workspace-local pack:
    # built-in skills first, then the global ``~/.noeta/skills``. Both are
    # deployment-fixed dirs the agent layer supplies (the SDK / test path
    # leaves them empty for byte-identical single-dir behaviour).
    builtin_skills_dirs: Sequence[Path] = (),
    global_skills_dir: Optional[Path] = None,
    require_approval_tools: tuple[str, ...] = (),
    #: Per-call conditional approval predicate, forwarded verbatim into
    #: ``PermissionPolicy.conditional_approval``. Built by the SDK host for the
    #: shell allowlist-or-approve gate; ``None`` on every other path.
    shell_approval_predicate: Optional[
        Callable[[str, Mapping[str, Any]], bool]
    ] = None,
    skill_tool_enforcement: SkillEnforcementMode = "off",
    delegation_enabled: bool = False,
    allow_skill_scripts: bool = False,
    todo_write_enabled: bool = False,
    ask_user_question_enabled: bool = False,
    skill_invocation_enabled: bool = False,
    workflow_enabled: bool = False,
    #: When set, expose a per-helper ``structured_output`` control
    #: schema (its ``parameters`` = this JSON Schema). Set ONLY for a workflow
    #: helper subtask whose ``agent(schema=...)`` declared a schema; the
    #: orchestration's StructuredOutputPolicy wrapper intercepts the call.
    structured_output_schema: Optional[dict[str, Any]] = None,
    memory_enabled: bool = False,
    memory_dir: Optional[Path] = None,
    # The global memory root (default ``~/.noeta/memories``).
    # ``None`` ⇒ the SDK default global dir. Memory is pinned here, never
    # derived from the per-session workspace, so it survives workspace
    # switches. ``memory_dir`` (the explicit override) still wins over this.
    global_memory_dir: Optional[Path] = None,
    instructions_enabled: bool = False,
    instructions_file: Optional[Path] = None,
    #: `read`-triggered discovery of subdirectory ``NOETA.md``/``AGENTS.md``
    #: (docs/adr/anchored-content-placement.md). Default off ⇒ byte-identical
    #: for every existing caller. When on, ``SessionInputs`` additionally
    #: carries the ``content_discovery`` / ``content_preloader`` seams the
    #: host wires into the Engine.
    instructions_discovery: bool = False,
    mcp_tools_override: Optional[dict[str, Tool]] = None,
    custom_tools: Optional[dict[str, Tool]] = None,
    #: The host's live preview gateway. When set, the ``open_app``
    #: tool (gateway-injected) is merged into the tool set so the agent can
    #: render HTML apps in the right-side panel. ``None`` (resume + every
    #: SDK/test fixture) ⇒ no open_app, so the tool set + stable hash stay
    #: byte-identical (a resumed turn that wires no gateway rebuilds the
    #: identical tool schemas); only noeta-agent's live serving
    #: path wires a real gateway.
    app_gateway: Optional[AppPreviewGateway] = None,
    #: Execution backend for the fs / shell pack. ``None`` (resume + every
    #: SDK/test fixture) ⇒ the host ``LocalExecEnv`` and a host ``WorkspaceRoot``
    #: — byte-identical, and the tool schemas are unchanged so the stable prefix
    #: is unaffected. A sandbox ``ExecEnv`` (supplied per-task by the product
    #: host once it has provisioned / attached a container, T5/T6) makes the
    #: pack act against that container and switches the workspace to lexical
    #: (container-path) containment. Wiring-only, never session identity.
    exec_env: Optional[ExecEnv] = None,
    #: Per-session browser backend + the agent's browser capability flag
    #: (sandbox-only). ``None`` backend / ``False`` flag (resume + every
    #: SDK/test fixture + every non-browser agent) ⇒ no browser tools merged, so
    #: the tool set + stable hash stay byte-identical. The SDK host supplies a
    #: live ``AioBrowserBackend`` (built from the session's sandbox handle) only
    #: when it has provisioned a container AND the agent opens ``browser``.
    browser_backend: Optional[BrowserBackend] = None,
    browser_enabled: bool = False,
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
    #: Loader-resolved browser tool pack factory (microkernel M3, D2): the
    #: ``browser`` built-in plugin's ``build_browser_tools``, resolved by the
    #: SDK host and injected here; ``None`` fails loudly only when a live
    #: backend + the browser capability are both present.
    browser_tools_factory: Optional[BrowserToolsFactory] = None,
    #: Loader-resolved app-preview pack factory (microkernel M3, D2): the
    #: ``app`` built-in plugin's ``build_app_tools``, resolved by the SDK
    #: host and injected here; ``None`` fails loudly only when a live
    #: preview gateway is present.
    app_tools_factory: Optional[AppToolsFactory] = None,
    #: Loader-resolved default policy factory builder (microkernel phase 2b):
    #: the ``react`` built-in plugin's ``build_react_policy_factory``,
    #: resolved by the SDK host and injected here; ``None`` fails loudly at
    #: policy construction unless ``policy_factory_override`` replaces the
    #: default outright.
    default_policy_factory: Optional[PolicyFactoryBuilder] = None,
    #: Injected resident kits (phase 2c): the memory built-in's
    #: ``build_memory_index_kit`` and the workspace built-in's
    #: ``build_instructions_kit`` / ``build_environment_kit``. Each fails
    #: loudly when its resident is wired without it — the kernel ships no
    #: resident prose, hash rule, or filename convention.
    memory_index_kit: Optional[MemoryIndexKit] = None,
    instructions_kit: Optional[InstructionsKit] = None,
    environment_kit: Optional[EnvironmentKit] = None,
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
    * ``write_mode`` defaults to ``DRY_RUN`` — defence in depth: even if
      the tool were somehow ``invoke``-d during a resume, the closure would
      refuse to write. Tests sentinel-pin the no-write property anyway.
    * ``shell_mode`` should match the recording's mode so the rebuilt
      ``shell_run`` tool's allow-list shape (and thus its
      ``input_schema``) reproduces the recorded ``provider_tool_schemas`` bytes.

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
        write_mode=write_mode,
        shell_mode=shell_mode,
        shell_allowlist=shell_allowlist,
        write_path_globs=write_path_globs,
        write_roots=write_roots,
        skills_dir=skills_dir,
        builtin_skills_dirs=builtin_skills_dirs,
        global_skills_dir=global_skills_dir,
        require_approval_tools=require_approval_tools,
        shell_approval_predicate=shell_approval_predicate,
        skill_tool_enforcement=skill_tool_enforcement,
        delegation_enabled=delegation_enabled,
        allow_skill_scripts=allow_skill_scripts,
        todo_write_enabled=todo_write_enabled,
        ask_user_question_enabled=ask_user_question_enabled,
        skill_invocation_enabled=skill_invocation_enabled,
        workflow_enabled=workflow_enabled,
        structured_output_schema=structured_output_schema,
        memory_enabled=memory_enabled,
        memory_dir=memory_dir,
        global_memory_dir=global_memory_dir,
        instructions_enabled=instructions_enabled,
        instructions_file=instructions_file,
        instructions_discovery=instructions_discovery,
        mcp_tools_override=mcp_tools_override,
        custom_tools=custom_tools,
        app_gateway=app_gateway,
        exec_env=exec_env,
        browser_backend=browser_backend,
        browser_enabled=browser_enabled,
        hooks_pre_tool_use=hooks_pre_tool_use,
        extra_guards=extra_guards,
        extra_content_kinds=extra_content_kinds,
        extra_reminders=extra_reminders,
        session_packs=session_packs,
        base_reminders=base_reminders,
        guards_factory=guards_factory,
        provider_family=provider_family,
        memory_index_kit=memory_index_kit,
        instructions_kit=instructions_kit,
        environment_kit=environment_kit,
        browser_tools_factory=browser_tools_factory,
        app_tools_factory=app_tools_factory,
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
    # reads THIS, never the spec. The backend bag and capability flags are
    # synthesized from the legacy parameters until S3/S5 replace them with
    # generic inputs.
    backends: dict[str, object] = {}
    if browser_backend is not None:
        backends["browser"] = browser_backend
    if app_gateway is not None:
        backends["app_preview"] = app_gateway
    ctx = SessionBuildContext(
        workspace=workspace,
        workspace_dir=workspace_dir,
        content_store=content_store,
        exec_env=exec_env,
        model=model,
        provider_family=provider_family,
        allowed_tools=allowed_tools,
        backends=backends,
        capability_flags={
            "memory": memory_enabled,
            "browser": browser_enabled,
        },
        # S2-transitional synthesis: the packs read their own config entry;
        # S4 replaces the feature-named parameters with a caller-supplied
        # ``plugin_config`` and this block collapses into a passthrough.
        plugin_config={
            "memory": {
                "memory_dir": memory_dir,
                "global_memory_dir": global_memory_dir,
            },
            "skills": {
                "skills_dir": skills_dir,
                "builtin_skills_dirs": tuple(builtin_skills_dirs),
                "global_skills_dir": global_skills_dir,
                "allow_skill_scripts": allow_skill_scripts,
            },
        },
        write_mode=write_mode,
        shell_mode=shell_mode,
        shell_allowlist=shell_allowlist,
        write_path_globs=write_path_globs,
        write_roots=write_roots,
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
        SessionPackEntry("instructions", 400, partial(_instructions_pack, spec)),
        SessionPackEntry("environment", 500, partial(_environment_pack, spec)),
        SessionPackEntry("browser", 700, partial(_browser_pack, spec)),
        SessionPackEntry("mcp", 800, _mcp_entry),
        SessionPackEntry("custom", 900, _custom_entry),
        SessionPackEntry("app", 1000, partial(_app_pack, spec)),
    ]
    entries.sort(key=lambda e: (e.priority, e.name))

    pack_kinds: list[tuple[int, int, ContentKindSpec]] = []
    exports: dict[str, object] = {}
    for seq, entry in enumerate(entries):
        contrib = entry.factory(ctx)
        for name, tool in contrib.tools.items():
            tools[name] = tool
        for ck in contrib.content_kinds:
            pack_kinds.append((ck.priority, seq, ck.spec))
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
    control_action_schemas = _build_control_action_schemas(spec, asm)
    skill_menu_names = _skill_menu_names(spec, asm)
    content_registry = _build_content_registry(
        spec,
        asm,
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
            delegation_enabled=delegation_enabled,
            todo_write_enabled=todo_write_enabled,
            ask_user_question_enabled=ask_user_question_enabled,
            skill_invocation_enabled=skill_invocation_enabled,
            workflow_enabled=workflow_enabled,
            skill_menu_names=skill_menu_names,
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

    # Anchored-content seams (docs/adr/anchored-content-placement.md): armed
    # only by ``instructions_discovery``. Both close over the SAME snapshot
    # mapping the composer's instructions kind renders from, so a discovered
    # (or resume-preloaded) file is renderable the moment its activation folds.
    content_discovery = None
    content_preloader = None
    if instructions_discovery:
        if instructions_kit is None:
            raise RuntimeError(
                "instructions_discovery=True requires an injected "
                "instructions_kit (the workspace built-in's "
                "build_instructions_kit — phase 2c)."
            )
        content_discovery = build_instructions_discovery(
            asm.workspace,
            asm.instructions_snapshots,
            kit=instructions_kit,
            exec_env=exec_env
        )
        content_preloader = build_instructions_preloader(
            workspace_dir, asm.instructions_snapshots, exec_env=exec_env
        )

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
    )
