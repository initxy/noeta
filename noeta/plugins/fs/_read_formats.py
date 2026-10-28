"""``Read`` for the two structured formats Claude Code's Read understands:
PDF documents (pages rendered to images) and Jupyter notebooks (cells with
their outputs). The wording, page caps and size limits are Claude Code's.

PDF pages are rendered by poppler's ``pdftoppm`` (page count from
``pdfinfo``) through the ``ExecEnv``, so a container renders inside itself
exactly as the host does locally; no Python dependency.
"""

from __future__ import annotations

import base64
import binascii
import json
import posixpath
import re
from typing import Any

from noeta.core.execenv import ExecEnv, RunResult
from noeta.core.tool import ToolOutput
from noeta.core.types import Image, Text
from noeta.toolkit.tools import format_size, tool_error

__all__ = [
    "MAX_READ_TOKENS",
    "NOTEBOOK_MAX_BYTES",
    "PDF_MAX_PAGES_PER_READ",
    "PDF_MAX_PAGES_WHOLE",
    "estimate_tokens",
    "read_notebook",
    "read_pdf",
    "too_many_tokens",
]


#: Claude Code's Read token ceiling (``CLAUDE_CODE_FILE_READ_MAX_OUTPUT_TOKENS``
#: default).
MAX_READ_TOKENS = 25000
#: Pages one ``Read`` of a PDF may render (Claude Code: 20).
PDF_MAX_PAGES_PER_READ = 20
#: A PDF with more pages than this must be read with ``pages`` (Claude Code: 10).
PDF_MAX_PAGES_WHOLE = 10
#: Largest notebook ``Read`` opens (Claude Code: 100 MB).
NOTEBOOK_MAX_BYTES = 104_857_600
#: Characters of cell output past which a cell's outputs are replaced by a
#: pointer (Claude Code: 10,000).
_NOTEBOOK_OUTPUTS_MAX_CHARS = 10_000
#: Characters of one output text kept before a line-count note (Claude Code's
#: ``BASH_MAX_OUTPUT_LENGTH`` default, 30,000).
_NOTEBOOK_OUTPUT_TEXT_MAX_CHARS = 30_000

_PDF_TIMEOUT_S = 120.0
_PDF_OUTPUT_CAP = 1024 * 1024
_PAGES_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d*)\s*)?$")
_PAGE_FILE_RE = re.compile(r"^page-\d+\.jpg$")


def estimate_tokens(text: str, suffix: str) -> int:
    """Claude Code's token estimate for a read: characters / 4, or / 2 for
    JSON-shaped files."""
    per_token = 2 if suffix in ("json", "jsonl", "jsonc") else 4
    return round(len(text) / per_token)


def too_many_tokens(tokens: int, limit: int = MAX_READ_TOKENS) -> str:
    return (
        f"File content ({tokens} tokens) exceeds maximum allowed tokens "
        f"({limit}). Use offset and limit parameters to read "
        "specific portions of the file, search for specific content "
        "instead of reading the whole file, or set allow_large when you need all of it."
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _parse_pages(spec: str) -> tuple[int, int | None] | None:
    """``"3"`` -> (3, 3); ``"1-5"`` -> (1, 5); ``"4-"`` -> (4, None)."""
    m = _PAGES_RE.match(spec)
    if m is None:
        return None
    first = int(m.group(1))
    if m.group(2) is None:
        last: int | None = first
    elif m.group(2) == "":
        last = None
    else:
        last = int(m.group(2))
    if first < 1 or (last is not None and last < first):
        return None
    return first, last


async def _run(env: ExecEnv, argv: list[str], cwd: str) -> RunResult | None:
    """One bounded helper-program run; ``None`` when the program is missing."""
    try:
        return await env.run(
            argv, cwd=cwd, timeout=_PDF_TIMEOUT_S, output_cap=_PDF_OUTPUT_CAP
        )
    except OSError:
        return None


async def _page_count(env: ExecEnv, path: str) -> int | None:
    outcome = await _run(env, ["pdfinfo", path], posixpath.dirname(path))
    if outcome is None or outcome.exit_code != 0:
        return None
    m = re.search(rb"^Pages:\s+(\d+)", outcome.stdout, re.MULTILINE)
    return int(m.group(1)) if m else None


async def read_pdf(
    tool_name: str,
    env: ExecEnv,
    path: str,
    display: str,
    size: int,
    pages: Any,
) -> ToolOutput:
    """Render a PDF's pages to JPEG images the model sees.

    Without ``pages`` a PDF of up to ten pages is rendered whole; a longer one
    is refused with Claude Code's message. Claude Code sends a short whole PDF
    as a document block instead; noeta has no document block, so it is
    rendered page by page like a ``pages`` read.
    """

    def err(message: str) -> ToolOutput:
        return tool_error(tool_name, message)

    total = await _page_count(env, path)
    if pages is None:
        if total is not None and total > PDF_MAX_PAGES_WHOLE:
            return err(
                f"This PDF has {total} pages, which is too many to read at "
                "once. Use the pages parameter to read specific page ranges "
                '(e.g., pages: "1-5"). Maximum '
                f"{PDF_MAX_PAGES_PER_READ} pages per request."
            )
        first, last = 1, total
    else:
        spec = str(pages)
        parsed = _parse_pages(spec)
        if parsed is None:
            return err(
                f'Invalid pages parameter: "{spec}". Give one page ("3") or '
                'one range ("1-5"), not a list. To read several pages or '
                "ranges, read each one separately. Pages are 1-indexed."
            )
        first, last = parsed
        span = PDF_MAX_PAGES_PER_READ + 1 if last is None else last - first + 1
        if span > PDF_MAX_PAGES_PER_READ:
            return err(
                f'Page range "{spec}" exceeds maximum of '
                f"{PDF_MAX_PAGES_PER_READ} pages per request. Please use a "
                "smaller range."
            )
        if total is not None and first > total:
            return err(
                f'Page range "{spec}" is outside the document (PDF has '
                f"{total} pages). Use a range within 1-{total}."
            )
    if total is not None:
        last = total if last is None else min(last, total)

    cwd = posixpath.dirname(path)
    made = await _run(env, ["mktemp", "-d"], cwd)
    if made is None:
        return err("read failed: could not make a directory for the pages")
    out_dir = made.stdout.decode("utf-8", "replace").strip()
    if made.exit_code != 0 or not posixpath.isabs(out_dir):
        return err("read failed: could not make a directory for the pages")
    try:
        argv = ["pdftoppm", "-jpeg", "-r", "100", "-f", str(first)]
        if last is not None:
            argv += ["-l", str(last)]
        argv += [path, posixpath.join(out_dir, "page")]
        outcome = await _run(env, argv, cwd)
        if outcome is None or outcome.exit_code == 127:
            return err(
                "pdftoppm is not installed. Install poppler-utils (e.g. `brew "
                "install poppler` or `apt-get install poppler-utils`) to "
                "enable PDF page rendering."
            )
        if outcome.exit_code != 0:
            stderr = outcome.stderr.decode("utf-8", "replace").strip()
            if "Incorrect password" in stderr:
                return err(
                    "PDF is password-protected. Please provide an unprotected "
                    "version."
                )
            return err(f"Could not render PDF: {stderr or 'pdftoppm failed'}")
        listing = await _run(env, ["ls", "-1", out_dir], cwd)
        names = (
            sorted(
                name
                for name in listing.stdout.decode("utf-8", "replace").splitlines()
                if _PAGE_FILE_RE.match(name)
            )
            if listing is not None and listing.exit_code == 0
            else []
        )
        if not names:
            return err("pdftoppm produced no output pages. The PDF may be invalid.")
        images = [
            Image(
                media_type="image/jpeg",
                data=await env.read_bytes(posixpath.join(out_dir, name)),
            )
            for name in names
        ]
    finally:
        await _run(env, ["rm", "-rf", out_dir], cwd)

    count = len(images)
    text = (
        f"PDF pages extracted: {count} page(s) from {display} ({format_size(size)})"
    )
    return ToolOutput(
        content=[Text(text=text), *images],
        meta={"path": display, "pages": count},
    )


# ---------------------------------------------------------------------------
# Notebook
# ---------------------------------------------------------------------------


def _clip_output_text(text: str) -> str:
    """Claude Code keeps an output's first 30,000 characters and counts the
    lines dropped after them."""
    if len(text) <= _NOTEBOOK_OUTPUT_TEXT_MAX_CHARS:
        return text
    rest = text.count("\n", _NOTEBOOK_OUTPUT_TEXT_MAX_CHARS) + 1
    return f"{text[:_NOTEBOOK_OUTPUT_TEXT_MAX_CHARS]}\n... [{rest} lines truncated] ..."


def _joined(value: Any) -> str:
    if isinstance(value, list):
        return "".join(str(v) for v in value)
    return value if isinstance(value, str) else ""


def _output(raw: Any) -> tuple[str, tuple[str, str] | None]:
    """One cell output -> ``(text, (media_type, base64) | None)``."""
    if not isinstance(raw, dict):
        return "", None
    kind = raw.get("output_type")
    if kind == "stream":
        return _clip_output_text(_joined(raw.get("text"))), None
    if kind in ("execute_result", "display_data"):
        data = raw.get("data") or {}
        image: tuple[str, str] | None = None
        for media_type in ("image/png", "image/jpeg"):
            body = data.get(media_type)
            if isinstance(body, str):
                image = (media_type, re.sub(r"\s", "", body))
                break
        return _clip_output_text(_joined(data.get("text/plain"))), image
    if kind == "error":
        traceback = raw.get("traceback") or []
        return (
            _clip_output_text(
                f"{raw.get('ename')}: {raw.get('evalue')}\n"
                + "\n".join(str(t) for t in traceback)
            ),
            None,
        )
    return "", None


def _valid_cells(cells: Any) -> bool:
    if not isinstance(cells, list):
        return False
    for c in cells:
        if not isinstance(c, dict):
            return False
        source = c.get("source")
        if isinstance(source, str):
            continue
        if not (isinstance(source, list) and all(isinstance(s, str) for s in source)):
            return False
    return True


def read_notebook(tool_name: str, raw: bytes, display: str) -> ToolOutput:
    """Every cell of a notebook with its outputs, in Claude Code's form:
    ``<cell id="...">source</cell id="...">`` followed by each output's text,
    and output images for the model to see."""

    def err(message: str) -> ToolOutput:
        return tool_error(tool_name, message)

    if len(raw) > NOTEBOOK_MAX_BYTES:
        return err(
            "Notebook file exceeds the maximum size this tool can read "
            f"({format_size(NOTEBOOK_MAX_BYTES)})."
        )
    try:
        nb = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        return err(
            "Notebook file is not valid JSON (it may be truncated, corrupted, "
            f"or still being written): {exc}"
        )
    cells = nb.get("cells") if isinstance(nb, dict) else None
    if not _valid_cells(cells):
        return err(
            "Notebook file is not a valid Jupyter notebook (top-level "
            '"cells" must be an array of cell objects, each with a string or '
            'string-array "source").'
        )
    assert isinstance(nb, dict) and isinstance(cells, list)
    language = ((nb.get("metadata") or {}).get("language_info") or {}).get(
        "name"
    ) or "python"
    parts: list[str] = []
    images: list[Image] = []
    for index, cell in enumerate(cells):
        cell_id = cell.get("id") or f"cell-{index}"
        cell_type = cell.get("cell_type")
        tags = ""
        if cell_type != "code":
            tags += f"<cell_type>{cell_type}</cell_type>"
        elif language != "python":
            tags += f"<language>{language}</language>"
        parts.append(
            f'<cell id="{cell_id}">{tags}{_joined(cell["source"])}</cell id="{cell_id}">'
        )
        if cell_type != "code" or not cell.get("outputs"):
            continue
        outputs = [_output(o) for o in cell["outputs"]]
        weight = 0
        for text, image in outputs:
            weight += len(text) + (len(image[1]) if image else 0)
        if weight > _NOTEBOOK_OUTPUTS_MAX_CHARS:
            parts.append(
                "\nOutputs are too large to include. Use Bash with: cat "
                f"<notebook_path> | jq '.cells[{index}].outputs'"
            )
            continue
        for text, image in outputs:
            if text:
                parts.append(f"\n{text}")
            if image is not None:
                try:
                    body = base64.b64decode(image[1], validate=True)
                except (binascii.Error, ValueError):
                    continue
                images.append(Image(media_type=image[0], data=body))
    rendered = "\n".join(parts)
    tokens = estimate_tokens(rendered, "ipynb")
    if tokens > MAX_READ_TOKENS:
        return err(too_many_tokens(tokens))
    return ToolOutput(
        content=[Text(text=rendered), *images],
        meta={"path": display, "cells": len(cells)},
    )
