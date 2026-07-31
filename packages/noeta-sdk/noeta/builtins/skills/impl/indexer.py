"""SKILL.md discovery — the indexer, the registry, and the parsed description.

Indexing is one synchronous pass with no watcher (re-indexing means a new
indexer) and it has to be deterministic: candidates are sorted by normalised
POSIX relative path before duplicate names resolve first-wins, because raw
directory iteration order varies across platforms and file systems while the
rendered bytes feed the composer's ``semi_stable`` segment hash. Those bytes
embed each skill's own directory path, so a registry stays cache-stable only
against the same skill directories — not a relocated copy of the same tree.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from noeta.context.composer import ContentRenderer, ContentResolve, RenderedContent
from noeta.protocols.messages import Message, TextBlock

from . import _frontmatter


__all__ = [
    "SkillDescription",
    "SkillIndexer",
    "SkillRegistry",
    "build_skill_renderer",
]


_log = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True, slots=True)
class SkillDescription:
    """One parsed SKILL.md file.

    ``priority`` drives render order (ascending, ties broken by ``name``);
    ``version`` is recorded but never filters.

    ``source_path``'s **parent directory** is rendered as the ``Base directory
    for this skill:`` line, so the path influences the canonical ``Message``
    bytes and the ``semi_stable`` segment hash. It is rendered verbatim — no
    ``resolve()``, no IO — so re-indexing the same tree reproduces the same
    bytes; a synthetic skill without one renders body-only.

    ``metadata`` keeps every non-semantic frontmatter key as opaque
    ``(key, value)`` strings sorted by the **raw** key — no normalisation, so
    ``allowed-tools`` enforcement sees exactly what the author wrote — and
    ``resources`` lists the files bundled beside the SKILL.md for audit.
    Both participate in equality but neither is read by ``render``, so two
    descriptions differing only there render byte-equal and cannot perturb the
    cache-stable bytes.
    """

    name: str
    description: str
    body: str
    version: str = "1"
    priority: int = 100
    source_path: Optional[Path] = None
    metadata: tuple[tuple[str, str], ...] = ()
    resources: tuple[str, ...] = ()


class SkillIndexer:
    """Scan ``<root>/<name>/SKILL.md`` files into a :class:`SkillRegistry`.

    ``exec_env`` (duck-typed, so fakes and alternative backends work) routes
    every read through a **container**: ``root`` is then a container path, and
    ``source_path`` — hence the rendered base-directory line — is the container
    path the model will read against. Discovery then uses the ExecEnv's
    recursive glob rather than the host walk's symlink-cycle recursion, since
    the container is the isolation boundary and globstar does not chase symlink
    cycles.

    ``prefetched`` short-circuits the per-file container IO: it is one tree
    snapshot spanning every tier (regular files plus each SKILL.md's bytes)
    that the caller obtained in a single round-trip. Candidates, bytes, and
    resources are then derived in-process, scoped to ``root`` by the same
    lexical filter a per-tier walk applies.
    """

    def __init__(
        self,
        root: Path,
        *,
        exec_env: Optional[Any] = None,
        prefetched: Optional[Any] = None,
    ) -> None:
        self._root = root
        self._exec_env = exec_env
        self._prefetched = prefetched

    def index(self) -> "SkillRegistry":
        skills: dict[str, SkillDescription] = {}
        for posix_rel, path in self._candidates():
            description = self._parse_one(path)
            if description is None:
                continue
            existing = skills.get(description.name)
            if existing is not None:
                _log.warning(
                    "skill: duplicate name %r — keeping %s; ignoring %s",
                    description.name,
                    existing.source_path,
                    path,
                )
                continue
            skills[description.name] = description
        return SkillRegistry(skills)

    def _candidates(self) -> list[tuple[str, Path]]:
        if self._prefetched is not None:
            return self._candidates_via_prefetched()
        if self._exec_env is not None:
            return self._candidates_via_exec_env()
        if not self._root.is_dir():
            return []
        found: list[tuple[str, Path]] = []
        seen_dirs: set[Path] = set()

        def walk(current: Path) -> None:
            try:
                real = current.resolve()
            except OSError:
                return
            if real in seen_dirs:
                return
            seen_dirs.add(real)

            try:
                entries = sorted(current.iterdir(), key=lambda p: p.name)
            except OSError:
                return

            for entry in entries:
                try:
                    if entry.name == "SKILL.md" and entry.is_file():
                        rel = entry.relative_to(self._root)
                        found.append((rel.as_posix(), entry))
                        continue
                    if entry.is_dir():
                        walk(entry)
                except (OSError, ValueError):
                    continue

        walk(self._root)
        found.sort(key=lambda item: item[0])
        return found

    def _candidates_via_prefetched(self) -> list[tuple[str, Path]]:
        """Zero-IO candidate derivation: the snapshot lists regular files only
        (no ``is_file`` probe needed) and spans every tier, so scoping to THIS
        indexer's root is the same lexical ``relative_to`` filter the per-tier
        walks apply. Sorted by normalised POSIX relative path exactly as the
        other two discovery paths are."""
        found: list[tuple[str, Path]] = []
        for path in self._prefetched.files:
            if path.name != "SKILL.md":
                continue
            try:
                rel = path.relative_to(self._root)
            except ValueError:
                continue
            found.append((rel.as_posix(), path))
        found.sort(key=lambda item: item[0])
        return found

    def _candidates_via_exec_env(self) -> list[tuple[str, Path]]:
        """One container round-trip: the ExecEnv's recursive glob replaces the
        host walk, and the resulting container paths are sorted by normalised
        POSIX relative path so the Registry stays deterministic."""
        exec_env = self._exec_env
        if not exec_env.is_dir(self._root):
            return []
        found: list[tuple[str, Path]] = []
        for path in exec_env.rglob(self._root, "SKILL.md"):
            if not exec_env.is_file(path):
                continue
            try:
                rel = path.relative_to(self._root)
            except ValueError:
                continue
            found.append((rel.as_posix(), path))
        found.sort(key=lambda item: item[0])
        return found

    def _parse_one(self, path: Path) -> Optional[SkillDescription]:
        try:
            if self._prefetched is not None:
                raw_bytes = self._prefetched.contents.get(path)
                if raw_bytes is None:
                    raise OSError("bytes not captured in the tier snapshot")
                raw = raw_bytes.decode("utf-8")
            elif self._exec_env is not None:
                raw = self._exec_env.read_text(path, encoding="utf-8")
            else:
                raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            _log.warning("skill: %s: failed to read (%s); skipped", path, exc)
            return None

        try:
            fields, body, warnings = _frontmatter.parse(raw)
        except _frontmatter.FrontmatterError as exc:
            _log.warning(
                "skill: %s: invalid frontmatter (%s); skipped", path, exc
            )
            return None
        for warning in warnings:
            _log.warning("skill: %s: %s", path, warning)

        name = fields.get("name")
        if name is None:
            _log.warning(
                "skill: %s: missing required key 'name'; skipped", path
            )
            return None
        if not _NAME_PATTERN.match(name):
            _log.warning(
                "skill: %s: invalid 'name' value %r "
                "(must match ^[a-z0-9][a-z0-9-]*$); skipped",
                path,
                name,
            )
            return None

        description = fields.get("description")
        if description is None:
            _log.warning(
                "skill: %s: missing required key 'description'; skipped",
                path,
            )
            return None

        version = fields.get("version", "1")
        priority_raw = fields.get("priority", "100")
        try:
            priority = int(priority_raw)
        except ValueError:
            _log.warning(
                "skill: %s: invalid 'priority' value %r "
                "(must be integer); skipped",
                path,
                priority_raw,
            )
            return None

        metadata = tuple(
            sorted(
                (key, value)
                for key, value in fields.items()
                if key not in _frontmatter.KNOWN_KEYS
            )
        )
        resources = self._discover_resources(path)

        return SkillDescription(
            name=name,
            description=description,
            body=body,
            version=version,
            priority=priority,
            source_path=path,
            metadata=metadata,
            resources=resources,
        )

    def _discover_resources(self, skill_md: Path) -> tuple[str, ...]:
        """List files bundled beside ``skill_md``.

        Boundaries: **files only**, sorted POSIX-relative to the skill root
        (``skill_md.parent``), no absolute paths, no ``..``, and **no resource
        body reads** — paths are recorded for audit, never loaded or executed.
        Any nested ``SKILL.md`` is excluded, so a sibling skill's manifest is
        never mistaken for a resource of this one.

        Sandbox mode (``exec_env`` set) enumerates through the container, so a
        skill's bundled resources are those that exist INSIDE it; a
        ``prefetched`` snapshot gives the same view for free, since it already
        holds every regular file and the ``relative_to`` filter below scopes it
        to this skill's root.
        """
        root = skill_md.parent
        exec_env = self._exec_env
        if self._prefetched is not None:
            # The snapshot lists regular files only — no per-entry probe.
            candidates = list(self._prefetched.files)
            is_file = lambda p: True  # noqa: E731
        elif exec_env is not None:
            candidates = list(exec_env.rglob(root, "*"))
            is_file = exec_env.is_file
        else:
            candidates = list(root.rglob("*"))
            is_file = lambda p: p.is_file()  # noqa: E731
        rels: list[str] = []
        for candidate in candidates:
            if not is_file(candidate):
                continue
            if candidate.name == "SKILL.md":
                continue
            try:
                rel = candidate.relative_to(root)
            except ValueError:
                continue
            rels.append(rel.as_posix())
        rels.sort()
        return tuple(rels)


class SkillRegistry:
    """Immutable in-memory snapshot of skill name → description.

    The Registry is the sole source of truth for skill body bytes during a
    Composer ``compose`` call: reusing the same instance (or re-indexing the
    same disk state) reproduces the same ``semi_stable`` segment hashes,
    keeping that segment cache-stable across steps.
    """

    def __init__(self, skills: dict[str, SkillDescription]) -> None:
        self._skills: dict[str, SkillDescription] = dict(skills)

    def get(self, name: str) -> Optional[SkillDescription]:
        return self._skills.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._skills.keys())

    def resolve(
        self, active: list[str]
    ) -> tuple[SkillDescription, ...]:
        """Return the ``SkillDescription``\\s that ``render`` will emit
        for ``active``, in render order.

        Unknown names are dropped and duplicates deduplicated. Ordering is
        ``priority`` ascending, ties broken by ``name`` — independent of input
        order, so a Policy reshuffle never drifts the ``semi_stable`` hash.
        """
        resolved: list[SkillDescription] = []
        seen: set[str] = set()
        for raw in active:
            if raw in seen:
                continue
            seen.add(raw)
            description = self._skills.get(raw)
            if description is None:
                _log.info(
                    "skill: active name %r not in Registry; dropping", raw
                )
                continue
            resolved.append(description)
        resolved.sort(key=lambda d: (d.priority, d.name))
        return tuple(resolved)

    def render(
        self, active: list[str], resolve: ContentResolve | None = None
    ) -> RenderedContent:
        """Adapter consumed by :class:`ThreeSegmentComposer`.

        Each surviving description renders as a ``role='user'`` Message
        because the L0 ``Message`` contract bars ``role='system'`` from
        ``LLMRequest.messages``. The post-resolve name list is returned
        alongside so Composer can write ``ContextPlan.selected_skills``
        without re-implementing the resolution rules.

        ``resolve`` is accepted for the uniform ``ContentRenderer`` shape but
        unused: a skill's body comes from the preloaded
        :class:`SkillRegistry` (its environment-specific base-directory line is
        not content-addressed) and skills are ``pinned``, so there is no
        refresh to dereference.
        """
        resolved = self.resolve(active)
        messages: list[Message] = []
        selected: list[str] = []
        for description in resolved:
            # Prepend the skill's base directory so the model can ``read`` its
            # bundled references (``references/x.md`` …) on demand. Nothing is
            # inlined and no disk is touched — the path string is rendered
            # verbatim — which keeps the rendered bytes independent of what a
            # skill happens to bundle.
            base = (
                f"Base directory for this skill: {description.source_path.parent}\n\n"
                if description.source_path is not None
                else ""
            )
            text = (
                f"Activated skill: {description.name}\n\n"
                f"{description.description}\n\n"
                f"{base}{description.body}"
            )
            messages.append(
                Message(role="user", content=[TextBlock(text=text)])
            )
            selected.append(description.name)
        return RenderedContent(
            messages=messages,
            selected_skills=selected,
        )


def build_skill_renderer(registry: SkillRegistry) -> ContentRenderer:
    """Bind a :class:`SkillRegistry` to the :data:`ContentRenderer` shape."""
    return registry.render
