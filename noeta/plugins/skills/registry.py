"""SKILL.md discovery: the parsed description, the merged registry, and the
activation text.

Discovery reads through the task's ExecEnv (one ``find`` per directory plus a
read per SKILL.md), so in a container the skill directories and the rendered
base-directory lines are container paths. As in Claude Code, a skill is a
``<directory>/SKILL.md`` one level below a skills directory, named by that
directory (its frontmatter ``name`` is a display name), and a commands
directory's ``*.md`` files are skills too. It is deterministic: candidates are
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
    "body_description",
    "display_label",
    "former_name",
    "index_commands_dir",
    "index_dir",
    "load_registry",
    "parse_list",
    "path_patterns",
    "plugin_skill_name",
    "render_body",
    "render_command_body",
    "skill_dir_name",
    "strip_argument_placeholder",
    "substitute_variables",
]

_log = logging.getLogger(__name__)

#: A skill's own name (its directory's, or a command file's path): letters,
#: digits, marks, ``_``, ``.`` and ``-`` (Claude Code's name characters; a
#: ``:`` only in names noeta gives: a plugin's, a scoped or nested command's).
_NAME_PATTERN = re.compile(r"^[\w.\-\u0300-\u036f]+$")
#: What a display name (a skill's frontmatter ``name``) may hold to be shown
#: beside the skill's name in the listing (Claude Code's).
_DISPLAY_PATTERN = re.compile(r"^[\w:.\-\u0300-\u036f]+$")
#: Longest name or display name (Claude Code's).
NAME_MAX_CHARS = 256
#: A description taken from the body's first line is cut to this length.
BODY_DESCRIPTION_MAX_CHARS = 100
#: What a plugin skill's directory name keeps (Claude Code's).
_PLUGIN_NAME_UNSAFE = re.compile(r"[^a-zA-Z0-9_-]")
#: A name a ``SKILL.md`` could give itself before skills were named by
#: their directories (noeta 0.6/1.0): see :func:`former_name`.
_FORMER_NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
#: ``SKILL.md`` files deeper than one level already warned about (they are
#: no longer skills), the newest few.
_warned_deep: dict[str, None] = {}
_WARNED_DEEP_KEEP = 1024

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
    #: The frontmatter ``name`` when it differs from the skill's name (the
    #: listing shows it after the name, as Claude Code does).
    display_name: str | None = None
    #: ``user-invocable: false``: not offered as ``/<name>``; the model may
    #: still load it.
    user_invocable: bool = True
    #: ``model`` / ``effort``: what the rest of the turn that loads the skill
    #: runs with (Claude Code's); ``None`` = the task's.
    model: str | None = None
    effort: str | None = None
    #: ``disallowed-tools``: tools a ``context: fork`` skill's agent lacks.
    disallowed_tools: tuple[str, ...] = ()
    #: ``False`` for a plain command file (``commands/<name>.md``): it has no
    #: directory of its own, so no "Base directory" line.
    own_dir: bool = True
    #: Literal ``(variable, value)`` pairs filled into the body first (a
    #: Claude Code plugin's ``${CLAUDE_PLUGIN_ROOT}`` and the like).
    variables: tuple[tuple[str, str], ...] = ()

    @property
    def base_dir(self) -> str | None:
        if not self.source_path or not self.own_dir:
            return None
        return posixpath.dirname(self.source_path)

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

    def recorded(self, name: str) -> SkillDescription | None:
        """The skill a task's log names ``name``: that skill, else the one
        plugin skill ``<plugin>:<name>`` (logs written before a plugin's
        skills were named so hold the bare name). Never for a model's call:
        the ``Skill`` tool answers a bare name with the full one."""
        found = self._skills.get(name)
        if found is not None or ":" in name:
            return found
        former = [d for n, d in self._skills.items()
                  if d.unqualified is None and n.endswith(f":{name}")]
        return former[0] if len(former) == 1 else None

    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def items(self) -> tuple[tuple[str, SkillDescription], ...]:
        return tuple(self._skills.items())

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

    def formerly(self, name: str) -> SkillDescription | None:
        """The one skill that was named ``name`` before skills were named by
        their directories (:func:`former_name`), when no skill is named so
        now; ``None`` when none or several were."""
        if name in self._skills:
            return None
        found = [d for d in self._skills.values() if former_name(d) == name]
        return found[0] if len(found) == 1 else None


def former_name(desc: SkillDescription) -> str | None:
    """The name ``desc`` had before skills were named by their directories
    (noeta 0.6/1.0 named a skill by its frontmatter ``name``, and a
    directory-scoped one ``<scope>:<that name>``), when it differs from its
    name now; ``None`` for a plugin's skill or a command file (their names
    did not change). Old ``Skill(...)`` rules and preloads still find the
    skill by it."""
    shown = desc.display_name
    if shown is None or not _FORMER_NAME.match(shown):
        return None
    if desc.unqualified is not None:
        former = f"{desc.name[:-len(desc.unqualified) - 1]}:{shown}"
    elif ":" in desc.name or not desc.own_dir:
        return None
    else:
        former = shown
    return former if former != desc.name else None


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


def skill_dir_name(name: str) -> bool:
    """Whether ``name`` (a directory's) may name a skill."""
    return len(name) <= NAME_MAX_CHARS and _NAME_PATTERN.match(name) is not None


def body_description(body: str) -> str | None:
    """The first non-blank line of ``body`` (a heading without its ``#``),
    cut to 100 characters: what a skill without a ``description`` is listed
    with (Claude Code's)."""
    for line in body.split("\n"):
        text = line.strip()
        if text:
            heading = re.match(r"^#+\s+(\S.*)$", text)
            text = heading.group(1) if heading else text
            if len(text) > BODY_DESCRIPTION_MAX_CHARS:
                return text[:BODY_DESCRIPTION_MAX_CHARS - 3] + "..."
            return text
    return None


def _flag(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in ("true", "yes", "on", "1"):
        return True
    if token in ("false", "no", "off", "0"):
        return False
    return default


def parse_skill(path: str, raw: str, resources: tuple[str, ...] = (), *,
                name: str, own_dir: bool = True,
                variables: tuple[tuple[str, str], ...] = ()) -> SkillDescription | None:
    """One SKILL.md (or command file) text into a description named
    ``name`` (its directory's, or what its path gives), or ``None`` (logged)
    when the frontmatter is unusable. The frontmatter ``name`` is a display
    name; a skill without a ``description`` is described by its body's first
    line."""
    try:
        fields, body, warnings = _frontmatter.parse(raw)
    except _frontmatter.FrontmatterError as exc:
        _log.warning("skill: %s: invalid frontmatter (%s); skipped", path, exc)
        return None
    for warning in warnings:
        _log.warning("skill: %s: %s", path, warning)
    if not name.split(":", 1)[0]:
        _log.warning("skill: %s: no name; skipped", path)
        return None
    description = (fields.get("description") or "").strip() or body_description(body) or ""
    shown = (fields.get("name") or "").strip()
    display = shown if shown and shown != name and len(shown) <= NAME_MAX_CHARS else None
    effort = (fields.get("effort") or "").strip() or None
    model = (fields.get("model") or "").strip() or None
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
        display_name=display,
        user_invocable=_flag(fields.get("user-invocable"), True),
        model=None if model is not None and model.lower() == "inherit" else model,
        effort=effort,
        disallowed_tools=parse_list(fields.get("disallowed-tools")),
        own_dir=own_dir,
        variables=variables,
    )


def display_label(desc: SkillDescription, registry: SkillRegistry) -> str:
    """How the listing names a skill: ``name (Display name)`` when its
    frontmatter gives another name that no other skill answers to, else its
    name (Claude Code's)."""
    shown = desc.display_name
    if shown is None or desc.name.endswith(f":{shown}") \
            or _DISPLAY_PATTERN.match(shown) is None:
        return desc.name
    if registry.get(shown) is not None:
        return desc.name
    first = next((d for _n, d in sorted(registry.items()) if d.display_name == shown), None)
    return f"{desc.name} ({shown})" if first is desc else desc.name


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


async def index_dir(env: ExecEnv, root: str, plugin: str | None = None, *,
                    variables: tuple[tuple[str, str], ...] = ()) -> dict[str, SkillDescription]:
    """Index ``<root>/<directory>/SKILL.md`` (one level, as Claude Code
    reads a skills directory), each named by its directory. ``plugin``: the
    plugin whose skill directory ``root`` is (its skills are named
    ``<plugin>:<directory>``, and a ``SKILL.md`` at ``root`` itself is one
    too); ``variables``: filled into each body."""
    files = await _list_files(env, root)
    rels = sorted(r for r in (_rel(f, root) for f in files) if r is not None)
    candidates = [r for r in rels if posixpath.basename(r) == "SKILL.md"
                  and (r.count("/") == 1 or (plugin is not None and r == "SKILL.md"))]
    if plugin is None:
        _warn_deep(root, [r for r in rels if posixpath.basename(r) == "SKILL.md"
                          and r.count("/") > 1
                          and posixpath.join(r.split("/", 1)[0], "SKILL.md") not in rels])
    skills: dict[str, SkillDescription] = {}
    for rel in candidates:
        path = posixpath.join(root, rel)
        skill_dir = posixpath.dirname(rel)
        if plugin is None and not skill_dir_name(skill_dir):
            _log.warning("skill: %s: the directory name %r cannot name a skill; skipped",
                         path, skill_dir)
            continue
        raw = await read_text(env, path)
        if raw is None:
            _log.warning("skill: %s: failed to read; skipped", path)
            continue
        resources = tuple(sorted(
            r2 for r2 in (_rel(r, skill_dir) if skill_dir else r for r in rels)
            if r2 is not None and posixpath.basename(r2) != "SKILL.md"
        ))
        if plugin is not None:
            front = None
            if not skill_dir:
                try:
                    front = _frontmatter.parse(raw)[0].get("name")
                except _frontmatter.FrontmatterError:
                    front = None
            name = plugin_skill_name(plugin, rel, root, front)
        else:
            name = skill_dir
        desc = parse_skill(path, raw, resources, name=name, variables=variables)
        if desc is None:
            continue
        if desc.name in skills:
            _log.warning("skill: duplicate name %r — keeping %s; ignoring %s",
                         desc.name, skills[desc.name].source_path, path)
            continue
        skills[desc.name] = desc
    return skills


def _warn_deep(root: str, rels: Sequence[str]) -> None:
    """Warn once about each ``SKILL.md`` deeper than one level below a
    skills directory (not inside another skill): it is not a skill, as in
    Claude Code (noeta 1.0 read any depth)."""
    for rel in rels:
        path = posixpath.join(root, rel)
        if path in _warned_deep:
            continue
        if len(_warned_deep) >= _WARNED_DEEP_KEEP:
            del _warned_deep[next(iter(_warned_deep))]
        _warned_deep[path] = None
        _log.warning("skill: %s is not loaded: a skill is <skills directory>/<name>/SKILL.md, "
                     "one level down; move its directory up to %s", path,
                     posixpath.join(root, posixpath.basename(posixpath.dirname(rel))))


def command_name(rel: str) -> str | None:
    """A commands directory's file's skill name (Claude Code's): its path
    without ``.md``, directories joined by ``:``; a ``SKILL.md`` is named by
    its directory. ``None`` when a part cannot name a skill."""
    parts = rel.split("/")
    if parts[-1] == "SKILL.md":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1][:-3]
    if not parts or not all(skill_dir_name(p) for p in parts):
        return None
    return ":".join(parts)


async def index_commands_dir(env: ExecEnv, root: str) -> dict[str, SkillDescription]:
    """Index a commands directory: every ``*.md`` below it is a skill (a
    directory holding a ``SKILL.md`` gives only that one), named by its path
    (:func:`command_name`)."""
    files = await _list_files(env, root)
    rels = sorted(r for r in (_rel(f, root) for f in files)
                  if r is not None and r.endswith(".md"))
    with_skill = {posixpath.dirname(r) for r in rels if posixpath.basename(r) == "SKILL.md"}
    skills: dict[str, SkillDescription] = {}
    for rel in rels:
        own = posixpath.basename(rel) == "SKILL.md"
        if not own and posixpath.dirname(rel) in with_skill:
            continue
        name = command_name(rel)
        path = posixpath.join(root, rel)
        if name is None or name in skills:
            continue
        raw = await read_text(env, path)
        if raw is None:
            continue
        desc = parse_skill(path, raw, name=name, own_dir=own)
        if desc is not None:
            skills[name] = desc
    return skills


async def load_registry(env: ExecEnv, tiers: Sequence[tuple[str, int]], *,
                        plugins: Mapping[str, str] | None = None,
                        scopes: Mapping[str, str] | None = None,
                        commands: Collection[str] = (),
                        variables: Mapping[str, tuple[tuple[str, str], ...]] | None = None,
                        extra: Sequence[SkillDescription] = (),
                        ) -> SkillRegistry:
    """Merge ``(directory, tier)`` pairs, highest precedence first: the
    first skill of a name wins (Claude Code keeps the first it loaded). ``plugins``:
    directory -> the plugin whose skills it holds. ``commands``: the
    directories that are commands directories (:func:`index_commands_dir`).
    ``variables``: directory -> what its skills' bodies fill in. ``scopes``: a skill
    directory found below the working directory -> the directory it serves
    (relative to the working directory); such a skill never shadows another:
    one whose name another skill has too is named ``<scope>:<name>``, as
    Claude Code names directory-scoped skills, and each one's description
    says which files it is for. Of one directory's two skill folders, the
    first given wins a name they share. ``extra``: skills that come from no
    directory (a plugin's commands), after the directories' (tier 0)."""
    plugins, scopes, variables = plugins or {}, scopes or {}, variables or {}
    merged: dict[str, SkillDescription] = {}
    tier_of: dict[str, int] = {}
    below: dict[str, tuple[str, int, dict[str, SkillDescription]]] = {}
    #: A command file in a subdirectory (``sp/brain.md`` -> ``sp:brain``),
    #: kept back until the names a plugin or a directory scope gives are
    #: known: no file may pass for one (and take on its rules).
    nested_commands: list[tuple[SkillDescription, int]] = []
    for root, tier in tiers:
        if root in commands:
            found = await index_commands_dir(env, root)
            nested_commands += [(d, tier) for n, d in found.items() if ":" in n]
            found = {n: d for n, d in found.items() if ":" not in n}
        else:
            found = await index_dir(env, root, plugins.get(root),
                                    variables=variables.get(root, ()))
        if root in scopes:
            scope = scopes[root]
            first_root, first_tier, have = below.get(scope, (root, tier, {}))
            below[scope] = (first_root, first_tier,
                            {**found, **have})  # the first folder keeps a shared name
            continue
        for name, desc in found.items():
            if name in merged:
                _log.info("skill: %s: %r is already %s; skipped", desc.source_path, name,
                          merged[name].source_path)
                continue
            merged[name] = desc
            tier_of[name] = tier
    for desc in extra:
        if desc.name not in merged:
            merged[desc.name] = desc
            tier_of[desc.name] = 0
    named = Counter(name for _root, _tier, found in below.values() for name in found)
    plain = set(merged)
    for scope, (root, tier, found) in below.items():
        for name, desc in sorted(found.items()):
            if name in plain or named[name] > 1:
                desc = replace(desc, name=f"{scope}:{name}", unqualified=name,
                               description=desc.description
                               + SCOPED_NOTE.format(scope=scope, name=name))
            else:
                skills_root = posixpath.dirname(posixpath.dirname(desc.source_path or root))
                where = posixpath.join(scope, posixpath.relpath(
                    skills_root, posixpath.dirname(posixpath.dirname(skills_root))))
                desc = replace(desc, description=desc.description
                               + FOUND_BELOW_NOTE.format(dir=where, scope=scope))
            if desc.name in merged:
                # Nested skills rank last: a scoped name never replaces a
                # skill already known by it (a plugin's ``<plugin>:<name>``).
                _log.warning("skill: %s: its name %r is taken by %s; skipped",
                             desc.source_path, desc.name, merged[desc.name].source_path)
                continue
            merged[desc.name] = desc
            tier_of[desc.name] = tier
    for desc, tier in nested_commands:
        if desc.name in merged:
            _log.warning("skill: %s: its name %r is taken by %s; skipped", desc.source_path,
                         desc.name, merged[desc.name].source_path)
            continue
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
#: The conversation's id (noeta: the task tree's root task id) and the
#: effort the skill runs at (Claude Code's).
ROOT_ID_VARIABLES = ("${CLAUDE_SESSION_ID}",)
EFFORT_VARIABLES = ("${NOETA_EFFORT}", "${CLAUDE_EFFORT}")


def substitute_variables(text: str, skill_dir: str | None, project_dir: str | None,
                         root_id: str | None = None, effort: str | None = None) -> str:
    """``text`` with the skill's directory, the project's (the task's
    working directory), the session id and the effort filled in; a variable
    with no value stays as written."""
    for names, value in ((SKILL_DIR_VARIABLES, skill_dir),
                         (PROJECT_DIR_VARIABLES, project_dir),
                         (ROOT_ID_VARIABLES, root_id),
                         (EFFORT_VARIABLES, effort)):
        if value:
            for name in names:
                text = text.replace(name, value)
    return text


def render_body(desc: SkillDescription, args: str | None = None,
                project_dir: str | None = None, root_id: str | None = None,
                effort: str | None = None) -> str:
    """The skill's body as the model gets it: its own variables (a plugin's
    root, data directory and options), the directory, session and effort
    variables, then the argument placeholders (an argument naming a
    variable stays as typed)."""
    body = desc.body
    for name, value in desc.variables:
        body = body.replace(name, value)
    body = substitute_variables(body, desc.base_dir, project_dir, root_id,
                                desc.effort or effort)
    escape = neutralise if has_commands(body) else None
    return substitute_arguments(body, args, desc.arguments, escape)


def render_command_body(desc: SkillDescription, args: str | None = None,
                        project_dir: str | None = None, root_id: str | None = None,
                        effort: str | None = None) -> str:
    """What a skill puts in the conversation when it loads (after the
    command tags of a typed ``/<name> <args>``, or after a ``Skill`` call's
    result): the skill's directory and its rendered body."""
    base = f"Base directory for this skill: {desc.base_dir}\n\n" if desc.base_dir else ""
    return base + render_body(desc, args, project_dir, root_id, effort).lstrip("\n")
