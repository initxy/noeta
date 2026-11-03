"""``Write``: create a file, or overwrite one the model has read.

Creating a new file always works — nothing could have been read — and
missing parent directories are created. Overwriting an existing file needs a
read whose digest still matches, so the model never clobbers a file it has
not seen. The diff rides in ``meta`` and ``display``.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput
from noeta.core.types import Text
from noeta.plugins.fs._encoding import decode, encode, not_text, refusal
from noeta.plugins.fs._globs import compile_glob, glob_matches
from noeta.plugins.fs._mutate import (
    WriteRoots,
    authorized_roots,
    diff_display,
    file_diff,
    mutation_result,
    read_precondition_error,
    record_written,
    unread_write_allowed,
)
from noeta.toolkit.tools import (
    SCHEMA_DIALECT,
    PathError,
    Workspace,
    input_error,
    load_description,
    require_str,
    tool_error,
)

__all__ = ["WRITE_FILE_MAX_BYTES", "WriteTool"]

#: Hard cap on one ``Write`` body — a runaway-file guard.
WRITE_FILE_MAX_BYTES = 8 * 1024 * 1024



def _input_schema() -> dict[str, Any]:
    return {
        "$schema": SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "file_path": {"type": "string",
                          "description": ("Absolute path of the file to write (an absolute "
                                          "path, not a relative one).")},
            "content": {"type": "string", "description": "The whole content of the file."},
        },
        "required": ["file_path", "content"],
        "additionalProperties": False,
    }


class WriteTool:
    """``Write``. ``allowed_path_globs`` confines it to workspace-relative
    paths matching one of the globs (empty = unrestricted); ``write_roots``
    authorizes directories outside the workspace per call."""

    name = "Write"
    description = load_description("noeta.plugins.fs", "write")
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
            return input_error(f"{self.name} requires non-empty 'file_path'")
        content = input.get("content")
        if not isinstance(content, str):
            return input_error(f"{self.name} requires string 'content'")
        body = content.encode("utf-8")
        if len(body) > WRITE_FILE_MAX_BYTES:
            return input_error(f"content {len(body)}B exceeds {WRITE_FILE_MAX_BYTES}B cap")
        env = ctx.env
        ws = await Workspace.of(env)
        try:
            resolved = await ws.resolve_write(
                path, await authorized_roots(ws, self.write_roots, ctx)
            )
        except PathError as exc:
            return input_error(str(exc))
        # The allow-list is checked on the canonical form, so ``..`` and
        # symlink escapes are already collapsed.
        rel = ws.relative(resolved)
        if not self._path_allowed(rel):
            allowed = ", ".join(self.allowed_path_globs)
            return input_error(f"path {rel!r} is outside the writable allow-list "
                               f"({allowed}); this agent may only write matching paths")
        st = await env.stat(resolved)
        overwrite = st is not None
        before_text = ""
        if st is not None:
            if st.is_dir:
                return input_error(f"{path} is a directory. To create a file in it, "
                                   "put the file's name at the end of file_path.")
            try:
                existing = await env.read_bytes(resolved)
            except OSError as exc:
                return tool_error(self.name, f"read failed: {exc}")
            refused = read_precondition_error(
                ctx, resolved, existing, tool_name=self.name, verb="overwriting",
                unread_ok=unread_write_allowed(ctx),
            )
            if refused is not None:
                return refused
            reason = not_text(existing)
            if reason is not None:
                return input_error(refusal(reason))
            before_text = decode(existing)
            # Read never shows a byte-order mark, so new content lacks it;
            # the file keeps its own, and its encoding (Claude Code's).
            body = encode(content, existing)

        diff, added, removed = await file_diff(before_text, content, rel)
        skipped = not diff and before_text != content
        try:
            # ``write_bytes`` creates missing parents: a new module usually
            # starts with its first file.
            await env.write_bytes(resolved, body)
        except OSError as exc:
            return tool_error(self.name, f"write failed: {exc}")
        await record_written(ctx, resolved, body)
        output = mutation_result(resolved, created=not overwrite)
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
            display=diff_display(rel, diff, added, removed, skipped=skipped),
        )
