"""SKILL.md discovery: the parsed description, the merged registry, and the
activation text.

Discovery reads through the task's ExecEnv (one ``find`` per directory plus a
read per SKILL.md), so in a container the skill directories and the rendered
base-directory lines are container paths. It is deterministic: candidates are
sorted by POSIX relative path before duplicate names resolve first-wins, so
the same tree always yields the same registry and the same listing bytes.
"""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Sequence
from dataclasses import dataclass

from noeta.core.execenv import ExecEnv
from noeta.core.helpers import read_text
from noeta.plugins.skills import _frontmatter

__all__ = [
    "ARGUMENTS_PLACEHOLDER",
    "DEFAULT_SKILL_PRIORITY",
    "SkillDescription",
    "SkillRegistry",
    "index_dir",
    "load_registry",
    "render_activation",
    "strip_argument_placeholder",
]

_log = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Render-order default when ``priority:`` is absent or unreadable.
DEFAULT_SKILL_PRIORITY = 100

#: Claude Code's argument placeholder. The ``skill`` tool takes a name and
#: nothing else, so there is no argument channel to substitute from; lines
#: carrying it are dropped at render.
ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"

#: Bound on one ``find`` listing of a skill directory.
_LIST_OUTPUT_CAP = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SkillDescription:
    """One parsed SKILL.md.

    ``model_invocable`` is the negation of ``disable-model-invocation``: such
    a skill stays loadable by the user (``/<name>``) and the host
    (``Skills(preload=...)``) but is not in the model's listing. ``source_path``
    is rendered verbatim (its directory is the "Base directory" line).
    ``metadata`` keeps every non-semantic frontmatter key, sorted by raw key;
    ``resources`` lists the files bundled beside the SKILL.md, relative to
    the skill directory."""

    name: str
    description: str
    body: str
    version: str = "1"
    priority: int = DEFAULT_SKILL_PRIORITY
    model_invocable: bool = True
    source_path: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    resources: tuple[str, ...] = ()

    @property
    def base_dir(self) -> str | None:
        return posixpath.dirname(self.source_path) if self.source_path else None

    def meta(self, key: str) -> str | None:
        for k, v in self.metadata:
            if k == key:
                return v
        return None


class SkillRegistry:
    """Skill name -> description, plus the merge tier each name came from
    (higher = narrower scope: workspace above global above borrowed)."""

    def __init__(
        self, skills: dict[str, SkillDescription], tiers: dict[str, int] | None = None
    ) -> None:
        self._skills = dict(skills)
        self._tiers = {n: (tiers or {}).get(n, 0) for n in self._skills}

    def get(self, name: str) -> SkillDescription | None:
        return self._skills.get(name)

    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def tier_of(self, name: str) -> int:
        return self._tiers.get(name, 0)

    def model_invocable(self) -> tuple[str, ...]:
        """Sorted names the model may activate (what the listing names and
        the ``skill`` tool accepts)."""
        return tuple(sorted(n for n, d in self._skills.items() if d.model_invocable))


def _parse_disabled_flag(raw: str | None, path: str) -> bool:
    """``disable-model-invocation`` in the YAML 1.1 boolean dialect; anything
    else is warned and read as false. Absent => false."""
    if raw is None:
        return False
    token = raw.strip().lower()
    if token in ("true", "yes", "on", "1"):
        return True
    if token in ("false", "no", "off", "0"):
        return False
    _log.warning(
        "skill: %s: invalid 'disable-model-invocation' value %r "
        "(expected true/false); treating as false", path, raw)
    return False


def parse_skill(path: str, raw: str, resources: tuple[str, ...] = ()) -> SkillDescription | None:
    """One SKILL.md text into a description, or ``None`` (logged) when it is
    unusable: bad frontmatter, a missing or invalid ``name``, no
    ``description``."""
    try:
        fields, body, warnings = _frontmatter.parse(raw)
    except _frontmatter.FrontmatterError as exc:
        _log.warning("skill: %s: invalid frontmatter (%s); skipped", path, exc)
        return None
    for warning in warnings:
        _log.warning("skill: %s: %s", path, warning)
    name = fields.get("name")
    if name is None:
        _log.warning("skill: %s: missing required key 'name'; skipped", path)
        return None
    if not _NAME_PATTERN.match(name):
        _log.warning("skill: %s: invalid 'name' value %r "
                     "(must match ^[a-z0-9][a-z0-9-]*$); skipped", path, name)
        return None
    description = fields.get("description")
    if description is None:
        _log.warning("skill: %s: missing required key 'description'; skipped", path)
        return None
    priority_raw = fields.get("priority", str(DEFAULT_SKILL_PRIORITY))
    try:
        priority = int(priority_raw)
    except ValueError:
        _log.warning("skill: %s: invalid 'priority' value %r (must be an integer); "
                     "using the default %d", path, priority_raw, DEFAULT_SKILL_PRIORITY)
        priority = DEFAULT_SKILL_PRIORITY
    return SkillDescription(
        name=name,
        description=description,
        body=body,
        version=fields.get("version", "1"),
        priority=priority,
        model_invocable=not _parse_disabled_flag(fields.get("disable-model-invocation"), path),
        source_path=path,
        metadata=tuple(sorted((k, v) for k, v in fields.items()
                              if k not in _frontmatter.KNOWN_KEYS)),
        resources=resources,
    )


async def _list_files(env: ExecEnv, root: str) -> list[str]:
    """Every regular file under ``root`` (following symlinks; ``find``
    stops at a cycle), or ``[]`` when ``root`` is not a directory."""
    st = await env.stat(root)
    if st is None or not st.is_dir:
        return []
    try:
        out = await env.run(["find", "-L", root, "-type", "f"], timeout=30,
                            output_cap=_LIST_OUTPUT_CAP)
    except (OSError, ValueError) as exc:
        _log.warning("skill: listing %s failed (%s); skipped", root, exc)
        return []
    text = out.stdout.decode("utf-8", errors="replace")
    return [line for line in text.split("\n") if line]


def _rel(path: str, root: str) -> str | None:
    rel = posixpath.relpath(path, root)
    return None if rel.startswith("..") else rel


async def index_dir(env: ExecEnv, root: str) -> dict[str, SkillDescription]:
    """Index ``<root>/**/SKILL.md``. Candidates are sorted by relative path
    and a duplicate name keeps the first."""
    files = await _list_files(env, root)
    rels = sorted(r for r in (_rel(f, root) for f in files) if r is not None)
    candidates = [r for r in rels if posixpath.basename(r) == "SKILL.md"]
    skills: dict[str, SkillDescription] = {}
    for rel in candidates:
        path = posixpath.join(root, rel)
        raw = await read_text(env, path)
        if raw is None:
            _log.warning("skill: %s: failed to read; skipped", path)
            continue
        skill_dir = posixpath.dirname(rel)
        resources = tuple(sorted(
            r2 for r2 in (_rel(r, skill_dir) if skill_dir else r for r in rels)
            if r2 is not None and posixpath.basename(r2) != "SKILL.md"
        ))
        desc = parse_skill(path, raw, resources)
        if desc is None:
            continue
        if desc.name in skills:
            _log.warning("skill: duplicate name %r — keeping %s; ignoring %s",
                         desc.name, skills[desc.name].source_path, path)
            continue
        skills[desc.name] = desc
    return skills


async def load_registry(env: ExecEnv, tiers: Sequence[tuple[str, int]]) -> SkillRegistry:
    """Merge ``(directory, tier)`` pairs, low precedence first: a later
    directory's skill shadows an earlier one of the same name."""
    merged: dict[str, SkillDescription] = {}
    tier_of: dict[str, int] = {}
    for root, tier in tiers:
        for name, desc in (await index_dir(env, root)).items():
            merged[name] = desc
            tier_of[name] = tier
    return SkillRegistry(merged, tier_of)


def strip_argument_placeholder(text: str) -> str:
    """Excise ``$ARGUMENTS`` from a one-line description, collapsing the
    whitespace it leaves."""
    if ARGUMENTS_PLACEHOLDER not in text:
        return text
    return re.sub(r"\s{2,}", " ", text.replace(ARGUMENTS_PLACEHOLDER, "")).strip()


def _strip_argument_lines(body: str) -> str:
    """Drop every body line carrying ``$ARGUMENTS`` (and the blank line it
    leaves behind)."""
    if ARGUMENTS_PLACEHOLDER not in body:
        return body
    kept: list[str] = []
    for line in body.split("\n"):
        if ARGUMENTS_PLACEHOLDER in line:
            if kept and kept[-1].strip() == "":
                kept.pop()
            continue
        kept.append(line)
    return "\n".join(kept)


def render_activation(desc: SkillDescription) -> str:
    """The text an activated skill puts in the conversation."""
    base = f"Base directory for this skill: {desc.base_dir}\n\n" if desc.base_dir else ""
    return (
        f"Activated skill: {desc.name}\n\n"
        f"{strip_argument_placeholder(desc.description)}\n\n"
        f"{base}{_strip_argument_lines(desc.body)}"
    )
