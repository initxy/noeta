"""``Glob``: file names matching a pattern under one directory.

The walk is ``rg --files`` (gitignore-aware, hidden skipped, symlinks not
followed); the pattern then matches with pathlib glob semantics plus
``{a,b}``, so ``*.py`` stays top-level and ``**/*.py`` recurses. Results are
POSIX paths, newest first, capped with a notice.
"""

from __future__ import annotations

import posixpath
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.plugins.fs._rg import compile_glob, glob_matches, looks_relative, run_rg
from noeta.toolkit.tools import (
    Workspace,
    limit_output,
    load_description,
    require_str,
    tool_error,
)

__all__ = ["GlobTool"]

_MAX_GLOB_MATCHES = 200
#: 0.6's effective inline threshold for ``Glob``: the default host limit
#: (50,000) under the tool's own 100,000.
_OUTPUT_LIMIT = 50_000



def _input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }


class GlobTool:
    """``Glob``."""

    name = "Glob"
    description = load_description("noeta.plugins.fs", "glob")
    read_only = True
    clearable = True
    input_schema: dict[str, Any]

    def __init__(self) -> None:
        self.input_schema = _input_schema()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        pattern = require_str(input, "pattern")
        if pattern is None:
            return tool_error(self.name, "requires non-empty 'pattern'")
        if not looks_relative(pattern):
            return tool_error(
                self.name,
                "pattern must be relative to the searched directory (no leading "
                "'/' / '..') — use 'path' to search another tree",
            )
        path_arg = input.get("path")
        if path_arg is None or path_arg == "":
            path_arg = "."
        if not isinstance(path_arg, str):
            return tool_error(self.name, "'path' must be a string")
        try:
            compiled = compile_glob(pattern)
        except ValueError as exc:
            return tool_error(self.name, str(exc))

        env = ctx.env
        ws = await Workspace.of(env)
        root = await ws.canonical(path_arg)
        st = await env.stat(root)
        if st is None or not st.is_dir:
            # Probing a tree that may not exist is routine: empty, not an error.
            return text_output("No files found", matches=0, total=0)

        ran = await run_rg(
            env, ["rg", "--files", "--no-config", "--no-messages", "--", root], ws.root
        )
        if isinstance(ran, str):
            return tool_error(self.name, ran)
        # Exit 1 = clean walk, zero files. Exit 2 with a silent stderr is
        # per-entry IO noise (--no-messages); only a spoken error fails.
        reason = ran.stderr.decode("utf-8", errors="replace").strip()
        if ran.exit_code not in (0, 1) and reason:
            return tool_error(self.name, f"rg: {reason}")

        entries: list[tuple[float, str]] = []
        for line in ran.stdout.decode("utf-8", errors="replace").splitlines():
            if not line:
                continue
            rel = posixpath.relpath(line, root)
            if rel.startswith(".."):  # rg stays under its root
                continue
            if not glob_matches(tuple(p for p in rel.split("/") if p not in ("", ".")), compiled):
                continue
            try:
                found = await env.stat(line)
            except OSError:
                found = None
            entries.append((found.mtime if found is not None else 0.0, ws.relative(line)))
        # Newest first, alphabetical tiebreak — recency is the model's usual
        # relevance signal when it scans a truncated list top-down.
        entries.sort(key=lambda e: (-e[0], e[1]))
        total = len(entries)
        shown = [rel for _, rel in entries[:_MAX_GLOB_MATCHES]]

        if not shown:
            output = "No files found (hidden and gitignored files are not listed)"
        else:
            output = "\n".join(shown)
            if total > len(shown):
                output += (
                    f"\n(Results truncated: showing {len(shown)} of {total} "
                    "matches, newest first. Narrow the pattern.)"
                )
        if ran.stdout_truncated:
            output += (
                "\n(rg output exceeded the capture cap — the listing is "
                "partial; narrow the pattern or path.)"
            )
        output = await limit_output(output, _OUTPUT_LIMIT, ctx)
        return text_output(output, matches=len(shown), total=total)
