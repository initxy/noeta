"""Claude Code's post-compaction file restore.

After a summary, the files the newest successful ``Read`` / ``Edit`` /
``Write`` calls touched are read again through the ``Read`` tool, newest
first, at most 5, each within 5,000 tokens and 50,000 in all, skipping a
file whose ``Read`` the kept messages already show and the instruction
files. Each is recorded as a Read call and its result (or a too-large
note) and given in the next request's system turn, so a resume folds them
back and never reads the disk again.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from noeta.core.tool import Tool, ToolContext
from noeta.core.types import Image, Message, ToolResult, ToolUse, user
from noeta.plugins.compaction.estimate import messages_tokens

POST_COMPACT_MAX_FILES_TO_RESTORE = 5
POST_COMPACT_MAX_TOKENS_PER_FILE = 5_000
POST_COMPACT_TOKEN_BUDGET = 50_000

READ_TOOL = "Read"
_FILE_TOOLS = (READ_TOOL, "Edit", "Write")
#: Carried by the instructions resident; restoring them would repeat it.
_INSTRUCTION_FILE_NAMES = frozenset({"NOETA.md", "NOETA.local.md", "AGENTS.md"})


def _touched(message: Message, succeeded: set[str]) -> list[tuple[str, str]]:
    if message.role != "assistant":
        return []
    out: list[tuple[str, str]] = []
    for b in message.content:
        if isinstance(b, ToolUse) and b.name in _FILE_TOOLS and b.id in succeeded:
            path = b.input.get("file_path")
            if isinstance(path, str) and path:
                out.append((path, b.name))
    return out


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def recent_files(history: Sequence[Message], boundary: int) -> list[str]:
    """Paths to restore, newest first. ``history`` is the whole uncompacted
    history and ``boundary`` the number of its messages the new summary
    covers."""
    succeeded = {
        b.call_id for m in history for b in m.content
        if isinstance(b, ToolResult) and not b.is_error
    }
    kept_reads = {
        path for m in history[boundary:] for path, tool in _touched(m, succeeded)
        if tool == READ_TOOL
    }
    recent: list[str] = []
    for message in reversed(history):
        for path, _tool in reversed(_touched(message, succeeded)):
            if path in recent or path in kept_reads or _basename(path) in _INSTRUCTION_FILE_NAMES:
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
) -> tuple[list[Message], list[tuple[str, str, Any]]]:
    """The restore turns for ``paths`` (host-injected user messages), and
    what the reads of the restored files recorded (so ``Edit`` treats them
    as read; not for a file too large to include). A read that fails, or
    returns an image, is skipped."""
    budget = POST_COMPACT_TOKEN_BUDGET
    texts: list[str] = []
    records: list[tuple[str, str, Any]] = []
    used = 0
    for i, path in enumerate(paths):
        arguments = {"file_path": path}
        recorded: list[tuple[str, str, Any]] = []
        try:
            result = await read_tool.run(
                arguments, context(i, _sink(recorded)))
        except Exception:  # noqa: BLE001 — a failed restore is skipped
            continue
        if result.is_error or result.park is not None or any(
                isinstance(b, Image) for b in result.content):
            continue
        output = "".join(getattr(b, "text", "") for b in result.content)
        too_large = _estimate(output) > POST_COMPACT_MAX_TOKENS_PER_FILE
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
    return [user(t, origin="system") for t in texts], records
