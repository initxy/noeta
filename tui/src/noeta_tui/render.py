"""How the terminal draws what a task recorded, in Claude Code's shapes, as
plain functions: a tool call's ``Name(argument)`` header and its ``⎿``
result block (diffs with line numbers, long output folded), the task list,
the spinner line, the footer's mode hint and ``/context``'s grid."""

from __future__ import annotations

import difflib
import os
import re
import shlex
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Any

from rich.console import Console, ConsoleOptions, RenderResult
from rich.measure import Measurement
from rich.text import Text

from noeta.core.types import ToolUse

#: Claude Code's names for the tools whose user-facing name differs.
DISPLAY_NAMES = {"Edit": "Update", "MultiEdit": "Update", "NotebookEdit": "Update",
                 "Grep": "Search", "Glob": "Search", "WebFetch": "Fetch",
                 "WebSearch": "Web Search"}
#: What a folded line says opens it (a click on it does too).
EXPAND_HINT = "(ctrl+o to expand)"
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
#: A terminal control sequence (CSI, OSC, DCS / SOS / PM / APC, a two-byte
#: escape) or any other control character but newline and tab, 8-bit ones
#: included.
_CONTROL = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]?"
                      r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"
                      r"|\x1b[PX^_][^\x1b]*(?:\x1b\\)?"
                      r"|\x1b[ -~]?"
                      r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def clean(text: str) -> str:
    """``text`` as it may reach the terminal: without control sequences.
    What a task recorded (a tool's output, the model's text, a hook's or a
    provider's error) is shown through this: an escape in it would write
    the clipboard (OSC 52), hide a link (OSC 8) or clear the screen."""
    return _CONTROL.sub("", text)


def plain(text: str, style: str = "") -> Text:
    """``text`` cleaned, as plain text: never read as markup."""
    return Text(clean(text), style=style)


def display_path(path: str, cwd: str) -> str:
    """``path`` as a call's line shows it: relative to the task's directory
    ``cwd`` when under it (Claude Code's ``Update(calc.py)``), the home
    directory as ``~``, else as it is."""
    if cwd and os.path.isabs(path):
        try:
            return str(PurePath(path).relative_to(cwd))
        except ValueError:  # outside the task's directory
            pass
    home = os.path.expanduser("~")
    if home not in ("", "~", os.sep) and (path == home or path.startswith(home + os.sep)):
        return "~" + path[len(home):]
    return path


#: The arguments that name a file or directory.
PATH_KEYS = ("file_path", "notebook_path", "path")


def _main_argument(call: ToolUse, cwd: str = "") -> str:
    i = call.input
    if call.name in ("Grep", "Glob"):
        arg = f'pattern: "{i.get("pattern", "")}"'
        return arg + (f', path: "{display_path(str(i["path"]), cwd)}"' if i.get("path") else "")
    for key in (*PATH_KEYS, "command", "url", "query",
                "subject", "description", "prompt", "skill", "taskId"):
        if i.get(key):
            return display_path(str(i[key]), cwd) if key in PATH_KEYS else str(i[key])
    return ""


def tool_name(call: ToolUse) -> str:
    """``Update`` for ``Edit``; ``github - get_issue (MCP)`` for an MCP tool."""
    if call.name.startswith("mcp__"):
        server, _, tool = call.name[5:].partition("__")
        return clean(f"{server} - {tool} (MCP)" if tool else call.name)
    return clean(DISPLAY_NAMES.get(call.name, call.name))


def tool_header(call: ToolUse, limit: int = 100, cwd: str = "") -> str:
    """``Bash(ls -la)``: the name and the main argument's first line, a
    path relative to the task's directory ``cwd`` when under it."""
    arg = clean(_main_argument(call, cwd))
    lines = arg.splitlines()
    first = lines[0] if lines else ""
    if len(first) > limit or len(lines) > 1:
        first = first[:limit].rstrip() + "…"
    return f"{tool_name(call)}({first})" if first else tool_name(call)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def change_summary(call: ToolUse, meta: Mapping[str, Any], cwd: str = "") -> str:
    """``Added 1 line, removed 1 line`` (``Wrote 3 lines to a.py`` for a new file)."""
    added, removed = int(meta.get("added") or 0), int(meta.get("removed") or 0)
    if call.name == "Write" and removed == 0:
        path = display_path(str(meta["path"]), cwd) if meta.get("path") \
            else _main_argument(call, cwd)
        return f"Wrote {_plural(added, 'line')} to {path}"
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
                is_error: bool, cwd: str = "") -> list[tuple[str, str]]:
    """A finished call's result as ``(text, style)`` rows, its summary first."""
    diff = meta.get("diff")
    if isinstance(diff, str) and diff and not is_error:
        return [(change_summary(call, meta, cwd), ""), *diff_rows(diff)]
    lines = text[:RESULT_CHARS].rstrip("\n").splitlines()
    if is_error:
        return [(line, "red") for line in lines] or [("Error", "red")]
    if not text.strip():
        return [("(No content)", "dim")]
    if call.name == "Read":
        return [(f"Read {_plural(len(lines), 'line')}", ""), *((line, "dim") for line in lines)]
    return [(line, "") for line in lines]


#: Diff lines an approval dialog shows of the edit it asks about.
PREVIEW_LINES = 30


def edit_preview(tool: str, input: Mapping[str, Any], current: str | None
                 ) -> list[tuple[str, str]]:
    """The numbered diff an ``Edit`` or ``Write`` would make, for its
    approval dialog (``[]`` for any other tool); ``current``: the file's
    text now (``None``: absent or not readable here, so an edit's lines
    are numbered from its own first line)."""
    if tool == "Write":
        before, after = current or "", str(input.get("content") or "")
    elif tool == "Edit":
        old, new = str(input.get("old_string") or ""), str(input.get("new_string") or "")
        if current is not None and old and old in current:
            before = current
            after = current.replace(old, new) if input.get("replace_all") \
                else current.replace(old, new, 1)
        else:
            before, after = old, new
    else:
        return []
    diff = "\n".join(difflib.unified_diff(before.splitlines(), after.splitlines(),
                                            lineterm=""))
    rows = diff_rows(diff)
    if len(rows) > PREVIEW_LINES:
        rows = [*rows[:PREVIEW_LINES], (f"… +{_plural(len(rows) - PREVIEW_LINES, 'line')}",
                                        "dim")]
    return rows


def folded(rows: list[tuple[str, str]], *, diff: bool, expanded: bool,
           read: bool = False) -> list[tuple[str, str]]:
    """``rows`` cut to what a folded call shows, the cut named
    (``… +12 lines (ctrl+o to expand)``)."""
    if expanded:
        return rows
    if read and len(rows) > 1:  # Claude Code: ``Read 20 lines (ctrl+o to expand)``
        return [(f"{rows[0][0]} {EXPAND_HINT}", rows[0][1])]
    keep = 1 + (FOLDED_DIFF_LINES if diff else FOLDED_LINES)
    if len(rows) <= keep:
        return rows
    hidden = len(rows) - keep
    return [*rows[:keep], (f"… +{_plural(hidden, 'line')} {EXPAND_HINT}", "dim")]


# ------------------------------------------------------------- runs of calls

#: What the line of a run of quiet calls counts a tool as; any other tool
#: not in :data:`OWN_LINE` counts as ``tool``, an MCP server's as ``mcp``,
#: a shell command by what it runs (:func:`shell_kind`).
CALL_KINDS = {"Grep": "search", "Glob": "search", "Read": "read", "LS": "list",
              "WebFetch": "fetch", "WebSearch": "web", "Bash": "shell", "PowerShell": "shell"}
#: The tools shown on their own, never in a run: an edit keeps its diff, an
#: agent or a workflow its progress; the task list, a question, a plan, a skill.
OWN_LINE = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit", "Agent", "Task", "Workflow",
                      "TodoWrite", "TaskCreate", "TaskGet", "TaskUpdate", "TaskList",
                      "AskUserQuestion", "ExitPlanMode", "EnterPlanMode", "Skill"})
#: The edits; consecutive ones to one file are one line.
EDIT_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "NotebookEdit"})
#: The agents; a reply's agents started together are one line.
AGENT_TOOLS = frozenset({"Agent", "Task"})
#: Calls a run goes on past (the task list's): they do not end it.
PASSING = frozenset({"TodoWrite", "TaskCreate", "TaskGet", "TaskUpdate", "TaskList"})
#: Per kind, in the order the line names them (Claude Code's): the verb
#: while a call runs, once all are done, and the thing counted (one, many).
RUN_WORDS = (("search", "searching for", "searched for", "pattern", "patterns"),
             ("read", "reading", "read", "file", "files"),
             ("list", "listing", "listed", "directory", "directories"),
             ("fetch", "fetching", "fetched", "page", "pages"),
             ("web", "running", "ran", "web search", "web searches"),
             ("tool", "calling", "called", "tool", "tools"),
             ("shell", "running", "ran", "shell command", "shell commands"))
#: Programs that only read (``cat``), search (``grep``) or list (``ls``):
#: a shell command made of them counts as what it does.
READ_PROGRAMS = frozenset({"cat", "head", "tail", "less", "more", "wc", "stat", "file", "nl",
                           "strings", "od", "xxd", "hexdump", "bat"})
SEARCH_PROGRAMS = frozenset({"grep", "egrep", "fgrep", "rg", "ag", "ack", "find", "fd", "locate",
                             "which", "whereis"})
LIST_PROGRAMS = frozenset({"ls", "tree", "du", "df", "pwd"})
#: git's subcommands that only read.
GIT_READS = frozenset({"status", "log", "diff", "show", "blame", "rev-parse", "ls-files",
                       "shortlog", "describe"})
#: ``find``'s actions that change or run something.
_FIND_ACTIONS = frozenset({"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fls"})
_DISCARDED = re.compile(r"\s\d?>\s*/dev/null|\s2>&1")


def shell_kind(command: str) -> str:
    """What a shell command counts as in a run: ``read``, ``search`` or
    ``list`` when every part of it only does that (a pipe of ``grep`` and
    ``head`` searches), else ``shell``."""
    text = _DISCARDED.sub(" ", command)
    if re.search(r"[<>`]|\$\(", text):
        return "shell"
    kinds: list[str] = []
    for part in re.split(r"\|\||&&|[|;\n&]", text):
        try:
            words = shlex.split(part)
        except ValueError:
            return "shell"
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words.pop(0)
        if not words:
            continue
        program = os.path.basename(words[0])
        if program == "cd":
            continue
        if program == "git":
            sub = next((w for w in words[1:] if not w.startswith("-")), "")
            kind = "read" if sub in GIT_READS else "search" if sub == "grep" else "shell"
        elif program == "find" and _FIND_ACTIONS.intersection(words):
            kind = "shell"
        elif program in READ_PROGRAMS:
            kind = "read"
        elif program in SEARCH_PROGRAMS:
            kind = "search"
        elif program in LIST_PROGRAMS:
            kind = "list"
        else:
            return "shell"
        kinds.append(kind)
    return kinds[0] if kinds else "shell"


def call_kind(call: ToolUse) -> str:
    """What a run of quiet calls counts ``call`` as (``read``, ``shell``,
    ``mcp``, …); ``""`` for a call shown on its own."""
    if call.name in OWN_LINE:
        return ""
    if call.name.startswith("mcp__"):
        return "mcp"
    kind = CALL_KINDS.get(call.name, "tool")
    if kind == "shell":
        return shell_kind(str(call.input.get("command") or ""))
    return kind


def call_key(call: ToolUse) -> str:
    """What makes two calls of a kind one: a read's file (the same file read
    twice is one), an MCP tool's server; else the call itself."""
    if call.name.startswith("mcp__"):
        return call.name[5:].partition("__")[0]
    if call.name == "Read":
        return str(call.input.get("file_path") or call.id)
    return call.id


_COMMITTED = re.compile(r"^\[(\S+?)(?: \(root-commit\))? ([0-9a-f]{7,})\]", re.MULTILINE)
_PUSHED = re.compile(r"\S+\s+->\s+(\S+)")
_PULL = re.compile(r"/pull/(\d+)")


def git_ops(call: ToolUse, output: str) -> list[tuple[str, str]]:
    """What a finished shell command did to the repository, in Claude
    Code's words: ``("committed", "a1b2c3d")``, ``("pushed to", "main")``,
    ``("created", "PR #12")``; ``[]`` for anything else."""
    command = str(call.input.get("command") or "") if call.name in ("Bash", "PowerShell") else ""
    out: list[tuple[str, str]] = []
    if re.search(r"\bgit\b[^|;&]*\bcommit\b", command) and (m := _COMMITTED.search(output)):
        verb = "amended commit" if "--amend" in command else "committed"
        out.append((verb, m.group(2)[:7]))
    if re.search(r"\bgit\b[^|;&]*\bpush\b", command) and (m := _PUSHED.search(output)):
        out.append(("pushed to", m.group(1)))
    if re.search(r"\bgh\s+pr\s+create\b", command) and (m := _PULL.search(output)):
        out.append(("created", f"PR #{m.group(1)}"))
    return out


@dataclass(frozen=True)
class CallFacts:
    """What the line of a run counts of one call: its kind, what makes two
    of a kind one (:func:`call_key`) and what it did to the repository."""

    kind: str
    key: str
    git: tuple[tuple[str, str], ...] = ()


def run_line(facts: Iterable[CallFacts], *, thought: float = 0.0, running: bool,
             waiting: bool, failed: int, expanded: bool) -> Text:
    """A run of calls as one line, Claude Code's: ``Thought for 4s, searched
    for 2 patterns, read 3 files (ctrl+o to expand)``; ``Reading 3 files,
    running 1 shell command…`` while one runs (yellow while one waits for an
    approval), ``(1 failed)`` in red. Reads count files (one read twice is
    one), MCP calls their servers (``called github 3 times``), a commit,
    push or pull request what it made (``committed a1b2c3d``) instead of a
    shell command. ``thought``: seconds of the model's reasoning between
    the calls."""
    facts = list(facts)
    style = "yellow" if waiting else "dim"
    out = Text()

    def part(verb: str, *pieces: tuple[str, str]) -> None:
        if out:
            out.append(", ", style=style)
        else:
            verb = verb[0].upper() + verb[1:]
        out.append(verb, style=style)
        for text, piece_style in pieces:
            out.append(text, style=piece_style or style)

    if thought:
        part("thought for ", (elapsed_text(thought), "bold"))
    gits: dict[str, list[str]] = {}
    for fact in facts:
        for verb, what in fact.git:
            gits.setdefault(verb, []).append(what)
    for verb in ("committed", "amended commit", "pushed to", "created"):
        if verb in gits:
            part(f"{verb} ", (", ".join(dict.fromkeys(gits[verb])), "bold"))
    counted = [f for f in facts if not f.git]
    for kind, now, done, one, many in RUN_WORDS:
        of_kind = [f for f in counted if f.kind == kind]
        n = len({f.key for f in of_kind}) if kind == "read" else len(of_kind)
        if n:
            part(f"{now if running else done} ", (str(n), "bold"),
                 (f" {one if n == 1 else many}", ""))
        if kind == "list":  # Claude Code names the MCP servers after these
            mcp = [f for f in counted if f.kind == "mcp"]
            if mcp:
                servers = ", ".join(dict.fromkeys(f.key for f in mcp))
                times = [(" ", ""), (str(len(mcp)), "bold"), (" times", "")] if len(mcp) > 1 else []
                part(f"{'calling' if running else 'called'} ", (clean(servers), ""), *times)
    if running:
        out.append("…", style=style)
    if failed:
        out.append(f" ({failed} failed)", style="red")
    if not expanded:
        out.append(f" {EXPAND_HINT}", style="dim")
    return out


def edit_line(path: str, edits: int, added: int, removed: int, *, running: bool,
              failed: int, expanded: bool) -> Text:
    """Consecutive edits to one file as one line: ``Edited calc.py ×3 (+12 −4)``."""
    out = Text()
    out.append("Editing " if running else "Edited ", style="dim")
    out.append(clean(path), style="bold")
    out.append(f" ×{edits}", style="dim")
    if added or removed:
        out.append(" (", style="dim")
        out.append(f"+{added}", style="green")
        out.append(" ")
        out.append(f"−{removed}", style="red")
        out.append(")", style="dim")
    if running:
        out.append("…", style="dim")
    if failed:
        out.append(f" ({failed} failed)", style="red")
    if not expanded:
        out.append(f" {EXPAND_HINT}", style="dim")
    return out


def agents_line(agents: int, *, running: bool, failed: int, expanded: bool) -> Text:
    """A reply's agents started together: ``Running 3 agents…``, then ``Ran 3 agents``."""
    out = Text()
    out.append("Running " if running else "Ran ", style="dim")
    out.append(str(agents), style="bold")
    out.append(" agents" + ("…" if running else ""), style="dim")
    if failed:
        out.append(f" ({failed} failed)", style="red")
    if not expanded:
        out.append(f" {EXPAND_HINT}", style="dim")
    return out


#: What a short reply between two calls is at most: a line or two of words
#: (``Let me check X.``) goes into the run; anything longer breaks it.
SHORT_WORDS = 200
_NOT_WORDS = re.compile(r"^\s*(?:[-*+>|#]|\d+[.)]\s)|```")


def interstitial(text: str) -> bool:
    """``text`` is a remark between calls (one or two lines, no code, no
    list or table), not something to read on its own."""
    lines = text.strip().splitlines()
    return 0 < len(lines) <= 2 and len(text.strip()) <= SHORT_WORDS \
        and not any(_NOT_WORDS.search(line) for line in lines)


def running_hint(call: ToolUse, cwd: str = "") -> str:
    """What a running call in a folded run shows under its line: the
    command, the pattern or the path."""
    lines = clean(_main_argument(call, cwd)).splitlines()
    return lines[0] if lines else tool_name(call)


def edited_path(call: ToolUse) -> str:
    """The file an edit changes."""
    return str(call.input.get("file_path") or call.input.get("notebook_path") or "")


def result_block(rows: Iterable[tuple[str, str]]) -> Hanging:
    """Rows under a call: the first after ``⎿``, the rest indented."""
    out = Text()
    for n, (line, style) in enumerate(rows):
        out.append("\n" if n else "")
        out.append("  ⎿  " if n == 0 else "     ", style="dim")
        out.append(clean(line), style=style)
    return Hanging(out)


class Hanging:
    """``text`` with each line's first :attr:`indent` cells (the ``  ⎿  ``
    gutter) kept apart: a long line wraps under its own text, not under
    the gutter. ``plain`` is the text's, as a ``Text``'s."""

    def __init__(self, text: Text, indent: int = 5) -> None:
        self.text = text
        self.indent = indent

    @property
    def plain(self) -> str:
        return self.text.plain

    def visualize(self) -> Any:
        """What Textual draws: the same, selectable (``selection.HangingContent``)."""
        from textual.content import Content

        from noeta_tui.selection import HangingContent

        return HangingContent(Content.from_rich_text(self.text), self.indent)

    def __rich_measure__(self, console: Console, options: ConsoleOptions) -> Measurement:
        return Measurement.get(console, options, self.text)

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        width = max(options.max_width, self.indent + 8)
        out: list[Text] = []
        for line in self.text.split("\n", allow_blank=True):
            head, rest = line[:self.indent], line[self.indent:]
            if not rest.plain:
                out.append(head)
                continue
            for n, part in enumerate(rest.wrap(console, width - self.indent)):
                out.append(Text.assemble(head if n == 0 else " " * self.indent, part))
        joined = Text("\n").join(out)
        joined.no_wrap = True
        yield joined


def rows_text(rows: Iterable[tuple[str, str]]) -> Text:
    """Rows as lines, each in its style."""
    out = Text()
    for n, (line, style) in enumerate(rows):
        out.append("\n" if n else "")
        out.append(clean(line), style=style)
    return out


def progress_block(text: str) -> Hanging:
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


def todo_block(items: Mapping[str, Mapping[str, Any]]) -> Hanging:
    """The task list, Claude Code's marks: ``◻`` to do, ``◼`` doing, ``✔`` done."""
    out = Text()
    for n, item in enumerate(items.values()):
        mark, style = TODO_MARKS.get(str(item.get("status")), ("◻", ""))
        out.append("\n" if n else "")
        out.append(f"  {'⎿' if n == 0 else ' '}  {mark} ", style="dim")
        out.append(clean(str(item.get("subject") or "")), style=style)
    return Hanging(out, 7)


def active_form(items: Mapping[str, Mapping[str, Any]]) -> str:
    """What the in-progress task says it is doing (``Running tests``), or ``""``."""
    for item in items.values():
        if item.get("status") == "in_progress":
            return clean(str(item.get("active_form") or item.get("subject") or ""))
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


def spinner_line(frame: int, verb: str, elapsed: float, tokens: int, *,
                 thinking: bool = False) -> Text:
    """``✻ Brewing… (12s · ↓ 1.2k tokens · thinking · esc to interrupt)``:
    the tokens count what streamed so far, reasoning included."""
    out = Text()
    out.append(SPINNER_FRAMES[frame % len(SPINNER_FRAMES)] + " ", style=f"bold {WORKING}")
    out.append(f"{verb}…", style=WORKING)
    detail = elapsed_text(elapsed)
    if tokens:
        detail += f" · ↓ {tokens_text(tokens)} tokens"
    if thinking:
        detail += " · thinking"
    out.append(f" ({detail} · esc to interrupt)", style="dim")
    return out


#: Claude Code's glyph per effort level.
EFFORT_GLYPHS = {"low": "○", "medium": "◐", "high": "●", "xhigh": "◉", "max": "◈"}


def effort_line(level: str) -> Text:
    """``◐ medium · /effort``: the effort, over the prompt's right end."""
    return Text(f"{EFFORT_GLYPHS.get(level, '●')} {level} · /effort", style="dim")


def mode_hint(mode: str) -> Text:
    """The footer's left side: the mode when it is not the default, else
    ``? for shortcuts``."""
    line = MODE_LINES.get(mode)
    if line is None:
        return Text("? for shortcuts", style="dim")
    out = Text().append(line, style=MODE_STYLES.get(mode, ""))
    out.append(" · shift+tab", style="dim")
    return out


#: How far under the point it counts down to the footer starts to warn.
CONTEXT_WARN_TOKENS = 20_000


def context_left(tokens: int, threshold: int, window: int, auto: bool) -> Text | None:
    """Claude Code's footer warning once the context is within
    :data:`CONTEXT_WARN_TOKENS` of the auto-compact threshold (of the window
    when automatic compaction is off); ``None`` before that."""
    limit = threshold if auto else window
    if limit <= 0 or tokens < limit - CONTEXT_WARN_TOKENS:
        return None
    left = max(0, round((limit - tokens) / limit * 100))
    if auto:
        return Text(f"{left}% until auto-compact", style="dim")
    return Text(f"Context low ({left}% remaining) · Run /compact to compact & continue",
                style="red")


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


# ------------------------------------------------------------- the logo

#: The mark (docs/public/logo.svg), four rows of half blocks: an event log,
#: bar by bar, folding into one state slice. ``(text, colour)`` per piece.
LOGO = ((("▀" * 10, "#10b981"),),
        (("▀" * 8, "#06b6d4"),),
        (("▀" * 6, "#8b5cf6"), (" ", ""), ("▄" * 4, "#10b981")),
        (("▀" * 4, "#f59e0b"), ("   ", ""), ("█" * 4, "#10b981")))


def logo() -> Text:
    out = Text()
    for n, row in enumerate(LOGO):
        out.append("\n" if n else "")
        for piece, colour in row:
            out.append(piece, style=colour)
    return out


#: The accent the app draws its own chrome in (the mark's green): the
#: name, the picked line of a menu or list.
ACCENT = "#10b981"
#: What is working right now (the mark's amber): the spinner, a running agent.
WORKING = "#f59e0b"
#: Bash mode (Claude Code's pink).
BASH = "#fd5db1"


# ------------------------------------------------------------- agents and workflows


def agent_line(tools: int, tokens: int, seconds: float) -> str:
    """``3 tool uses · 1.2k tokens · 12s``."""
    parts = [_plural(tools, "tool use")]
    if tokens:
        parts.append(f"{tokens_text(tokens)} tokens")
    parts.append(elapsed_text(seconds))
    return " · ".join(parts)


WORKFLOW_MARKS = {"running": ("◼", WORKING), "done": ("✔", "green"),
                  "failed": ("✘", "red")}


def workflow_rows(run: Mapping[str, Any], *, expanded: bool) -> list[tuple[str, str]]:
    """A workflow run's rows under its call: its description, then per
    phase how many of its agents are done, and (folded: the running ones
    only) one line per agent."""
    agents: list[Mapping[str, Any]] = list(run.get("agents", {}).values())
    rows: list[tuple[str, str]] = []
    if run.get("description"):
        rows.append((str(run["description"]), ""))
    done = sum(1 for a in agents if a.get("state") == "done")
    failed = sum(1 for a in agents if a.get("state") == "failed")
    head = f"{done}/{len(agents)} agents done" if agents else "starting…"
    if failed:
        head += f", {failed} failed"
    rows.append((head, "dim"))
    shown = agents if expanded else [a for a in agents if a.get("state") == "running"]
    for a in shown[:12 if not expanded else None]:
        mark, style = WORKFLOW_MARKS.get(str(a.get("state")), ("◻", "dim"))
        label = str(a.get("label") or a.get("key") or "agent")
        phase = f"{a['phase']} · " if a.get("phase") else ""
        rows.append((f"{mark} {phase}{label}", style if a.get("state") != "done" else "dim"))
    hidden = len(agents) - len(shown)
    if hidden > 0 and not expanded:
        rows.append((f"… {hidden} more {EXPAND_HINT}", "dim"))
    status = run.get("status")
    if status == "completed":
        rows.append(("Completed", "green"))
    elif status in ("failed", "killed"):
        rows.append((f"{'Stopped' if status == 'killed' else 'Failed'}"
                     + (f": {run['error']}" if run.get("error") else ""), "red"))
    return rows


# ------------------------------------------------------------- the footer


def context_bar(fraction: float, cells: int = 8) -> Text:
    """``▰▰▱▱▱▱▱▱ 23%``: how full the context window is, greener to redder."""
    fraction = max(0.0, min(1.0, fraction))
    full = round(fraction * cells)
    colour = "green" if fraction < 0.5 else "yellow" if fraction < 0.8 else "red"
    out = Text()
    out.append("▰" * full, style=colour)
    out.append("▱" * (cells - full), style="dim")
    out.append(f" {fraction:.0%}", style="dim")
    return out
