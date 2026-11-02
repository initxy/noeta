"""The main prompt's skill line (``# Session-specific guidance``): Claude
Code gives it only when there are skills and the ``Skill`` tool is offered,
and leaves the heading out when nothing is left under it."""

from __future__ import annotations

import re

#: The section heading, at the start of a line.
HEADING = "# Session-specific guidance"
#: The skill line names the typed form it is about.
_MARK = "`/<skill-name>`"
_SECTION = re.compile(r"^# Session-specific guidance\n((?: - .*\n?)*)\n?", re.MULTILINE)


def drop_skill_guidance(text: str) -> str:
    """``text`` without the skill line of its ``# Session-specific
    guidance`` section, and without the section once it is empty (the blank
    line after it goes too)."""
    match = _SECTION.search(text)
    if match is None or _MARK not in match.group(1):
        return text
    kept = [line for line in match.group(1).splitlines(keepends=True) if _MARK not in line]
    if kept:
        body = "".join(kept)
        replacement = f"{HEADING}\n{body if body.endswith(chr(10)) else body + chr(10)}"
        if match.group(0).endswith("\n\n"):
            replacement += "\n"
    else:
        replacement = ""
    return text[:match.start()] + replacement + text[match.end():]
