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
import shlex
from collections.abc import Sequence
from dataclasses import dataclass

from noeta.core.execenv import ExecEnv
from noeta.plugins.skills import _frontmatter
from noeta.toolkit.context import read_text

__all__ = [
    "ARGUMENTS_PLACEHOLDER",
    "DEFAULT_SKILL_PRIORITY",
    "SkillDescription",
    "SkillRegistry",
    "argument_names",
    "index_dir",
    "load_registry",
    "parse_list",
    "render_activation",
    "render_body",
    "render_command_body",
    "strip_argument_placeholder",
    "substitute_arguments",
    "substitute_variables",
]

_log = logging.getLogger(__name__)

_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")

#: Render-order default when ``priority:`` is absent or unreadable.
DEFAULT_SKILL_PRIORITY = 100

#: Claude Code's argument placeholder, filled by :func:`substitute_arguments`.
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
    #: ``when_to_use``: when the model should reach for the skill; the
    #: listing shows it after the description.
    when_to_use: str | None = None
    #: ``arguments``: names ``$name`` fills from the argument words, in order.
    arguments: tuple[str, ...] = ()

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
        when_to_use=fields.get("when_to_use") or None,
        arguments=argument_names(fields.get("arguments")),
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


#: Stands in for an escaped ``\$`` while placeholders are filled.
_ESCAPED_DOLLAR = "￿"


def _split_arguments(args: str) -> list[str]:
    try:
        return shlex.split(args)
    except ValueError:
        return args.split()


#: Claude Code's limits on ``arguments`` names: how many, and how long each.
MAX_ARGUMENT_NAMES = 64
MAX_ARGUMENT_NAME_LEN = 64


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


def argument_names(raw: str | None) -> tuple[str, ...]:
    """The ``arguments`` frontmatter: the names ``$name`` fills from the
    words of the arguments, in order (a name that is only digits is
    dropped, since ``$N`` is positional)."""
    return tuple(n for n in parse_list(raw) if not n.isdigit())


def substitute_arguments(body: str, args: str | None,
                         names: Sequence[str] = ()) -> str:
    """``body`` with its argument placeholders filled the way Claude Code
    fills them: ``$ARGUMENTS`` is the whole argument string,
    ``$ARGUMENTS[N]`` and ``$N`` the N-th (from 0) shell-quoted word (left as
    written when there is no such word), ``$<name>`` the word at that name's
    place in ``names`` (the skill's ``arguments``), and ``\\$`` keeps a
    ``$`` literal.
    When arguments are given but no placeholder took them, they follow the
    body on an ``ARGUMENTS:`` line. ``None`` (no arguments passed) leaves the
    body as written."""
    if args is None:
        return body
    words: list[str] | None = None

    def word(i: int) -> str | None:
        nonlocal words
        words = words if words is not None else _split_arguments(args)
        return words[i] if i < len(words) else None

    used = False

    def fill(value: str) -> str:
        nonlocal used
        used = True
        return value.replace("$", _ESCAPED_DOLLAR)

    kept = [(n, i) for i, n in enumerate(names) if n and len(n) <= MAX_ARGUMENT_NAME_LEN]
    # Longest first, so ``$file`` never eats the start of ``$filename``.
    named = sorted(kept[:MAX_ARGUMENT_NAMES], key=lambda item: -len(item[0]))
    escapable = "|".join([r"\d", "ARGUMENTS",
                          *(re.escape(n) + r"(?![\[\w])" for n, _ in named)])
    text = body.replace(_ESCAPED_DOLLAR, "�")
    text = re.sub(rf"(?<!\\)\\\$(?={escapable})", _ESCAPED_DOLLAR, text)
    for name, index in named:
        def by_name(_m: re.Match[str], index: int = index) -> str:
            return fill(word(index) or "")
        text = re.sub(r"\$" + re.escape(name) + r"(?![\[\w])", by_name, text)

    def indexed(m: re.Match[str]) -> str:
        found = word(int(m.group(1)))
        return m.group(0).replace("$", _ESCAPED_DOLLAR, 1) if found is None else fill(found)

    def positional(m: re.Match[str]) -> str:
        found = word(int(m.group(1)))
        return m.group(0) if found is None else fill(found)

    text = re.sub(r"\$ARGUMENTS\[(\d+)\]", indexed, text)
    text = re.sub(r"\$(\d+)(?!\w)", positional, text)
    text = re.sub(re.escape(ARGUMENTS_PLACEHOLDER), lambda _m: fill(args), text)
    if not used and args:
        text += f"\nARGUMENTS: {args}"
    return text.replace(_ESCAPED_DOLLAR, "$")


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
    return substitute_arguments(body, args, desc.arguments)


def render_activation(desc: SkillDescription, args: str | None = None,
                      project_dir: str | None = None) -> str:
    """The text an activated skill puts in the conversation, its body
    rendered by :func:`render_body`."""
    base = f"Base directory for this skill: {desc.base_dir}\n\n" if desc.base_dir else ""
    return (
        f"Activated skill: {desc.name}\n\n"
        f"{strip_argument_placeholder(desc.description)}\n\n"
        f"{base}{render_body(desc, args, project_dir)}"
    )


def render_command_body(desc: SkillDescription, args: str | None = None,
                        project_dir: str | None = None) -> str:
    """What ``/<name> <args>`` adds to the person's turn after the command
    tags: the skill's directory and its rendered body."""
    base = f"Base directory for this skill: {desc.base_dir}\n\n" if desc.base_dir else ""
    return f"{base}{render_body(desc, args, project_dir)}"
