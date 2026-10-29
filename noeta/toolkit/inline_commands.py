"""Shell commands written into a skill or markdown command body, run when it
loads.

Claude Code's syntax: ``!`command``` inline (after a line start or
whitespace) and a fenced block opened with ```` ```! ````. Each command is
put to the task's permission checks as a ``Bash`` call; one that would be
refused or would need a person's answer stops the skill from loading (there
is nobody to ask while it loads). Allowed commands run through the task's
``Bash`` tool and their output takes the command's place in the text.
Arguments are filled in before the commands run, so an argument's ``!``
is neutralised first (:func:`neutralise`).
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass

from noeta.core.extension import Allow, Ask, Deny, Rewrite, TaskView
from noeta.core.tool import ToolContext
from noeta.core.types import ToolUse
from noeta.toolkit import permission_rules as pr
from noeta.toolkit.sandbox import run_in_tenant_sandbox

__all__ = [
    "DISABLED_MARKER",
    "InlineCommandError",
    "has_commands",
    "neutralise",
    "rules_cover",
    "run_inline_commands",
    "strip_commands",
]

#: Stands in for each command when a host turns skill commands off.
DISABLED_MARKER = "[shell command not run: skill commands are turned off here]"

_BLOCK = re.compile(r"```!\s*\n?([\s\S]*?)\n?```")
_INLINE = re.compile(r"(?:(?<=\s)|^)!`([^`]+)`", re.MULTILINE)
_CODE_SPAN = re.compile(r"`[^`\n]+`")


class InlineCommandError(Exception):
    """A command the skill names could not run; the skill does not load."""


@dataclass(frozen=True)
class _Found:
    start: int
    end: int
    command: str


def has_commands(text: str) -> bool:
    return "!`" in text or "```!" in text


def neutralise(value: str) -> str:
    """An argument value made inert before it is filled into a body whose
    commands run: it can neither open nor close an inline command."""
    value = value.replace("`!", "` !").replace("!`", "! `")
    return re.sub(r"(^|\s)!", r"\1\\!", value, flags=re.MULTILINE)


def _mask_code_spans(text: str) -> str:
    """``text`` with ordinary code spans blanked (same length), so a
    ``!`` inside one is not read as a command."""

    def blank(m: re.Match[str]) -> str:
        before = text[m.start() - 1] if m.start() else ""
        if before in ("!", "`"):
            return m.group(0)
        return "`" + " " * (len(m.group(0)) - 2) + "`"

    return _CODE_SPAN.sub(blank, text)


def _find(text: str) -> list[_Found]:
    found = [_Found(m.start(), m.end(), m.group(1)) for m in _BLOCK.finditer(text)]
    taken = [(f.start, f.end) for f in found]
    if "!`" in text:
        masked = _mask_code_spans(text)
        for m in _INLINE.finditer(masked):
            if any(s <= m.start() < e for s, e in taken):
                continue
            found.append(_Found(m.start(), m.end(), text[m.start(1):m.end(1)]))
    return sorted(found, key=lambda f: f.start)


def strip_commands(text: str, marker: str = DISABLED_MARKER) -> str:
    """Every command replaced by ``marker``, nothing run."""
    for f in reversed(_find(text)):
        text = text[:f.start] + marker + text[f.end:]
    return text


async def _allowed(view: TaskView, use: ToolUse, approve_rules: Sequence[str]) -> str | None:
    """``None`` when the task's checks let ``use`` run without asking anyone,
    else why not."""
    verdicts = []
    for ext in view.extensions:
        try:
            verdict = await ext.before_tool(view, use)
        except Exception as exc:  # noqa: BLE001 — a failing check refuses
            return f"permission check failed: {exc}"
        if verdict is not None:
            verdicts.append(verdict)
    for v in verdicts:
        if isinstance(v, Deny):
            return v.reason or "denied"
        if isinstance(v, Rewrite):
            return "a permission check would change the command"
    asks = [v for v in verdicts if isinstance(v, Ask)]
    if not asks:
        return None
    if not any(a.firm for a in asks) and (
            any(isinstance(v, Allow) and v.approve for v in verdicts)
            or rules_cover(view, use, approve_rules)):
        return None
    return asks[0].prompt or "it needs approval"


def rules_cover(view: TaskView, use: ToolUse, rules: Sequence[str]) -> bool:
    """Whether one of the permission rule texts ``rules`` allows ``use``
    (``*`` allows everything); a text that is no rule allows nothing."""
    if "*" in rules:
        return True
    cwd = view.cwd or os.getcwd()
    ctx = pr.RuleContext(root=cwd, home=os.path.expanduser("~"), cwd=cwd)
    for text in rules:
        try:
            if pr.match(pr.parse_rule(text), use.name, use.input, ctx, for_allow=True):
                return True
        except ValueError:
            continue
    return False


async def _run(view: TaskView, command: str, call_id: str) -> str:
    tool = view.find_tool("Bash")
    if tool is not None and view.env is not None:
        ctx = ToolContext(task_id=view.task_id, call_id=call_id, env=view.env,
                          state=view.state.ext, record=lambda *_a, **_k: None)
        output = await tool.run({"command": command}, ctx)
        return "\n".join(getattr(c, "text", "") for c in output.content).strip()
    if view.env is None:
        raise InlineCommandError("there is no environment to run it in")
    try:
        result = await run_in_tenant_sandbox(view.env, ["bash", "-c", command], timeout=120)
    except OSError as exc:
        raise InlineCommandError(str(exc)) from exc
    parts = [result.stdout.decode("utf-8", "replace").strip()]
    stderr = result.stderr.decode("utf-8", "replace").strip()
    if stderr:
        parts.append(f"[stderr]\n{stderr}")
    return "\n".join(p for p in parts if p)


async def run_inline_commands(view: TaskView, text: str, *, skill: str, call_id: str,
                              approve_rules: Sequence[str] = (), what: str = "skill") -> str:
    """``text`` with each command replaced by what it printed (``skill``
    names the ``what`` the text comes from in an error). Raises
    :class:`InlineCommandError` when one may not run; nothing runs then."""
    found = _find(text)
    if not found:
        return text
    for i, f in enumerate(found):
        use = ToolUse(id=f"{call_id}.cmd{i}", name="Bash", input={"command": f.command})
        why = await _allowed(view, use, approve_rules)
        if why is not None:
            raise InlineCommandError(
                f"the {what} {skill!r} runs `{f.command.strip()}` while it loads, and that "
                f"command may not run without asking: {why}")
    outputs = [await _run(view, f.command, f"{call_id}.cmd{i}") for i, f in enumerate(found)]
    for f, out in zip(reversed(found), reversed(outputs), strict=True):
        text = text[:f.start] + out + text[f.end:]
    return text
