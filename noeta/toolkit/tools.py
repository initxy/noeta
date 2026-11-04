"""Helpers every file / shell tool shares: descriptions, errors, path
resolution against the workspace, and the large-output rule."""

from __future__ import annotations

import posixpath
from functools import cache
from importlib import resources
from pathlib import PurePosixPath
from typing import Any

from noeta.core.execenv import ExecEnv
from noeta.core.large_output import (
    PERSISTED_PREVIEW_CHARS,
    format_size,
    large_output_message,
    limit_output,
    mark_spill_dir,
    save_spilled,
    spill_path,
    task_spill_dir,
)
from noeta.core.tool import ToolOutput, text_output
from noeta.core.tool_input import tool_use_error
from noeta.core.types import Text

__all__ = [
    "PERSISTED_PREVIEW_CHARS",
    "SCHEMA_DIALECT",
    "PathError",
    "Workspace",
    "error_output",
    "format_size",
    "input_error",
    "large_output_message",
    "limit_output",
    "load_description",
    "mark_spill_dir",
    "output_text",
    "path_within",
    "require_str",
    "save_spilled",
    "spill_path",
    "task_spill_dir",
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


def input_error(message: str, **meta: Any) -> ToolOutput:
    """A call refused before the tool did anything — its arguments, the file's
    or the task's state do not allow it (Claude Code's ``validateInput``):
    ``message`` in Claude Code's ``<tool_use_error>`` frame. A failure while
    the tool works is a :func:`tool_error`."""
    return text_output(tool_use_error(message), is_error=True, **meta)


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

    A relative path is anchored on the root, an absolute one is taken where
    it points; :meth:`resolve_write` also holds a write to the root and the
    extra roots given (the file tools' opt-in fence)."""

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
