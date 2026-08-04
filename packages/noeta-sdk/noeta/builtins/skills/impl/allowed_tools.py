"""Skill ``allowed-tools`` resolution — the Claude→Noeta alias map + parser.

The permission guard operates only on **neutral** Noeta tool names, so the
product (Claude) vocabulary and its alias map live here, in the one layer that
knows both. Stdlib-only on purpose: nothing here may reach into the guard
implementations.
"""

from __future__ import annotations

import logging
from typing import Optional


__all__ = [
    "CLAUDE_TO_NOETA_TOOL",
    "parse_allowed_tools",
    "alias_to_noeta",
    "resolve_skill_allowed_tools",
]


_log = logging.getLogger(__name__)


#: The **exact 1:1** Claude→Noeta tool-name alias map. Claude tool names from a
#: skill's ``allowed-tools`` never enter the Noeta tool namespace; each expands
#: to its Noeta equivalent and an unknown Claude name grants nothing.
#: Deliberately narrow: ``Read`` maps to ``read`` alone (Claude has separate
#: ``Glob`` / ``Grep``), and Claude's ``LS`` has no Noeta equivalent, so an
#: ``LS`` token degrades the whole declaration.
CLAUDE_TO_NOETA_TOOL: dict[str, str] = {
    "Read": "Read",
    "Glob": "Glob",
    "Grep": "Grep",
    "Write": "write",
    "Edit": "edit",
    "Bash": "shell_run",
}


def parse_allowed_tools(value: str) -> Optional[frozenset[str]]:
    """Parse a skill's opaque ``allowed-tools`` string into **Claude** names.

    Accepts only the simple forms — an inline list ``[Read, Glob, Bash]`` or a
    bare comma list ``Read, Bash`` — and returns ``None`` for anything else
    (nested / quoted / colon / brace tokens); there is no YAML here. A caller
    must read ``None`` as a **fail-safe empty grant**, never as "all tools
    allowed".
    """
    inner = value.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    if inner.strip() == "":
        return frozenset()
    names: set[str] = set()
    for raw_tok in inner.split(","):
        tok = raw_tok.strip()
        if tok == "":
            continue
        if any(c in tok for c in " \t'\":{}[]"):
            return None
        names.add(tok)
    return frozenset(names)


def alias_to_noeta(claude_names: frozenset[str], *, skill: str) -> frozenset[str]:
    """Map Claude tool names to Noeta tool names via the exact 1:1 table.

    Fail-safe: if **any** entry has no Noeta mapping the **whole** declaration
    degrades to an empty grant. A typo in a security-relevant grant
    (``[Read, Bogus]``) must not silently keep the rest of it — gating every
    call until the typo is fixed is the safe direction.
    """
    unknown = sorted(n for n in claude_names if n not in CLAUDE_TO_NOETA_TOOL)
    if unknown:
        _log.warning(
            "skill %r: allowed-tools has unmapped entries %r; degrading "
            "the whole declaration to an empty grant (fail-safe)",
            skill,
            unknown,
        )
        return frozenset()
    return frozenset(CLAUDE_TO_NOETA_TOOL[n] for n in claude_names)


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
        parsed = parse_allowed_tools(raw_value)
        if parsed is None:
            _log.warning(
                "skill %r: unparseable allowed-tools value %r; "
                "granting nothing (enforcement stays on)",
                skill_name,
                raw_value,
            )
            resolved.append((skill_name, frozenset()))
        else:
            resolved.append((skill_name, alias_to_noeta(parsed, skill=skill_name)))
    return tuple(resolved)
