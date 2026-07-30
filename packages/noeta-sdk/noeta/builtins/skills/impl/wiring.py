"""Skill registry loading + session wiring — the ``skills`` built-in's body.

Moved out of ``noeta.execution.skills`` (microkernel phase 2a): everything
that CONSTRUCTS or WALKS registries / indexers / the script tool lives here;
the kernel keeps only the typed seams (``SkillsKit``, the recording path
``activate_skills``, the hash helpers) in ``noeta.execution.skills``.
:func:`build_skills_kit` is the injection the kernel builder requires (its
``skills_factory`` param), resolved by the SDK through
``noeta.client.parts.default_skills_kit_factory()``.

The function bodies are unchanged moves — the rendered skill bytes, the
tier-merge precedence (built-in < global < workspace), and the script-wiring
triple are byte-identical to the pre-move kernel implementations.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional, Sequence

from noeta.context.composer import ThreeSegmentComposer
from noeta.context.reminders import ReminderRegistry
from noeta.context.content_channel import (
    ContentChannelRegistry,
    ContentKindSpec,
)
from noeta.execution.skills import (
    SKILL_DRIFT_POLICY,
    SKILL_KIND,
    SkillsKit,
    build_skill_hashes,
)
from noeta.protocols.content_store import ContentStore
from noeta.protocols.tool import Tool
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

    Built purely from the public ``names()`` / ``get()`` API so the
    internal storage stays opaque. ``base`` seeds the map, then ``overlay``
    entries overwrite any shared names. The result is a fresh
    ``SkillRegistry`` (neither input is mutated).
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
    symlinks) so a ``read`` of ``<base>/<relpath>`` (whose realpath the tool
    computes) lands inside it. Sandbox mode (``exec_env`` set) keeps the
    **container** path verbatim: the source_path is already a container path and
    a host ``.resolve()`` would (wrongly) resolve it against the host filesystem
    — the container is the isolation boundary and the ``read`` tool operates on
    lexical container paths. ``None`` on a synthetic (``source_path``-less)
    skill or an unresolvable host path."""
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
    """Resolve the runnable bundled scripts across a registry (Issue E).

    Returns sorted ``(skill, relpath, root_path)`` tuples for every
    I5-discovered resource whose **suffix has an allowlisted interpreter**
    and whose skill has a resolvable root. ``root_path`` is the skill
    root's **absolute realpath** (host) or its **container path**
    (``exec_env`` set, sandbox mode). A ``source_path``-less (synthetic) skill
    or one whose root cannot be resolved contributes nothing.
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


# NOTE: there was a ``resolve_skill_roots`` here, returning every skill's root
# so the ``read`` tool's containment fence could be widened to reach a skill's
# bundled resources. ``read`` is now unfenced outright (an absolute path is
# read where it points), the builder's read-fence stage is gone, and the
# renderer writes its ``Base directory for this skill:`` line from the
# description's own ``source_path.parent`` — so the function had no caller left
# and was deleted rather than kept as a plausible-looking seam.


def build_skill_script_wiring(
    registry: SkillRegistry,
    workspace: WorkspaceRoot,
    *,
    enabled: bool,
    exec_env: Optional[ExecEnv] = None,
) -> tuple[Optional[Tool], frozenset[str], frozenset[tuple[str, str]]]:
    """Single source for Issue E wiring — every construction path builds the
    ``run_skill_script`` tool + the guard's ``skill_script_tools`` /
    ``skill_scripts`` through here (so a resumed turn rebuilds the same guard
    shape).

    ``enabled=False`` (default everywhere, incl. every sub-agent child)
    returns ``(None, frozenset(), frozenset())`` — the tool is never
    constructed, so the tools dict / schema / stable hash are unchanged.

    ``exec_env`` (sandbox mode) resolves the script roots as container paths and
    is threaded into the tool so the hash check + execution run INSIDE the
    container (S7); ``None`` keeps the host path byte-identical.
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
    ``(skill_name, raw_allowed_tools_value)`` map (Issue B).

    Only skills that **declare** an ``allowed-tools`` key contribute one
    entry (the verbatim opaque metadata string). Parsing + Claude→Noeta
    aliasing is :func:`resolve_skill_allowed_tools`'s job, applied at the
    ``PermissionPolicy`` build site; the kernel guard receives the
    already-resolved neutral grants. Sorted by skill name for deterministic
    ordering. This extraction lives beside the indexer on purpose: it reads
    the registry here so the permission guard
    (``noeta.builtins.governance.impl``) never has to — a guard keeps a
    ``noeta.protocols``-only diet.
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
#: The runner / CLI override is ``--skills-dir`` (wired in I4).
DEFAULT_SKILLS_SUBDIR = ".noeta/skills"


def _snapshot_skill_tiers(
    exec_env: Optional[ExecEnv], tiers: Sequence[Path]
) -> Optional[Any]:
    """One-round-trip snapshot of every skill tier, for sandbox indexing.

    Issue 46: indexing skills through the container per-file costs one HTTP
    round-trip per ``is_file`` / ``rglob`` / ``read_text`` — minutes of
    ``seed_start`` wall time at a few dozen skills. ``ExecEnv.tree_snapshot``
    folds the whole walk (every file under every tier + every SKILL.md's
    bytes) into ONE container exec; the snapshot is handed to each per-tier
    ``SkillIndexer``, which scopes it to its own root lexically.

    Returns ``None`` — meaning "index per-file as before" — for a local
    session (no ``exec_env``), an ExecEnv that does not implement
    ``tree_snapshot`` (duck-typed fakes / older custom backends), or a
    snapshot that fails outright (logged; correctness over speed).
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
) -> SkillRegistry:
    """Build a ``SkillRegistry`` by merging the skill tiers.

    The workspace-local pack is resolved as before: ``override_skills_dir``
    (the CLI ``--skills-dir`` value) wins when provided; otherwise
    ``<workspace>/.noeta/skills`` is indexed.

    ``lower_skill_dirs`` are the **lower-precedence** tiers below the
    workspace-local pack, ordered low→high (the agent layer passes the
    built-in pack first, then the global ``~/.noeta/skills`` pack). Each
    dir is indexed independently with the unchanged single-dir
    ``SkillIndexer`` and folded together with :func:`merge_skill_registries`
    (overlay wins on name clash), so the final precedence is
    **built-in < global < workspace** — the workspace-local skill always
    shadows a same-named global / built-in one. An empty
    ``lower_skill_dirs`` (the default, and the SDK / test path with no
    extra tiers) keeps the old single-dir behaviour byte-for-byte.

    Missing directories produce an **empty** Registry rather than an
    error — a workspace with no skills is still a valid coding session,
    and a fresh empty workspace still sees the global / built-in tiers.

    The indexer is the unchanged Phase-1 ``SkillIndexer`` shape
    (deterministic recursive walk with symlink-dir support +
    POSIX-relative-path sort + duplicate-name first-wins +
    skip-malformed-frontmatter).
    """
    skills_dir = (
        override_skills_dir
        if override_skills_dir is not None
        else workspace / DEFAULT_SKILLS_SUBDIR
    )
    # Fold the lower tiers low→high (built-in < global), then let the
    # workspace-local pack win as the top overlay. In sandbox mode each tier's
    # SKILL.md is indexed THROUGH the container (``exec_env``): the dirs are then
    # container mount points and the rendered base directories are container
    # paths (D6-Skills). All tiers are fetched in ONE container round-trip
    # (``prefetched``, issue 46) when the backend supports it; each indexer
    # scopes the shared snapshot to its own root.
    prefetched = _snapshot_skill_tiers(
        exec_env, [*lower_skill_dirs, skills_dir]
    )
    merged = SkillRegistry({})
    for lower in lower_skill_dirs:
        merged = merge_skill_registries(
            merged,
            SkillIndexer(lower, exec_env=exec_env, prefetched=prefetched).index(),
        )
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

    (Original name in noeta.agent: ``build_coding_composer`` — same body,
    renamed on hoist to reflect that the skill-composer glue is no longer
    coding-product specific.)

    Scope, stated plainly: the session build does NOT come through here — the
    kernel builder constructs :class:`ThreeSegmentComposer` directly, with the
    multi-kind content registry it already assembled. What remains is a
    convenience constructor for callers holding just a skill registry (the
    composer tests, and an embedder wiring skills without the kernel builder).

    The 3-segment context policy is reused as-is:

    * ``stable_prefix`` — coding-Agent role + tool schema + safety prompt.
    * ``semi_stable`` — the rendered bodies of *activated* skills.
    * ``dynamic_suffix`` — conversation / tool results.

    The renderer reads from ``task.state.active_skills`` on each
    ``compose`` call, so the activation in
    :func:`noeta.execution.skills.activate_skills` is what flips a skill on
    (PRD D10/D11).

    ③ (finding 1): ``tail_token_budget`` arms the composer's deterministic
    tail-window prune (D-3e). ``None`` (default, and for any model the catalog
    does not describe) keeps the legacy no-prune behaviour. The value is a
    deterministic function of the model
    (``derive_compaction_config``, in the providers built-in's catalog), so a resumed
    turn derives the SAME budget and composes the same prefix bytes (the
    stable-prefix prompt cache only hits when the prefix is byte-stable).
    ``available_window`` (``context_window - max_output - buffer``) arms the
    prune's relief-valve gate so it only clears once the history nears the
    usable window — below it, tool outputs stay verbatim and the model never
    re-reads content it already fetched. Also a deterministic function of the
    model, so live + resume gate identically.

    The skill renderer is wired as the
    ``kind="skill"`` item of a content-channel registry — same renderer
    function, byte-identical output; further kinds (memory, issue 05)
    extend the registry instead of the composer. A caller that already built a
    multi-kind registry passes it as ``content_renderers``; the default builds
    the single-kind registry.
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

    Skills are the channel's first resident: render rule =
    :func:`build_skill_renderer` (unchanged bytes), fingerprints =
    :func:`noeta.execution.skills.build_skill_hashes` (``(version,
    sha256(SKILL.md bytes))``), drift policy = ``pinned`` — a SKILL.md
    edit without a declared version bump changes the rendered prefix bytes,
    so the recorded fingerprint pins the content for resume. New kinds
    register their own spec next to this one; neither the composer nor the
    runtime changes.

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
) -> SkillsKit:
    """The kernel builder's ``skills_factory`` injection (microkernel 2a).

    One call assembles everything the session build needs from the skill
    subsystem — registry, script wiring, content kind, resolved allowed-tools
    grants — so the kernel's tool pipeline consumes a finished
    :class:`~noeta.execution.skills.SkillsKit` and imports no skills
    implementation. Each part is the unchanged pre-move construction, in the
    same relative order the builder ran them.
    """
    registry = load_workspace_skills(
        workspace_dir,
        override_skills_dir=override_skills_dir,
        lower_skill_dirs=lower_skill_dirs,
        exec_env=exec_env,
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
