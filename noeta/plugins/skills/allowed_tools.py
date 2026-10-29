"""A skill's ``allowed-tools``: the permission rules it pre-approves while the
skill is active, read as Claude Code reads them, and the tools they name
(the grant ``tool_enforcement`` holds the task to)."""

from __future__ import annotations

import logging

from noeta.toolkit import permission_rules as pr

__all__ = ["allow_rule_texts", "granted_tools"]


_log = logging.getLogger(__name__)


def _split_rule_entries(item: str) -> list[str]:
    """Entries of one string, split on commas and spaces outside a rule's
    parentheses (``Bash(git add:*) Read`` is two rules)."""
    out: list[str] = []
    current = ""
    depth = False
    for ch in item:
        if ch == "(":
            depth = True
        elif ch == ")":
            depth = False
        elif ch in ", " and not depth:
            if current.strip():
                out.append(current.strip())
            current = ""
            continue
        if current or ch.strip():
            current += ch
    if current.strip():
        out.append(current.strip())
    return out


def allow_rule_texts(value: str) -> tuple[str, ...]:
    """A skill's ``allowed-tools`` as permission rule texts, read as Claude
    Code reads them: a YAML list or one string of entries separated by
    commas or spaces (outside a rule's parentheses); ``*`` alone means every
    tool."""
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        items = [text[1:-1]]
    elif text.startswith("- "):
        items = [line.strip()[2:] for line in text.splitlines()
                 if line.strip().startswith("- ")]
    else:
        items = [text]
    rules: list[str] = []
    for item in items:
        for entry in _split_rule_entries(item):
            if len(entry) >= 2 and entry[0] == entry[-1] and entry[0] in "'\"":
                entry = entry[1:-1].strip()
            if entry:
                rules.append(entry)
    return ("*",) if "*" in rules else tuple(rules)


def granted_tools(value: str, *, skill: str = "<unknown>") -> tuple[str, ...]:
    """The tools a skill's ``allowed-tools`` names, as its rules name them
    (``Bash(git:*)`` grants ``Bash``: a rule's content narrows pre-approval,
    not the grant; ``*`` is every tool). An entry that is no rule grants
    nothing and is logged; the rest stand."""
    tools: list[str] = []
    for text in allow_rule_texts(value):
        if text == "*":
            return ("*",)
        try:
            tools.append(pr.parse_rule(text).tool)
        except ValueError as exc:
            _log.warning("skill %r: allowed-tools entry %r grants nothing: %s",
                         skill, text, exc)
    return tuple(sorted(set(tools)))
