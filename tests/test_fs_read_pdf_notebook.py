"""``Read`` on PDFs and Jupyter notebooks, as Claude Code's Read does them.

A PDF's pages come back as JPEG images (``pdftoppm``), at most 20 per call,
and a PDF over ten pages needs ``pages``. A notebook comes back as its cells
in ``<cell id="…">`` form with each output's text, and output images for the
model to see.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from noeta.builtins.fs.impl import ReadFileTool
from noeta.protocols.tool import ToolContext
from noeta.runtime.exec_env import LocalExecEnv
from noeta.runtime.tool import InMemoryFileReadRegistry
from noeta.runtime.subproc import RunOutcome
from noeta.runtime.workspace import WorkspaceRoot
from noeta.storage.memory import InMemoryContentStore


def _ctx() -> ToolContext:
    return ToolContext(
        artifact_store=InMemoryContentStore(),
        file_read_registry=InMemoryFileReadRegistry(),
    )


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
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>"
        for _ in range(pages)
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


def _read(tmp_path: Path, args: dict[str, Any], **kw: Any) -> Any:
    return ReadFileTool(workspace=WorkspaceRoot.from_path(tmp_path), **kw).invoke(
        args, _ctx()
    )


@needs_poppler
def test_a_short_pdf_is_read_whole_as_page_images(tmp_path: Path) -> None:
    (tmp_path / "doc.pdf").write_bytes(_pdf(2))
    result = _read(tmp_path, {"file_path": "doc.pdf"})
    assert result.success, result.summary
    assert result.output.startswith("PDF pages extracted: 2 page(s) from doc.pdf (")
    assert len(result.images) == 2
    assert all(ref.media_type == "image/jpeg" for ref in result.images)


@needs_poppler
def test_pages_picks_one_page_or_a_range(tmp_path: Path) -> None:
    (tmp_path / "doc.pdf").write_bytes(_pdf(4))
    assert len(_read(tmp_path, {"file_path": "doc.pdf", "pages": "3"}).images) == 1
    assert len(_read(tmp_path, {"file_path": "doc.pdf", "pages": "2-3"}).images) == 2


@needs_poppler
def test_a_long_pdf_needs_pages(tmp_path: Path) -> None:
    (tmp_path / "doc.pdf").write_bytes(_pdf(12))
    result = _read(tmp_path, {"file_path": "doc.pdf"})
    assert not result.success
    assert result.summary == (
        "Read: This PDF has 12 pages, which is too many to read at once. Use "
        'the pages parameter to read specific page ranges (e.g., pages: "1-5"). '
        "Maximum 20 pages per request."
    )


@needs_poppler
def test_pages_out_of_the_document_is_refused(tmp_path: Path) -> None:
    (tmp_path / "doc.pdf").write_bytes(_pdf(2))
    result = _read(tmp_path, {"file_path": "doc.pdf", "pages": "5"})
    assert not result.success
    assert "outside the document (PDF has 2 pages)" in result.summary


def test_bad_or_too_wide_pages_are_refused(tmp_path: Path) -> None:
    (tmp_path / "doc.pdf").write_bytes(_pdf(2))
    bad = _read(tmp_path, {"file_path": "doc.pdf", "pages": "1,3"})
    assert bad.summary == (
        'Read: Invalid pages parameter: "1,3". Give one page ("3") or one range '
        '("1-5"), not a list. To read several pages or ranges, read each one '
        "separately. Pages are 1-indexed."
    )
    wide = _read(tmp_path, {"file_path": "doc.pdf", "pages": "1-21"})
    assert wide.summary == (
        'Read: Page range "1-21" exceeds maximum of 20 pages per request. '
        "Please use a smaller range."
    )
    assert "exceeds maximum" in _read(
        tmp_path, {"file_path": "doc.pdf", "pages": "3-"}
    ).summary


class _NoPoppler(LocalExecEnv):
    def run_argv(self, argv: list[str], **kw: Any) -> RunOutcome:
        if argv[0] in ("pdftoppm", "pdfinfo"):
            raise OSError("not found")
        return super().run_argv(argv, **kw)


def test_missing_pdftoppm_says_how_to_install_it(tmp_path: Path) -> None:
    (tmp_path / "doc.pdf").write_bytes(_pdf(1))
    result = _read(tmp_path, {"file_path": "doc.pdf"}, exec_env=_NoPoppler())
    assert result.summary == (
        "Read: pdftoppm is not installed. Install poppler-utils (e.g. `brew "
        "install poppler` or `apt-get install poppler-utils`) to enable PDF "
        "page rendering."
    )


@needs_poppler
def test_a_broken_pdf_reports_the_render_failure(tmp_path: Path) -> None:
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf\n")
    result = _read(tmp_path, {"file_path": "doc.pdf"})
    assert not result.success


# ---------------------------------------------------------------------------
# Notebooks
# ---------------------------------------------------------------------------

_PNG = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f9d0000000049454e44ae426082"
    )
).decode()


def _notebook(cells: list[dict[str, Any]], language: str = "python") -> bytes:
    return json.dumps(
        {"cells": cells, "metadata": {"language_info": {"name": language}}}
    ).encode()


def test_a_notebook_reads_as_cells_with_outputs(tmp_path: Path) -> None:
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
    (tmp_path / "nb.ipynb").write_bytes(body)
    ctx = _ctx()
    result = ReadFileTool(workspace=WorkspaceRoot.from_path(tmp_path)).invoke(
        {"file_path": "nb.ipynb"}, ctx
    )
    assert result.success, result.summary
    assert result.output == (
        '<cell id="intro"><cell_type>markdown</cell_type># Title\ntext</cell id="intro">\n'
        '<cell id="cell-1">print(1)</cell id="cell-1">\n'
        "\n1\n\n"
        "\n<Figure>\n"
        "\nValueError: bad\ntb line"
    )
    assert [ref.media_type for ref in result.images] == ["image/png"]
    # Registered like any read, so Edit / Write may follow.
    assert ctx.file_read_registry is not None
    assert ctx.file_read_registry.digest(
        str((tmp_path / "nb.ipynb").resolve())
    ) == hashlib.sha256(body).hexdigest()


def test_a_non_python_code_cell_names_its_language(tmp_path: Path) -> None:
    (tmp_path / "nb.ipynb").write_bytes(
        _notebook([{"cell_type": "code", "source": "x", "outputs": []}], "julia")
    )
    out = _read(tmp_path, {"file_path": "nb.ipynb"}).output
    assert out == '<cell id="cell-0"><language>julia</language>x</cell id="cell-0">'


def test_large_cell_outputs_point_at_jq(tmp_path: Path) -> None:
    (tmp_path / "nb.ipynb").write_bytes(
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
    out = _read(tmp_path, {"file_path": "nb.ipynb"}).output
    assert out.endswith(
        "\nOutputs are too large to include. Use Bash with: cat "
        "<notebook_path> | jq '.cells[0].outputs'"
    )


def test_a_broken_notebook_is_refused(tmp_path: Path) -> None:
    (tmp_path / "bad.ipynb").write_text("{not json")
    assert "Notebook file is not valid JSON" in _read(
        tmp_path, {"file_path": "bad.ipynb"}
    ).summary
    (tmp_path / "shape.ipynb").write_text(json.dumps({"cells": [{"x": 1}]}))
    assert "is not a valid Jupyter notebook" in _read(
        tmp_path, {"file_path": "shape.ipynb"}
    ).summary


def test_an_over_large_notebook_is_refused_by_tokens(tmp_path: Path) -> None:
    (tmp_path / "nb.ipynb").write_bytes(
        _notebook([{"cell_type": "markdown", "source": "z" * 120_000}])
    )
    assert "exceeds maximum allowed tokens (25000)" in _read(
        tmp_path, {"file_path": "nb.ipynb"}
    ).summary
