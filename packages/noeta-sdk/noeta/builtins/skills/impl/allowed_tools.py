"""Skill ``allowed-tools`` resolution — the recognised vocabulary + parser.

The permission guard operates only on **neutral** Noeta tool names, so the
model-visible tool vocabulary and the parser that reads a skill author's
declaration against it live here, in the one layer that knows both.
Stdlib-only on purpose: nothing here may reach into the guard implementations.

The vocabulary is a **recognition set**, not a translation table: since the tool
surface adopted the Claude Code names natively (see ``CONTEXT.md``,
"Model-visible tool names"), a declared name either is a tool this session can
mount or it is nothing. Recognition is per name — an entry outside the set is
dropped from the grant while the rest of the declaration stands — because
degrading the whole declaration punished every real Claude Code skill that
happened to name ``WebFetch`` or ``TodoWrite``.
"""

from __future__ import annotations

import logging
import re
from typing import Optional


__all__ = [
    "RECOGNIZED_TOOL_NAMES",
    "parse_allowed_tools",
    "filter_recognized_tools",
    "resolve_skill_allowed_tools",
]


_log = logging.getLogger(__name__)


#: Every model-visible tool name a session can mount, across the built-ins that
#: contribute one: ``fs`` (read/edit/shell), ``web``, the control tools, and the
#: activation-gated ``memory`` / ``browser`` / ``app`` / ``skills`` packs. A
#: name outside this set cannot name a tool the guard will ever see, so granting
#: it would be granting nothing under a plausible-looking spelling.
#:
#: MCP tools are deliberately absent: their names are host-configured aliases
#: (``<alias>__<tool>``), not a fixed vocabulary, so no static set can carry
#: them.
RECOGNIZED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        # fs
        "Read",
        "Glob",
        "Grep",
        "Edit",
        "Write",
        "Bash",
        "BashOutput",
        "KillShell",
        # web
        "WebFetch",
        "WebSearch",
        # control tools
        "TodoWrite",
        "AskUserQuestion",
        "Task",
        "skill",
        # skills
        "run_skill_script",
        # app
        "open_app",
        # memory
        "memory_write",
        "memory_read",
        "memory_search",
        "memory_archive",
        # browser
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_extract",
        "browser_screenshot",
    }
)


#: ``Bash(git:*)`` — a Claude Code argument-level spec. The name is honoured,
#: the parenthesised spec is not (that boundary is the shell allowlist's).
_SPEC_ENTRY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\((.*)\)$", re.DOTALL)

#: A bare tool name. Anything else in a token (quotes, a colon, a brace, an
#: inner space) means a syntax this parser does not read, and the whole value
#: degrades — there is no YAML here.
_BARE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


def parse_allowed_tools(
    value: str, *, skill: str = "<unknown>"
) -> Optional[frozenset[str]]:
    """Parse a skill's opaque ``allowed-tools`` string into tool names.

    Accepts the simple forms — an inline list ``[Read, Glob, Bash]``, a bare
    comma list ``Read, Bash`` — plus Claude Code's ``Name(spec)`` entries
    (``Bash(git:*)``), which parse to ``Name`` with a warning: argument-level
    specs are **not** enforced here, because narrowing *what* a command may do
    is the shell allowlist's job, not a frontmatter grant's.

    Returns ``None`` for anything else (nested / quoted / colon / brace tokens).
    A caller must read ``None`` as a **fail-safe empty grant**, never as "all
    tools allowed".
    """
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    if inner.strip() == "":
        return frozenset()
    names: set[str] = set()
    for raw_tok in _split_entries(inner):
        tok = raw_tok.strip()
        if tok == "":
            continue
        spec = _SPEC_ENTRY.match(tok)
        if spec is not None:
            base = spec.group(1)
            _log.warning(
                "skill %r: allowed-tools entry %r carries an argument-level "
                "spec; granting %r — argument-level specs are not enforced by "
                "this grant (the shell allowlist is that boundary)",
                skill,
                tok,
                base,
            )
            names.add(base)
            continue
        if _BARE_NAME.match(tok) is None:
            return None
        names.add(tok)
    return frozenset(names)


def _split_entries(inner: str) -> list[str]:
    """Split on commas that are **not** inside a ``(...)`` argument spec, so
    ``Bash(git status:*), Read`` yields two entries instead of tearing the spec
    in half at its own comma."""
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in inner:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(ch)
    parts.append("".join(current))
    return parts


def filter_recognized_tools(
    names: frozenset[str], *, skill: str
) -> frozenset[str]:
    """Keep the entries naming a tool a session can mount, drop the rest.

    Fail-safe **per name**: an unrecognised entry grants nothing and is logged,
    while the recognised entries stand and enforcement stays ON for the
    declaring skill. The alternative — degrading the whole declaration to an
    empty grant on one unknown token — gated every legitimate call of a skill
    that merely named a tool this build does not mount, which is a harsher
    penalty than the typo it was meant to catch.
    """
    unknown = sorted(n for n in names if n not in RECOGNIZED_TOOL_NAMES)
    if unknown:
        _log.warning(
            "skill %r: allowed-tools names %r are not tools this session can "
            "mount; dropping them from the grant (the rest stands)",
            skill,
            unknown,
        )
    return frozenset(n for n in names if n in RECOGNIZED_TOOL_NAMES)


def resolve_skill_allowed_tools(
    raw_pairs: tuple[tuple[str, str], ...],
) -> tuple[tuple[str, frozenset[str]], ...]:
    """Resolve raw ``(skill, raw_allowed_tools_value)`` pairs into the neutral
    grants the guard's ``PermissionPolicy.skill_allowed_tools`` expects.

    A declaring skill always appears in the result: a malformed value maps to
    the empty frozenset — enforcement stays ON for that skill and it grants
    nothing — never to "all tools". Diagnostics are logged once here, at
    resolution, so the guard itself stays silent.
    """
    resolved: list[tuple[str, frozenset[str]]] = []
    for skill_name, raw_value in raw_pairs:
        parsed = parse_allowed_tools(raw_value, skill=skill_name)
        if parsed is None:
            _log.warning(
                "skill %r: unparseable allowed-tools value %r; "
                "granting nothing (enforcement stays on)",
                skill_name,
                raw_value,
            )
            resolved.append((skill_name, frozenset()))
        else:
            resolved.append(
                (skill_name, filter_recognized_tools(parsed, skill=skill_name))
            )
    return tuple(resolved)
