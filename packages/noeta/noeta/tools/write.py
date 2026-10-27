"""``Write``: create a file, or overwrite one the model has read.

Creating a new file always works — nothing could have been read — and
missing parent directories are created. Overwriting an existing file needs a
read whose digest still matches, so the model never clobbers a file it has
not seen. The diff rides in ``meta`` and ``display``.
"""

from __future__ import annotations

from collections.abc import Sequence
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
from noeta.tools._rg import compile_glob, glob_matches
from noeta.tools.base import ToolContext, ToolOutput
from noeta.types import Text

__all__ = ["WRITE_FILE_MAX_BYTES", "WriteTool"]

#: Hard cap on one ``Write`` body — a runaway-file guard.
WRITE_FILE_MAX_BYTES = 8 * 1024 * 1024



def _input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "file_path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["file_path", "content"],
        "additionalProperties": False,
    }


class WriteTool:
    """``Write``. ``allowed_path_globs`` confines it to workspace-relative
    paths matching one of the globs (empty = unrestricted); ``write_roots``
    authorizes directories outside the workspace per call."""

    name = "Write"
    description = load_description("write")
    read_only = False
    clearable = True
    input_schema: dict[str, Any]

    def __init__(
        self,
        *,
        write_roots: WriteRoots | None = None,
        allowed_path_globs: Sequence[str] = (),
    ) -> None:
        self.input_schema = _input_schema()
        self.write_roots = write_roots
        self.allowed_path_globs = tuple(sorted(allowed_path_globs))

    def _path_allowed(self, rel: str) -> bool:
        if not self.allowed_path_globs:
            return True
        parts = tuple(p for p in rel.split("/") if p not in ("", "."))
        for pat in self.allowed_path_globs:
            try:
                compiled = compile_glob(pat)
            except ValueError:
                continue  # an unusable operator glob admits nothing
            if glob_matches(parts, compiled):
                return True
        return False

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        path = require_str(input, "file_path")
        if path is None:
            return tool_error(self.name, "requires non-empty 'file_path'")
        content = input.get("content")
        if not isinstance(content, str):
            return tool_error(self.name, "requires string 'content'")
        body = content.encode("utf-8")
        if len(body) > WRITE_FILE_MAX_BYTES:
            return tool_error(
                self.name, f"content {len(body)}B exceeds {WRITE_FILE_MAX_BYTES}B cap"
            )
        env = ctx.env
        ws = await Workspace.of(env)
        try:
            resolved = await ws.resolve_write(
                path, await authorized_roots(ws, self.write_roots, ctx)
            )
        except PathError as exc:
            return tool_error(self.name, str(exc))
        # The allow-list is checked on the canonical form, so ``..`` and
        # symlink escapes are already collapsed.
        rel = ws.relative(resolved)
        if not self._path_allowed(rel):
            allowed = ", ".join(self.allowed_path_globs)
            return tool_error(
                self.name,
                f"path {rel!r} is outside the writable allow-list "
                f"({allowed}); this agent may only write matching paths",
            )
        st = await env.stat(resolved)
        overwrite = st is not None
        before_text = ""
        if st is not None:
            if st.is_dir:
                return tool_error(self.name, f"not a file: {path!r}")
            try:
                existing = await env.read_bytes(resolved)
            except OSError as exc:
                return tool_error(self.name, f"read failed: {exc}")
            refused = read_precondition_error(
                ctx, resolved, existing, tool_name=self.name, verb="overwriting"
            )
            if refused is not None:
                return refused
            try:
                before_text = existing.decode("utf-8")
            except UnicodeDecodeError:
                return tool_error(self.name, f"{path!r} is not utf-8 text")

        diff = compute_diff(before_text, content, rel)
        added, removed = diff_stat_counts(diff)
        try:
            # ``write_bytes`` creates missing parents: a new module usually
            # starts with its first file.
            await env.write_bytes(resolved, body)
        except OSError as exc:
            return tool_error(self.name, f"write failed: {exc}")
        await record_written(ctx, resolved, body)
        output = (
            f"The file {rel} has been updated."
            if overwrite
            else f"File created successfully at: {rel}"
        )
        return ToolOutput(
            content=[Text(text=output)],
            meta={
                "path": rel,
                "diff": diff,
                "added": added,
                "removed": removed,
                "bytes": len(body),
                "created": not overwrite,
            },
            display=diff_display(rel, diff, added, removed),
        )
