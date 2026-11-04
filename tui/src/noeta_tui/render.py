"""How the terminal draws what a task recorded, in Claude Code's shapes, as
plain functions: a tool call's ``Name(argument)`` header and its ``⎿``
result block (diffs with line numbers, long output folded), the task list,
the spinner line, the footer's mode hint and ``/context``'s grid."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from rich.text import Text

from noeta.core.types import ToolUse

#: Claude Code's names for the tools whose user-facing name differs.
DISPLAY_NAMES = {"Edit": "Update", "MultiEdit": "Update", "NotebookEdit": "Update",
                 "Grep": "Search", "Glob": "Search", "WebFetch": "Fetch",
                 "WebSearch": "Web Search"}
#: Lines of a folded result after its first; ctrl+o shows the rest.
FOLDED_LINES = 2
#: Diff lines shown while folded.
FOLDED_DIFF_LINES = 24
#: Characters of a result shown at most, even unfolded.
RESULT_CHARS = 20000
#: Lines of a running call's latest output shown under it.
PROGRESS_LINES = 5
#: Claude Code's spinner glyphs and the verbs it shows when nothing says
#: what is being done (an in-progress task's ``activeForm`` does).
SPINNER_FRAMES = "·✢✳✶✻✽✻✶✳✢"
VERBS = ("Thinking", "Working", "Pondering", "Musing", "Computing", "Brewing",
         "Crafting", "Noodling", "Percolating", "Tinkering")
#: The footer's line per permission mode (Claude Code's); the default
#: mode shows the shortcuts hint.
MODE_LINES = {"acceptEdits": "⏵⏵ accept edits on", "plan": "⏸ plan mode on",
              "auto": "⏵⏵ auto mode on", "dontAsk": "⏵⏵ don't ask on",
              "bypassPermissions": "⏵⏵ bypass permissions on"}
MODE_STYLES = {"acceptEdits": "magenta", "plan": "cyan", "auto": "yellow",
               "dontAsk": "red", "bypassPermissions": "red"}
#: ``/context``'s categories: colour per name (the rest are grey).
CONTEXT_COLOURS = {"System prompt": "bright_black", "System tools": "cyan",
                   "MCP tools": "magenta", "Messages": "bright_magenta"}
_HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def _main_argument(call: ToolUse) -> str:
    i = call.input
    if call.name in ("Grep", "Glob"):
        arg = f'pattern: "{i.get("pattern", "")}"'
        return arg + (f', path: "{i["path"]}"' if i.get("path") else "")
    for key in ("file_path", "notebook_path", "path", "command", "url", "query",
                "subject", "description", "prompt", "skill", "taskId"):
        if i.get(key):
            return str(i[key])
    return ""


def tool_name(call: ToolUse) -> str:
    """``Update`` for ``Edit``; ``github - get_issue (MCP)`` for an MCP tool."""
    if call.name.startswith("mcp__"):
        server, _, tool = call.name[5:].partition("__")
        return f"{server} - {tool} (MCP)" if tool else call.name
    return DISPLAY_NAMES.get(call.name, call.name)


def tool_header(call: ToolUse, limit: int = 100) -> str:
    """``Bash(ls -la)``: the name and the main argument's first line."""
    arg = _main_argument(call)
    lines = arg.splitlines()
    first = lines[0] if lines else ""
    if len(first) > limit or len(lines) > 1:
        first = first[:limit].rstrip() + "…"
    return f"{tool_name(call)}({first})" if first else tool_name(call)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def change_summary(call: ToolUse, meta: Mapping[str, Any]) -> str:
    """``Added 1 line, removed 1 line`` (``Wrote 3 lines to a.py`` for a new file)."""
    added, removed = int(meta.get("added") or 0), int(meta.get("removed") or 0)
    if call.name == "Write" and removed == 0:
        return f"Wrote {_plural(added, 'line')} to {meta.get('path') or _main_argument(call)}"
    parts = []
    if added:
        parts.append(f"added {_plural(added, 'line')}")
    if removed:
        parts.append(f"removed {_plural(removed, 'line')}")
    text = ", ".join(parts) or "no lines changed"
    return text[0].upper() + text[1:]


def diff_rows(diff: str) -> list[tuple[str, str]]:
    """A unified diff as numbered rows ``(text, style)``: removed lines red,
    added green, hunks apart by ``...``."""
    rows: list[tuple[str, str]] = []
    old = new = 0
    for line in diff.splitlines():
        if line.startswith(("---", "+++")):
            continue
        if (m := _HUNK.match(line)) is not None:
            if rows:
                rows.append(("   ...", "dim"))
            old, new = int(m.group(1)), int(m.group(2))
        elif line.startswith("-"):
            rows.append((f"{old:>4} -{line[1:]}", "red"))
            old += 1
        elif line.startswith("+"):
            rows.append((f"{new:>4} +{line[1:]}", "green"))
            new += 1
        elif line.startswith(" ") or not line:
            rows.append((f"{new:>4}  {line[1:]}", ""))
            old += 1
            new += 1
    return rows


def result_rows(call: ToolUse, text: str, meta: Mapping[str, Any], *,
                is_error: bool) -> list[tuple[str, str]]:
    """A finished call's result as ``(text, style)`` rows, its summary first."""
    diff = meta.get("diff")
    if isinstance(diff, str) and diff and not is_error:
        return [(change_summary(call, meta), ""), *diff_rows(diff)]
    lines = text[:RESULT_CHARS].rstrip("\n").splitlines()
    if is_error:
        return [(line, "red") for line in lines] or [("Error", "red")]
    if not text.strip():
        return [("(No content)", "dim")]
    if call.name == "Read":
        return [(f"Read {_plural(len(lines), 'line')}", ""), *((line, "dim") for line in lines)]
    return [(line, "") for line in lines]


def folded(rows: list[tuple[str, str]], *, diff: bool, expanded: bool,
           read: bool = False) -> list[tuple[str, str]]:
    """``rows`` cut to what a folded call shows, the cut named
    (``… +12 lines (ctrl+o to expand)``)."""
    if expanded:
        return rows
    if read and len(rows) > 1:  # Claude Code: ``Read 20 lines (ctrl+o to expand)``
        return [(f"{rows[0][0]} (ctrl+o to expand)", rows[0][1])]
    keep = 1 + (FOLDED_DIFF_LINES if diff else FOLDED_LINES)
    if len(rows) <= keep:
        return rows
    hidden = len(rows) - keep
    return [*rows[:keep], (f"… +{_plural(hidden, 'line')} (ctrl+o to expand)", "dim")]


def result_block(rows: Iterable[tuple[str, str]]) -> Text:
    """Rows under a call: the first after ``⎿``, the rest indented."""
    out = Text()
    for n, (line, style) in enumerate(rows):
        out.append("\n" if n else "")
        out.append("  ⎿  " if n == 0 else "     ", style="dim")
        out.append(line, style=style)
    return out


def progress_block(text: str) -> Text:
    """A running call's latest output: its last lines, dim."""
    lines = text.rstrip("\n").splitlines()[-PROGRESS_LINES:]
    return result_block((line, "dim") for line in lines)


# ------------------------------------------------------------- task list


def fold_todos(items: dict[str, dict[str, Any]], kind: str, data: Any) -> None:
    """Apply one of the todos plugin's records (``created`` / ``updated`` /
    ``deleted``) to ``items`` (id -> fields)."""
    if not isinstance(data, dict) or "id" not in data:
        return
    key = str(data["id"])
    if kind == "created":
        items[key] = {"subject": str(data.get("subject") or ""), "status": "pending",
                      "active_form": data.get("active_form")}
    elif kind == "updated" and key in items:
        items[key].update(data.get("fields") or {})
    elif kind == "deleted":
        items.pop(key, None)


TODO_MARKS = {"pending": ("◻", ""), "in_progress": ("◼", "bold"),
              "completed": ("✔", "dim strike")}


def todo_block(items: Mapping[str, Mapping[str, Any]]) -> Text:
    """The task list, Claude Code's marks: ``◻`` to do, ``◼`` doing, ``✔`` done."""
    out = Text()
    for n, item in enumerate(items.values()):
        mark, style = TODO_MARKS.get(str(item.get("status")), ("◻", ""))
        out.append("\n" if n else "")
        out.append(f"  {'⎿' if n == 0 else ' '}  {mark} ", style="dim")
        out.append(str(item.get("subject") or ""), style=style)
    return out


def active_form(items: Mapping[str, Mapping[str, Any]]) -> str:
    """What the in-progress task says it is doing (``Running tests``), or ``""``."""
    for item in items.values():
        if item.get("status") == "in_progress":
            return str(item.get("active_form") or item.get("subject") or "")
    return ""


# ------------------------------------------------------------- status lines


def tokens_text(n: int) -> str:
    """``950``, ``46.8k``, ``1.2m``."""
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".") + "m"


def elapsed_text(seconds: float) -> str:
    """``12s``, ``3m 5s``, ``1h 2m``."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60}s"
    return f"{s // 3600}h {s % 3600 // 60}m"


def spinner_line(frame: int, verb: str, elapsed: float, tokens: int) -> Text:
    """``✻ Thinking… (12s · ↓ 1.2k tokens · esc to interrupt)``."""
    out = Text()
    out.append(SPINNER_FRAMES[frame % len(SPINNER_FRAMES)] + " ", style="bold #d77757")
    out.append(f"{verb}…", style="#d77757")
    detail = elapsed_text(elapsed)
    if tokens:
        detail += f" · ↓ {tokens_text(tokens)} tokens"
    out.append(f" ({detail} · esc to interrupt)", style="dim")
    return out


def mode_hint(mode: str) -> Text:
    """The footer's left side: the mode when it is not the default, else
    ``? for shortcuts``."""
    line = MODE_LINES.get(mode)
    if line is None:
        return Text("? for shortcuts", style="dim")
    out = Text().append(line, style=MODE_STYLES.get(mode, ""))
    out.append(" (shift+tab to cycle)", style="dim")
    return out


# ------------------------------------------------------------- /context

#: Cells of the grid, each a hundredth of the window.
GRID = 10


def context_grid(breakdown: Mapping[str, Any]) -> Text:
    """``/context``: a 10x10 grid of the window, one cell per hundredth
    (``⛁`` used, ``⛶`` free), with the categories beside it."""
    window = max(int(breakdown.get("window") or 0), 1)
    total = int(breakdown.get("total") or 0)
    categories = dict(breakdown.get("categories") or {})
    free = categories.pop("Free space", max(window - total, 0))
    cells: list[tuple[str, str]] = []
    for name, tokens in categories.items():
        if tokens > 0:
            n = max(1, round(tokens / window * GRID * GRID))
            cells += [("⛁", CONTEXT_COLOURS.get(name, "white"))] * n
    cells = cells[:GRID * GRID]
    cells += [("⛶", "dim")] * (GRID * GRID - len(cells))
    legend: list[tuple[str, str]] = [
        (str(breakdown.get("model") or ""), "bold"),
        (f"{tokens_text(total)}/{tokens_text(window)} tokens ({total / window:.0%})", ""),
        ("", ""), ("Estimated usage by category", "italic")]
    for name, tokens in categories.items():
        legend.append((f"⛁ {name}: {tokens_text(tokens)} tokens ({tokens / window:.1%})",
                       CONTEXT_COLOURS.get(name, "white")))
    legend.append((f"⛶ Free space: {tokens_text(free)} ({free / window:.1%})", "dim"))
    if breakdown.get("deferred_tools"):
        legend.append((f"{breakdown['deferred_tools']} deferred tools load on demand", "dim"))
    out = Text().append("Context Usage\n", style="bold")
    for row in range(max(GRID, len(legend))):
        if row:
            out.append("\n")
        for col in range(GRID):
            if row < GRID:
                glyph, style = cells[row * GRID + col]
                out.append(glyph + " ", style=style)
            else:
                out.append("  ")
        if row < len(legend):
            out.append("  ")
            out.append(*legend[row])
    return out
