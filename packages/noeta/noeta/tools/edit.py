"""``Edit``: replace an exact, unique string in a file the model has read.

The match must be exactly once — zero or several matches fail and write
nothing (unless ``replace_all``). There is deliberately no unified-diff
applier: an invalid patch lands too easily on a file that shifted underneath.
The result shows a ``cat -n`` snippet of the edited region; the diff rides in
``meta`` and ``display``.
"""

from __future__ import annotations

from typing import Any

from noeta.tools._common import (
    PathError,
    Workspace,
    load_description,
    require_str,
    tool_error,
)
from noeta.tools._mutate import (
    WriteRoots,
    authorized_roots,
    compute_diff,
    diff_display,
    diff_stat_counts,
    read_precondition_error,
    record_written,
)
from noeta.tools.base import ToolContext, ToolOutput
from noeta.types import Text

__all__ = ["EditTool"]

#: Lines of surrounding context in the post-edit ``cat -n`` snippet.
_SNIPPET_CONTEXT_LINES = 4


def _lines(text: str) -> list[str]:
    """``text`` split the way ``Read`` numbers it: on ``\\n`` only, each
    line's trailing ``\\r`` dropped, no phantom line after a final newline."""
    if not text:
        return []
    parts = text.split("\n")
    if parts[-1] == "":
        parts.pop()
    return [p.removesuffix("\r") for p in parts]


def _to_eol(text: str, eol: str) -> str:
    lf = text.replace("\r\n", "\n")
    return lf if eol == "\n" else lf.replace("\n", eol)


def _dominant_eol(text: str) -> str:
    """``"\\r\\n"`` when most of ``text``'s line endings are CRLF."""
    crlf = text.count("\r\n")
    return "\r\n" if crlf and crlf >= text.count("\n") - crlf else "\n"


def _first_changed_line(before: str, after: str) -> int:
    b_lines, a_lines = _lines(before), _lines(after)
    for i, (b, a) in enumerate(zip(b_lines, a_lines, strict=False)):
        if b != a:
            return i
    return min(len(b_lines), len(a_lines))


def _cat_n_snippet(after_lines: list[str], center: int, span: int) -> str:
    """``cat -n`` rendering of the edited region plus context. ``center`` is
    the 0-based first changed line of the AFTER text; ``span`` the lines the
    replacement occupies (0 for a deletion: the snippet shows the seam)."""
    lo = max(0, center - _SNIPPET_CONTEXT_LINES)
    hi = min(len(after_lines), center + max(span, 1) + _SNIPPET_CONTEXT_LINES)
    return "\n".join(f"{i + 1:>6}\t{after_lines[i]}" for i in range(lo, hi))



def _input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "old_string": {"type": "string"},
            "new_string": {"type": "string"},
            "replace_all": {"type": "boolean"},
        },
        "required": ["file_path", "old_string", "new_string"],
        "additionalProperties": False,
    }


class EditTool:
    """``Edit``. ``write_roots`` authorizes directories outside the
    workspace per call; ``None`` keeps writes inside it."""

    name = "Edit"
    description = load_description("edit")
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
            ctx, resolved, raw, tool_name=self.name, verb="editing"
        )
        if refused is not None:
            return refused

        # ``Read`` shows lines without their ``\r``, so the model writes bare
        # ``\n``. In a CRLF file, match and write with the file's own ending —
        # never a mixed file.
        if _dominant_eol(before) == "\r\n":
            old_eol, new_eol = _to_eol(old, "\r\n"), _to_eol(new, "\r\n")
            if before.count(old_eol):
                old, new = old_eol, new_eol
        count = before.count(old)
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
        await record_written(ctx, resolved, after_bytes)

        snippet = _cat_n_snippet(
            _lines(after), _first_changed_line(before, after), len(_lines(new))
        )
        head = (
            f"The file {rel} has been updated. Here's the result of "
            "running `cat -n` on a snippet of the edited file:"
        )
        return ToolOutput(
            content=[Text(text=f"{head}\n{snippet}")],
            meta={
                "path": rel,
                "diff": diff,
                "added": added,
                "removed": removed,
                "replacements": count if replace_all else 1,
            },
            display=diff_display(rel, diff, added, removed),
        )
