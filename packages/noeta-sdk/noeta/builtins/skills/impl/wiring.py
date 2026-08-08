"""Skill registry loading + session wiring — the ``skills`` built-in's body.

Everything that constructs or walks registries / indexers / the script tool
lives here; the bundle types and helpers live in the sibling
:mod:`~noeta.builtins.skills.impl.kit`. :func:`build_skills_session_pack` is
this plugin's ``session_pack`` contribution, and the whole kit stays inside its
closure so no registry, menu, or kit crosses into kernel code.
"""

from __future__ import annotations

import logging
import warnings
from pathlib import Path
from typing import Any, Optional, Sequence, cast

from noeta.context.composer import ThreeSegmentComposer
from noeta.context.reminders import ReminderRegistry
from noeta.context.content_channel import (
    ContentChannelRegistry,
    ContentKindSpec,
)
from noeta.client.plugins import is_trusted
from noeta.execution.control_tool import ControlToolEntry
from noeta.execution.session_pack import (
    ContentKindContribution,
    PackContribution,
    SessionBuildContext,
)
from noeta.builtins.skills.impl.kit import (
    SKILL_DRIFT_POLICY,
    SKILL_KIND,
    SkillsKit,
    build_skill_hashes,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.tool import Tool
from noeta.runtime.governance import SkillEnforcementMode, SkillGuardFacts
from noeta.runtime.workspace import WorkspaceRoot
from noeta.runtime.exec_env import ExecEnv

from .allowed_tools import resolve_skill_allowed_tools
from .indexer import (
    SkillDescription,
    SkillIndexer,
    SkillRegistry,
    build_skill_renderer,
)
from .script import (
    SKILL_SCRIPT_TOOL_NAME,
    RunSkillScriptTool,
    is_skill_script_resource,
)


__all__ = [
    "DEFAULT_SKILLS_SUBDIR",
    "WORKSPACE_AGENTS_SKILLS_SUBDIR",
    "UntrustedWorkspaceSkillsWarning",
    "build_skill_composer",
    "build_skill_script_wiring",
    "build_skills_kit",
    "extract_skill_allowed_tools_raw",
    "load_workspace_skills",
    "merge_skill_registries",
    "resolve_skill_scripts",
    "skill_content_kind",
]


_log = logging.getLogger(__name__)


def merge_skill_registries(
    base: SkillRegistry, overlay: SkillRegistry
) -> SkillRegistry:
    """Merge two registries into a new one — ``overlay`` wins on name clash.

    Built purely from the public ``names()`` / ``get()`` API so the internal
    storage stays opaque. The result is a fresh ``SkillRegistry`` (neither input
    is mutated).
    """
    merged: dict[str, SkillDescription] = {}
    for name in base.names():
        desc = base.get(name)
        if desc is not None:
            merged[name] = desc
    for name in overlay.names():
        desc = overlay.get(name)
        if desc is not None:
            merged[name] = desc
    return SkillRegistry(merged)


def _skill_root(desc: SkillDescription, exec_env: Optional[ExecEnv]) -> Optional[Path]:
    """A skill's containment root — its ``source_path.parent``.

    Local mode canonicalises with ``.resolve()`` (host realpath, follows
    symlinks) so a ``read`` of ``<base>/<relpath>`` lands inside it. Sandbox
    mode (``exec_env`` set) keeps the **container** path verbatim: a host
    ``.resolve()`` would wrongly resolve a container path against the host
    filesystem, and the ``read`` tool operates on lexical container paths.
    ``None`` on a synthetic (``source_path``-less) skill or an unresolvable host
    path."""
    if desc.source_path is None:
        return None
    parent = desc.source_path.parent
    if exec_env is not None:
        return parent
    try:
        return parent.resolve()
    except OSError:
        return None


def resolve_skill_scripts(
    registry: SkillRegistry,
    *,
    exec_env: Optional[ExecEnv] = None,
) -> tuple[tuple[str, str, Path], ...]:
    """Resolve the runnable bundled scripts across a registry.

    Returns sorted ``(skill, relpath, root_path)`` tuples for every discovered
    resource whose **suffix has an allowlisted interpreter** and whose skill has
    a resolvable root. ``root_path`` is the skill root's absolute realpath (host)
    or its container path (``exec_env`` set, sandbox mode). A synthetic skill or
    one whose root cannot be resolved contributes nothing.
    """
    out: list[tuple[str, str, Path]] = []
    for name in registry.names():
        desc = registry.get(name)
        if desc is None:
            continue
        root = _skill_root(desc, exec_env)
        if root is None:
            continue
        for rel in desc.resources:
            if is_skill_script_resource(rel):
                out.append((name, rel, root))
    out.sort(key=lambda t: (t[0], t[1]))
    return tuple(out)


def build_skill_script_wiring(
    registry: SkillRegistry,
    workspace: WorkspaceRoot,
    *,
    enabled: bool,
    exec_env: Optional[ExecEnv] = None,
) -> tuple[Optional[Tool], frozenset[str], frozenset[tuple[str, str]]]:
    """Single source for script wiring — every construction path builds the
    ``run_skill_script`` tool + the guard's ``skill_script_tools`` /
    ``skill_scripts`` through here, so a resumed turn rebuilds the same guard
    shape.

    ``enabled=False`` (the default, including every sub-agent child) returns
    ``(None, frozenset(), frozenset())`` — the tool is never constructed, so the
    tools dict / schema / stable hash are unchanged.

    ``exec_env`` (sandbox mode) resolves the script roots as container paths and
    is threaded into the tool so the hash check + execution run INSIDE the
    container.
    """
    if not enabled:
        return None, frozenset(), frozenset()
    scripts = resolve_skill_scripts(registry, exec_env=exec_env)
    tool: Tool = RunSkillScriptTool(
        workspace=workspace, scripts=scripts, exec_env=exec_env
    )
    skill_scripts = frozenset((s, rel) for s, rel, _ in scripts)
    return tool, frozenset({SKILL_SCRIPT_TOOL_NAME}), skill_scripts


def extract_skill_allowed_tools_raw(
    registry: SkillRegistry,
) -> tuple[tuple[str, str], ...]:
    """Reduce a :class:`SkillRegistry` to the plain immutable
    ``(skill_name, raw_allowed_tools_value)`` map.

    Only skills that **declare** an ``allowed-tools`` key contribute one entry
    (the verbatim opaque metadata string). Parsing + recognising the names
    against the mountable tool vocabulary is
    :func:`resolve_skill_allowed_tools`'s job, applied at the
    ``PermissionPolicy`` build site; the kernel guard receives the
    already-resolved neutral grants. This extraction lives beside the indexer so
    the permission guard (``noeta.builtins.governance.impl``) never has to read
    the registry — a guard keeps a ``noeta.protocols``-only diet.
    """
    out: list[tuple[str, str]] = []
    for name in registry.names():
        desc = registry.get(name)
        if desc is None:
            continue
        for key, value in desc.metadata:
            if key == "allowed-tools":
                out.append((name, value))
                break
    out.sort(key=lambda item: item[0])
    return tuple(out)


#: Per-workspace default location for skill packs — ``<workspace>/.noeta/skills``.
#: The ``--skills-dir`` override supplies ``override_skills_dir`` below.
DEFAULT_SKILLS_SUBDIR = ".noeta/skills"

#: Vendor-neutral per-workspace tier — ``<workspace>/.agents/skills`` — indexed
#: just below the ``.noeta/skills`` pack: at equal scope the vendor-specific
#: directory wins, so ``.noeta/skills`` keeps sovereignty over skills borrowed
#: from the shared ecosystem convention.
WORKSPACE_AGENTS_SKILLS_SUBDIR = ".agents/skills"


class UntrustedWorkspaceSkillsWarning(UserWarning):
    """Repo-derived workspace skill tiers were skipped: workspace not trusted.

    Carries ``workspace_dir`` (the trust subject — the HOST-side path an
    operator would ``grant_trust``, never a container mount point) and
    ``skipped_dirs`` (the tiers actually dropped) as attributes, so a host can
    consume them without parsing the message. Python's default warning filter
    dedups per message+location, so an interactive host that wants a
    per-session prompt must call ``is_trusted`` itself rather than listen for
    this warning.
    """

    def __init__(
        self, workspace_dir: Path, skipped_dirs: tuple[Path, ...]
    ) -> None:
        self.workspace_dir = workspace_dir
        self.skipped_dirs = skipped_dirs
        super().__init__(
            f"workspace {workspace_dir} is not trusted; skipping its skill "
            f"tiers: {', '.join(str(d) for d in skipped_dirs)} "
            "(grant_trust to enable them)"
        )


#: The two admissible ``workspace_skills_trust`` values. Anything else raises:
#: a fail-open typo on a security knob must not read as "open".
_TRUST_MODES = ("open", "trust-store")


def _workspace_skills_trusted(
    trust_mode: str,
    store: Optional[Path],
    subject: Path,
    gated_dirs: Sequence[Path],
) -> bool:
    """Whether the repo-derived workspace skill tiers (``gated_dirs``) may load.

    ``trust_mode`` = ``"open"`` (always) or ``"trust-store"`` (only when
    ``subject`` is recorded in the plugin trust store — the same store that
    gates workspace plugin directories, so one ``grant_trust`` covers both).
    ``subject`` is the HOST-side workspace path (the ``trust_subject`` config
    key when the host supplies one): in sandbox mode the pack's own
    ``workspace_dir`` is a container path like ``/workspace``, which the
    operator never granted — and which every session shares, so keying on it
    would collapse the gate to a global on/off. An untrusted subject skips the
    tiers with a warning — never an error, mirroring the plugin loader's
    ``UntrustedPluginDirWarning``. ``store`` overrides the trust-store path
    (hermetic tests).

    Note the trust store is a file OUTSIDE the workspace: a grant or
    revocation between turns changes the tier set — and with it the skill
    menu bytes in the stable prefix — on the next session build.
    """
    if trust_mode != "trust-store":
        return True
    if is_trusted(subject, store):
        return True
    warnings.warn(
        UntrustedWorkspaceSkillsWarning(subject, tuple(gated_dirs)),
        stacklevel=2,
    )
    return False


def _snapshot_skill_tiers(
    exec_env: Optional[ExecEnv], tiers: Sequence[Path]
) -> Optional[Any]:
    """One-round-trip snapshot of every skill tier, for sandbox indexing.

    Indexing skills through the container per-file costs one HTTP round-trip per
    ``is_file`` / ``rglob`` / ``read_text`` — minutes of ``seed_start`` wall time
    at a few dozen skills. ``ExecEnv.tree_snapshot`` folds the whole walk (every
    file under every tier + every SKILL.md's bytes) into ONE container exec; the
    snapshot is handed to each per-tier ``SkillIndexer``, which scopes it to its
    own root lexically.

    Returns ``None`` — meaning "index per-file" — for a local session (no
    ``exec_env``), an ExecEnv that does not implement ``tree_snapshot``
    (duck-typed fakes / custom backends), or a snapshot that fails outright
    (logged; correctness over speed).
    """
    if exec_env is None:
        return None
    snapshot = getattr(exec_env, "tree_snapshot", None)
    if snapshot is None:
        return None
    try:
        return snapshot(tuple(tiers), content_name="SKILL.md")
    except OSError as exc:
        _log.warning(
            "skill: tier snapshot failed (%s); falling back to per-file indexing",
            exc,
        )
        return None


def load_workspace_skills(
    workspace: Path,
    *,
    override_skills_dir: Optional[Path] = None,
    lower_skill_dirs: Sequence[Path] = (),
    exec_env: Optional[ExecEnv] = None,
    workspace_pack_enabled: bool = True,
) -> SkillRegistry:
    """Build a ``SkillRegistry`` by merging the skill tiers.

    ``override_skills_dir`` (the ``--skills-dir`` value) wins for the
    workspace-local pack when provided; otherwise ``<workspace>/.noeta/skills``
    is indexed.

    ``lower_skill_dirs`` are the **lower-precedence** tiers below the
    workspace-local pack, ordered low→high (the built-in pack first — including
    the directories loaded plugins contribute on the ``skills`` surface, which
    ride that same lowest tier — then the global ``~/.noeta/skills`` pack). Each
    dir is indexed independently and folded with :func:`merge_skill_registries`
    (overlay wins on name clash), so the final precedence is **built-in <
    global < workspace** — a workspace-local skill always shadows a same-named
    global / built-in / plugin-contributed one.

    Missing directories produce an **empty** Registry rather than an error — a
    workspace with no skills is still a valid coding session, and a fresh empty
    workspace still sees the global / built-in tiers.

    ``workspace_pack_enabled=False`` skips the workspace-local top tier
    entirely (the fold stops at ``lower_skill_dirs``) — the trust gate's lever
    for an untrusted workspace with no operator ``override_skills_dir``.
    """
    skills_dir = (
        override_skills_dir
        if override_skills_dir is not None
        else workspace / DEFAULT_SKILLS_SUBDIR
    )
    # Fold the lower tiers low→high, then let the workspace-local pack win as
    # the top overlay. In sandbox mode each tier's SKILL.md is indexed THROUGH
    # the container (``exec_env``): the dirs are container mount points and the
    # rendered base directories are container paths. All tiers are fetched in
    # ONE container round-trip (``prefetched``) when the backend supports it;
    # each indexer scopes the shared snapshot to its own root.
    tiers = (
        [*lower_skill_dirs, skills_dir]
        if workspace_pack_enabled
        else list(lower_skill_dirs)
    )
    prefetched = _snapshot_skill_tiers(exec_env, tiers)
    merged = SkillRegistry({})
    for lower in lower_skill_dirs:
        merged = merge_skill_registries(
            merged,
            SkillIndexer(lower, exec_env=exec_env, prefetched=prefetched).index(),
        )
    if not workspace_pack_enabled:
        return merged
    return merge_skill_registries(
        merged,
        SkillIndexer(skills_dir, exec_env=exec_env, prefetched=prefetched).index(),
    )


def build_skill_composer(
    *,
    system_prompt: str,
    tools: dict[str, Tool],
    content_store: ContentStore,
    skill_registry: SkillRegistry,
    control_action_schemas: Optional[list[dict[str, Any]]] = None,
    tail_token_budget: Optional[int] = None,
    available_window: Optional[int] = None,
    content_renderers: Optional[ContentChannelRegistry] = None,
    reminders: Optional[ReminderRegistry] = None,
) -> ThreeSegmentComposer:
    """Wire ``ThreeSegmentComposer`` with the workspace skill renderer.

    A convenience constructor for callers holding just a skill registry (the
    composer tests, and an embedder wiring skills without the kernel builder).
    The session build does NOT come through here — the kernel builder
    constructs :class:`ThreeSegmentComposer` directly, with the multi-kind
    content registry it already assembled.

    The 3-segment context policy:

    * ``stable_prefix`` — role + tool schema + safety prompt.
    * ``semi_stable`` — the rendered bodies of *activated* skills.
    * ``dynamic_suffix`` — conversation / tool results.

    The renderer reads from ``task.state.active_skills`` on each ``compose``
    call, so the activation in
    :func:`~noeta.builtins.skills.impl.kit.activate_skills` is what flips a skill
    on.

    ``tail_token_budget`` arms the composer's deterministic tail-window prune;
    ``None`` (the default, and for any model the catalog does not describe)
    keeps the no-prune behaviour. The value is a deterministic function of the
    model (``derive_compaction_config``, in the providers built-in's catalog), so
    a resumed turn derives the SAME budget and composes the same prefix bytes —
    the stable-prefix prompt cache only hits when the prefix is byte-stable.
    ``available_window`` (``context_window - max_output - buffer``) arms the
    prune's relief-valve gate so it only clears once the history nears the usable
    window; below it, tool outputs stay verbatim and the model never re-reads
    content it already fetched. Also a deterministic function of the model, so
    live + resume gate identically.

    The skill renderer is wired as the ``kind="skill"`` item of a
    content-channel registry; further kinds extend the registry instead of the
    composer. A caller that already built a multi-kind registry passes it as
    ``content_renderers``; the default builds the single-kind registry.
    """
    return ThreeSegmentComposer(
        system_prompt=system_prompt,
        tools=tools,
        content_store=content_store,
        content_renderers=(
            content_renderers
            if content_renderers is not None
            else ContentChannelRegistry([skill_content_kind(skill_registry)])
        ),
        reminders=reminders,
        control_action_schemas=control_action_schemas,
        tail_token_budget=tail_token_budget,
        available_window=available_window,
    )


def skill_content_kind(
    skill_registry: SkillRegistry,
    *,
    exec_env: Optional[ExecEnv] = None,
) -> ContentKindSpec:
    """The skill kind's content-channel registry item.

    Render rule = :func:`build_skill_renderer`, fingerprints =
    :func:`~noeta.builtins.skills.impl.kit.build_skill_hashes` (``(version,
    sha256(SKILL.md bytes))``), drift policy = ``pinned`` — a SKILL.md edit
    without a declared version bump changes the rendered prefix bytes, so the
    recorded fingerprint pins the content for resume. New kinds register their
    own spec next to this one; neither the composer nor the runtime changes.

    ``exec_env`` (sandbox mode) makes the fingerprints hash the SKILL.md bytes
    read THROUGH the container, matching where the model actually reads them.
    """
    return ContentKindSpec(
        kind=SKILL_KIND,
        renderer=build_skill_renderer(skill_registry),
        hashes=build_skill_hashes(skill_registry, exec_env=exec_env),
        policy=SKILL_DRIFT_POLICY,
    )


def build_skills_kit(
    *,
    workspace_dir: Path,
    override_skills_dir: Optional[Path],
    lower_skill_dirs: Sequence[Path],
    workspace: WorkspaceRoot,
    scripts_enabled: bool,
    exec_env: Optional[ExecEnv],
    workspace_pack_enabled: bool = True,
) -> SkillsKit:
    """Assemble everything the session build needs from the skill subsystem.

    One call produces the registry, script wiring, content kind, and resolved
    allowed-tools grants, so the kernel's tool pipeline consumes a finished
    :class:`~noeta.builtins.skills.impl.kit.SkillsKit` and imports no skills
    implementation.
    """
    registry = load_workspace_skills(
        workspace_dir,
        override_skills_dir=override_skills_dir,
        lower_skill_dirs=lower_skill_dirs,
        exec_env=exec_env,
        workspace_pack_enabled=workspace_pack_enabled,
    )
    script_tool, skill_script_tools, skill_scripts = build_skill_script_wiring(
        registry,
        workspace,
        enabled=scripts_enabled,
        exec_env=exec_env,
    )
    return SkillsKit(
        registry=registry,
        script_tool=script_tool,
        skill_script_tools=skill_script_tools,
        skill_scripts=skill_scripts,
        content_kind=skill_content_kind(registry, exec_env=exec_env),
        allowed_tools=resolve_skill_allowed_tools(
            extract_skill_allowed_tools_raw(registry)
        ),
    )


def build_skills_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The skills kit as a ``session_pack`` contribution.

    The manifest-declared factory. Reads this plugin's own config entry —
    ``skills_dir`` (workspace override; when set it PINS the workspace-scoped
    set: the ``.agents/skills`` tier does not mount beneath it),
    ``builtin_skills_dirs`` + ``extra_skill_dirs`` + ``global_agents_skills_dir``
    + ``global_skills_dir`` (the lower tiers, low→high),
    ``workspace_agents_tier`` (mounts ``<workspace>/.agents/skills`` just below
    the workspace pack — a host-set switch on the full session environment; the
    reduced orchestration shape omits it, though operator-supplied override
    keys such as ``extra_skill_dirs`` reach both shapes by the override
    channel's own contract: naming a key is an explicit act),
    ``workspace_skills_trust`` + ``trust_store`` + ``trust_subject`` (the
    workspace-tier trust gate; the subject is the HOST-side workspace path —
    see :func:`_workspace_skills_trusted`), ``allow_skill_scripts``,
    ``tool_enforcement`` — and assembles the tiered kit.
    ``builtin_skills_dirs`` opens the lowest tier as the host assembled it:
    its own built-in packs plus every directory a loaded plugin contributed on
    the ``skills`` surface; ``extra_skill_dirs`` (operator-named foreign
    directories, e.g. ``~/.claude/skills``) rides that same lowest tier after
    them, so anything local shadows a borrowed skill. A directory that does not
    exist indexes to an empty registry, so a plugin whose pack is absent in
    some install contributes nothing rather than failing the build. The kit stays
    INSIDE this factory (no kit crosses into kernel code): the ``skill`` control
    tool rides ``control_tools`` as a closure over the merged registry, the
    guard inputs ride the opaque ``guard_facts`` bundle, and the
    ``run_skill_script`` tool, when scripts are on and a skill ships one, is the
    pack's only tool.

    Disabling the built-in (``disabled_builtins=["skills"]``) removes this pack
    from the session entirely.
    """
    cfg = ctx.config("skills")
    trust_mode = cast(str, cfg.get("workspace_skills_trust", "open"))
    if trust_mode not in _TRUST_MODES:
        raise ValueError(
            f"skills config: unknown workspace_skills_trust value "
            f"{trust_mode!r}; expected one of {_TRUST_MODES}"
        )
    override_dir = cast(Optional[Path], cfg.get("skills_dir"))
    lower_skill_dirs: list[Path] = list(
        cast("Sequence[Path]", cfg.get("builtin_skills_dirs", ()))
    )
    lower_skill_dirs.extend(
        cast("Sequence[Path]", cfg.get("extra_skill_dirs", ()))
    )
    agents_global_dir = cast(
        Optional[Path], cfg.get("global_agents_skills_dir")
    )
    if agents_global_dir is not None:
        lower_skill_dirs.append(agents_global_dir)
    global_dir = cast(Optional[Path], cfg.get("global_skills_dir"))
    if global_dir is not None:
        lower_skill_dirs.append(global_dir)
    # An operator ``skills_dir`` override PINS the workspace-scoped skill set:
    # no repo-derived tier mounts beneath it, so a host that names its own
    # directory keeps exact control over what a session sees.
    agents_tier_active = override_dir is None and bool(
        cfg.get("workspace_agents_tier", False)
    )
    # The trust gate covers exactly the repo-derived tiers in play; when
    # nothing repo-derived would mount (override set, `.agents` switch off)
    # there is no stake and no warning.
    gated_dirs: list[Path] = []
    if agents_tier_active:
        gated_dirs.append(ctx.workspace_dir / WORKSPACE_AGENTS_SKILLS_SUBDIR)
    if override_dir is None:
        gated_dirs.append(ctx.workspace_dir / DEFAULT_SKILLS_SUBDIR)
    trusted = True
    if gated_dirs:
        trusted = _workspace_skills_trusted(
            trust_mode,
            cast(Optional[Path], cfg.get("trust_store")),
            cast(Optional[Path], cfg.get("trust_subject"))
            or ctx.workspace_dir,
            gated_dirs,
        )
    if trusted and agents_tier_active:
        lower_skill_dirs.append(
            ctx.workspace_dir / WORKSPACE_AGENTS_SKILLS_SUBDIR
        )
    kit = build_skills_kit(
        workspace_dir=ctx.workspace_dir,
        override_skills_dir=override_dir,
        lower_skill_dirs=lower_skill_dirs,
        workspace=ctx.workspace,
        scripts_enabled=bool(cfg.get("allow_skill_scripts", False)),
        exec_env=ctx.exec_env,
        workspace_pack_enabled=trusted or override_dir is not None,
    )
    tools: dict[str, Tool] = {}
    if kit.script_tool is not None:
        tools[kit.script_tool.name] = kit.script_tool
    # Late import: control_tool.py imports nothing from wiring; keeping the
    # dependency one-way at module load mirrors the layering.
    from .control_tool import make_skills_control_tool

    # The skill resident leads the semi_stable layout (kind band 100); the
    # ``skill`` control tool rides this contribution as a closure over the
    # merged registry (band 400), and the guard facts travel as one opaque
    # bundle the governance factory unpacks.
    return PackContribution(
        tools=tools,
        content_kinds=(ContentKindContribution(100, kit.content_kind),),
        control_tools=(
            ControlToolEntry(
                "skill", 400, make_skills_control_tool(kit.registry)
            ),
        ),
        guard_facts=SkillGuardFacts(
            tool_enforcement=cast(
                SkillEnforcementMode, cfg.get("tool_enforcement", "off")
            ),
            allowed_tools=kit.allowed_tools,
            script_tools=kit.skill_script_tools,
            scripts=kit.skill_scripts,
        ),
    )
