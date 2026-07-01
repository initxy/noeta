"""Skill ``allowed-tools`` resolution — the Claude→Noeta alias map + parser.

Relocated from ``noeta.guards.permission`` (mechanism-vs-material):
the kernel guard operates only on **neutral** Noeta tool names, so the
product (Claude) vocabulary + its alias map belong in noeta-sdk, which knows
both vocabularies. A host (noeta-code wiring) calls
:func:`resolve_skill_allowed_tools` to turn the raw
``(skill, raw_allowed_tools_value)`` pairs it extracts from the
``SkillRegistry`` into the resolved
``(skill, frozenset_of_neutral_noeta_tool_names)`` pairs the guard's
``PermissionPolicy.skill_allowed_tools`` field expects.

Layering: this module imports only stdlib — it sits in the SDK
materials band (``noeta.policies``), above ``noeta.guards``.
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


#: The **exact 1:1** Claude→Noeta tool-name alias map (architect-pinned).
#: Claude tool names from a skill's ``allowed-tools`` never enter the Noeta
#: tool namespace; each expands to its Noeta equivalent. An unknown Claude
#: name grants nothing. NOT widened — ``Read`` is ``read`` only (Claude has
#: separate ``Glob`` / ``Grep``). ``list_dir`` retired, so Claude's
#: ``LS`` has no Noeta equivalent and is intentionally absent (an ``LS`` token
#: in a skill grant degrades the whole declaration, fail-safe).
CLAUDE_TO_NOETA_TOOL: dict[str, str] = {
    "Read": "read",
    "Glob": "glob",
    "Grep": "grep",
    "Write": "write",
    "Edit": "edit",
    "Bash": "shell_run",
}


def parse_allowed_tools(value: str) -> Optional[frozenset[str]]:
    """Parse a skill's opaque ``allowed-tools`` string into Claude tool
    names, conservatively.

    Accepts only the real-fixture simple forms — an inline list
    ``[Read, Glob, Bash]`` or a bare comma list ``Read, Bash``. Returns
    the frozenset of **Claude** names (alias-mapping to Noeta names is a
    separate step). Returns ``None`` on any form we don't recognise
    (nested/quoted/colon/brace tokens) — the caller treats ``None`` as a
    **fail-safe empty grant**, never as "all tools allowed". No YAML.
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
        # Reject anything that is not a bare identifier-ish token: a
        # space, quote, colon, or brace signals a form we won't parse.
        if any(c in tok for c in " \t'\":{}[]"):
            return None
        names.add(tok)
    return frozenset(names)


def alias_to_noeta(claude_names: frozenset[str], *, skill: str) -> frozenset[str]:
    """Map Claude tool names to Noeta tool names via the exact 1:1 table.

    Fail-safe: if **any** entry has no Noeta mapping, the **whole**
    declaration degrades to an empty grant (logged once). A typo in a
    security-relevant grant (``[Read, Bogus]``) must not silently
    preserve the rest of the grant — it gates *all* out-of-grant calls
    until fixed, rather than keeping ``read`` allowed.
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
    """Resolve raw ``(skill, raw_allowed_tools_value)`` pairs into the
    ``(skill, frozenset_of_neutral_noeta_tool_names)`` pairs the kernel
    guard's ``PermissionPolicy.skill_allowed_tools`` expects.

    A declaring skill always appears in the result — a malformed value
    maps to the empty frozenset (fail-safe: enforcement stays ON for that
    skill and it grants nothing), never to "all tools". The single
    diagnostic per skill is logged here (once at resolution), so the guard
    no longer needs to log.
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
