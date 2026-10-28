"""``Edit``: replace an exact, unique string in a file the model has read.

The match must be exactly once — zero or several matches fail and write
nothing (unless ``replace_all``). There is deliberately no unified-diff
applier: an invalid patch lands too easily on a file that shifted underneath.
The result is one line naming the file (Claude Code's); the diff rides in
``meta`` and ``display``.
"""

from __future__ import annotations

from typing import Any

from noeta.core.tool import ToolContext, ToolOutput
from noeta.core.types import Text
from noeta.plugins.fs._mutate import (
    WriteRoots,
    authorized_roots,
    changed_since_read,
    compute_diff,
    diff_display,
    diff_stat_counts,
    mutation_result,
    read_precondition_error,
    record_written,
    seen_whole,
    stale_error,
)
from noeta.toolkit.tools import (
    SCHEMA_DIALECT,
    PathError,
    Workspace,
    load_description,
    require_str,
    tool_error,
)

__all__ = ["EditTool"]

def _to_eol(text: str, eol: str) -> str:
    lf = text.replace("\r\n", "\n")
    return lf if eol == "\n" else lf.replace("\n", eol)


def _dominant_eol(text: str) -> str:
    """``"\\r\\n"`` when most of ``text``'s line endings are CRLF."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"


def _input_schema() -> dict[str, Any]:
    return {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "file_path": {"type": "string",
                          "description": "Absolute path of the file to change."},
            "old_string": {"type": "string", "description": "The text to replace."},
            "new_string": {"type": "string",
                           "description": "The text to put in its place (it must differ "
                                          "from old_string)."},
            "replace_all": {"type": "boolean", "default": False,
                            "description": "Replace every occurrence of old_string "
                                           "(default false)."},
        },
        "required": ["file_path", "old_string", "new_string"],
        "additionalProperties": False,
    }


class EditTool:
    """``Edit``. ``write_roots`` authorizes directories outside the
    workspace per call; ``None`` keeps writes inside it."""

    name = "Edit"
    description = load_description("noeta.plugins.fs", "edit")
    read_only = False
    clearable = True
    input_schema: dict[str, Any]

    def __init__(self, *, write_roots: WriteRoots | None = None) -> None:
        self.input_schema = _input_schema()
        self.write_roots = write_roots

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        path = require_str(input, "file_path")
        if path is None:
            return tool_error(self.name, "requires non-empty 'file_path'")
        old = require_str(input, "old_string")
        if old is None:
            return tool_error(self.name, "requires non-empty 'old_string'")
        new = input.get("new_string")
        if not isinstance(new, str):
            return tool_error(self.name, "requires string 'new_string'")
        if old == new:
            return tool_error(self.name, "old_string and new_string must be different")
        replace_all = bool(input.get("replace_all"))

        env = ctx.env
        ws = await Workspace.of(env)
        try:
            resolved = await ws.resolve_write(
                path, await authorized_roots(ws, self.write_roots, ctx)
            )
        except PathError as exc:
            return tool_error(self.name, str(exc))
        st = await env.stat(resolved)
        if st is None or st.is_dir:
            return tool_error(self.name, f"not a file: {path!r}")
        try:
            raw = await env.read_bytes(resolved)
        except OSError as exc:
            return tool_error(self.name, f"read failed: {exc}")
        try:
            before = raw.decode("utf-8")
        except UnicodeDecodeError:
            return tool_error(self.name, f"{path!r} is not utf-8 text")
        refused = read_precondition_error(
            ctx, resolved, raw, tool_name=self.name, verb="editing",
            unread_ok=not ctx.approved, partial_ok=True, check_stale=False,
        )
        if refused is not None:
            return refused
        stale = changed_since_read(ctx, resolved, raw)
        in_view = seen_whole(ctx, resolved, raw)

        # ``Read`` shows lines without their ``\r``, so the model writes bare
        # ``\n``. In a CRLF file, match and write with the file's own ending —
        # never a mixed file.
        if _dominant_eol(before) == "\r\n":
            old_eol, new_eol = _to_eol(old, "\r\n"), _to_eol(new, "\r\n")
            if before.count(old_eol):
                old, new = old_eol, new_eol
        count = before.count(old)
        # Claude Code applies an edit over changes made since the read when
        # its permissions allow the write outright and old_string still
        # picks one place (or replace_all); otherwise the model must re-read.
        if stale and (ctx.approved or count == 0 or (count > 1 and not replace_all)):
            return stale_error(self.name, "editing")
        if count == 0:
            return tool_error(self.name, "String to replace not found in file.")
        if count > 1 and not replace_all:
            return tool_error(
                self.name,
                f"Found {count} matches of the string to replace, but "
                "replace_all is false. To replace all occurrences, set "
                "replace_all to true. To replace only one, provide more "
                "surrounding context to uniquely identify the instance.",
            )
        after = before.replace(old, new) if replace_all else before.replace(old, new, 1)
        rel = ws.relative(resolved)
        diff = compute_diff(before, after, rel)
        added, removed = diff_stat_counts(diff)
        after_bytes = after.encode("utf-8")
        try:
            await env.write_bytes(resolved, after_bytes)
        except OSError as exc:
            return tool_error(self.name, f"write failed: {exc}")
        await record_written(ctx, resolved, after_bytes, whole=in_view)

        return ToolOutput(
            content=[Text(text=mutation_result(resolved, created=False, stale=stale,
                                               in_view=in_view, every=replace_all))],
            meta={
                "path": rel,
                "diff": diff,
                "added": added,
                "removed": removed,
                "replacements": count if replace_all else 1,
            },
            display=diff_display(rel, diff, added, removed),
        )
