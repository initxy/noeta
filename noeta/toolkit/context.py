"""Small helpers the workspace-context extensions share (instructions,
skills, memory, compaction): the task's ExecEnv, the messages a person added
since a point, and reading a text file through the exec env or on this
machine."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from noeta.core.execenv import ExecEnv
from noeta.core.types import Message, ToolResult
from noeta.toolkit.local_env import is_local

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
        if (at == seq and message.from_person and message.text
                and not any(isinstance(b, ToolResult) for b in message.content)):
            out.append(message)
    return out


async def read_text(env: ExecEnv, path: str) -> str | None:
    """``path`` as UTF-8 text, or ``None`` on any read fault (missing, a
    directory, not UTF-8)."""
    try:
        return (await env.read_bytes(path)).decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


async def read_local_text(env: ExecEnv, path: str) -> str | None:
    """``path`` on this machine as UTF-8 text, or ``None`` on any read
    fault."""
    try:
        return await asyncio.to_thread(Path(path).read_text, "utf-8")
    except (OSError, UnicodeDecodeError, ValueError):
        return None


async def read_task_text(env: ExecEnv, path: str) -> str | None:
    """``path`` as UTF-8 text where the task's own files are (its memory):
    on this machine for a local env, else through ``env``, where the model's
    file tools write them; ``None`` on any read fault."""
    if is_local(env):
        return await read_local_text(env, path)
    return await read_text(env, path)


def _local_files(directory: str, suffix: str) -> list[tuple[str, float]]:
    out = []
    for path in Path(directory).glob(f"*{suffix}"):
        try:
            if path.is_file():
                out.append((path.name, path.stat().st_mtime))
        except OSError:
            continue
    return out


#: ``<mtime> <name>`` of each file directly in ``$1`` ending in ``$2``. Not
#: ``find -printf``, which only GNU's find has (not macOS's, nor BusyBox's);
#: ``date -r FILE`` is in all three.
_LIST_FILES = ('[ -d "$1" ] || exit 1; for f in "$1"/*"$2"; do '
               '[ -f "$f" ] && printf "%s %s\\n" "$(date -r "$f" +%s)" "${f##*/}"; done; exit 0')


async def task_files(env: ExecEnv, directory: str, suffix: str) -> list[tuple[str, float]]:
    """``(name, modification time)`` of the files directly in ``directory``
    whose names end in ``suffix``, where the task's own files are (as
    :func:`read_task_text`); empty when it cannot be listed."""
    if is_local(env):
        return await asyncio.to_thread(_local_files, directory, suffix)
    try:
        done = await env.run(["sh", "-c", _LIST_FILES, "_", directory, suffix],
                             timeout=10, output_cap=1024 * 1024)
    except Exception:  # noqa: BLE001 — no such directory, or no find
        return []
    out = []
    for line in done.stdout.decode("utf-8", "replace").splitlines():
        mtime, _, name = line.partition(" ")
        try:
            out.append((name, float(mtime)))
        except ValueError:
            continue
    return out if done.exit_code == 0 else []
