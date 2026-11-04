"""``Edit``: replace an exact, unique string in a file the model has read,
or create a file (an empty ``old_string``).

The match must be exactly once — zero or several matches fail and write
nothing (unless ``replace_all``). There is deliberately no unified-diff
applier: an invalid patch lands too easily on a file that shifted underneath.
The result is one line naming the file (Claude Code's); the diff rides in
``meta`` and ``display``.
"""

from __future__ import annotations

import re
from typing import Any

from noeta.core.tool import ToolContext, ToolOutput
from noeta.core.types import Text
from noeta.plugins.fs._encoding import decode, encode, not_text, refusal
from noeta.plugins.fs._missing import missing_file_message
from noeta.plugins.fs._mutate import (
    WriteRoots,
    authorized_roots,
    changed_since_read,
    diff_display,
    file_diff,
    mutation_result,
    read_precondition_error,
    record_written,
    seen_whole,
    stale_error,
    unread_write_allowed,
    write_target,
)
from noeta.toolkit.tools import (
    SCHEMA_DIALECT,
    PathError,
    Workspace,
    format_size,
    input_error,
    load_description,
    require_str,
    tool_error,
)

__all__ = ["EDIT_MAX_BYTES", "EditTool"]

#: Largest file ``Edit`` changes (Claude Code's 1 GB).
EDIT_MAX_BYTES = 1024 * 1024 * 1024

def _to_eol(text: str, eol: str) -> str:
    lf = text.replace("\r\n", "\n")
    return lf if eol == "\n" else lf.replace("\n", eol)


_LEFT_SINGLE, _RIGHT_SINGLE, _LEFT_DOUBLE, _RIGHT_DOUBLE = "\u2018", "\u2019", "\u201c", "\u201d"
_STRAIGHTEN = str.maketrans({_LEFT_SINGLE: "'", _RIGHT_SINGLE: "'",
                             _LEFT_DOUBLE: '"', _RIGHT_DOUBLE: '"'})
#: What may come right before an opening quote.
_OPENERS = frozenset(" \t\n\r([{\u2014\u2013")


def _as_in_file(before: str, old: str) -> str | None:
    """The file's own text for ``old`` when they differ only in curly versus
    straight quotes (the model types straight ones), else ``None``."""
    at = before.translate(_STRAIGHTEN).find(old.translate(_STRAIGHTEN))
    return before[at:at + len(old)] if at >= 0 else None


def _curl(text: str, found: str) -> str:
    """``text``'s straight quotes curled the way the matched file text uses
    curly ones, so a replacement keeps the file's typography."""
    doubles = _LEFT_DOUBLE in found or _RIGHT_DOUBLE in found
    singles = _LEFT_SINGLE in found or _RIGHT_SINGLE in found
    out = []
    for i, ch in enumerate(text):
        opens = i == 0 or text[i - 1] in _OPENERS
        if ch == '"' and doubles:
            ch = _LEFT_DOUBLE if opens else _RIGHT_DOUBLE
        elif ch == "'" and singles:
            between_letters = (0 < i < len(text) - 1 and text[i - 1].isalpha()
                               and text[i + 1].isalpha())
            ch = _RIGHT_SINGLE if between_letters or not opens else _LEFT_SINGLE
        out.append(ch)
    return "".join(out)


_ESCAPE = re.compile(r"(\\\\)|\\u([0-9a-fA-F]{4})")
_NON_ASCII = re.compile(r"[\u0080-\uffff]")
_HAS_ESCAPE = re.compile(r"\\u[0-9a-fA-F]{4}")


def _unescape(text: str) -> str:
    """``\\uXXXX`` escapes turned into their characters (an escaped
    backslash stays as it is)."""
    return _ESCAPE.sub(lambda m: m.group(1) or chr(int(m.group(2), 16)), text)


def _escaped_in_file(before: str, old: str) -> tuple[str, dict[str, str]] | None:
    """The file's own text for ``old`` when the file spells ``old``'s
    non-ASCII characters as ``\\uXXXX`` escapes, with the hex spelling the
    file uses for each character."""
    pattern = "".join(
        "\\\\u" + "".join(f"[{d}{d.upper()}]" if d.isalpha() else d
                          for d in f"{ord(ch):04x}")
        if ord(ch) >= 0x80 else re.escape(ch)
        for ch in old)
    m = re.search(pattern, before)
    if m is None:
        return None
    found, spelled, at = m.group(0), {}, 0
    for ch in old:
        if ord(ch) >= 0x80:
            spelled[ch] = found[at + 2:at + 6]
            at += 6
        else:
            at += 1
    return found, spelled


def _escape_like(text: str, spelled: dict[str, str]) -> str:
    """``text``'s non-ASCII characters as ``\\uXXXX``, in the hex case the
    file uses."""
    upper = sum(c.isupper() for h in spelled.values() for c in h)
    lower = sum(c.islower() for h in spelled.values() for c in h)

    def one(m: re.Match[str]) -> str:
        ch = m.group(0)
        hexa = spelled.get(ch) or f"{ord(ch):04x}"
        return "\\u" + (hexa.upper() if upper > lower and ch not in spelled else hexa)

    return _NON_ASCII.sub(one, text)


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
    """``Edit``. ``write_roots`` fences it to the workspace and the
    directories it names per call (``None``: no fence, the permission layer
    decides)."""

    name = "Edit"
    description = load_description("noeta.plugins.fs", "edit")
    read_only = False
    clearable = True
    input_schema: dict[str, Any]

    def __init__(self, *, write_roots: WriteRoots | None = None) -> None:
        self.input_schema = _input_schema()
        self.write_roots = write_roots

    def _replace(self, before: str, old: str, new: str, replace_all: bool, stale: bool,
                 outright: bool) -> tuple[int, str] | ToolOutput:
        """``(matches, new content)``, or why the edit cannot be made."""
        # ``Read`` shows lines without their ``\r``, so the model writes bare
        # ``\n``. In a CRLF file, match and write with the file's own ending —
        # never a mixed file.
        asked = old
        eol = _dominant_eol(before)
        if eol == "\r\n":
            old_eol, new_eol = _to_eol(old, "\r\n"), _to_eol(new, "\r\n")
            # Also when neither matches: the quote and escape fallbacks
            # below then look for the file's own line ends.
            if before.count(old_eol) or old not in before:
                old, new = old_eol, new_eol
        tried_escapes = False
        if old not in before and (found := _as_in_file(before, old)) is not None:
            old, new = found, _curl(new, found)
        elif old not in before and _HAS_ESCAPE.search(old):
            # The model wrote \uXXXX escapes for characters the file holds.
            tried_escapes = True
            if (plain := _unescape(old)) != old and plain in before:
                old, new = plain, _unescape(new)
        elif old not in before and _NON_ASCII.search(old):
            # The model wrote characters the file spells as \uXXXX escapes.
            tried_escapes = True
            if (spelled := _escaped_in_file(before, old)) is not None:
                old, new = spelled[0], _escape_like(new, spelled[1])
        count = before.count(old)
        # Claude Code applies an edit over changes made since the read when
        # its permissions allow the write outright and old_string still
        # picks one place (or replace_all); otherwise the model must re-read.
        if stale and (not outright or count == 0 or (count > 1 and not replace_all)):
            return stale_error(self.name, "editing")
        if count == 0:
            hint = ("\n(Edit also tried \\uXXXX escapes and the characters they stand for, "
                    "both ways; neither matched, so the difference is elsewhere in old_string. "
                    "Read the file again and copy the exact text.)" if tried_escapes else "")
            return input_error(f"old_string does not occur in the file.\nLooked for: {asked}{hint}")
        if count > 1 and not replace_all:
            return input_error(
                f"old_string occurs {count} times in the file and replace_all is "
                f"false, so nothing was changed. Set replace_all to true to change "
                f"all {count}, or add nearby lines to old_string until it matches "
                "only one.")
        # Deleting whole lines without their line break would leave a blank
        # line behind; Claude Code takes the break along.
        if not new and not old.endswith("\n") and old + eol in before:
            old += eol
        return count, (before.replace(old, new) if replace_all
                       else before.replace(old, new, 1))

    async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
        path = require_str(input, "file_path")
        if path is None:
            return input_error(f"{self.name} requires non-empty 'file_path'")
        old = input.get("old_string")
        if not isinstance(old, str):
            return input_error(f"{self.name} requires string 'old_string'")
        new = input.get("new_string")
        if not isinstance(new, str):
            return input_error(f"{self.name} requires string 'new_string'")
        if old == new:
            return input_error("old_string and new_string must be different")
        replace_all = bool(input.get("replace_all"))

        env = ctx.env
        ws = await Workspace.of(env)
        try:
            resolved = await write_target(
                ws, path, await authorized_roots(ws, self.write_roots, ctx))
        except PathError as exc:
            return input_error(str(exc))
        st = await env.stat(resolved)
        if st is not None and st.is_dir:
            return input_error(f"not a file: {path!r}")
        if st is not None and st.size > EDIT_MAX_BYTES:
            return input_error(f"At {format_size(st.size)} the file is too large for "
                               f"Edit, which takes files up to "
                               f"{format_size(EDIT_MAX_BYTES)}.")
        if st is None:
            # An empty old_string creates the file (Claude Code's).
            if old:
                return input_error(await missing_file_message(ws, resolved))
            raw = b""
        else:
            try:
                raw = await env.read_bytes(resolved)
            except OSError as exc:
                return tool_error(self.name, f"read failed: {exc}")
        before = decode(raw)
        stale = False
        if not old:
            # Creating a file, or filling an empty one: nothing to have read.
            if before.strip():
                return input_error(
                    "An empty old_string creates a new file, but this file already "
                    "has content. Give the text to replace instead.")
            in_view = st is None
            count, after = 1, new
        else:
            if resolved.endswith(".ipynb"):
                return input_error("This is a Jupyter notebook: change its cells with "
                                   "NotebookEdit.")
            refused = read_precondition_error(
                ctx, resolved, raw, tool_name=self.name, verb="editing",
                unread_ok=unread_write_allowed(ctx), partial_ok=True, check_stale=False,
            )
            if refused is not None:
                return refused
            reason = not_text(raw)
            if reason is not None:
                return input_error(refusal(reason))
            stale = changed_since_read(ctx, resolved, raw)
            in_view = seen_whole(ctx, resolved, raw)
            planned = self._replace(before, old, new, replace_all, stale,
                                    unread_write_allowed(ctx))
            if isinstance(planned, ToolOutput):
                return planned
            count, after = planned
        rel = ws.relative(resolved)
        diff, added, removed = await file_diff(before, after, rel)
        skipped = not diff and before != after
        # The file's own encoding and byte-order mark (Claude Code's).
        after_bytes = encode(after, raw)
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
            display=diff_display(rel, diff, added, removed, skipped=skipped),
        )
