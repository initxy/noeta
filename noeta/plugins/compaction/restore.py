"""Claude Code's post-compaction file restore.

After a summary, the files the newest successful ``Read`` / ``Edit`` /
``Write`` calls touched, and those a successful ``Bash`` command showed
(``noeta.toolkit.shell.read_files``: ``cat``, ``head``, ``sed -n`` and the
like, as Claude Code counts them), are read again through the ``Read``
tool, newest first, at most 5, each within 5,000 tokens and 50,000 in all, skipping a
file whose ``Read`` the kept messages already show and the files the
conversation carries anyway (Claude Code's filter, by exact path: the
instruction files and memory index the instructions resident gives again,
the task's plan file). Each is recorded as a Read call and its result (or a too-large
note) and given in the next request's system turn, so a resume folds them
back and never reads the disk again.
"""

from __future__ import annotations

import json
import posixpath
from collections.abc import Callable, Collection, Mapping, Sequence
from typing import Any

from noeta.core.tool import Tool, ToolContext
from noeta.core.types import Image, Message, ToolResult, ToolUse, user
from noeta.plugins.compaction.estimate import messages_tokens
from noeta.toolkit.shell import NO_OUTPUT, read_files

POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
POST_COMPACT_TOKEN_BUDGET = 50_000

READ_TOOL = "Read"
_FILE_TOOLS = (READ_TOOL, "Edit", "Write")
SHELL_TOOL = "Bash"


def _touched(message: Message, succeeded: Mapping[str, str], cwd: str = "",
             shown: Mapping[str, Sequence[str]] | None = None) -> list[tuple[str, str]]:
    """``(path, tool)`` per file ``message``'s successful calls touched; a
    path a command gave relative is taken from the directory the command
    ran in when ``shown`` (call id -> the resolved paths the file tools
    recorded for it) tells it, else from ``cwd``."""
    if message.role != "assistant":
        return []
    out: list[tuple[str, str]] = []
    for b in message.content:
        if not isinstance(b, ToolUse) or b.id not in succeeded:
            continue
        if b.name in _FILE_TOOLS:
            path = b.input.get("file_path")
            if isinstance(path, str) and path:
                out.append((path, b.name))
        elif b.name == SHELL_TOOL and not b.input.get("run_in_background"):
            command = b.input.get("command")
            output = succeeded[b.id].strip()
            files = read_files(command if isinstance(command, str) else "")
            base = _ran_in(files, (shown or {}).get(b.id, ())) or cwd
            for path, needs_match in files:
                if not needs_match or (output and output != NO_OUTPUT):
                    out.append((posixpath.normpath(posixpath.join(base, path)), b.name))
    return out


def _ran_in(files: Sequence[tuple[str, bool]], resolved: Sequence[str]) -> str | None:
    """The directory a command ran in, told by a resolved path the file
    tools recorded for one of the relative paths it named."""
    for path, _ in files:
        if posixpath.isabs(path):
            continue
        rel = posixpath.normpath(path)
        for full in resolved:
            if full.endswith("/" + rel):
                return full[:-len(rel) - 1] or "/"
    return None


def recent_files(messages: Sequence[Message], boundary: int, *, since: int = 0,
                 carried: Sequence[str] = (), cwd: str = "",
                 shown: Mapping[str, Sequence[str]] | None = None,
                 skip: Collection[str] = ()) -> list[str]:
    """Paths to restore, newest first. ``messages`` is the conversation as
    the model sees it and ``boundary`` the number of its messages the new
    summary covers. As Claude Code's read-file state, which a compaction resets to
    the files it restored: only touches from message ``since`` on (those
    after the previous compaction) count, then the files that compaction
    restored in full (``carried``); a file it could only name as too large,
    or left to its kept messages, is not restored again unless touched
    since. A path a shell command gave relative is taken from where that
    command ran (``shown``, as :func:`_touched` takes it), else ``cwd``.
    A file at a path in ``skip`` (absolute) is never restored."""
    succeeded = {
        b.call_id: b.text for m in messages for b in m.content
        if isinstance(b, ToolResult) and not b.is_error
    }
    kept_reads = {
        path for m in messages[boundary:] for path, tool in _touched(m, succeeded, cwd, shown)
        if tool == READ_TOOL
    }
    touched = [path for message in reversed(messages[since:])
               for path, _tool in reversed(_touched(message, succeeded, cwd, shown))]
    recent: list[str] = []
    for path in [*touched, *carried]:
        if path in recent or path in kept_reads \
                or posixpath.normpath(posixpath.join(cwd, path)) in skip:
            continue
        recent.append(path)
    return recent[:POST_COMPACT_MAX_FILES_TO_RESTORE]


def _sink(target: list[tuple[str, str, Any]]) -> Callable[[str, str, Any], None]:
    def record(extension: str, kind: str, data: Any) -> None:
        target.append((extension, kind, data))

    return record


def _estimate(text: str) -> int:
    return messages_tokens([user(text)])


async def restore_files(
    paths: Sequence[str],
    read_tool: Tool,
    context: Callable[[int, Callable[[str, str, Any], None]], ToolContext],
) -> tuple[list[Message], list[tuple[str, str, Any]], list[str]]:
    """The restore turns for ``paths`` (host-injected user messages), what
    the reads of the restored files recorded (so ``Edit`` treats them as
    read; not for a file too large to include), and the paths restored in
    full. A read the ``Read`` tool
    refuses for its size gets the too-large note too; one that fails
    otherwise (the file is gone), or returns an image, is skipped."""
    budget = POST_COMPACT_TOKEN_BUDGET
    texts: list[str] = []
    records: list[tuple[str, str, Any]] = []
    full: list[str] = []
    used = 0
    for i, path in enumerate(paths):
        arguments = {"file_path": path}
        recorded: list[tuple[str, str, Any]] = []
        try:
            result = await read_tool.run(
                arguments, context(i, _sink(recorded)))
        except Exception:  # noqa: BLE001 — a failed restore is skipped
            continue
        refused_for_size = result.is_error and bool(result.meta.get("too_large"))
        if not refused_for_size and (result.is_error or result.park is not None or any(
                isinstance(b, Image) for b in result.content)):
            continue
        output = "".join(getattr(b, "text", "") for b in result.content)
        too_large = refused_for_size or _estimate(output) > POST_COMPACT_MAX_TOKENS_PER_FILE
        if too_large:
            # The model never sees these contents, so the read is not
            # recorded: Edit / Write must Read the file again first.
            text = (f"{path} was read before the summary, but it is too large to show "
                    f"again here; read it with the {READ_TOOL} tool if you need it.")
        else:
            text = (f"The {READ_TOOL} tool was called with this input: "
                    + json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
                    + f"\nIt returned:\n{output}")
        size = _estimate(text)
        if used + size > budget:
            continue
        used += size
        texts.append(text)
        if not too_large:
            records.extend(recorded)
            full.append(path)
    return [user(t, origin="system") for t in texts], records, full
