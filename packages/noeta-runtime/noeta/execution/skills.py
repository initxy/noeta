"""Skill seams — the kernel's skills surface (microkernel phase 2a).

The skill *material* — the SKILL.md indexer, the three-tier merge, the
``run_skill_script`` tool, the registry-consuming wiring — lives in the
``skills`` built-in plugin (``noeta.builtins.skills.impl``, the noeta-sdk
wheel). This module keeps only what the kernel itself owns:

* :class:`SkillsKit` — the typed bundle the builder's ``skills_factory``
  injection returns: everything one session build consumes from the skill
  subsystem, with the registry carried as an **opaque handle** (the kernel
  never walks it beyond the duck-typed ``names()`` / ``get()`` surface).
* :func:`activate_skills` — runner-driven, **pre-loop**, **durable**
  activation (B11 + B17). Emits a real ``TaskStatePatched`` event
  through ``Engine.apply_state_patch`` so resume folds the same active
  set. Activation is intentionally **not** model-driven —
  ``ReActPolicy`` does not parse ``activate_skills`` from LLM text in
  Phase 4 (PRD D10), so v1 activates skills here, deterministically.
* :func:`skill_content_hash` / :func:`build_skill_hashes` — the content
  fingerprint helpers the recording path and the ``ContentKindSpec`` hashes
  share (duck-typed over the descriptor: ``source_path`` / ``body`` /
  ``version``).
* ``SKILL_KIND`` / ``SKILL_DRIFT_POLICY`` — the content-channel vocabulary
  (single source for the kind spec, the pre-loop activation emission, and
  host wiring).

Content-hash provenance: :func:`activate_skills` emits one
``ContextContentRecorded`` event (kind="skill", policy="pinned") per
newly-activated skill (per-task, first-only) with ``sha256(SKILL.md
bytes)``, right before the ``TaskStatePatched(activate_skills=…)`` so the
active skill set and its content fingerprint are part of the durable record
(resume re-derives both from the stream).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Optional

from noeta.context.content_channel import ContentKindSpec
from noeta.core.engine import Engine, SkillHashesFn, emit_context_content_recorded
from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.task import Task
from noeta.protocols.tool import Tool
from noeta.runtime.exec_env import ExecEnv


__all__ = [
    "SKILL_DRIFT_POLICY",
    "SKILL_KIND",
    "SkillsKit",
    "activate_skills",
    "build_skill_hashes",
    "skill_content_hash",
]


#: The skill kind's content-channel key and drift policy
#: (``pinned``: a SKILL.md edit without a declared version bump changes the
#: rendered prefix bytes, so the recorded fingerprint pins the content for
#: resume). Single source for the kind spec, the pre-loop activation
#: emission, and host wiring.
SKILL_KIND = "skill"
SKILL_DRIFT_POLICY = "pinned"


@dataclass(frozen=True)
class SkillsKit:
    """What one session build consumes from the skill subsystem.

    Returned whole by the builder's ``skills_factory`` injection
    (``noeta.builtins.skills.impl.wiring:build_skills_kit`` — resolved by the
    SDK through the plugin loader's dynamic doorway). The kernel reads the
    fields; it never constructs the parts.
    """

    #: The merged ``SkillRegistry`` — opaque to the kernel beyond the
    #: duck-typed ``names()`` / ``get()`` surface (menu names, activation
    #: provenance).
    registry: Any
    #: The ``run_skill_script`` tool, or ``None`` when scripts are disabled
    #: (the default everywhere, incl. every sub-agent child).
    script_tool: Optional[Tool]
    #: The PermissionGuard's script-gating facts (Issue E) — travel as plain
    #: values so the guard keeps its ``noeta.protocols``-only diet.
    skill_script_tools: frozenset[str]
    skill_scripts: frozenset[tuple[str, str]]
    #: The ``kind="skill"`` content-channel registry item (renderer + hashes
    #: bound to the registry above) — registered first, so the semi_stable
    #: byte layout is unchanged.
    content_kind: ContentKindSpec
    #: The resolved ``(skill, frozenset_of_neutral_tool_names)`` grants the
    #: guard factory's ``skill_allowed_tools`` expects.
    allowed_tools: tuple[tuple[str, frozenset[str]], ...]


def skill_content_hash(
    desc: Any, *, exec_env: Optional[ExecEnv] = None
) -> str:
    """``sha256`` of a skill's ``SKILL.md`` full bytes (issue 08).

    Precedence: if ``desc.source_path`` points to an on-disk file, read
    the raw bytes directly (the SKILL.md author's authoritative file on
    disk — matches what a git diff would flag). Otherwise fall back to
    ``desc.body.encode("utf-8")`` for synthetic / memory-only skills.

    ``desc`` is duck-typed (``source_path`` / ``body``) — the descriptor
    class lives in the skills built-in and the kernel holds no type on it.

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
    # method. The type is deliberately not imported — it lives in the skills
    # built-in and the kernel stays loosely coupled to it.
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
    skill_registry: Optional[Any] = None,
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
