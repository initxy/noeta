"""Skill kit — the session bundle plus the fingerprint and activation helpers.

Plugin-internal: the kernel consumes nothing from the skill subsystem, so both
the bundle the session pack hands around and the helpers that hash and
activate skills live here. Activation is deliberately host-driven and
pre-loop rather than parsed out of model text, and it is durable — one
``ContextContentRecorded`` per newly-activated skill, then the
``TaskStatePatched`` — so a resumed task folds back the same active set and
the same content fingerprints.
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


#: The skill kind's content-channel key and drift policy — one source for the
#: kind spec, the pre-loop activation emission, and host wiring. ``pinned``
#: because a SKILL.md edit without a declared version bump changes the rendered
#: prefix bytes, so the recorded fingerprint has to pin the content for resume.
SKILL_KIND = "skill"
SKILL_DRIFT_POLICY = "pinned"


@dataclass(frozen=True)
class SkillsKit:
    """Everything one session build takes from the skill subsystem.

    Assembled whole by :func:`~.wiring.build_skills_kit` and consumed only by
    the session pack in this package.
    """

    #: The merged ``SkillRegistry``, kept opaque beyond its duck-typed
    #: ``names()`` / ``get()`` surface.
    registry: Any
    #: The ``run_skill_script`` tool, or ``None`` when scripts are disabled —
    #: the default everywhere, including every sub-agent child.
    script_tool: Optional[Tool]
    #: The PermissionGuard's script-gating facts, carried as plain values so the
    #: guard keeps its ``noeta.protocols``-only diet.
    skill_script_tools: frozenset[str]
    skill_scripts: frozenset[tuple[str, str]]
    #: The ``kind="skill"`` content-channel item: renderer + hashes bound to the
    #: registry above.
    content_kind: ContentKindSpec
    #: The resolved ``(skill, frozenset_of_neutral_tool_names)`` grants the
    #: guard factory's ``skill_allowed_tools`` expects.
    allowed_tools: tuple[tuple[str, frozenset[str]], ...]


def skill_content_hash(
    desc: Any, *, exec_env: Optional[ExecEnv] = None
) -> str:
    """``sha256`` of a skill's full ``SKILL.md`` bytes.

    The on-disk file wins when ``desc.source_path`` points at one, so the
    fingerprint tracks what a diff of the author's file would flag; synthetic
    or memory-only skills fall back to ``desc.body``. In sandbox mode
    ``exec_env`` reads the bytes THROUGH the container (``source_path`` is then
    a container path) so the fingerprint matches the file the model reads, with
    the same fallback on a read failure.
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

    A ``None`` registry yields ``None`` so a host that configures no skills
    leaves the Engine's ``skill_hashes`` at its default and emits no mid-loop
    provenance events. The lookup answers ``(version, content_hash)`` for known
    names and ``None`` otherwise, matching what :func:`activate_skills` records
    pre-loop. Hits are memoised — skill content is treated as static for the
    lifetime of a task, so repeated lookups avoid re-reading and re-hashing
    files; misses are not cached.
    """
    if skill_registry is None:
        return None
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
    """Host-driven pre-loop skill activation.

    Emits a durable ``TaskStatePatched(activate_skills=[...])``, whose apply
    unions the names into ``task.state.active_skills`` (no duplicates, order
    preserved). An empty list is a no-op so a caller can reach here
    unconditionally. Because the activation is recorded rather than inferred, a
    resumed task folds the same patch and composes the same ``semi_stable``
    bodies without the model having to re-request the skills.

    A ``skill_registry`` also buys content provenance: one
    ``ContextContentRecorded`` per newly-activated skill is emitted *before*
    the state patch, so the log's causal order is unambiguous, and
    :func:`emit_context_content_recorded` keeps it per-task first-only. Without
    a registry the activation simply carries no content fingerprint.
    """
    if not skills:
        return task

    if skill_registry is not None:
        for name in skills:
            desc = skill_registry.get(name)
            if desc is None:
                # No descriptor to fingerprint; the patch still activates it.
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
