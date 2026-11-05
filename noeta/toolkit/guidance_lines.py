"""The main prompt's ``# Session-specific guidance`` lines that hold only
for some tasks: Claude Code gives each line only when its condition holds
(the skill line with skills and the ``Skill`` tool, the ``!`` line in a
front end that runs ``!`` commands), and leaves the heading out when nothing
is left under it. The plugin that knows a line's condition drops it."""

from __future__ import annotations

import re

__all__ = ["HEADING", "drop_guidance_line"]

#: The section heading, at the start of a line.
HEADING = "# Session-specific guidance"
_SECTION = re.compile(r"^# Session-specific guidance\n((?: - .*\n?)*)\n?", re.MULTILINE)


def drop_guidance_line(text: str, mark: str) -> str:
    """``text`` without the line of its ``# Session-specific guidance``
    section that contains ``mark``, and without the section once it is
    empty (the blank line after it goes too)."""
    match = _SECTION.search(text)
    if match is None or mark not in match.group(1):
        return text
    kept = [line for line in match.group(1).splitlines(keepends=True) if mark not in line]
    if kept:
        body = "".join(kept)
        replacement = f"{HEADING}\n{body if body.endswith(chr(10)) else body + chr(10)}"
        if match.group(0).endswith("\n\n"):
            replacement += "\n"
    else:
        replacement = ""
    return text[:match.start()] + replacement + text[match.end():]
