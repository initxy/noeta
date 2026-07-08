"""SkillIndexer / SkillRegistry / SkillDescription (L2, issue 21).

Phase 1 single-host: ``SkillIndexer.index()`` walks ``<root>/<name>/SKILL.md``
files at construction time, parses each strict-minimal frontmatter
block via :mod:`._frontmatter`, and returns a frozen
:class:`SkillRegistry`. The Registry exposes ``resolve(active)`` and
``render(active)``; wire ``registry.render`` (or
:func:`build_skill_renderer`) as
:attr:`noeta.context.composer.ThreeSegmentComposer.skill_renderer` so
``task.state.active_skills`` activations materialise into the
``semi_stable`` View segment.

Determinism notes (rev3 G5):

* SkillIndexer collects candidate paths with a deterministic recursive
  walk (following symlinked directories, guarded by realpath cycle
  detection) then sorts them by **normalised POSIX relative path**
  before resolving duplicate-name conflicts first-wins (rev3 B2). Raw
  directory iteration order varies across platforms / file systems;
  explicit sort guarantees the same disk state always produces the same
  Registry.
* :attr:`SkillDescription.source_path` participates in default
  dataclass ``__eq__`` (rev2 NB1). Its **parent
  directory** is rendered into the ``Message`` bytes (the ``Base
  directory for this skill:`` line), so two descriptions that differ
  only in path no longer render byte-equal — the rendered bytes (and
  thus the ``semi_stable`` segment hash) depend on the skill directory
  path, so they stay cache-stable only against the **same** skill
  directory paths (single-machine; not a relocated copy). The path
  string is rendered as-is — no disk IO — so re-indexing the same tree
  still reproduces the same bytes.
* :meth:`SkillRegistry.render` returns a :class:`RenderedSkills`
  (rev3 B1 seam), giving Composer the post-filter, post-sort name
  list so ``ContextPlan.selected_skills`` records what was actually
  rendered rather than the raw active list.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from noeta.context.composer import RenderedSkills, SkillRenderer
from noeta.protocols.messages import Message, TextBlock

from . import _frontmatter


# A skill's bundled resources are
# read on demand with the ordinary ``read`` tool, same as Claude Code. The
# renderer prepends a ``Base directory for this skill: <abs dir>`` line so
# the model can resolve the body's relative references (``references/x.md``)
# to an absolute path and ``read`` it; the read tool's containment seam is
# widened to the skill roots at wiring time
# (``noeta.execution.skills.resolve_skill_roots``). No resource is inlined
# and no resource bytes are read here — the only addition is the skill's
# own absolute directory string. This retired the dedicated
# ``read_skill_resource`` tool and its body-reference manifest.


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

    ``description`` becomes the one-line header in the rendered
    Message; ``body`` is appended verbatim (CRLF already normalised to
    LF by :mod:`._frontmatter`). ``priority`` drives render order
    (ascending, ties broken by ``name``); ``version`` is recorded for
    future schema evolution but is not used for filtering.

    ``source_path`` is the on-disk SKILL.md path. It participates in the
    default dataclass equality (rev2 NB1). ``render``
    emits its **parent directory** as the ``Base directory for this
    skill:`` line so the model can ``read`` the skill's bundled
    references by absolute path — so, unlike before, ``source_path`` now
    influences the canonical ``Message`` bytes (and thus the
    ``semi_stable`` ``segment_hash``). The directory string is rendered
    verbatim (no ``resolve()``/IO), so re-indexing the same tree is still
    deterministic; a ``source_path``-less synthetic skill renders
    body-only, with no base-directory line.

    ``metadata`` (4.5-I5) holds every non-semantic frontmatter key —
    everything other than ``name`` / ``description`` / ``version`` /
    ``priority`` — captured as opaque ``(key, value)`` strings sorted by
    the **raw** key (no key-name normalisation, so future
    ``allowed-tools`` enforcement keeps full compat info). It lets real
    public skills load unchanged. ``resources`` (4.5-I5) lists files
    bundled beside the SKILL.md (progressive-disclosure references,
    scripts) as sorted POSIX-relative paths from the skill root,
    excluding ``SKILL.md``. Both are immutable tuples; like
    ``source_path`` they participate in equality but are **NOT** read by
    ``render`` / the canonical ``Message`` bytes — two descriptions
    differing only in ``metadata`` / ``resources`` render byte-equal, so
    they do not perturb the cache-stable ``semi_stable`` bytes. I5 records
    ``resources`` for audit only; it does not inline their content or
    execute scripts.
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

    Phase 1 single-host: one synchronous ``index()`` call. Re-index
    requires constructing a new Indexer; no watcher / hot-reload
    (Phase 2 daemon).

    ``exec_env`` (a duck-typed :class:`~noeta.tools.fs.exec_env.ExecEnv`; kept
    ``Any`` so this L2 ``noeta.context`` module does not import its L2 sibling
    ``noeta.tools``) routes every filesystem read through a **container** in
    sandbox mode (D6-Skills): ``root`` is then a container path
    (``/opt/noeta/skills/builtin`` …), the SKILL.md bytes are read over the
    ExecEnv, and ``source_path`` — hence the rendered ``Base directory for this
    skill:`` line — is the container path the model will ``read`` against.
    ``None`` (the default, every local session) walks the host filesystem
    byte-identically to before. Sandbox mode expresses discovery with the
    ExecEnv's recursive glob rather than the host walk's symlink-cycle recursion
    (the container is the isolation boundary; ``**`` via the shell's globstar
    does not chase symlink cycles).
    """

    def __init__(self, root: Path, *, exec_env: Optional[Any] = None) -> None:
        self._root = root
        self._exec_env = exec_env

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

    def _candidates_via_exec_env(self) -> list[tuple[str, Path]]:
        """Discover ``SKILL.md`` files under ``root`` through the ExecEnv.

        Uses the ExecEnv's recursive glob (a single container round-trip) rather
        than the host walk; the resulting container paths are sorted by
        normalised POSIX relative path so the Registry is deterministic exactly
        as the host path is."""
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
            if self._exec_env is not None:
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
        """List files bundled beside ``skill_md`` (4.5-I5).

        Boundaries (architect P2): **files only**, sorted POSIX-relative
        to the skill root (``skill_md.parent``), the skill's own
        ``SKILL.md`` excluded, no absolute paths, no ``..``, and **no
        resource body reads** — paths are recorded for audit, never
        loaded or executed. A nested ``SKILL.md`` (a sibling skill's
        manifest) is likewise excluded so it is not mistaken for a
        resource of this skill.

        Sandbox mode (``exec_env`` set) enumerates the same way through the
        container's recursive glob + per-entry file test, so a skill's bundled
        resources are those that exist INSIDE the container.
        """
        root = skill_md.parent
        exec_env = self._exec_env
        if exec_env is not None:
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

    Constructed by :meth:`SkillIndexer.index`. The Registry is the
    sole canonical source of truth for skill body bytes during a
    Composer ``compose`` call — reusing the same Registry instance (or
    re-indexing the same disk state) reproduces the same ``semi_stable``
    segment hashes, keeping that segment cache-stable across steps.
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

        Unknown names (present in ``active`` but absent from the
        Registry) are dropped with an INFO log. Duplicates in
        ``active`` are deduplicated defensively. Final ordering is
        ``priority`` ascending, ties broken by ``name`` ascending —
        independent of input order, so Policy reshuffles never drift
        the ``semi_stable`` hash.
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

    def render(self, active: list[str]) -> RenderedSkills:
        """Adapter consumed by :class:`ThreeSegmentComposer`.

        Resolves ``active`` (drop unknowns, sort by priority/name),
        renders each surviving description into a ``role='user'``
        :class:`Message` (L0 ``messages.py:109`` forbids ``role='system'``
        inside ``LLMRequest.messages``), and returns both the rendered
        Messages and the post-resolve name list so Composer can write
        ``ContextPlan.selected_skills`` without re-implementing the
        resolution rules.
        """
        resolved = self.resolve(active)
        messages: list[Message] = []
        selected: list[str] = []
        for description in resolved:
            # Prepend the skill's absolute base directory so the
            # model can ``read`` its bundled references (``references/x.md``
            # …) on demand — same contract as Claude Code. Nothing is
            # inlined and no disk is read (the path string is rendered
            # verbatim), so exactly one message per skill. A synthetic
            # (source_path-less) skill renders body-only, no base line.
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
        return RenderedSkills(
            messages=messages,
            selected_skills=selected,
        )


def build_skill_renderer(registry: SkillRegistry) -> SkillRenderer:
    """Bind a :class:`SkillRegistry` to the :data:`SkillRenderer` shape.

    Callers wire the result as
    ``ThreeSegmentComposer(skill_renderer=build_skill_renderer(registry))``.
    """
    return registry.render
