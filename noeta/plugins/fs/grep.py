"""``Grep``: regex search through ripgrep.

``pattern`` is an rg regex and the walk carries rg's defaults: gitignore-aware,
hidden and binary skipped, symlinks not followed. The parameter surface is the
reference agent's: output modes, ``-i``, ``-n`` (content mode, default on),
``context`` with ``-C`` as its alias plus ``-A`` / ``-B``, ``-o``, ``type``,
``head_limit`` (default 250, 0 = unlimited) and ``offset``. ``-u``
(``--no-ignore --hidden``) is this tool's one extension. rg's stream is read
as ``--json`` events and re-rendered so caps and relative paths stay here.
"""

from __future__ import annotations

from typing import Any

from noeta.core.tool import ToolContext, ToolOutput, text_output
from noeta.core.tool_helpers import (
    Workspace,
    limit_output,
    load_description,
    output_text,
    require_str,
    tool_error,
)
from noeta.plugins.fs._rg import (
    FileHits,
    clip_line,
    decode_rg_events,
    looks_relative,
    run_rg,
)

__all__ = ["GrepTool"]

_GREP_OUTPUT_MODES = ("files_with_matches", "content", "count")
#: ``head_limit`` when the model passes none (an explicit 0 lifts it).
_GREP_DEFAULT_HEAD_LIMIT = 250
#: A fence for content mode, not the working rule: a wall of clipped-wide
#: lines is halved until it fits.
_INLINE_OUTPUT_MAX_BYTES = 32 * 1024
#: 0.6's effective inline threshold for ``Grep`` (its own 20,000).
_OUTPUT_LIMIT = 20_000


def _optional_count(arguments: dict[str, Any], key: str) -> int | str | None:
    """A non-negative int argument, ``None`` when absent, error text when bad."""
    value = arguments.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return f"'{key}' must be a non-negative integer"
    return value


def _window(items: list[Any], head_limit: int | None, offset: int) -> list[Any]:
    """``| tail -n +N | head -N``: ``offset`` skips, ``head_limit`` caps."""
    rest = items[offset:] if offset else items
    return rest if head_limit is None else rest[:head_limit]



def _input_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "pattern": {"type": "string"},
            "path": {"type": "string"},
            "glob": {"type": "string"},
            "type": {"type": "string"},
            "output_mode": {"type": "string", "enum": list(_GREP_OUTPUT_MODES)},
            "-i": {"type": "boolean"},
            "-n": {"type": "boolean"},
            "-o": {"type": "boolean"},
            "-u": {"type": "boolean"},
            "-A": {"type": "integer", "minimum": 0},
            "-B": {"type": "integer", "minimum": 0},
            "-C": {"type": "integer", "minimum": 0},
            "context": {"type": "integer", "minimum": 0},
            "head_limit": {"type": "integer", "minimum": 0},
            "offset": {"type": "integer", "minimum": 0},
            "multiline": {"type": "boolean"},
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }


class GrepTool:
    """``Grep``."""

    name = "Grep"
    description = load_description("noeta.plugins.fs", "grep")
    read_only = True
    clearable = True
    input_schema: dict[str, Any]

    def __init__(self) -> None:
        self.input_schema = _input_schema()

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        output = await self._search(input, ctx)
        if output.is_error:
            return output
        text = output_text(output)
        limited = await limit_output(text, _OUTPUT_LIMIT, ctx)
        if limited == text:
            return output
        return text_output(limited, **output.meta)

    async def _search(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        def err(message: str) -> ToolOutput:
            return tool_error(self.name, message)

        pattern = require_str(input, "pattern")
        if pattern is None:
            return err("requires non-empty 'pattern'")
        mode = input.get("output_mode", "files_with_matches")
        if mode not in _GREP_OUTPUT_MODES:
            return err("'output_mode' must be one of: " + ", ".join(_GREP_OUTPUT_MODES))
        case_insensitive = bool(input.get("-i"))
        line_numbers = bool(input.get("-n", True))
        only_matching = bool(input.get("-o")) and mode == "content"
        unrestricted = bool(input.get("-u"))
        multiline = bool(input.get("multiline"))
        after = _optional_count(input, "-A")
        if isinstance(after, str):
            return err(after)
        before = _optional_count(input, "-B")
        if isinstance(before, str):
            return err(before)
        around = _optional_count(input, "context")
        if isinstance(around, str):
            return err(around)
        if around is None:  # ``-C`` is context's alias
            around = _optional_count(input, "-C")
            if isinstance(around, str):
                return err(around)
        head_limit_raw = _optional_count(input, "head_limit")
        if isinstance(head_limit_raw, str):
            return err(head_limit_raw + " (0 means unlimited)")
        head_limit: int | None
        if head_limit_raw is None:
            head_limit = _GREP_DEFAULT_HEAD_LIMIT
        elif head_limit_raw == 0:
            head_limit = None
        else:
            head_limit = head_limit_raw
        offset_raw = _optional_count(input, "offset")
        if isinstance(offset_raw, str):
            return err(offset_raw)
        offset = offset_raw or 0
        ctx_before = around if around is not None else (before or 0)
        ctx_after = around if around is not None else (after or 0)

        path_arg = input.get("path")
        if path_arg is None or path_arg == "":
            path_arg = "."
        if not isinstance(path_arg, str):
            return err("'path' must be a string")
        glob_filter = input.get("glob")
        if glob_filter is not None and not isinstance(glob_filter, str):
            return err("'glob' must be a string")
        if glob_filter and not looks_relative(glob_filter):
            return err(
                "'glob' must be relative to the searched directory (no leading "
                "'/' / '..') — use 'path' to search another tree"
            )
        type_filter = input.get("type")
        if type_filter is not None and not isinstance(type_filter, str):
            return err("'type' must be a string")

        env = ctx.env
        ws = await Workspace.of(env)
        resolved = await ws.canonical(path_arg)
        st = await env.stat(resolved)
        target_is_file = st is not None and not st.is_dir
        partial = False
        hits: list[FileHits] = []
        if st is not None:
            argv = ["rg", "--json", "--no-config", "--no-messages"]
            if not target_is_file:
                # Deterministic single-threaded walk, so a capped result keeps
                # a stable prefix across repeated searches.
                argv.append("--sort=path")
                if unrestricted:
                    argv += ["--no-ignore", "--hidden"]
                # glob / type / ignore filters scope the WALK; an explicitly
                # targeted file is always searched.
                if glob_filter:
                    argv += ["--glob", glob_filter]
                if type_filter:
                    argv += ["--type", type_filter]
            if case_insensitive:
                argv.append("--ignore-case")
            if multiline:
                argv += ["--multiline", "--multiline-dotall"]
            if mode == "content" and not only_matching and (ctx_before or ctx_after):
                argv += [
                    "--before-context", str(ctx_before),
                    "--after-context", str(ctx_after),
                ]
            argv += ["--regexp", pattern, "--", resolved]
            ran = await run_rg(env, argv, ws.root)
            if isinstance(ran, str):
                return err(ran)
            # Exit 2 with stderr is a usage error (bad regex, unknown type).
            # Exit 2 with a SILENT stderr is per-file IO noise: rg still
            # printed every match it could reach, so the search stands.
            reason = ran.stderr.decode("utf-8", errors="replace").strip()
            if ran.exit_code not in (0, 1) and reason:
                return err(f"rg: {reason}")
            partial = ran.stdout_truncated
            hits = decode_rg_events(ran.stdout, ws.relative, only_matching=only_matching)
        # A missing search root yields the empty result: probing a tree that
        # may not exist is routine.

        # A zero-match answer must not read as "nowhere in the tree" when the
        # walk filters part of it — name the escape hatch.
        empty_note = (
            ""
            if unrestricted or target_is_file
            else "\n(hidden and gitignored files are not searched; pass -u: true to include them)"
        )
        if mode == "files_with_matches":
            result = self._files_mode(hits, head_limit, offset, empty_note)
        elif mode == "count":
            result = self._count_mode(hits, head_limit, offset, empty_note)
        else:
            result = self._content_mode(
                hits,
                line_numbers=line_numbers,
                with_context=not only_matching and bool(ctx_before or ctx_after),
                head_limit=head_limit,
                offset=offset,
                empty_note=empty_note,
            )
        if partial:
            # The decoded stream is a prefix; say so rather than letting a
            # short list read as complete.
            text = output_text(result)
            result = text_output(
                text + "\n(rg output exceeded the capture cap — results are "
                "partial; narrow the query.)",
                **result.meta,
            )
        return result

    @staticmethod
    def _files_mode(
        hits: list[FileHits], head_limit: int | None, offset: int, empty_note: str
    ) -> ToolOutput:
        paths = [h.rel for h in hits if h.match_lines]
        shown = _window(paths, head_limit, offset)
        if not shown:
            return text_output("No matches found" + empty_note, files=0)
        output = "\n".join(shown)
        if len(paths) > offset + len(shown):
            output += (
                f"\n(Results truncated: {len(shown)} of {len(paths)} files "
                "shown. Narrow the query or raise head_limit.)"
            )
        return text_output(output, files=len(shown))

    @staticmethod
    def _count_mode(
        hits: list[FileHits], head_limit: int | None, offset: int, empty_note: str
    ) -> ToolOutput:
        with_matches = [h for h in hits if h.match_lines]
        windowed = _window(with_matches, head_limit, offset)
        rows = [f"{h.rel}:{h.match_lines}" for h in windowed]
        total = sum(h.match_lines for h in windowed)
        if not rows:
            return text_output("No matches found" + empty_note, matches=0)
        output = "\n".join(rows)
        if len(with_matches) > offset + len(rows):
            output += f"\n(Results truncated: {len(rows)} of {len(with_matches)} files shown.)"
        return text_output(output, matches=total, files=len(rows))

    @staticmethod
    def _content_mode(
        hits: list[FileHits],
        *,
        line_numbers: bool,
        with_context: bool,
        head_limit: int | None,
        offset: int,
        empty_note: str,
    ) -> ToolOutput:
        # Render everything first, then slice: content-mode head_limit /
        # offset are ``head`` / ``tail`` over OUTPUT lines — context rows and
        # ``--`` separators count.
        rendered: list[tuple[str, bool]] = []  # (output line, is a match line)
        total = 0
        for h in hits:
            if not h.rows:
                continue
            total += h.match_lines
            if rendered and with_context:
                rendered.append(("--", False))
            prev_line: int | None = None
            for line_no, text, is_match in h.rows:
                if with_context and prev_line is not None and line_no > prev_line + 1:
                    rendered.append(("--", False))
                clipped = clip_line(text)
                sep = ":" if is_match else "-"
                if line_numbers:
                    rendered.append((f"{h.rel}{sep}{line_no}{sep}{clipped}", is_match))
                else:
                    rendered.append((f"{h.rel}{sep}{clipped}", is_match))
                prev_line = line_no
        if total == 0:
            return text_output("No matches found" + empty_note, matches=0)
        window = _window(rendered, head_limit, offset)
        # Trim whole lines if a wall of wide lines still overflows the fence;
        # BEFORE counting, so the footer describes the lines actually kept.
        while (
            len(window) > 1
            and len("\n".join(line for line, _ in window).encode("utf-8"))
            > _INLINE_OUTPUT_MAX_BYTES
        ):
            window = window[: max(1, len(window) // 2)]
        shown = sum(1 for _, is_match in window if is_match)
        output = "\n".join(line for line, _ in window)
        if shown < total:
            output += f"\n(Showing {shown} of {total} matches. Narrow the query or use head_limit.)"
        return text_output(output, matches=shown, total=total)
