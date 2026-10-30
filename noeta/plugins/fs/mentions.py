"""Files and directories the person names with ``@path`` in a message,
attached as Claude Code attaches them: read in the task's exec env through
``Read`` itself, so the file counts as read for a later ``Edit``, a PDF,
notebook or image comes out as ``Read`` shows it, and the read's limits hold.

Each attachment is a note in the person's turn holding the call and its
result (``Called the Read tool with the following input: …`` / ``Result of
calling the Read tool: …``); a directory is shown as an ``ls`` through Bash.
A text file over the read limit is attached by its first 2,000 lines, and
one too large even for that, a PDF longer than one read takes, or a path that
cannot be read is named with how to read it instead. A path a deny rule keeps
from ``Read`` is left out. Forms: ``@path``, ``@"path with spaces"``,
``@path#L10-20`` (those lines) and ``@file:///abs/path`` (an editor's link).
"""

from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
from dataclasses import dataclass
from typing import Any

from noeta.core.extension import Deny, TaskView
from noeta.core.tool import ToolContext, ToolOutput
from noeta.core.types import Block, Image, Message, Text, ToolUse
from noeta.plugins.fs._read_formats import PDF_MAX_PAGES_PER_READ, PDF_MAX_PAGES_WHOLE, page_count
from noeta.plugins.fs.read import ReadTool
from noeta.plugins.fs.state import FS, KIND_MENTIONS, KIND_NOTICE, current_fs_state
from noeta.toolkit.local_env import is_local
from noeta.toolkit.tools import format_size, output_text

__all__ = ["FIRST_LINES", "MENTION", "attach_mentions", "mentioned", "new_prompts"]

#: ``@path`` or ``@"path with spaces"``, not inside a word or an e-mail address.
MENTION = re.compile(r'(?<![\w@])@("([^"]+)"|[^\s"]+)')
_LINES = re.compile(r"^(.+?)#L(\d+)(?:-(\d+))?$")
#: A file over the read limit is attached by this many first lines.
FIRST_LINES = 2000
#: Entries of a mentioned directory listed at most.
MAX_ENTRIES = 1000

CALLED = "Called the {tool} tool with the following input: {input}"
RESULT = "Result of calling the {tool} tool:\n{text}"
FIRST_LINES_NOTE = (
    "Note: {path} is too long to attach whole, so only its first {lines} lines are shown. "
    "No need to mention that; read on with Read if you need the rest.")
TOO_LARGE = (
    "The user @-mentioned {path} ({size}). It was not attached: it is too large to read at "
    "once. Read it in parts with offset and limit, a few hundred lines to start, or search it "
    "for what you need; only if every line is needed, call Read with allow_large: true.")
UNREAD = (
    "The user @-mentioned {paths}. {why}: if these are files or directories in your working "
    "directory, read them with your file tools before you answer.")
WHY_NO_ENV = "Files are not attached automatically here"
WHY_UNREADABLE = "They could not be read, so nothing was attached"
PDF_REFERENCE = (
    "PDF file: {path} ({pages} pages, {size}). None of it is in this conversation yet. Before "
    'you write anything else, call Read with file_path set to this path and a few pages (for '
    'example pages: "1-5"), as a tool call; a call without pages fails. Read on about five '
    "pages a call ({most} at most) until you have what the request needs, describe only pages "
    "you have read, and if you answer from part of the file, say which pages you read.")


@dataclass(frozen=True)
class Mention:
    path: str
    first: int | None = None
    last: int | None = None


def mentioned(text: str) -> list[Mention]:
    """The mentions in ``text``, in order, each once."""
    out: list[Mention] = []
    for m in MENTION.finditer(text):
        raw = m.group(2) or m.group(1).rstrip(".,;:!?)")
        if raw.startswith("file://"):
            raw = raw[len("file://"):]
        first = last = None
        lines = _LINES.match(raw)
        if lines is not None:
            raw, first = lines.group(1), int(lines.group(2))
            last = int(lines.group(3)) if lines.group(3) else None
        mention = Mention(raw, first, last)
        if raw and mention not in out:
            out.append(mention)
    return out


def new_prompts(view: TaskView, through: int) -> list[tuple[int, Message]]:
    """The person's messages of the current exchange (after the model's
    last reply) newer than seq ``through``."""
    messages, seqs = view.state.messages, view.state.message_seqs
    start = max((i + 1 for i, m in enumerate(messages) if m.role == "assistant"), default=0)
    return [(seqs[i], messages[i]) for i in range(start, min(len(messages), len(seqs)))
            if seqs[i] > through and messages[i].role == "user"
            and messages[i].origin == "human"]


async def attach_mentions(view: TaskView) -> None:
    """Record an attachment for each new mention of the person's (once per
    message: what was handled is recorded, in the log)."""
    prompts = new_prompts(view, current_fs_state(view.state.ext).mentions_through)
    found = [m for _, message in prompts for m in mentioned(message.text)]
    if not found:
        return
    through = max(seq for seq, _ in prompts)
    view.record(FS, KIND_MENTIONS, {"through": through})
    env = view.env
    if env is None:
        named = [m.path for m in found if "/" in m.path or "." in m.path]
        if named:
            _note(view, through, ", ".join(named),
                  UNREAD.format(paths=", ".join(named), why=WHY_NO_ENV))
        return
    unread: list[str] = []
    for i, mention in enumerate(found):
        path = _absolute(view, mention.path)
        if path is None or await env.stat(path) is None:
            continue
        if await _denied(view, ToolUse(id=f"mention.{through}.{i}", name="Read",
                                       input={"file_path": path})):
            continue
        try:
            content = await _attachment(view, mention, path, f"mention.{through}.{i}")
        except OSError:
            content = None
        if content is None:
            unread.append(path)
        else:
            view.record(FS, KIND_MENTIONS, {"through": through, "path": path},
                        message=Message(role="user", content=content, origin="system"))
    if unread:
        _note(view, through, ", ".join(unread),
              UNREAD.format(paths=", ".join(unread), why=WHY_UNREADABLE))


def _note(view: TaskView, through: int, path: str, text: str) -> None:
    view.record(FS, KIND_MENTIONS, {"through": through, "path": path},
                message=Message(role="user", content=[Text(text=text)], origin="system"))


def _absolute(view: TaskView, raw: str) -> str | None:
    if raw.startswith("~"):
        if not is_local(view.env):
            return None
        raw = os.path.expanduser(raw)
    base = view.cwd or getattr(view.env, "cwd", "") or "/"
    return posixpath.normpath(posixpath.join(base, raw))


async def _denied(view: TaskView, call: ToolUse) -> bool:
    """Whether a check of the task refuses ``Read`` of the path (a deny
    rule). Only a refusal counts: the person named the file, so nothing
    asks. Whatever a check records while it is asked does not stand."""
    marks = len(view.pending), len(view.enqueued), len(view.creates)
    try:
        for ext in view.extensions:
            if ext.name == FS:
                continue
            try:
                verdict = await ext.before_tool(view, call)
            except Exception:  # noqa: BLE001 — a failing check refuses
                return True
            if isinstance(verdict, Deny):
                return True
        return False
    finally:
        del view.pending[marks[0]:]
        del view.enqueued[marks[1]:]
        del view.creates[marks[2]:]


async def _attachment(view: TaskView, mention: Mention, path: str,
                      call_id: str) -> list[Block] | None:
    """The attachment's blocks, or ``None`` when the path cannot be read."""
    env = view.env
    assert env is not None
    stat = await env.stat(path)
    if stat is None:
        return None
    if stat.is_dir:
        return await _directory(view, path)
    if posixpath.splitext(path)[1].lower() == ".pdf" and mention.first is None:
        pages = await page_count(env, path, posixpath.dirname(path))
        if pages is not None and pages > PDF_MAX_PAGES_WHOLE:
            return [Text(text=view.prompt("reminder/mention_pdf", PDF_REFERENCE).format(
                path=path, pages=pages, size=format_size(stat.size),
                most=PDF_MAX_PAGES_PER_READ))]
    asked: dict[str, Any] = {"file_path": path}
    if mention.first is not None:
        asked["offset"] = mention.first
        if mention.last is not None:
            asked["limit"] = max(1, mention.last - mention.first + 1)
    output, records = await _read(view, asked, call_id)
    note = ""
    if mention.first is None and (output.meta.get("too_large") or output.meta.get("truncated")):
        asked = {"file_path": path, "limit": FIRST_LINES}
        output, records = await _read(view, asked, call_id)
        if output.is_error:
            return [Text(text=view.prompt("reminder/mention_too_large", TOO_LARGE).format(
                path=path, size=format_size(stat.size)))]
        note = view.prompt("reminder/mention_first_lines", FIRST_LINES_NOTE).format(
            path=path, lines=FIRST_LINES)
    if output.is_error:
        return None
    notices: list[str] = []
    for extension, kind, data in records:
        if extension == FS and kind == KIND_NOTICE:
            notices.append(str(data.get("text", "")))
        else:
            view.record(extension, kind, data)
    blocks: list[Block] = [Text(text=CALLED.format(tool="Read", input=_json(asked)))]
    if any(isinstance(b, Image) for b in output.content):
        blocks += [b for b in output.content if isinstance(b, (Text, Image))]
    else:
        blocks.append(Text(text=RESULT.format(tool="Read", text=output_text(output))))
    blocks += [Text(text=t) for t in [*notices, note] if t]
    return blocks


async def _read(view: TaskView, asked: dict[str, Any],
                call_id: str) -> tuple[ToolOutput, list[tuple[str, str, Any]]]:
    """``Read`` run as the model would run it, its records kept back."""
    env = view.env
    assert env is not None
    records: list[tuple[str, str, Any]] = []
    ctx = ToolContext(task_id=view.task_id, call_id=call_id, env=env, state=view.state.ext,
                      record=lambda e, k, d: records.append((e, k, d)),
                      count_tokens=view.count_tokens, model=view.model, prompt=view.prompt)
    return await ReadTool().run(dict(asked), ctx), records


async def _directory(view: TaskView, path: str) -> list[Block] | None:
    env = view.env
    assert env is not None
    done = await env.run(["ls", "-1Ap", path], timeout=30, output_cap=1024 * 1024)
    if done.exit_code != 0:
        return None
    names = done.stdout.decode("utf-8", "replace").splitlines()
    listing = "\n".join(names[:MAX_ENTRIES])
    if len(names) > MAX_ENTRIES:
        listing += f"\n(and {len(names) - MAX_ENTRIES} more)"
    call = {"command": f"ls {shlex.quote(path)}", "description": f"Lists files in {path}"}
    return [Text(text=CALLED.format(tool="Bash", input=_json(call))),
            Text(text=RESULT.format(tool="Bash", text=listing))]


def _json(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

