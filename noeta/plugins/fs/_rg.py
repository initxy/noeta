"""ripgrep plumbing shared by ``Glob`` and ``Grep``, plus the glob dialect.

Both tools shell out to ``rg`` through ``ExecEnv.run`` — the reference
agent's engine, so the walk semantics (gitignore-aware, hidden skipped,
symlinks not followed, binary skipped) and the regex dialect (linear-time, no
lookaround or backreferences) are the ones the model was trained on, the same
on the host and inside a container. ``rg`` is a hard requirement; a missing
binary fails loud with an install hint.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from noeta.core.execenv import ExecEnv, RunResult

__all__ = [
    "MAX_LINE_CHARS",
    "RG_MISSING",
    "RG_OUTPUT_CAP",
    "RG_TIMEOUT_S",
    "FileHits",
    "clip_line",
    "compile_glob",
    "decode_rg_events",
    "expand_braces",
    "glob_matches",
    "looks_relative",
    "run_rg",
]

RG_TIMEOUT_S = 60
#: Ceiling on captured rg output — far above what the display caps show. A
#: capped ``--json`` stream ends mid-line; the decoder skips the partial tail.
RG_OUTPUT_CAP = 16 * 1024 * 1024

RG_MISSING = (
    "ripgrep (rg) is required but was not found in the execution environment "
    "— install it (https://github.com/BurntSushi/ripgrep) or add it to the "
    "sandbox image"
)

#: Per-line visible-char cap in ``Grep`` content output.
MAX_LINE_CHARS = 2000
_LINE_TRUNC_MARKER = " … [line truncated]"


async def run_rg(env: ExecEnv, argv: list[str], cwd: str) -> RunResult | str:
    """One bounded rg run; a ``str`` return is the model-facing failure."""
    try:
        outcome = await env.run(
            argv, cwd=cwd, timeout=RG_TIMEOUT_S, output_cap=RG_OUTPUT_CAP
        )
    except OSError:
        return RG_MISSING
    if outcome.timed_out:
        return f"search timed out after {RG_TIMEOUT_S}s — narrow the pattern or path"
    if outcome.exit_code == 127:  # how a shell in between reports a missing binary
        return RG_MISSING
    return outcome


def looks_relative(pattern: str) -> bool:
    """Patterns must be relative to the searched root: a leading ``/`` or
    ``..`` is rejected. An absolute pattern is unbounded (``/**/*.py`` walks
    the whole filesystem); scoping is the ``path`` argument's job."""
    if not pattern:
        return False
    return not (pattern.startswith(("/", "..")))


def clip_line(line: str) -> str:
    """Cap one (ending-free) line's visible chars with a truncation marker."""
    if len(line) <= MAX_LINE_CHARS:
        return line
    return line[:MAX_LINE_CHARS] + _LINE_TRUNC_MARKER


# ---------------------------------------------------------------------------
# rg --json decoding
# ---------------------------------------------------------------------------


@dataclass
class FileHits:
    """One file's rows decoded from the ``rg --json`` event stream."""

    rel: str
    #: ``(1-based line number, text, is_match)`` in stream order.
    rows: list[tuple[int, str, bool]]
    match_lines: int = 0


def _rg_lines(text: str) -> list[str]:
    """Split rg's ``lines.text`` on ``\\n`` only (rg's own boundary), dropping
    each line's ``\\r\\n`` / ``\\n`` ending."""
    parts = text.split("\n")
    if len(parts) > 1 and parts[-1] == "":
        parts.pop()
    return [p.removesuffix("\r") for p in parts]


def decode_rg_events(
    stdout: bytes,
    rel_of: Callable[[str], str],
    *,
    only_matching: bool = False,
) -> list[FileHits]:
    """Fold ``rg --json`` events into per-file ordered line rows.

    A multiline match spans several lines and each spanned line counts as a
    match line; with ``only_matching`` each submatch is its own row (``rg
    -o``) and context events are dropped. Binary files never emit match
    events, so they drop out; a non-utf8 path or line (rg encodes those as
    base64 ``bytes``) is skipped. A cap-truncated stream just ends mid-line:
    the partial tail fails to parse and everything before it renders.
    """
    by_path: dict[str, FileHits] = {}
    order: list[FileHits] = []
    for raw in stdout.split(b"\n"):
        if not raw:
            continue
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") not in ("match", "context"):
            continue
        data = event.get("data") or {}
        path_text = (data.get("path") or {}).get("text")
        text = (data.get("lines") or {}).get("text")
        first_line = data.get("line_number")
        if path_text is None or text is None or first_line is None:
            continue
        is_match = event["type"] == "match"
        if only_matching and not is_match:
            continue
        hits = by_path.get(path_text)
        if hits is None:
            hits = FileHits(rel=rel_of(path_text), rows=[])
            by_path[path_text] = hits
            order.append(hits)
        if only_matching:
            for sub in data.get("submatches") or []:
                sub_text = (sub.get("match") or {}).get("text")
                if sub_text is None:
                    continue
                for j, part in enumerate(_rg_lines(sub_text)):
                    hits.rows.append((first_line + j, part, True))
                    hits.match_lines += 1
            continue
        for i, line in enumerate(_rg_lines(text)):
            hits.rows.append((first_line + i, line, is_match))
            if is_match:
                hits.match_lines += 1
    return order


# ---------------------------------------------------------------------------
# Glob dialect: pathlib-style segments (``*`` / ``?`` / ``[...]`` stay inside
# one segment, ``**`` spans zero or more) plus ``{a,b}`` alternatives — the
# dialect ``Grep``'s ripgrep ``glob`` already speaks.
# ---------------------------------------------------------------------------

#: Ceiling on the alternatives one pattern may expand to.
_MAX_BRACE_EXPANSIONS = 256


def _split_top_level(body: str) -> list[str]:
    """``body`` split on commas outside any nested ``{...}``."""
    parts: list[str] = []
    depth = 0
    last = 0
    for i, ch in enumerate(body):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(body[last:i])
            last = i + 1
    parts.append(body[last:])
    return parts


def expand_braces(pattern: str) -> list[str]:
    """Every alternative of ``pattern``'s ``{a,b}`` groups (nested allowed).
    A group without a top-level comma (``{x}``) or an unbalanced brace stays
    literal, as in ripgrep and bash."""
    search_from = 0
    while True:
        open_at = pattern.find("{", search_from)
        if open_at < 0:
            return [pattern]
        depth = 0
        close_at = -1
        for i in range(open_at, len(pattern)):
            if pattern[i] == "{":
                depth += 1
            elif pattern[i] == "}":
                depth -= 1
                if depth == 0:
                    close_at = i
                    break
        if close_at < 0:
            return [pattern]
        alts = _split_top_level(pattern[open_at + 1 : close_at])
        if len(alts) > 1:
            head, tail = pattern[:open_at], pattern[close_at + 1 :]
            out: list[str] = []
            for alt in alts:
                for expanded in expand_braces(head + alt + tail):
                    out.append(expanded)
                    if len(out) > _MAX_BRACE_EXPANSIONS:
                        raise ValueError(
                            f"glob {pattern!r} expands to more than "
                            f"{_MAX_BRACE_EXPANSIONS} alternatives"
                        )
            return out
        search_from = open_at + 1


def _segments(pattern: str) -> tuple[str, ...]:
    return tuple(seg for seg in pattern.split("/") if seg not in ("", "."))


def _match(parts: Sequence[str], pats: Sequence[str]) -> bool:
    if not pats:
        return not parts
    head, rest = pats[0], pats[1:]
    if head == "**":
        return any(_match(parts[i:], rest) for i in range(len(parts) + 1))
    return bool(parts) and fnmatch.fnmatchcase(parts[0], head) and _match(parts[1:], rest)


def compile_glob(pattern: str) -> tuple[tuple[str, ...], ...]:
    """``pattern`` pre-split into its brace alternatives' segments. Raises
    ``ValueError`` for a pattern with too many alternatives."""
    return tuple(_segments(p) for p in expand_braces(pattern))


def glob_matches(parts: Sequence[str], compiled: tuple[tuple[str, ...], ...]) -> bool:
    """Whether the relative path ``parts`` matches any compiled alternative."""
    return any(_match(parts, pats) for pats in compiled)
