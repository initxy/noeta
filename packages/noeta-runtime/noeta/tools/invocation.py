"""The cross-cutting contract every tool's ``invoke`` repeats, behind one seam:
argument extraction with a uniform ``tool_error``, path resolution through the
``WorkspaceRoot`` containment fence, and keeping the inline
``ToolResult.output`` under the canonical byte budget.

Routing all three through one place is what lets the policy change once instead
of per tool. Tool-specific validation (shell's allowlist, patch's per-edit
parsing, ``edit``'s exactly-once match) stays in the tool; only the generic
steps are hoisted here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional

from noeta.protocols.tool import ToolResult
from noeta.tools.limits import (
    INLINE_OUTPUT_MAX_BYTES,
    encoded_len,
)
from noeta.runtime.workspace import (
    WorkspaceRoot,
    resolve_anywhere,
    resolve_or_error,
    tool_error,
)
from noeta.runtime.exec_env import ExecEnv, LocalExecEnv


#: Shared stateless host backend for the existing-file check when a caller does
#: not inject one.
_DEFAULT_EXEC_ENV: ExecEnv = LocalExecEnv()


__all__ = [
    "ErrFn",
    "fit_dropping_tail",
    "require_str",
    "resolve_existing_file",
    "resolve_readable_file",
]


#: A tool's failure constructor: ``message -> ToolResult(success=False, ...)``.
#: Single-arg by design, so a caller may bind the tool name however it likes
#: (a lambda over ``tool_error(name, ...)``, or a module constant) and the
#: emitted ``summary`` bytes stay the caller's own.
ErrFn = Callable[[str], ToolResult]


def require_str(
    arguments: dict[str, Any],
    key: str,
    err: ErrFn,
    *,
    message: str,
) -> "str | ToolResult":
    """Return ``arguments[key]`` as a non-empty ``str``, or ``err(message)``.

    ``message`` is passed through verbatim: the failure ``summary`` is a
    model-facing string each tool phrases for itself.
    """
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        return err(message)
    return value


def resolve_existing_file(
    workspace: WorkspaceRoot,
    tool_name: str,
    path: str,
    *,
    exec_env: Optional[ExecEnv] = None,
) -> "Path | ToolResult":
    """Fence ``path`` to the workspace, then confirm it names an existing file.

    ``exec_env`` routes the existence check through the same backend the tool
    reads through, so under a sandbox the stat hits the *container*, not the
    host; ``None`` ⇒ the host.
    """
    resolved = resolve_or_error(workspace, tool_name, path)
    if isinstance(resolved, ToolResult):
        return resolved
    if not (exec_env or _DEFAULT_EXEC_ENV).is_file(resolved):
        return tool_error(tool_name, f"not a file: {path!r}")
    return resolved


def resolve_readable_file(
    workspace: WorkspaceRoot,
    tool_name: str,
    path: str,
    *,
    exec_env: Optional[ExecEnv] = None,
) -> "Path | ToolResult":
    """Resolve ``path`` unfenced — relative under the workspace, absolute as
    named — then confirm it names an existing file.

    ``exec_env`` routes the existence check through the tool's backend (the
    container under a sandbox); ``None`` ⇒ the host.
    """
    resolved = resolve_anywhere(workspace, tool_name, path)
    if isinstance(resolved, ToolResult):
        return resolved
    if not (exec_env or _DEFAULT_EXEC_ENV).is_file(resolved):
        return tool_error(tool_name, f"not a file: {path!r}")
    return resolved


def fit_dropping_tail(
    output: dict[str, Any],
    list_key: str,
    *,
    max_bytes: int = INLINE_OUTPUT_MAX_BYTES,
    truncated_key: str = "truncated",
) -> dict[str, Any]:
    """Shrink ``output`` under ``max_bytes`` by dropping ``output[list_key]``
    entries from the tail, marking ``output[truncated_key] = True`` once any is
    dropped.

    **Mutates** the list in place, re-measuring the canonical encoding after
    every pop. The caller still owns building ``output`` and setting the initial
    ``truncated`` flag from its own match count.
    """
    items = output.get(list_key)
    if not isinstance(items, list):
        return output
    while items and encoded_len(output) > max_bytes:
        items.pop()
        output[list_key] = items
        output[truncated_key] = True
    return output
