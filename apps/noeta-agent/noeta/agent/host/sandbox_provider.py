"""noeta-agent's custom SandboxProvider: per-session knowledge + skills mounted into the container.

On top of the vendored `LocalDockerSandboxProvider`, `allocate` takes the session's
space and:
- ro bind-mounts the knowledge directory to `<workdir>/knowledge` in the container
- ro bind-mounts every enabled skill to `<workdir>/.noeta/skills/<name>` in the container

Why mount instead of copy: knowledge can be huge and cannot be copied; skills are
small but belong to the same "read-only shared resource" family as knowledge, so a
uniform mounting policy is semantically cleaner and saves the copy/clean cost before
every drive. Globstar discovery inside the container walks real directories (a
bind-mount looks like a real directory from inside), so the does-not-follow-symlinks
problem does not exist. The space is derived by resolving session_id back out of the
workspace mount in the spec (source=workspaces_root/<session_id>, the rw mount the
manager appends), then querying the store via callback.
"""
from __future__ import annotations

import dataclasses
import logging
from pathlib import Path
from typing import Callable, Optional

from noeta.sdk import MountSpec, SandboxHandle, SandboxSpec

from noeta.agent.host.docker_sandbox import LocalDockerSandboxProvider

logger = logging.getLogger(__name__)


class KnowledgeMountSandboxProvider(LocalDockerSandboxProvider):
    """LocalDockerSandboxProvider + per-session knowledge & skills ro bind-mounts."""

    def __init__(
        self,
        *,
        knowledge_root: Path,
        workspaces_root: Path,
        resolve_space: Callable[[str], Optional[str]],
        space_has_knowledge: Callable[[str], bool],
        builtin_skills_root: Path,
        space_skills_root: Path,
        list_builtin_skill_names: Callable[[], list[str]],
        list_space_skill_names: Callable[[str], list[str]],
        list_knowledge_mounts: Optional[
            Callable[[str], Optional[list[tuple[str, str]]]]
        ] = None,
        **kw: object,
    ) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._knowledge_root = knowledge_root
        self._workspaces_root = workspaces_root
        self._resolve_space = resolve_space
        self._space_has_knowledge = space_has_knowledge
        self._builtin_skills_root = builtin_skills_root
        self._space_skills_root = space_skills_root
        self._list_builtin_skill_names = list_builtin_skill_names
        self._list_space_skill_names = list_space_skill_names
        # Per-source mount resolution for the space's knowledge sources:
        # [(source name, source dir)] = mount per source (mount point
        # knowledge/<source name>/, matching the skill reference contract; the
        # materialization id directories are not exposed inside the container);
        # None = callback unavailable (fall back to whole-directory mount)
        self._list_knowledge_mounts = list_knowledge_mounts

    def allocate(self, session_root_id: str, spec: SandboxSpec) -> SandboxHandle:
        mounts: list[MountSpec] = []

        # knowledge ro mounts (per-source by name; whole-directory fallback
        # when the callback is unavailable)
        mounts.extend(self._knowledge_mounts(spec))

        # skills ro mounts (one per enabled skill)
        for sm in self._skill_mounts(spec):
            mounts.append(sm)

        if mounts:
            spec = dataclasses.replace(spec, mounts=spec.mounts + tuple(mounts))
        return super().allocate(session_root_id, spec)

    # ------------------------------------------------------------------ #
    def _knowledge_mounts(self, spec: SandboxSpec) -> list[MountSpec]:
        session_id = self._session_from_spec(spec)
        if session_id is None:
            return []
        try:
            space_id = self._resolve_space(session_id)
        except Exception:  # noqa: BLE001 - a store failure must not block container start (degrade to no knowledge)
            logger.warning(
                "knowledge mount: failed to resolve space session=%s",
                session_id, exc_info=True,
            )
            return []
        if not space_id or not self._space_has_knowledge(space_id):
            return []
        target_root = f"{self._workdir.rstrip('/')}/knowledge"

        # Filter by selection: when a subset of knowledge sources is
        # configured, mount source by source (mount point named after the source)
        if self._list_knowledge_mounts is not None:
            try:
                selected = self._list_knowledge_mounts(space_id)
            except Exception:  # noqa: BLE001 - config read failure degrades to whole directory
                logger.warning(
                    "knowledge mount: failed to read source selection space=%s",
                    space_id, exc_info=True,
                )
                selected = None
            if selected is not None:
                return [
                    MountSpec(
                        source=src_dir,
                        target=f"{target_root}/{name}",
                        mode="ro",
                    )
                    for name, src_dir in selected
                ]

        src = self._knowledge_root / space_id
        if not src.is_dir():
            return []
        return [MountSpec(source=str(src), target=target_root, mode="ro")]

    def _skill_mounts(self, spec: SandboxSpec) -> list[MountSpec]:
        session_id = self._session_from_spec(spec)
        if session_id is None:
            return []

        # Resolve the space (for space-level skill filtering)
        try:
            space_id = self._resolve_space(session_id)
        except Exception:  # noqa: BLE001
            space_id = None

        skills_base = f"{self._workdir.rstrip('/')}/.noeta/skills"
        mounts: list[MountSpec] = []
        seen: set[str] = set()

        # Builtins install first (same-name space skills are skipped, matching
        # the workspace_for install order)
        try:
            builtin_names = self._list_builtin_skill_names()
        except Exception:  # noqa: BLE001
            logger.warning("skill mount: failed to list builtin skills", exc_info=True)
            builtin_names = []

        for name in builtin_names:
            src = self._builtin_skills_root / name
            if not (src / "SKILL.md").is_file():
                logger.warning("builtin skill directory missing SKILL.md, skipping mount: %s", name)
                continue
            seen.add(name)
            mounts.append(MountSpec(
                source=str(src), target=f"{skills_base}/{name}", mode="ro",
            ))

        # Space-level skills
        if space_id:
            try:
                space_names = self._list_space_skill_names(space_id)
            except Exception:  # noqa: BLE001
                logger.warning(
                    "skill mount: failed to list space skills space=%s",
                    space_id, exc_info=True,
                )
                space_names = []

            for name in space_names:
                if name in seen:
                    continue  # same-name builtin already installed, skip
                src = self._space_skills_root / space_id / name
                if not (src / "SKILL.md").is_file():
                    logger.warning(
                        "space skill directory missing SKILL.md, skipping mount: space=%s name=%s",
                        space_id, name,
                    )
                    continue
                seen.add(name)
                mounts.append(MountSpec(
                    source=str(src), target=f"{skills_base}/{name}", mode="ro",
                ))

        return mounts

    def _session_from_spec(self, spec: SandboxSpec) -> Optional[str]:
        """Resolve session_id back out of the workspace mount source (workspaces_root/<session_id>)."""
        try:
            root = self._workspaces_root.resolve()
        except OSError:
            return None
        for m in spec.mounts:
            try:
                p = Path(m.source).resolve()
            except OSError:
                continue
            if p.parent == root:
                return p.name
        return None
