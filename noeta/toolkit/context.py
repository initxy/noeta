"""Small helpers the workspace-context extensions share (instructions,
skills, memory, compaction): the task's ExecEnv, the messages a person added
since a point, and reading a text file through the exec env."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from noeta.core.execenv import ExecEnv
from noeta.core.types import Message, ToolResult

if TYPE_CHECKING:
    from noeta.core.loop.state import TaskState


def task_env(env: ExecEnv | None, config: Mapping[str, Any]) -> ExecEnv:
    """The task's ExecEnv, or a local one on the task's ``cwd`` when the
    runner has none."""
    if env is not None:
        return env
    from noeta.core import registry

    return registry.execenv("local", str(config.get("cwd") or os.getcwd()))


def new_user_messages(state: TaskState, seq: int) -> list[Message]:
    """The person's messages that entered the conversation with event
    ``seq``: taken straight from the inbox, or released then (held behind a
    tool batch, or queued behind the turn). Notices and recalled material
    (``origin`` system / memory) and tool results are not counted."""
    out: list[Message] = []
    for message, at in zip(state.messages, state.message_seqs, strict=False):
        if (at == seq and message.role == "user" and message.origin in (None, "human")
                and message.text and not any(isinstance(b, ToolResult) for b in message.content)):
            out.append(message)
    return out


async def read_text(env: ExecEnv, path: str) -> str | None:
    """``path`` as UTF-8 text, or ``None`` on any read fault (missing, a
    directory, not UTF-8)."""
    try:
        return (await env.read_bytes(path)).decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None
