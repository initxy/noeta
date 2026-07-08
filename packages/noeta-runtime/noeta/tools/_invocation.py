"""Tool-invocation cross-cutting contract — the three things every tool's
``invoke`` repeats, collapsed behind one small interface.

Every Noeta tool's ``invoke`` re-implements the same implicit contract by
hand: (a) pull each argument out of the ``arguments`` dict and reject a
missing / wrong-typed value with a uniform ``tool_error``; (b) resolve a
user-supplied path through the ``WorkspaceRoot`` containment seam (and,
for the common read/edit case, confirm it names an existing file); (c)
keep the inline ``ToolResult.output`` under the canonical byte budget,
either by shrinking string fields or by dropping list entries. Those
three were spread across ``fs/read.py``, ``fs/edit.py``, ``fs/patch.py``,
``fs/shell.py``, ``app/open_app.py`` and more — change the policy once and
you had to touch every tool.

This module is the **single seam** for that contract. The pieces it leans
on already existed (``WorkspaceRoot.resolve`` / ``resolve_or_error`` for
the fence, ``_limits.fit_output_fields`` / ``encoded_len`` for the
budget); the deepening is that the *call sites* now route through one
place, so the per-tool ``invoke`` carries only its real business logic.

**Byte contract (Noeta moat).** None of these helpers change an observable
byte. The argument helpers return the exact ``tool_error(name, message)``
shape the tools already produced; the budget helper is the same
``encoded_len``-bounded shrink the tools already ran inline. They are a
move of the *call point*, never a change of *behaviour* — a resumed run
re-derives the same recorded tool outputs. Tool-specific validation (shell's
allowlist, patch's per-edit parsing, ``edit``'s exactly-once match) stays
in the tool; only the generic three-step is hoisted here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from noeta.protocols.tool import ToolResult
from noeta.tools._limits import (
    INLINE_OUTPUT_MAX_BYTES,
    encoded_len,
)
from noeta.tools.fs._workspace import (
    WorkspaceRoot,
    resolve_or_error,
    resolve_readable,
    tool_error,
)
from noeta.tools.fs.exec_env import ExecEnv, LocalExecEnv


#: Shared stateless host backend for the existing-file check when a caller does
#: not inject one — keeps every non-sandbox caller byte-identical.
_DEFAULT_EXEC_ENV: ExecEnv = LocalExecEnv()


__all__ = [
    "ErrFn",
    "fit_dropping_tail",
    "require_str",
    "resolve_existing_file",
    "resolve_readable_file",
]


#: A tool's failure constructor: ``message -> ToolResult(success=False, ...)``.
#: Both shapes the tools use bind to this — ``tool_error(name, ...)`` via a
#: lambda (``read``/``edit``/``write``/``glob``/``grep``/``open_app``/the
#: shell tools, whose ``_err`` is also two-arg), and a module that binds the
#: name as a constant (``skill_script``'s one-arg ``_err``). The helpers stay
#: agnostic to which, so the emitted ``summary`` bytes are whatever the caller
#: already produced. ``apply_patch`` keeps its own index-prefixed per-edit
#: validation inline — its messages (``edit #N: ...``) are tool-specific, not
#: the generic single-arg shape this seam hoists.
ErrFn = Callable[[str], ToolResult]


def require_str(
    arguments: dict[str, Any],
    key: str,
    err: ErrFn,
    *,
    message: str,
) -> "str | ToolResult":
    """Return ``arguments[key]`` as a non-empty ``str``, or ``err(message)``.

    Collapses the ``value = arguments.get(key); if not isinstance(value, str)
    or not value: return tool_error(...)`` triple that opened nearly every
    ``invoke``. ``message`` is passed verbatim so the failure ``summary`` is
    byte-identical to the hand-written form (the tools phrase it as
    e.g. ``"requires non-empty 'path'"``).
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

    The read/edit two-step: ``resolve_or_error`` (symlink-safe containment)
    followed by ``if not resolved.is_file(): tool_error(name, f"not a file:
    {path!r}")``. Both byte forms are preserved — the escape message comes from
    ``resolve_or_error`` unchanged, the not-a-file message matches what
    ``read``/``edit`` emitted verbatim.

    ``exec_env`` routes the existence check through the same backend the tool
    reads through — so under a sandbox the ``is_file`` stat hits the *container*,
    not the host. ``None`` ⇒ the host (byte-identical to the pre-seam check).
    """
    resolved = resolve_or_error(workspace, tool_name, path)
    if isinstance(resolved, ToolResult):
        return resolved
    if not (exec_env or _DEFAULT_EXEC_ENV).is_file(resolved):
        return tool_error(tool_name, f"not a file: {path!r}")
    return resolved


def resolve_readable_file(
    workspace: WorkspaceRoot,
    extra_roots: Sequence[Path],
    tool_name: str,
    path: str,
    *,
    exec_env: Optional[ExecEnv] = None,
) -> "Path | ToolResult":
    """``read``'s fence: ``resolve_readable`` (workspace OR a skill root)
    then the existing-file check, both byte forms unchanged.

    ``exec_env`` routes the existence check through the tool's backend (the
    container under a sandbox); ``None`` ⇒ the host, byte-identical.
    """
    resolved = resolve_readable(workspace, extra_roots, tool_name, path)
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

    The ``glob``/``grep`` budget loop (``while matches: matches.pop()``), made a
    single call. **Mutates** the list in place exactly as the inline loop did —
    same canonical-byte ceiling, same per-pop re-measure, so the resulting
    ``output`` is byte-identical. The caller still owns building ``output`` and
    setting the initial ``truncated`` flag from its own match count.
    """
    items = output.get(list_key)
    if not isinstance(items, list):
        return output
    while items and encoded_len(output) > max_bytes:
        items.pop()
        output[list_key] = items
        output[truncated_key] = True
    return output
