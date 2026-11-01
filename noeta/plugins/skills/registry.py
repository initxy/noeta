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
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace

from noeta.core.execenv import ExecEnv
from noeta.plugins.skills import _frontmatter
from noeta.toolkit.arguments import ARGUMENTS_PLACEHOLDER, substitute_arguments
from noeta.toolkit.context import read_text
from noeta.toolkit.inline_commands import has_commands, neutralise

__all__ = [
    "ARGUMENTS_PLACEHOLDER",
    "SkillDescription",
    "SkillRegistry",
    "argument_names",
    "index_dir",
    "load_registry",
    "parse_list",
    "path_patterns",
    "plugin_skill_name",
    "render_body",
    "render_command_body",
    "strip_argument_placeholder",
    "substitute_variables",
]

_log = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
#: What a plugin skill's directory name keeps (Claude Code's).
_PLUGIN_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")

#: After the description of a skill found below the working directory: one
#: that shares its name with another skill is named ``<dir>:<name>``
#: (Claude Code's directory-scoped name) and says so; another says where it
#: comes from.
SCOPED_NOTE = (" (for files under {scope}/: when your changes are there, use it rather than "
               "the plain \"{name}\" skill)")
FOUND_BELOW_NOTE = " (from {dir}; meant for files under {scope}/)"

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
    model_invocable: bool = True
    source_path: str | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    resources: tuple[str, ...] = ()
    #: ``when_to_use``: when the model should reach for the skill; the
    #: listing shows it after the description.
    when_to_use: str | None = None
    #: ``arguments``: names ``$name`` fills from the argument words, in order.
    arguments: tuple[str, ...] = ()
    #: ``paths``: the skill is offered only once a file matching one of
    #: these patterns (gitignore-style, from the working directory) is
    #: touched; ``()`` = always offered.
    paths: tuple[str, ...] = ()
    #: ``context: fork``: the skill runs in a sub-agent of type ``agent``
    #: (the default agent when unset) and its result comes back from there.
    fork: bool = False
    agent: str | None = None
    #: A directory-scoped skill's own name (``deploy`` of ``apps/web:deploy``).
    unqualified: str | None = None

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

    def model_invocable(self, unlocked: Collection[str] = ()) -> tuple[str, ...]:
        """Sorted names the model may activate (what the listing names and
        the ``skill`` tool accepts): a skill with ``paths`` only once it is
        in ``unlocked``."""
        return tuple(sorted(n for n, d in self._skills.items()
                            if d.model_invocable and (not d.paths or n in unlocked)))

    def conditional(self) -> tuple[SkillDescription, ...]:
        """The skills with ``paths``, by name."""
        return tuple(d for _, d in sorted(self._skills.items()) if d.paths)

    def scoped(self, name: str) -> tuple[SkillDescription, ...]:
        """The directory-scoped skills whose own name is ``name``, by name."""
        return tuple(d for _, d in sorted(self._skills.items()) if d.unqualified == name)


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


def plugin_skill_name(plugin: str, rel: str, root: str, front_name: str | None) -> str:
    """A plugin's skill's name, as Claude Code names it: ``<plugin>:<the
    skill's directory>``; a ``SKILL.md`` at the top of the plugin's skill
    directory is named by its frontmatter ``name`` (else that directory)."""
    skill_dir = posixpath.dirname(rel)
    if skill_dir:
        base = posixpath.basename(skill_dir)
    else:
        own = (front_name or "").strip()
        base = own.removeprefix(f"{plugin}:") or posixpath.basename(root.rstrip("/"))
    return f"{plugin}:{_PLUGIN_NAME_UNSAFE.sub('-', base)}"


def parse_skill(path: str, raw: str, resources: tuple[str, ...] = (), *,
                name: str | None = None) -> SkillDescription | None:
    """One SKILL.md text into a description, or ``None`` (logged) when it is
    unusable: bad frontmatter, a missing or invalid ``name``, no
    ``description``. ``name`` is given for a plugin's skill (named by its
    directory); its frontmatter ``name`` is then not needed."""
    try:
        fields, body, warnings = _frontmatter.parse(raw)
    except _frontmatter.FrontmatterError as exc:
        _log.warning("skill: %s: invalid frontmatter (%s); skipped", path, exc)
        return None
    for warning in warnings:
        _log.warning("skill: %s: %s", path, warning)
    if name is None:
        name = fields.get("name")
        if name is None:
            _log.warning("skill: %s: missing required key 'name'; skipped", path)
            return None
    elif not name.split(":", 1)[0]:
        _log.warning("skill: %s: no plugin name; skipped", path)
        return None
    if ":" not in name and not _NAME_PATTERN.match(name):
        _log.warning("skill: %s: invalid 'name' value %r "
                     "(must match ^[a-z0-9][a-z0-9-]*$); skipped", path, name)
        return None
    description = fields.get("description")
    if description is None:
        _log.warning("skill: %s: missing required key 'description'; skipped", path)
        return None
    return SkillDescription(
        name=name,
        description=description,
        body=body,
        version=fields.get("version", "1"),
        model_invocable=not _parse_disabled_flag(fields.get("disable-model-invocation"), path),
        source_path=path,
        metadata=tuple(sorted((k, v) for k, v in fields.items()
                              if k not in _frontmatter.KNOWN_KEYS)),
        resources=resources,
        when_to_use=fields.get("when_to_use") or None,
        arguments=argument_names(fields.get("arguments")),
        paths=path_patterns(fields.get("paths")),
        fork=(fields.get("context") or "").strip() == "fork",
        agent=(fields.get("agent") or "").strip() or None,
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


async def index_dir(env: ExecEnv, root: str,
                    plugin: str | None = None) -> dict[str, SkillDescription]:
    """Index ``<root>/**/SKILL.md``. Candidates are sorted by relative path
    and a duplicate name keeps the first. ``plugin``: the plugin whose skill
    directory ``root`` is (its skills are named ``<plugin>:<directory>``)."""
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
        name = None
        if plugin is not None:
            front = None
            if not skill_dir:
                try:
                    front = _frontmatter.parse(raw)[0].get("name")
                except _frontmatter.FrontmatterError:
                    front = None
            name = plugin_skill_name(plugin, rel, root, front)
        desc = parse_skill(path, raw, resources, name=name)
        if desc is None:
            continue
        if desc.name in skills:
            _log.warning("skill: duplicate name %r — keeping %s; ignoring %s",
                         desc.name, skills[desc.name].source_path, path)
            continue
        skills[desc.name] = desc
    return skills


async def load_registry(env: ExecEnv, tiers: Sequence[tuple[str, int]], *,
                        plugins: Mapping[str, str] | None = None,
                        scopes: Mapping[str, str] | None = None) -> SkillRegistry:
    """Merge ``(directory, tier)`` pairs, low precedence first: a later
    directory's skill shadows an earlier one of the same name. ``plugins``:
    directory -> the plugin whose skills it holds. ``scopes``: a skill
    directory found below the working directory -> the directory it serves
    (relative to the working directory); such a skill never shadows another:
    one whose name another skill has too is named ``<scope>:<name>``, as
    Claude Code names directory-scoped skills, and each one's description
    says which files it is for."""
    plugins, scopes = plugins or {}, scopes or {}
    merged: dict[str, SkillDescription] = {}
    tier_of: dict[str, int] = {}
    below: list[tuple[str, str, int, dict[str, SkillDescription]]] = []
    for root, tier in tiers:
        found = await index_dir(env, root, plugins.get(root))
        if root in scopes:
            below.append((root, scopes[root], tier, found))
            continue
        for name, desc in found.items():
            merged[name] = desc
            tier_of[name] = tier
    named = Counter(name for _root, _scope, _tier, found in below for name in found)
    plain = set(merged)
    for root, scope, tier, found in below:
        for name, desc in found.items():
            if name in plain or named[name] > 1:
                desc = replace(desc, name=f"{scope}:{name}", unqualified=name,
                               description=desc.description
                               + SCOPED_NOTE.format(scope=scope, name=name))
            else:
                where = posixpath.join(scope, posixpath.relpath(
                    root, posixpath.dirname(posixpath.dirname(root))))
                desc = replace(desc, description=desc.description
                               + FOUND_BELOW_NOTE.format(dir=where, scope=scope))
            merged[desc.name] = desc
            tier_of[desc.name] = tier
    return SkillRegistry(merged, tier_of)


def strip_argument_placeholder(text: str) -> str:
    """Excise ``$ARGUMENTS`` from a one-line description, collapsing the
    whitespace it leaves."""
    if ARGUMENTS_PLACEHOLDER not in text:
        return text
    return re.sub(r"\s{2,}", " ", text.replace(ARGUMENTS_PLACEHOLDER, "")).strip()


def parse_list(raw: str | None) -> tuple[str, ...]:
    """A frontmatter list written as YAML block items (``- a``), a flow
    list (``[a, b]``) or words separated by commas or whitespace."""
    if raw is None:
        return ()
    text = raw.strip()
    if text.startswith("[") and text.endswith("]"):
        items = text[1:-1].split(",")
    elif text.startswith("- ") or text == "-":
        items = [line.strip()[1:] for line in text.splitlines() if line.strip().startswith("-")]
    else:
        items = re.split(r"[,\s]+", text)
    out = []
    for item in items:
        item = item.strip()
        if len(item) >= 2 and item[0] == item[-1] and item[0] in "'\"":
            item = item[1:-1]
        if item:
            out.append(item)
    return tuple(out)


def path_patterns(raw: str | None) -> tuple[str, ...]:
    """The ``paths`` frontmatter as patterns; a trailing ``/**`` is dropped
    (a directory covers what is under it), and patterns that all match
    everything leave the skill unconditional."""
    patterns = [p[:-3] if p.endswith("/**") else p for p in parse_list(raw)]
    patterns = [p for p in patterns if p]
    if not patterns or all(p == "**" for p in patterns):
        return ()
    return tuple(patterns)


def argument_names(raw: str | None) -> tuple[str, ...]:
    """The ``arguments`` frontmatter: the names ``$name`` fills from the
    words of the arguments, in order (a name that is only digits is
    dropped, since ``$N`` is positional)."""
    return tuple(n for n in parse_list(raw) if not n.isdigit())



#: Directory variables a skill's text may name: noeta's names, and Claude
#: Code's for skills written for it.
SKILL_DIR_VARIABLES = ("${NOETA_SKILL_DIR}", "${CLAUDE_SKILL_DIR}")
PROJECT_DIR_VARIABLES = ("${NOETA_PROJECT_DIR}", "${CLAUDE_PROJECT_DIR}")


def substitute_variables(text: str, skill_dir: str | None, project_dir: str | None) -> str:
    """``text`` with the skill's directory and the project's (the task's
    working directory) filled in; a variable with no value stays as
    written."""
    for names, value in ((SKILL_DIR_VARIABLES, skill_dir),
                         (PROJECT_DIR_VARIABLES, project_dir)):
        if value:
            for name in names:
                text = text.replace(name, value)
    return text


def render_body(desc: SkillDescription, args: str | None = None,
                project_dir: str | None = None) -> str:
    """The skill's body as the model gets it: the directory variables, then
    the argument placeholders (an argument naming a variable stays as
    typed)."""
    body = substitute_variables(desc.body, desc.base_dir, project_dir)
    escape = neutralise if has_commands(body) else None
    return substitute_arguments(body, args, desc.arguments, escape)


def render_command_body(desc: SkillDescription, args: str | None = None,
                        project_dir: str | None = None) -> str:
    """What a skill puts in the conversation when it loads (after the
    command tags of a typed ``/<name> <args>``, or after a ``Skill`` call's
    result): the skill's directory and its rendered body."""
    base = f"Base directory for this skill: {desc.base_dir}\n\n" if desc.base_dir else ""
    return base + render_body(desc, args, project_dir).lstrip("\n")
