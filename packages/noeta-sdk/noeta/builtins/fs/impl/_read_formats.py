"""``Read`` for the two structured formats Claude Code's Read understands:
PDF documents (pages rendered to images) and Jupyter notebooks (cells with
their outputs). The wording, page caps and size limits are Claude Code's.

PDF pages are rendered by poppler's ``pdftoppm`` (page count from
``pdfinfo``) through the session's :class:`ExecEnv`, so a sandbox renders
inside its container exactly as the host does locally — the same external
binaries Claude Code shells out to; no Python dependency.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from pathlib import Path
from typing import Any, Optional

from noeta.protocols.tool import ToolContext, ToolResult
from noeta.runtime.exec_env import ExecEnv
from noeta.runtime.workspace import tool_error


__all__ = [
    "MAX_READ_TOKENS",
    "PDF_MAX_PAGES_PER_READ",
    "estimate_tokens",
    "format_size",
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

_PDF_TIMEOUT_S = 120
_PDF_OUTPUT_CAP = 1024 * 1024
_PAGES_RE = re.compile(r"^\s*(\d+)\s*(?:-\s*(\d*)\s*)?$")


def format_size(n: int) -> str:
    """Claude Code's size label: ``512 bytes``, ``48.8KB``, ``1.2MB``."""
    kb = n / 1024
    if kb < 1:
        return f"{n} bytes"
    if kb < 1024:
        return f"{kb:.1f}".removesuffix(".0") + "KB"
    mb = kb / 1024
    if mb < 1024:
        return f"{mb:.1f}".removesuffix(".0") + "MB"
    return f"{mb / 1024:.1f}".removesuffix(".0") + "GB"


def estimate_tokens(text: str, suffix: str) -> int:
    """Claude Code's token estimate for a read: characters / 4, or / 2 for
    JSON-shaped files."""
    per_token = 2 if suffix in ("json", "jsonl", "jsonc") else 4
    return round(len(text) / per_token)


def too_many_tokens(tokens: int) -> str:
    return (
        f"File content ({tokens} tokens) exceeds maximum allowed tokens "
        f"({MAX_READ_TOKENS}). Use offset and limit parameters to read "
        "specific portions of the file, or search for specific content "
        "instead of reading the whole file."
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def _parse_pages(spec: str) -> Optional[tuple[int, Optional[int]]]:
    """``"3"`` → (3, 3); ``"1-5"`` → (1, 5); ``"4-"`` → (4, None)."""
    m = _PAGES_RE.match(spec)
    if m is None:
        return None
    first = int(m.group(1))
    if m.group(2) is None:
        last: Optional[int] = first
    elif m.group(2) == "":
        last = None
    else:
        last = int(m.group(2))
    if first < 1 or (last is not None and last < first):
        return None
    return first, last


def _page_count(exec_env: ExecEnv, path: Path) -> Optional[int]:
    try:
        outcome = exec_env.run_argv(
            ["pdfinfo", str(path)],
            cwd=path.parent,
            timeout_s=_PDF_TIMEOUT_S,
            output_cap=_PDF_OUTPUT_CAP,
        )
    except OSError:
        return None
    if outcome.returncode != 0:
        return None
    m = re.search(rb"^Pages:\s+(\d+)", outcome.stdout, re.MULTILINE)
    return int(m.group(1)) if m else None


def read_pdf(
    tool_name: str,
    exec_env: ExecEnv,
    path: Path,
    display: str,
    size: int,
    pages: Any,
    ctx: ToolContext,
) -> ToolResult:
    """Render a PDF's pages to JPEG images the model sees.

    Without ``pages`` a PDF of up to ten pages is rendered whole; a longer one
    is refused with Claude Code's message. Claude Code sends a short whole PDF
    as a document block instead — noeta's message model has no document
    block, so it is rendered page by page like a ``pages`` read.
    """
    def err(message: str) -> ToolResult:
        return tool_error(tool_name, message)

    total = _page_count(exec_env, path)
    if pages is None:
        if total is not None and total > PDF_MAX_PAGES_WHOLE:
            return err(
                f"This PDF has {total} pages, which is too many to read at "
                'once. Use the pages parameter to read specific page ranges '
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
        span = (
            PDF_MAX_PAGES_PER_READ + 1 if last is None else last - first + 1
        )
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

    try:
        made = exec_env.run_argv(
            ["mktemp", "-d"],
            cwd=path.parent,
            timeout_s=_PDF_TIMEOUT_S,
            output_cap=_PDF_OUTPUT_CAP,
        )
    except OSError as exc:
        return err(f"read failed: {exc}")
    out_dir = Path(made.stdout.decode("utf-8", "replace").strip())
    if made.returncode != 0 or not out_dir.is_absolute():
        return err("read failed: could not make a directory for the pages")
    try:
        argv = ["pdftoppm", "-jpeg", "-r", "100", "-f", str(first)]
        if last is not None:
            argv += ["-l", str(last)]
        argv += [str(path), str(out_dir / "page")]
        try:
            outcome = exec_env.run_argv(
                argv,
                cwd=path.parent,
                timeout_s=_PDF_TIMEOUT_S,
                output_cap=_PDF_OUTPUT_CAP,
            )
        except OSError:
            outcome = None
        if outcome is None or outcome.returncode == 127:
            return err(
                "pdftoppm is not installed. Install poppler-utils (e.g. `brew "
                "install poppler` or `apt-get install poppler-utils`) to "
                "enable PDF page rendering."
            )
        if outcome.returncode != 0:
            stderr = outcome.stderr.decode("utf-8", "replace").strip()
            if "Incorrect password" in stderr:
                return err(
                    "PDF is password-protected. Please provide an unprotected "
                    "version."
                )
            return err(f"Could not render PDF: {stderr or 'pdftoppm failed'}")
        files = sorted(exec_env.glob(out_dir, "page-*.jpg"))
        if not files:
            return err(
                "pdftoppm produced no output pages. The PDF may be invalid."
            )
        images = [
            ctx.artifact_store.put(exec_env.read_bytes(f), media_type="image/jpeg")
            for f in files
        ]
    finally:
        try:
            exec_env.run_argv(
                ["rm", "-rf", str(out_dir)],
                cwd=path.parent,
                timeout_s=_PDF_TIMEOUT_S,
                output_cap=_PDF_OUTPUT_CAP,
            )
        except OSError:
            pass

    count = len(images)
    text = (
        f"PDF pages extracted: {count} page(s) from {display} "
        f"({format_size(size)})"
    )
    return ToolResult(
        success=True,
        output=text,
        summary=f"read {display} (pdf, {count} page(s))",
        images=images,
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


def _output(raw: Any) -> tuple[str, Optional[tuple[str, str]]]:
    """One cell output → ``(text, (media_type, base64) | None)``."""
    if not isinstance(raw, dict):
        return "", None
    kind = raw.get("output_type")
    if kind == "stream":
        return _clip_output_text(_joined(raw.get("text"))), None
    if kind in ("execute_result", "display_data"):
        data = raw.get("data") or {}
        image: Optional[tuple[str, str]] = None
        for media_type in ("image/png", "image/jpeg"):
            body = data.get(media_type)
            if isinstance(body, str):
                image = (media_type, re.sub(r"\s", "", body))
                break
        return _clip_output_text(_joined(data.get("text/plain"))), image
    if kind == "error":
        traceback = raw.get("traceback") or []
        return _clip_output_text(
            f"{raw.get('ename')}: {raw.get('evalue')}\n"
            + "\n".join(str(t) for t in traceback)
        ), None
    return "", None


def read_notebook(
    tool_name: str,
    raw: bytes,
    display: str,
    ctx: ToolContext,
) -> ToolResult:
    """Every cell of a notebook with its outputs, in Claude Code's form:
    ``<cell id="…">source</cell id="…">`` followed by each output's text, and
    output images for the model to see."""
    def err(message: str) -> ToolResult:
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
    if not isinstance(cells, list) or any(
        not isinstance(c, dict)
        or not (
            isinstance(c.get("source"), str)
            or (
                isinstance(c.get("source"), list)
                and all(isinstance(s, str) for s in c["source"])
            )
        )
        for c in cells
    ):
        return err(
            "Notebook file is not a valid Jupyter notebook (top-level "
            '"cells" must be an array of cell objects, each with a string or '
            'string-array "source").'
        )
    language = (
        ((nb.get("metadata") or {}).get("language_info") or {}).get("name")
        or "python"
    )
    parts: list[str] = []
    images = []
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
                images.append(ctx.artifact_store.put(body, media_type=image[0]))
    rendered = "\n".join(parts)
    tokens = estimate_tokens(rendered, "ipynb")
    if tokens > MAX_READ_TOKENS:
        return err(too_many_tokens(tokens))
    return ToolResult(
        success=True,
        output=rendered,
        summary=f"read {display} (notebook, {len(cells)} cells)",
        images=images,
    )
