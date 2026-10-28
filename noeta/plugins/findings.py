"""``ReportFindings``: a code review's findings handed to the front end as
typed data, as Claude Code's tool hands them to its UI.

The findings reach the person as a ``core`` / ``notice`` record of kind
``findings`` whose data is the call's input (``level``, ``findings``); a
front end renders that list, and one that does not shows the notice's text.
The model is told only how many were recorded.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from noeta.core.extension import Extension, TaskView
from noeta.core.tool import Tool, ToolContext, ToolOutput, text_output
from noeta.toolkit.tools import SCHEMA_DIALECT

__all__ = ["DESCRIPTION", "MAX_FINDINGS", "Findings", "ReportFindingsTool"]

NAME = "findings"
TOOL = "ReportFindings"
MAX_FINDINGS = 32
_LEVELS = ["low", "medium", "high", "xhigh", "max"]
_SAFE_INT = 9_007_199_254_740_991

DESCRIPTION = (
    "Hand a code review's findings to the host interface as a typed list it can display. "
    "Use it only when the code-review instructions you are following say to report "
    "findings with this tool; otherwise use the output format those instructions give. "
    "Call it once per review, with the findings that survived verification, most severe "
    "first (an empty list when none did), and do not repeat the findings as text "
    "afterwards. Report again after applying fixes only when the instructions for applying "
    "them ask for it, and then set each finding's `outcome` to what actually happened."
)

_FINDING = {
    "type": "object",
    "properties": {
        "file": {"type": "string",
                 "description": "Path of the file the finding is in, relative to the repository."},
        "line": {"type": "integer", "minimum": -_SAFE_INT, "maximum": _SAFE_INT,
                 "description": "The 1-based line the finding points at."},
        "summary": {"type": "string", "description": "The defect, in one sentence."},
        "short_summary": {"type": "string", "maxLength": 60, "description": (
            "A short label for compact views (at most 60 characters): the claim only, without "
            "its reason or consequence.")},
        "failure_scenario": {"type": "string", "description": (
            "The concrete input or state that leads to the wrong result or the crash.")},
        "category": {"type": "string", "maxLength": 40, "description": (
            'What kind of finding, as a short kebab-case word, such as "correctness", '
            '"simplification", "efficiency" or "test-coverage".')},
        "verdict": {"type": "string", "enum": ["CONFIRMED", "PLAUSIBLE"], "description": (
            "Set when a verification pass ran; left out for reviews without one.")},
        "outcome": {"type": "string", "enum": ["fixed", "skipped", "no_change_needed"],
                    "description": ("Only when reporting again after applying fixes: what "
                                    "became of this finding.")},
    },
    "required": ["file", "summary", "failure_scenario"],
    "additionalProperties": False,
}

SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DIALECT,
    "type": "object",
    "properties": {
        "level": {"type": "string", "enum": _LEVELS,
                  "description": "The effort level the review ran at."},
        "findings": {"type": "array", "items": _FINDING, "maxItems": MAX_FINDINGS,
                     "description": ("The verified findings, most severe first; empty when "
                                     "none survived.")},
    },
    "required": ["findings"],
    "additionalProperties": False,
}


def _plural(n: int) -> str:
    return "finding" if n == 1 else "findings"


class ReportFindingsTool:
    name = TOOL
    description = DESCRIPTION
    input_schema = SCHEMA
    read_only = True
    clearable = False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        findings = input.get("findings")
        if not isinstance(findings, list) or len(findings) > MAX_FINDINGS:
            return text_output(f"{TOOL}: `findings` must be a list of at most "
                               f"{MAX_FINDINGS} findings.", is_error=True)
        bad = [i for i, f in enumerate(findings) if not isinstance(f, dict)
               or not all(isinstance(f.get(k), str) for k in ("file", "summary",
                                                                "failure_scenario"))]
        if bad:
            return text_output(f"{TOOL}: finding {bad[0]} needs `file`, `summary` and "
                               "`failure_scenario`.", is_error=True)
        level = input.get("level") if input.get("level") in _LEVELS else None
        n = len(findings)
        lines = [f"Code review{f' ({level})' if level else ''}: {n} {_plural(n)}"]
        for f in findings:
            where = f"{f['file']}:{f['line']}" if isinstance(f.get("line"), int) else f["file"]
            lines.append(f"- {where} — {f.get('short_summary') or f['summary']}")
        ctx.notify("\n".join(lines), kind="findings", data={"level": level, "findings": findings})
        text = "Recorded no findings." if n == 0 else f"Recorded {n} {_plural(n)}."
        return text_output(text, count=n)


class Findings(Extension):
    """Offers ``ReportFindings``."""

    name = NAME

    def tools(self, view: TaskView) -> Sequence[Tool]:
        return (ReportFindingsTool(),)
