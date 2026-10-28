"""Helpers every file / shell tool shares: descriptions, errors, path
resolution against the workspace, and the large-output rule."""

from __future__ import annotations

import posixpath
import re
from functools import cache
from importlib import resources
from pathlib import PurePosixPath
from typing import Any

from noeta.core.execenv import ExecEnv
from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.types import Text

__all__ = [
    "SCHEMA_DIALECT",
    "PathError",
    "Workspace",
    "error_output",
    "format_size",
    "limit_output",
    "load_description",
    "output_text",
    "path_within",
    "require_str",
    "tool_error",
    "truncate_bytes",
]


#: The ``$schema`` every built-in tool schema declares (Claude Code's).
SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


@cache
def load_description(package: str, name: str) -> str:
    """The model-facing description ``<package>/descriptions/<name>.md``,
    without its trailing newline."""
    text = (
        resources.files(package)
        .joinpath("descriptions", f"{name}.md")
        .read_text(encoding="utf-8")
    )
    return text.rstrip("\n")


def tool_error(tool_name: str, message: str, **meta: Any) -> ToolOutput:
    """A failed call: ``"<tool>: <message>"``."""
    return text_output(f"{tool_name}: {message}", is_error=True, **meta)


def error_output(error: str, body: str = "", **meta: Any) -> ToolOutput:
    """A failed call that still carries output: the error line leads, the
    body follows (how 0.6 rendered a failed result with output)."""
    return text_output(f"{error}\n{body}" if body else error, is_error=True, **meta)


def output_text(output: ToolOutput) -> str:
    """The text blocks of ``output``, joined."""
    return "".join(b.text for b in output.content if isinstance(b, Text))


def require_str(arguments: dict[str, Any], key: str) -> str | None:
    """``arguments[key]`` when it is a non-empty string, else ``None``."""
    value = arguments.get(key)
    return value if isinstance(value, str) and value else None


def truncate_bytes(value: str, max_bytes: int) -> str:
    """``value`` cut to at most ``max_bytes`` UTF-8 bytes (no split char)."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def format_size(n: int) -> str:
    """Claude Code's size label: ``512 bytes``, ``48.8KB``, ``1.2MB``."""
    kb = n / 1024
    if kb < 1:
        return f"{n} bytes"
    if kb < 1024:
        return f"{kb:.1f}".removesuffix(".0") + "KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f}".removesuffix(".0") + "MB"
    return f"{mb / 1024:.1f}".removesuffix(".0") + "GB"


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------


def path_within(path: str, root: str) -> bool:
    """Whether ``path`` is ``root`` or under it, component-wise (never a
    string prefix: ``/srv/app-old`` is not under ``/srv/app``). Both must
    already be canonical."""
    return path == root or PurePosixPath(path).is_relative_to(PurePosixPath(root))


class PathError(ValueError):
    """A path the tool must not touch; the message is model-facing."""


class Workspace:
    """The workspace root (the env's ``cwd``, canonicalised) and the path
    rules the tools share.

    Reads are not fenced: a relative path is anchored on the root, an
    absolute one is read where it points. Writes must land under the root or
    an authorized extra root."""

    def __init__(self, env: ExecEnv, root: str, display: str) -> None:
        self.env = env
        self.root = root
        self.display = display

    @classmethod
    async def of(cls, env: ExecEnv) -> Workspace:
        return cls(env, await env.realpath(env.cwd), env.cwd)

    async def canonical(self, target: str) -> str:
        joined = target if posixpath.isabs(target) else posixpath.join(self.root, target)
        return await self.env.realpath(joined)

    async def resolve_write(self, target: str, extra_roots: tuple[str, ...] = ()) -> str:
        """``target`` canonicalised and checked to be inside the fence."""
        resolved = await self.canonical(target)
        if not (
            path_within(resolved, self.root)
            or any(path_within(resolved, extra) for extra in extra_roots)
        ):
            raise PathError(
                f"path {target!r} resolves outside workspace {self.display!r}"
            )
        return resolved

    def relative(self, resolved: str) -> str:
        """Workspace-relative POSIX form, or the absolute path for one
        outside the workspace (the honest display)."""
        if not path_within(resolved, self.root):
            return resolved
        if resolved == self.root:
            return "."
        return posixpath.relpath(resolved, self.root)


# ---------------------------------------------------------------------------
# Large output
# ---------------------------------------------------------------------------

#: Characters of a spilled output shown inline as its preview.
PERSISTED_PREVIEW_CHARS = 2000

_SPILL_NAME_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _preview(text: str, limit: int) -> tuple[str, bool]:
    """The first ``limit`` characters of ``text``, ended at the last newline
    when that keeps more than half, and whether anything was left out."""
    if len(text) <= limit:
        return text, False
    newline = text[:limit].rfind("\n")
    end = newline if newline > limit * 0.5 else limit
    return text[:end], True


def _tail(text: str, limit: int) -> str:
    """The last ``limit`` characters of ``text``, started after the first
    newline when that keeps more than half."""
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    start = len(text) - limit
    newline = text.find("\n", start)
    if newline != -1 and len(text) - (newline + 1) > limit * 0.5:
        return text[newline + 1 :]
    return text[start:]


def large_output_message(output: str, limit: int, *, saved_to: str | None = None) -> str:
    """What the model sees for an output over ``limit`` — Claude Code's
    wording. With ``saved_to`` a ``<persisted-output>`` block (size, path, a
    2 KB preview); without, a ``<truncated-output>`` block keeping as much of
    the start and the end as fits ``limit``."""
    size = format_size(len(output))
    if saved_to is not None:
        preview, has_more = _preview(output, PERSISTED_PREVIEW_CHARS)
        return (
            "<persisted-output>\n"
            f"Output too large ({size}). Full output saved to: {saved_to}\n\n"
            f"Preview (first {format_size(PERSISTED_PREVIEW_CHARS)}):\n"
            f"{preview}" + ("\n...\n" if has_more else "\n")
            + "</persisted-output>"
        )

    def truncated(first: str, last: str, body: str) -> str:
        return (
            "<truncated-output>\n"
            f"Output too large ({size}). It could not be saved, so only the "
            f"first {first} and the last {last} are shown; the middle was "
            "dropped. If the tool can page or filter its results, call it "
            f"again for the part you need.\n{body}\n</truncated-output>"
        )

    def elision(dropped: int) -> str:
        return f"\n... [{dropped} characters dropped] ...\n"

    # Sized against the longest labels the shown parts could get, so the
    # whole block stays within ``limit``.
    widest = "1023 bytes"
    room = max(0, limit - len(truncated(widest, widest, "")) - len(elision(len(output))))
    head, _ = _preview(output, room // 2)
    tail = _tail(output[len(head) :], room - len(head))
    dropped = len(output) - len(head) - len(tail)
    return truncated(
        format_size(len(head)), format_size(len(tail)), head + elision(dropped) + tail
    )


def spill_path(spill_dir: str, task_id: str, call_id: str) -> str:
    """Where one call's full output is saved: a pure function of the
    directory, the task and the call, so a re-run names the same file."""
    task_part = _SPILL_NAME_UNSAFE.sub("_", task_id) or "task"
    call_part = _SPILL_NAME_UNSAFE.sub("_", call_id) or "call"
    return posixpath.join(spill_dir, task_part, f"{call_part}.txt")


async def limit_output(text: str, limit: int, ctx: ToolContext) -> str:
    """``text`` unchanged when it fits ``limit`` characters. Past it, the
    whole text is saved under ``ctx.spill_dir`` (through the env, so the
    model can ``Read`` it) and a preview is returned; with no spill directory,
    or when the save fails, the start and end are kept around an elision."""
    if len(text) <= limit:
        return text
    saved_to: str | None = None
    if ctx.spill_dir:
        path = spill_path(ctx.spill_dir, ctx.task_id, ctx.call_id)
        try:
            await ctx.env.write_bytes(path, text.encode("utf-8"))
            saved_to = path
        except OSError:
            saved_to = None
    return large_output_message(text, limit, saved_to=saved_to)
