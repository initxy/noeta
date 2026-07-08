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
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from noeta.context.composer import _COMPOSER_VERSION, ThreeSegmentComposer
from noeta.context.content_channel import ContentChannelRegistry, ContentKindSpec
from noeta.context.environment import EnvironmentSnapshot, environment_content_kind
from noeta.context.instructions import InstructionsSnapshot, instructions_content_kind
from noeta.context.memory import MemoryEntries, memory_content_kind
from noeta.core.hooks import HookManager
from noeta.execution.environment import load_environment
from noeta.execution.instructions import load_instructions
from noeta.execution.memory import DEFAULT_GLOBAL_MEMORY_DIR, load_memory_store
from noeta.execution.skills import (
    build_skill_composer,
    build_skill_script_wiring,
    extract_skill_allowed_tools_raw,
    load_workspace_skills,
    resolve_skill_roots,
    skill_content_kind,
)
from noeta.guards.budget import Budget, BudgetGuard
from noeta.guards.hook import HookGuard, PreToolUseRule
from noeta.guards.permission import (
    PermissionGuard,
    PermissionPolicy,
    SkillEnforcementMode,
)
from noeta.guards.repetition import (
    RepetitionAction,
    RepetitionGuard,
    RepetitionPolicy,
)
from noeta.policies.control_tools import (
    ask_user_question_tool_schema,
    run_workflow_tool_schema,
    skill_tool_schema,
    structured_output_tool_schema,
    todo_write_tool_schema,
)
from noeta.policies.react import (
    ReActPolicy,
    spawn_subagent_tool_schema,
)
from noeta.policies.skill_tools import resolve_skill_allowed_tools
from noeta.protocols.content_store import ContentStore
from noeta.protocols.hooks import Guard
from noeta.protocols.policy import Policy
from noeta.protocols.tool import Tool
from noeta.providers.catalog import provider_family, resolve_alias, spec_for
from noeta.tools.app import AppPreviewGateway, build_app_tools
from noeta.tools.fs import FsWriteMode, ShellMode, WorkspaceRoot, build_fs_tools
from noeta.tools.fs.exec_env import ExecEnv
from noeta.tools.mcp import MCP_PREFIX, McpConfigError
from noeta.tools.memory import MemoryStore, build_memory_tools
from noeta.tools.web import build_web_tools


__all__ = [
    "CompactionConfig",
    "COMPACTION_OFF",
    "SessionInputs",
    "build_session_inputs",
    "derive_compaction_config",
    "select_provider_edit_tool",
]


# ---------------------------------------------------------------------------
# provider-mutex edit tool selection (assembly-layer only)
# ---------------------------------------------------------------------------

#: The two mutually-exclusive batch/precise edit tools whose membership is
#: decided at assembly time by the bound model's vendor family. Both live in
#: the built-in catalog (and in the ``main`` / ``general-purpose`` whitelist),
#: but exactly one ever reaches a live tool set so the model sees a single,
#: provider-appropriate editing primitive.
_ANTHROPIC_EDIT_TOOL = "edit"
_OPENAI_EDIT_TOOL = "apply_patch"


def select_provider_edit_tool(model: str) -> dict[str, None]:
    """Return the edit-tool name(s) to **drop** for the bound ``model``.

    The provider difference (Anthropic ships ``edit``, OpenAI /
    GPT ships ``apply_patch``) is absorbed at the assembly layer — it is NOT a
    tool field and NOT a prompt instruction. This helper maps the model's vendor
    family (via :func:`noeta.providers.catalog.provider_family`) to the set of
    edit tools that must be removed from the freshly-built fs pack:

    * an **Anthropic** model drops ``apply_patch`` (keeps ``edit``);
    * an **OpenAI / GPT** model drops ``edit`` (keeps ``apply_patch``);
    * an **unrecognised** model (test/stub sentinel, uncatalogued id) drops
      nothing — both stay so existing recordings resume byte-equal.

    The return value is a ``{name: None}`` mapping (a cheap set-with-stable-
    iteration) the caller pops out of the tool dict; ``None`` family ⇒ empty
    mapping ⇒ no-op filter.
    """
    family = provider_family(model)
    if family == "anthropic":
        return {_OPENAI_EDIT_TOOL: None}
    if family == "openai":
        return {_ANTHROPIC_EDIT_TOOL: None}
    return {}


# ---------------------------------------------------------------------------
# Compaction config (③ finding 1) — hoisted from noeta.execution.builder
# ---------------------------------------------------------------------------

#: Fixed headroom (estimated tokens) reserved under the context window beyond
#: the output cap, so the available history window leaves slack for the system
#: prompt + provider tool schemas + the next response. Deterministic constant (D-3d):
#: the same value on live + resume.
_COMPACTION_BUFFER_TOKENS = 2_000

#: Fraction of the usable window kept as the verbatim recent tail, expressed as
#: a denominator (``tail = available // N``). Compaction keeps
#: a verbatim tail because noeta cannot re-read disk during compose (resume
#: determinism) — but half the window (the original ``N=2``) is heavier than
#: needed: the summary keeps file paths and the model re-reads with ``read``, so
#: a smaller tail frees window at the cost of less recent verbatim fidelity.
#: ``N=3`` (a third) is the conservative first step toward Claude's much leaner
#: stance (near-zero verbatim tail). Smaller tail ⇒ compaction fires LESS often
#: (more headroom after each) and each summary covers a bigger prefix.
#: Deterministic constant: same value on live + resume.
_TAIL_FRACTION_DENOM = 3


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


def derive_compaction_config(model: str) -> CompactionConfig:
    """Derive the compaction knobs for ``model`` from the sdk catalog.

    ``model`` may be a friendly ALIAS (``opus`` / ``sonnet`` / ``haiku``) or a
    real catalog id; it is resolved via :func:`resolve_alias` BEFORE the catalog
    lookup. Without that resolution an alias (the common selector a host passes)
    misses ``spec_for`` with ``KeyError`` and silently disables compaction
    (fix B). ``resolve_alias`` passes a non-alias value through unchanged, so a
    real id and the test-only ``stub-model`` are unaffected.

    Returns :data:`COMPACTION_OFF` for any model the catalog does not describe
    after resolution (so ``stub-model`` keeps legacy behaviour and existing
    recordings stay byte-equal). For a catalogued model the available window is
    ``context_window - max_output_tokens - buffer`` and the protected tail is
    ``available // _TAIL_FRACTION_DENOM`` (a third — see the constant's note for
    the trade-off; always strictly smaller than the window, so a misconfiguration
    that would otherwise leave nothing to summarise cannot arise). All numbers
    are deterministic functions of the spec, and live + resume resolve the SAME
    ``model`` string here, so both paths derive identical knobs.
    """
    try:
        spec = spec_for(resolve_alias(model))
    except KeyError:
        return COMPACTION_OFF
    available = max(
        0,
        spec.context_window
        - spec.max_output_tokens
        - _COMPACTION_BUFFER_TOKENS,
    )
    # Protect a third of the available window as the recent tail. Strictly < the
    # available window so summarising always has a non-empty prefix to collapse
    # when the trigger fires.
    tail = available // _TAIL_FRACTION_DENOM
    return CompactionConfig(
        context_window=spec.context_window,
        max_output_tokens=spec.max_output_tokens,
        compaction_buffer=_COMPACTION_BUFFER_TOKENS,
        tail_token_budget=tail,
        composer_version=_COMPOSER_VERSION,
    )


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
    #: the workspace's file store (``None`` when ``memory_enabled`` was
    #: off); ``memory_entries`` is the load-time index snapshot the
    #: composer's renderer AND the pre-loop ``record_memory_index`` must
    #: share (one snapshot, one fingerprint — record time equals compose
    #: time by construction).
    memory_store: Optional[MemoryStore] = None
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


# ---------------------------------------------------------------------------
# C02 deepening — frozen build spec + mutable tool-assembly accumulator
# ---------------------------------------------------------------------------


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
    mcp_tools_override: Optional[dict[str, Tool]]
    custom_tools: Optional[dict[str, Tool]]
    app_gateway: Optional[AppPreviewGateway]
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
    repetition_threshold: int
    repetition_action: RepetitionAction
    repetition_window: int
    subtask_agent_directory: tuple[tuple[str, str], ...]
    output_schema: Optional[dict[str, Any]]
    thinking: Optional[str]
    effort: Optional[str]
    tool_output_inline_limit: Optional[int]


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
    memory_store: Optional[MemoryStore] = None
    memory_entries: MemoryEntries = ()
    instructions_snapshot: Optional[InstructionsSnapshot] = None
    environment_snapshot: Optional[EnvironmentSnapshot] = None
    skill_script_tools: frozenset[str] = frozenset()
    skill_scripts: frozenset[tuple[str, str]] = frozenset()
    workspace: WorkspaceRoot = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Tool pipeline stages — each owns "whether to enable + how to build/filter" + its side effect.
# The ORDER of _TOOL_PIPELINE is the construction-order contract
# (fs → memory → script → mcp → custom → app, with the read-fence and
# allowed_tools filter folded into the fs stage). Every stage is a
# ``(_BuildSpec, _ToolAssembly) -> None`` that mutates ``asm`` in place.
# ---------------------------------------------------------------------------


def _stage_fs_pack(spec: _BuildSpec, asm: _ToolAssembly) -> None:
    """fs + web + provider-edit + ``allowed_tools`` filter (the base pack).

    Builds the full built-in pack, applies the provider-mutex edit drop, then
    filters by the spec whitelist — the result is the ONLY stage whose output
    passes through ``allowed_tools``; every later stage appends past the filter
    (flag-gated tools are never whitelist-filtered, by design).
    """
    # Sandbox mode (``exec_env`` set) makes ``workspace_dir`` a CONTAINER path:
    # a host ``realpath`` / existence check is wrong (it lives in the container),
    # so build a lexical containment root and let the ExecEnv do the remote IO.
    if spec.exec_env is None:
        asm.workspace = WorkspaceRoot.from_path(spec.workspace_dir)
    else:
        asm.workspace = WorkspaceRoot.for_container(spec.workspace_dir)
    full_pack = build_fs_tools(
        asm.workspace,
        mode=spec.write_mode,
        shell_mode=spec.shell_mode,
        shell_allowlist=spec.shell_allowlist,
        write_path_globs=spec.write_path_globs,
        exec_env=spec.exec_env,
    )
    # The web pack (``webfetch``) is a built-in but not an
    # fs tool (no WorkspaceRoot). Merge it into the full pack HERE, before the
    # ``allowed_tools`` filter below, so it is gated by the spec whitelist like
    # every other built-in — ``main`` (tools=None full catalog) gets it;
    # explore / plan / general-purpose (explicit whitelists without it) do not.
    full_pack.update(build_web_tools())
    # The edit↔apply_patch difference is provider-specific and is
    # absorbed HERE, at the assembly layer — not in any tool field, not in the
    # prompt, not in the AgentSpec whitelist (so fingerprints never drift on a
    # model swap). The bound model's vendor family decides which of the two
    # mutually-exclusive edit tools survives; an unrecognised model drops
    # neither (both stay → existing recordings resume byte-equal).
    for _drop in select_provider_edit_tool(spec.model):
        full_pack.pop(_drop, None)
    asm.tools = {
        name: tool
        for name, tool in full_pack.items()
        if name in spec.allowed_tools
    }


def _stage_memory(spec: _BuildSpec, asm: _ToolAssembly) -> None:
    """memory pack (flag-gated, NOT whitelist-filtered).

    Memory v1 (flag-gated like the script tool, NOT
    filtered by ``allowed_tools``): the memory pack appends at this fixed
    point so the construction-order contract reads
      fs → local → memory → script → mcp → custom
    identically live and resume. ``memory_write`` is present even for an
    empty store (you could never write the first memory otherwise); the
    index snapshot is taken ONCE here — the composer's renderer and the
    pre-loop ``record_memory_index`` share it, so the recorded fingerprint
    always equals what the model saw.
    """
    if not spec.memory_enabled:
        return
    # Memory root is a FIXED global dir, not workspace-
    # derived. Precedence: explicit ``memory_dir`` override >
    # ``global_memory_dir`` (agent-configured) > the SDK default
    # ``~/.noeta/memories``. The same root resolves live + resume.
    memory_root = (
        spec.memory_dir
        if spec.memory_dir is not None
        else (
            spec.global_memory_dir
            if spec.global_memory_dir is not None
            else DEFAULT_GLOBAL_MEMORY_DIR
        )
    )
    asm.memory_store = load_memory_store(root=memory_root)
    asm.memory_entries = asm.memory_store.entries()
    for _name, _tool in build_memory_tools(asm.memory_store).items():
        asm.tools[_name] = _tool


def _stage_instructions(spec: _BuildSpec, asm: _ToolAssembly) -> None:
    """instructions snapshot load (no tools — feeds the content kind only).

    Instructions file v1 (flag-gated): read once here at build time
    so the composer's renderer AND the pre-loop ``record_instructions``
    share the same snapshot. Append the instructions kind AFTER
    memory in the registry (contract: skill, memory, instructions) so
    the semi_stable layout keeps existing byte-positioning.
    """
    if not spec.instructions_enabled:
        return
    asm.instructions_snapshot = load_instructions(
        spec.workspace_dir, override_path=spec.instructions_file
    )


def _stage_environment(spec: _BuildSpec, asm: _ToolAssembly) -> None:
    """Workspace environment snapshot load (no tools — feeds the content kind).

    Always on (a workspace always exists): capture the session-static
    workspace facts once here at build time so the composer's renderer AND
    the pre-loop ``record_environment`` share the same snapshot. Append
    the environment kind LAST in the registry (contract: skill, memory,
    instructions, environment) so the existing semi_stable byte layout is
    unchanged for sessions that never activate it.
    """
    asm.environment_snapshot = load_environment(spec.workspace_dir)


def _stage_skills_registry(spec: _BuildSpec, asm: _ToolAssembly) -> None:
    """Three-tier skill registry load (feeds script / read-fence / menu / kind).

    Three-tier skill merge — built-in + global tiers below
    the workspace-local pack (built-in < global < workspace, workspace
    wins). The lower tiers default empty (SDK / test path), so existing
    single-dir recordings stay byte-identical.
    """
    lower_skill_dirs: list[Path] = list(spec.builtin_skills_dirs)
    if spec.global_skills_dir is not None:
        lower_skill_dirs.append(spec.global_skills_dir)
    asm.registry = load_workspace_skills(
        spec.workspace_dir,
        override_skills_dir=spec.skills_dir,
        lower_skill_dirs=lower_skill_dirs,
    )


def _stage_skill_scripts(spec: _BuildSpec, asm: _ToolAssembly) -> None:
    """run_skill_script tool + the PermissionGuard fields (flag-gated).

    Issue E: same single-source wiring as CodeSessionRunner.prepare —
    append run_skill_script (after the agent filter) + the guard fields,
    so a script/approval recording resumes byte-equal. Default off.
    """
    script_tool, skill_script_tools, skill_scripts = build_skill_script_wiring(
        asm.registry, asm.workspace, enabled=spec.allow_skill_scripts
    )
    asm.skill_script_tools = skill_script_tools
    asm.skill_scripts = skill_scripts
    if script_tool is not None:
        asm.tools[script_tool.name] = script_tool


def _stage_read_fence(spec: _BuildSpec, asm: _ToolAssembly) -> None:
    """Widen the ``read`` tool's containment fence to the skill roots.

    Skill resources are read with the ordinary ``read`` tool
    (the renderer hands the model each skill's absolute base directory).
    Widen ``read``'s containment seam to the skill roots so it can reach
    the global / built-in tiers outside the workspace. ``skill_roots`` is
    internal config (not in the schema), so the tool set / stable hash is
    unchanged and live + resume stay byte-equal; the same registry rebuilt
    on resume yields the same roots. No-op when ``read`` is filtered out
    (an agent whitelist without it) or no skill has a resolvable root.
    """
    read_tool = asm.tools.get("read")
    if read_tool is not None:
        skill_roots = resolve_skill_roots(asm.registry)
        if skill_roots:
            read_tool.skill_roots = skill_roots


def _stage_mcp(spec: _BuildSpec, asm: _ToolAssembly) -> None:
    """MCP tools — live override (real spawned servers) only.

    The live path (CodeSessionRunner.prepare) spawns real stdio MCP
    servers and passes the discovered tools as ``mcp_tools_override``;
    they are merged here after fs/script. When the override is ``None``
    (e.g. a delegated child that passes no MCP servers) there are no MCP
    entries in the tool set.
    """
    if spec.mcp_tools_override is not None:
        # Live path (CodeSessionRunner.prepare): the caller spawned real
        # stdio MCP servers and passes the discovered tools. Enforce the
        # reserved-prefix invariant the inline copy used to check, then
        # merge at this position.
        for existing in asm.tools:
            if existing.startswith(MCP_PREFIX):
                raise McpConfigError(
                    f"built-in tool {existing!r} occupies the reserved "
                    f"{MCP_PREFIX!r} namespace"
                )
        for name, tool in spec.mcp_tools_override.items():
            asm.tools[name] = tool


def _stage_custom(spec: _BuildSpec, asm: _ToolAssembly) -> None:
    """Custom (user-supplied) tools — merged LAST so they shadow built-ins.

    Custom (user-supplied) tools are merged LAST so
    a custom tool can intentionally shadow a built-in / MCP tool of the
    same name. Construction order is a CONTRACT:
      fs pack → skill scripts → MCP → custom
    so tools dict insertion order (and thus the Engine's deterministic
    ToolSchemaRecorded emission) is stable. ``None`` default means this
    entire block is skipped on existing call sites (zero byte-change).
    """
    if spec.custom_tools is not None:
        for name, tool in spec.custom_tools.items():
            asm.tools[name] = tool


def _stage_app(spec: _BuildSpec, asm: _ToolAssembly) -> None:
    """open_app pack — gateway-injected, merged after custom_tools.

    The app-preview pack (``open_app``), gateway-injected, merged
    after custom_tools so the host's open_app is authoritative. Gated on a
    live gateway — ``None`` (resume + every SDK/test fixture) skips this
    block, keeping the tool set + stable hash byte-identical (a resumed turn
    that wires no gateway rebuilds the identical tool schemas). The tool needs
    the workspace built above + the host gateway.
    """
    if spec.app_gateway is not None:
        for name, tool in build_app_tools(asm.workspace, spec.app_gateway).items():
            asm.tools[name] = tool


#: The explicit tool-assembly pipeline. Iterating this list top-to-bottom IS
#: the construction-order contract (fs → memory → instructions-snapshot →
#: skills-registry → script → read-fence → mcp → custom → app). The order is
#: load-bearing for byte-equality (tool dict insertion order feeds the Engine's
#: deterministic ToolSchemaRecorded emission); do not reorder.
_TOOL_PIPELINE: tuple[Callable[[_BuildSpec, _ToolAssembly], None], ...] = (
    _stage_fs_pack,
    _stage_memory,
    _stage_instructions,
    _stage_environment,
    _stage_skills_registry,
    _stage_skill_scripts,
    _stage_read_fence,
    _stage_mcp,
    _stage_custom,
    _stage_app,
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
    # source of truth so callers never pass a divergent menu.
    if spec.skill_invocation_enabled:
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
    when ``skill_invocation_enabled`` AND the registry has indexed skills, so
    the policy's ``skill_menu_names`` and the composer's skill schema agree.
    """
    if spec.skill_invocation_enabled:
        skill_names = asm.registry.names()
        if skill_names:
            return frozenset(skill_names)
    return frozenset()


def _build_content_registry(
    spec: _BuildSpec, asm: _ToolAssembly
) -> ContentChannelRegistry:
    """The content-channel registry — registration order IS semi_stable layout.

    The content-channel registry is built HERE —
    registration order IS the semi_stable layout (skill first; further
    kinds, e.g. memory, append behind it). The same registry feeds the
    composer's render rules AND the engine's generic content_hashes seam
    so the rendered content and the recorded fingerprint come from one source.
    """
    content_kinds: list[ContentKindSpec] = [skill_content_kind(asm.registry)]
    if spec.memory_enabled:
        # The second resident: renders the index snapshot
        # into semi_stable when activated; policy "evolving".
        content_kinds.append(memory_content_kind(asm.memory_entries))
    # Instructions (third resident): append AFTER memory so existing
    # semi_stable byte layout is unchanged for memory-only sessions.
    # ``instructions_snapshot is None`` → no kind registered (zero
    # footprint — same as never adding the feature).
    if asm.instructions_snapshot is not None:
        content_kinds.append(
            instructions_content_kind(asm.instructions_snapshot)
        )
    # Environment (fourth resident): append LAST so the semi_stable byte
    # layout is unchanged for sessions that never activate it. Always
    # registered (a workspace always exists); the renderer is a zero-
    # footprint no-op until the pre-loop ``record_environment`` activates
    # it, so existing recordings without an environment event resume
    # byte-equal.
    if asm.environment_snapshot is not None:
        content_kinds.append(
            environment_content_kind(asm.environment_snapshot)
        )
    # SDK ``Options.content_channels`` extension point (T3): user-registered
    # ContentKindSpec channels append LAST, after every built-in resident, so
    # existing sessions (no extra channels) keep their semi_stable byte layout
    # byte-identical. This is the ONLY composer extension seam — the composer
    # itself is not replaceable (stable-prefix cache hard constraint).
    content_kinds.extend(spec.extra_content_kinds)
    return ContentChannelRegistry(content_kinds)


def _build_guards(spec: _BuildSpec, asm: _ToolAssembly) -> HookManager:
    """The guard HookManager in the live session's registration order.

    Issue A: rebuild the exact guard shape the live session ran so a resumed
    Engine reproduces guard-origin events (the approval suspend +
    ``ToolCallApprovalRequested``, or a guard deny) consistently.
    Registration order mirrors the live runner. The budget and compaction
    defaults are supplied by the caller (product layer) so this helper
    stays noeta.agent-agnostic.
    """
    tools = asm.tools
    hooks = HookManager()
    hooks.register(BudgetGuard(budget=spec.budget))
    hooks.register(
        PermissionGuard(
            policy=PermissionPolicy(
                allowed_tools=frozenset(tools),
                require_approval_tools=frozenset(
                    n for n in spec.require_approval_tools if n in tools
                ),
                conditional_approval=spec.shell_approval_predicate,
                # Issue B: same raw map extraction + sdk resolve as the live
                # session so an enforcement recording reproduces byte-equal.
                skill_tool_enforcement=spec.skill_tool_enforcement,
                skill_allowed_tools=resolve_skill_allowed_tools(
                    extract_skill_allowed_tools_raw(asm.registry)
                ),
                # Issue C: authorize delegation targets (named sub-agents),
                # NOT via the normal tool allow-list. The caller has already
                # roster-filtered this set through the same single-source
                # helper the live runner uses, so an unknown `--delegate-to`
                # produces the identical (empty) allow-list — live deny ==
                # resume deny, no SubtaskDenied-vs-SubtaskSpawned divergence.
                allowed_subtask_agents=(
                    spec.allowed_subtask_agents
                    if spec.delegation_enabled
                    else None
                ),
                # Issue E: same guard fields the live session wired.
                skill_script_tools=asm.skill_script_tools,
                skill_scripts=asm.skill_scripts,
            ),
            tools=tools,
        )
    )
    # Work item ④: same anti-loop RepetitionGuard the live session wired
    # (same threshold/action/window, registered after Permission), so a
    # recording whose guard tripped reproduces its require_approval suspend /
    # deny byte-equal. Default threshold 0 ⇒ not registered (matches live).
    if spec.repetition_threshold > 0:
        hooks.register(
            RepetitionGuard(
                RepetitionPolicy(
                    threshold=spec.repetition_threshold,
                    action=spec.repetition_action,
                    window=spec.repetition_window,
                )
            )
        )
    # F3: rebuild the deterministic PreToolUse HookGuard from the same
    # rules the live session used (same priority-after-built-ins), so a
    # recording with hook-origin deny/approval events reproduces
    # byte-equal. The operator must pass the same --hooks-file at resume;
    # omitting it leaves the guard unbuilt and the recording diverges
    # (we never recover hooks config from the recording). The HookObserver
    # is intentionally NOT rebuilt (live-only side-effect).
    if spec.hooks_pre_tool_use:
        hooks.register(HookGuard(spec.hooks_pre_tool_use))
    # SDK ``Options.guards`` extension point (T3): user-supplied Guards
    # register AFTER the built-in stack, in the order given (the caller owns
    # ordering via each Guard's own ``priority``). Empty ⇒ byte-identical to
    # the built-in-only path.
    for guard in spec.extra_guards:
        hooks.register(guard)
    return hooks


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
        mcp_tools_override=mcp_tools_override,
        custom_tools=custom_tools,
        app_gateway=app_gateway,
        exec_env=exec_env,
        hooks_pre_tool_use=hooks_pre_tool_use,
        extra_guards=extra_guards,
        extra_content_kinds=extra_content_kinds,
        repetition_threshold=repetition_threshold,
        repetition_action=repetition_action,
        repetition_window=repetition_window,
        subtask_agent_directory=subtask_agent_directory,
        output_schema=output_schema,
        thinking=thinking,
        effort=effort,
        tool_output_inline_limit=tool_output_inline_limit,
    )

    # Explicit tool pipeline: each stage self-gates and appends into
    # ``asm.tools`` in the construction-order contract (fs → memory →
    # instructions-snapshot → skills-registry → script → read-fence → mcp →
    # custom → app). The read-fence side effect lives in its own stage.
    asm = _ToolAssembly()
    for stage in _TOOL_PIPELINE:
        stage(spec, asm)

    tools = asm.tools
    control_action_schemas = _build_control_action_schemas(spec, asm)
    skill_menu_names = _skill_menu_names(spec, asm)
    content_registry = _build_content_registry(spec, asm)
    composer = build_skill_composer(
        system_prompt=system_prompt,
        tools=tools,
        content_store=content_store,
        skill_registry=asm.registry,
        content_renderers=content_registry,
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

    def _default_react_factory(llm: Any) -> Policy:
        return ReActPolicy(
            llm=llm,
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
            # Wiring-only LLM request overrides, carried into every
            # LLMRequest the policy builds; omitted from canonical bytes
            # when unset so legacy recordings resume byte-equal.
            output_schema=output_schema,
            thinking=thinking,
            effort=effort,
        )

    # SDK ``Options.policy`` extension point (T3): a custom decision policy
    # factory fully replaces the default ReActPolicy. ``None`` ⇒ the built-in
    # ReAct path, byte-identical to before.
    policy_factory: Callable[[Any], Policy] = (
        policy_factory_override
        if policy_factory_override is not None
        else _default_react_factory
    )

    hooks = _build_guards(spec, asm)

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
    )
