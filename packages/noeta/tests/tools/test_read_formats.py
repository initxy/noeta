"""``Read`` on PDFs (pages as JPEG images via poppler) and Jupyter notebooks
(cells with outputs), as Claude Code's Read does them (ported from 0.6)."""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest
from noeta.execenv.base import RunResult
from noeta.execenv.local import LocalExecEnv
from noeta.tools.read import ReadTool

from .conftest import Harness, images_of, text_of


def _pdf(pages: int) -> bytes:
    """A minimal valid PDF with ``pages`` blank pages."""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        (
            "<< /Type /Pages /Kids ["
            + " ".join(f"{3 + i} 0 R" for i in range(pages))
            + f"] /Count {pages} >>"
        ).encode(),
    ]
    objects += [
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>" for _ in range(pages)
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for n, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{n} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


needs_poppler = pytest.mark.skipif(
    shutil.which("pdftoppm") is None or shutil.which("pdfinfo") is None,
    reason="poppler-utils (pdftoppm / pdfinfo) not installed",
)


@needs_poppler
async def test_a_short_pdf_is_read_whole_as_page_images(h: Harness, ws: Path) -> None:
    (ws / "doc.pdf").write_bytes(_pdf(2))
    out = await h.call(ReadTool(), file_path="doc.pdf")
    assert not out.is_error, text_of(out)
    assert text_of(out).startswith("PDF pages extracted: 2 page(s) from doc.pdf (")
    images = images_of(out)
    assert len(images) == 2
    assert all(img.media_type == "image/jpeg" for img in images)
    assert all(img.data.startswith(b"\xff\xd8\xff") for img in images)


@needs_poppler
async def test_pages_picks_one_page_or_a_range(h: Harness, ws: Path) -> None:
    (ws / "doc.pdf").write_bytes(_pdf(4))
    one = await h.call(ReadTool(), file_path="doc.pdf", pages="3")
    assert len(images_of(one)) == 1
    two = await h.call(ReadTool(), file_path="doc.pdf", pages="2-3")
    assert len(images_of(two)) == 2


@needs_poppler
async def test_page_images_are_cleaned_up(h: Harness, ws: Path) -> None:
    seen: list[str] = []

    class _Spy(LocalExecEnv):
        async def run(self, argv: Sequence[str], **kw: Any) -> RunResult:
            result = await super().run(argv, **kw)
            if argv[0] == "mktemp":
                seen.append(result.stdout.decode().strip())
            return result

    spy = Harness(env=_Spy(str(ws)))
    (ws / "doc.pdf").write_bytes(_pdf(1))
    assert not (await spy.call(ReadTool(), file_path="doc.pdf")).is_error
    [tmp] = seen
    assert not Path(tmp).exists()


@needs_poppler
async def test_a_long_pdf_needs_pages(h: Harness, ws: Path) -> None:
    (ws / "doc.pdf").write_bytes(_pdf(12))
    out = await h.call(ReadTool(), file_path="doc.pdf")
    assert out.is_error
    assert text_of(out) == (
        "Read: This PDF has 12 pages, which is too many to read at once. Use "
        'the pages parameter to read specific page ranges (e.g., pages: "1-5"). '
        "Maximum 20 pages per request."
    )


@needs_poppler
async def test_pages_out_of_the_document_is_refused(h: Harness, ws: Path) -> None:
    (ws / "doc.pdf").write_bytes(_pdf(2))
    out = await h.call(ReadTool(), file_path="doc.pdf", pages="5")
    assert out.is_error
    assert "outside the document (PDF has 2 pages)" in text_of(out)


async def test_bad_or_too_wide_pages_are_refused(h: Harness, ws: Path) -> None:
    (ws / "doc.pdf").write_bytes(_pdf(2))
    bad = await h.call(ReadTool(), file_path="doc.pdf", pages="1,3")
    assert text_of(bad) == (
        'Read: Invalid pages parameter: "1,3". Give one page ("3") or one range '
        '("1-5"), not a list. To read several pages or ranges, read each one '
        "separately. Pages are 1-indexed."
    )
    wide = await h.call(ReadTool(), file_path="doc.pdf", pages="1-21")
    assert text_of(wide) == (
        'Read: Page range "1-21" exceeds maximum of 20 pages per request. '
        "Please use a smaller range."
    )
    open_ended = await h.call(ReadTool(), file_path="doc.pdf", pages="3-")
    assert "exceeds maximum" in text_of(open_ended)


class _NoPoppler(LocalExecEnv):
    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
        output_cap: int | None = None,
    ) -> RunResult:
        if argv[0] in ("pdftoppm", "pdfinfo"):
            raise FileNotFoundError(argv[0])
        return await super().run(
            argv, cwd=cwd, env=env, stdin=stdin, timeout=timeout, output_cap=output_cap
        )


async def test_missing_pdftoppm_says_how_to_install_it(ws: Path) -> None:
    h = Harness(env=_NoPoppler(str(ws)))
    (ws / "doc.pdf").write_bytes(_pdf(1))
    out = await h.call(ReadTool(), file_path="doc.pdf")
    assert text_of(out) == (
        "Read: pdftoppm is not installed. Install poppler-utils (e.g. `brew "
        "install poppler` or `apt-get install poppler-utils`) to enable PDF "
        "page rendering."
    )


@needs_poppler
async def test_a_broken_pdf_reports_the_render_failure(h: Harness, ws: Path) -> None:
    (ws / "doc.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf\n")
    out = await h.call(ReadTool(), file_path="doc.pdf")
    assert out.is_error
    assert text_of(out).startswith("Read: Could not render PDF: ")


# ---------------------------------------------------------------------------
# Notebooks
# ---------------------------------------------------------------------------

_PNG_BYTES = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000154a24f9d0000000049454e44ae426082"
)
_PNG = base64.b64encode(_PNG_BYTES).decode()


def _notebook(cells: list[dict[str, Any]], language: str = "python") -> bytes:
    return json.dumps(
        {"cells": cells, "metadata": {"language_info": {"name": language}}}
    ).encode()


async def test_a_notebook_reads_as_cells_with_outputs(h: Harness, ws: Path) -> None:
    body = _notebook(
        [
            {"cell_type": "markdown", "id": "intro", "source": ["# Title\n", "text"]},
            {
                "cell_type": "code",
                "source": "print(1)",
                "outputs": [
                    {"output_type": "stream", "text": ["1\n"]},
                    {
                        "output_type": "display_data",
                        "data": {"text/plain": "<Figure>", "image/png": _PNG},
                    },
                    {
                        "output_type": "error",
                        "ename": "ValueError",
                        "evalue": "bad",
                        "traceback": ["tb line"],
                    },
                ],
            },
        ]
    )
    (ws / "nb.ipynb").write_bytes(body)
    out = await h.call(ReadTool(), file_path="nb.ipynb")
    assert not out.is_error, text_of(out)
    assert text_of(out) == (
        '<cell id="intro"><cell_type>markdown</cell_type># Title\ntext</cell id="intro">\n'
        '<cell id="cell-1">print(1)</cell id="cell-1">\n'
        "\n1\n\n"
        "\n<Figure>\n"
        "\nValueError: bad\ntb line"
    )
    [img] = images_of(out)
    assert img.media_type == "image/png"
    assert img.data == _PNG_BYTES
    # Recorded like any read, so Edit / Write may follow.
    assert h.fs.digest(str(ws / "nb.ipynb")) == hashlib.sha256(body).hexdigest()


async def test_a_non_python_code_cell_names_its_language(h: Harness, ws: Path) -> None:
    (ws / "nb.ipynb").write_bytes(
        _notebook([{"cell_type": "code", "source": "x", "outputs": []}], "julia")
    )
    out = await h.call(ReadTool(), file_path="nb.ipynb")
    assert text_of(out) == '<cell id="cell-0"><language>julia</language>x</cell id="cell-0">'


async def test_large_cell_outputs_point_at_jq(h: Harness, ws: Path) -> None:
    (ws / "nb.ipynb").write_bytes(
        _notebook(
            [
                {
                    "cell_type": "code",
                    "source": "x",
                    "outputs": [{"output_type": "stream", "text": "y" * 10_001}],
                }
            ]
        )
    )
    out = await h.call(ReadTool(), file_path="nb.ipynb")
    assert text_of(out).endswith(
        "\nOutputs are too large to include. Use Bash with: cat "
        "<notebook_path> | jq '.cells[0].outputs'"
    )


async def test_a_broken_notebook_is_refused(h: Harness, ws: Path) -> None:
    (ws / "bad.ipynb").write_text("{not json")
    bad = await h.call(ReadTool(), file_path="bad.ipynb")
    assert bad.is_error
    assert "Notebook file is not valid JSON" in text_of(bad)
    (ws / "shape.ipynb").write_text(json.dumps({"cells": [{"x": 1}]}))
    shape = await h.call(ReadTool(), file_path="shape.ipynb")
    assert "is not a valid Jupyter notebook" in text_of(shape)


async def test_an_over_large_notebook_is_refused_by_tokens(h: Harness, ws: Path) -> None:
    (ws / "nb.ipynb").write_bytes(
        _notebook([{"cell_type": "markdown", "source": "z" * 120_000}])
    )
    out = await h.call(ReadTool(), file_path="nb.ipynb")
    assert "exceeds maximum allowed tokens (25000)" in text_of(out)
