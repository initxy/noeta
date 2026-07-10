"""Skill wiring glue — hoisted from noeta.agent.

Machine lives in the SDK (execution layer); product content (builtin skill
root, :func:`load_builtin_skills`) stays in ``noeta.execution.skills`` as a
thin re-export shim. The two product-bound members are deliberately **not**
in this module so the SDK remains code-agnostic.

Three thin glue helpers over the Phase-1 Skill subsystem
(``noeta.context.skills``) and the existing ``ThreeSegmentComposer``:

* :func:`load_workspace_skills` — index ``<workspace>/.noeta/skills`` (or
  an explicit override) via the existing ``SkillIndexer`` and return a
  ``SkillRegistry``. No new skill engine.
* :func:`build_skill_composer` — wire the composer with the registry's
  ``skill_renderer`` so activated skill bodies materialise in the
  ``semi_stable`` segment and ``ContextPlan.selected_skills`` records
  what landed.  Original name in noeta.agent was ``build_coding_composer``.
* :func:`activate_skills` — runner-driven, **pre-loop**, **durable**
  activation (B11 + B17). Emits a real ``TaskStatePatched`` event
  through ``Engine.apply_state_patch`` so resume folds the same active
  set. Activation is intentionally **not** model-driven —
  ``ReActPolicy`` does not parse ``activate_skills`` from LLM text in
  Phase 4 (PRD D10), so v1 activates skills here, deterministically.

Per PRD B12, Phase 4 records **only** ``ContextPlan.selected_skills``;
file/search provenance is deferred to Phase 6. No ``ContextPlan`` L0
schema change in this issue.

Added content-hash provenance here; the
issue-07 generation switch upgraded it to the generic shape:
:func:`activate_skills` emits one ``ContextContentRecorded`` event
(kind="skill", policy="pinned") per newly-activated skill (per-task,
first-only) with ``sha256(SKILL.md bytes)``, right before the
``TaskStatePatched(activate_skills=…)`` so the active skill set and its
content fingerprint are part of the durable record (resume re-derives
both from the stream).
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any, Optional, Sequence

from noeta.context.composer import ThreeSegmentComposer
from noeta.context.content_channel import (
    ContentChannelRegistry,
    ContentKindSpec,
)
from noeta.context.skills import (
    SkillDescription,
    SkillIndexer,
    SkillRegistry,
    build_skill_renderer,
)
from noeta.core.engine import Engine, SkillHashesFn, emit_context_content_recorded
from noeta.protocols.content_store import ContentStore
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.task import Task
from noeta.protocols.tool import Tool
from noeta.tools.fs import WorkspaceRoot
from noeta.tools.fs.exec_env import ExecEnv
from noeta.tools.fs.skill_script import (
    SKILL_SCRIPT_TOOL_NAME,
    RunSkillScriptTool,
    is_skill_script_resource,
)


__all__ = [
    "DEFAULT_SKILLS_SUBDIR",
    "SKILL_DRIFT_POLICY",
    "SKILL_KIND",
    "activate_skills",
    "build_skill_composer",
    "build_skill_hashes",
    "build_skill_script_wiring",
    "extract_skill_allowed_tools_raw",
    "load_workspace_skills",
    "merge_skill_registries",
    "resolve_skill_roots",
    "resolve_skill_scripts",
    "skill_content_hash",
    "skill_content_kind",
]


_log = logging.getLogger(__name__)


#: The skill kind's content-channel key and drift policy
#: (``pinned``: a SKILL.md edit without a declared version bump changes the
#: rendered prefix bytes, so the recorded fingerprint pins the content for
#: resume). Single source for the kind spec, the pre-loop activation
#: emission, and host wiring.
SKILL_KIND = "skill"
SKILL_DRIFT_POLICY = "pinned"


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


def resolve_skill_roots(
    registry: SkillRegistry,
    *,
    exec_env: Optional[ExecEnv] = None,
) -> tuple[Path, ...]:
    """The roots of every skill with a resolvable path (host realpath or,
    in sandbox mode, the container path).

    These widen the ``read`` tool's containment seam so it can reach a
    skill's bundled resources outside the workspace — the global
    (``~/.noeta/skills``) / built-in tiers ``WorkspaceRoot`` would
    otherwise reject. The renderer hands the model each skill's
    ``source_path.parent`` as the ``Base directory for this skill:`` line;
    this returns the matching roots so a ``read`` of ``<base>/<relpath>``
    lands inside one — host-canonicalised locally, the verbatim container path
    under a sandbox (``exec_env`` set). Sorted + de-duplicated for determinism.
    """
    roots: set[Path] = set()
    for name in registry.names():
        desc = registry.get(name)
        if desc is None:
            continue
        root = _skill_root(desc, exec_env)
        if root is not None:
            roots.add(root)
    return tuple(sorted(roots))


def build_skill_script_wiring(
    registry: SkillRegistry,
    workspace: WorkspaceRoot,
    *,
    enabled: bool,
    exec_env: Optional[ExecEnv] = None,
) -> tuple[Optional[Tool], frozenset[str], frozenset[tuple[str, str]]]:
    """Single source for Issue E wiring — used by BOTH
    :meth:`CodeSessionRunner.prepare` and
    :func:`noeta.execution.builder.build_session_inputs` so the
    ``run_skill_script`` tool + the guard's ``skill_script_tools`` /
    ``skill_scripts`` are identical across every construction path (so a
    resumed turn rebuilds the same guard shape).

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
    aliasing is noeta-sdk's job
    (``noeta.policies.skill_tools.resolve_skill_allowed_tools``), applied at
    the ``PermissionPolicy`` build site; the kernel guard receives the
    already-resolved neutral grants. Sorted by skill name for deterministic
    ordering. This extraction lives in the execution layer on purpose: it
    reads ``noeta.context.skills`` here so ``noeta.guards`` never has to (it
    may only import ``noeta.protocols``).
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
) -> ThreeSegmentComposer:
    """Wire ``ThreeSegmentComposer`` with the workspace skill renderer.

    (Original name in noeta.agent: ``build_coding_composer`` — same body,
    renamed on hoist to reflect that the skill-composer glue is no longer
    coding-product specific.)

    The 3-segment context policy is reused as-is:

    * ``stable_prefix`` — coding-Agent role + tool schema + safety prompt.
    * ``semi_stable`` — the rendered bodies of *activated* skills.
    * ``dynamic_suffix`` — conversation / tool results.

    The renderer reads from ``task.state.active_skills`` on each
    ``compose`` call, so the activation in :func:`activate_skills` is
    what flips a skill on (PRD D10/D11).

    ③ (finding 1): ``tail_token_budget`` arms the composer's deterministic
    tail-window prune (D-3e). ``None`` (default, and for any model the catalog
    does not describe) keeps the legacy no-prune behaviour. The value is a
    deterministic function of the model
    (:func:`noeta.execution.builder.derive_compaction_config`), so a resumed
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
    extend the registry instead of the composer. A caller that already
    built a multi-kind registry (``noeta.execution.builder``) passes it as
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

    Skills are the channel's first resident: render rule = the existing
    :func:`noeta.context.skills.build_skill_renderer` (unchanged bytes),
    fingerprints = :func:`build_skill_hashes` (``(version,
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


def skill_content_hash(
    desc: SkillDescription, *, exec_env: Optional[ExecEnv] = None
) -> str:
    """``sha256`` of a skill's ``SKILL.md`` full bytes (issue 08).

    Precedence: if ``desc.source_path`` points to an on-disk file, read
    the raw bytes directly (the SKILL.md author's authoritative file on
    disk — matches what a git diff would flag). Otherwise fall back to
    ``desc.body.encode("utf-8")`` for synthetic / memory-only skills.

    ``exec_env`` (sandbox mode) reads the SKILL.md bytes THROUGH the container
    (the source_path is a container path), so the fingerprint matches the file
    the model reads; a read failure falls back to the ``body`` bytes.
    """
    if desc.source_path is not None:
        try:
            if exec_env is not None:
                return hashlib.sha256(
                    exec_env.read_bytes(desc.source_path)
                ).hexdigest()
            if desc.source_path.is_file():
                return hashlib.sha256(desc.source_path.read_bytes()).hexdigest()
        except OSError:
            pass
    return hashlib.sha256(desc.body.encode("utf-8")).hexdigest()


def build_skill_hashes(
    skill_registry: Optional[Any],
    *,
    exec_env: Optional[ExecEnv] = None,
) -> Optional[SkillHashesFn]:
    """Build a ``SkillHashesFn``-compatible lookup from a ``SkillRegistry``.

    Returns ``None`` when the registry is ``None`` so hosts that don't
    configure skills (kernel tests, SDK hosts with no workspace) leave
    the Engine's ``skill_hashes`` at its ``None`` default and no mid-loop
    provenance events are emitted (byte shape preserved).

    The returned callable maps a skill name to ``(version,
    content_hash)`` for known skills, ``None`` otherwise. Both values
    come from the registry (``desc.version`` and
    :func:`skill_content_hash`), matching what the pre-loop helper
    :func:`activate_skills` writes into ``SkillContentRecorded``.

    Results are memoised per name — skill contents are treated as static
    for the lifetime of a session, so repeated lookups avoid re-reading
    files and re-hashing. Unknown names (``None``) are not cached.
    """
    if skill_registry is None:
        return None
    # Resolve lazily: skill_registry is a SkillRegistry with a `.get(name)`
    # method. Avoid importing its type here to keep the SDK's execution
    # layer loosely coupled with the context layer's SkillRegistry class.
    cache: dict[str, tuple[str, str]] = {}

    def _lookup(skill_name: str) -> Optional[tuple[str, str]]:
        hit = cache.get(skill_name)
        if hit is not None:
            return hit
        desc = skill_registry.get(skill_name)
        if desc is None:
            return None
        resolved = (desc.version, skill_content_hash(desc, exec_env=exec_env))
        cache[skill_name] = resolved
        return resolved

    return _lookup


def activate_skills(
    engine: Engine,
    task: Task,
    *,
    skills: list[str],
    lease_id: str,
    trace_id: Optional[str] = None,
    skill_registry: Optional[SkillRegistry] = None,
    exec_env: Optional[ExecEnv] = None,
) -> Task:
    """Runner-driven pre-loop skill activation.

    Emits a durable ``TaskStatePatched(activate_skills=[...])`` event
    through :meth:`Engine.apply_state_patch` (Phase 4 B17), then the
    Engine's patch.apply unions the names into ``task.state.active_skills``
    (Phase-1 semantics: no duplicates, order preserved).

    Calling with an empty list is a no-op (no event emitted) — the
    caller (I4 runner) reaches here unconditionally so an Agent with no
    default skills + no ``--skill`` flag still works.

    The first subsequent ``ThreeSegmentComposer.compose`` call picks up
    the active set from ``task.state``, the renderer materialises the
    skill bodies into the ``semi_stable`` segment, and the post-resolve
    name list is written into ``ContextPlan.selected_skills``. Because
    the activation is recorded, a resumed session folds the same patch and
    reproduces the same active set without depending on the model
    emitting ``activate_skills``.

    **Content-hash provenance (generation
    switch).** When ``skill_registry`` is provided (the normal code-runner
    path), this function emits one generic
    ``ContextContentRecorded`` (kind="skill", policy="pinned") per
    newly-activated skill *before* the ``TaskStatePatched``, so the
    event log's causal order is unambiguous — new recordings carry only
    the generic shape (the old ``SkillContentRecorded`` is fold-read-only
    for pre-cutover recordings). The helper
    :func:`emit_context_content_recorded` enforces per-task first-only —
    duplicate activations of the same skill within a task do not
    re-emit. If no ``skill_registry`` is passed, provenance is skipped
    (compatibility path) and the activation carries no content fingerprint.
    """
    if not skills:
        return task

    # Generic content provenance per skill, per-task first-only.
    if skill_registry is not None:
        for name in skills:
            desc = skill_registry.get(name)
            if desc is None:
                # Unknown name → no descriptor to fingerprint; skip provenance.
                continue
            emit_context_content_recorded(
                engine,
                task,
                kind=SKILL_KIND,
                name=name,
                version=desc.version,
                content_hash=skill_content_hash(desc, exec_env=exec_env),
                policy=SKILL_DRIFT_POLICY,
                lease_id=lease_id,
                trace_id=trace_id,
            )

    patch = TaskStatePatch(activate_skills=list(skills))
    return engine.apply_state_patch(
        task, patch=patch, lease_id=lease_id, trace_id=trace_id
    )
