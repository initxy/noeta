"""A fenced env's PDF reaches the page converter only as a copy read
through the fence: the converter, run by the worker, never opens the path
itself (a link swapped in after the check would carry it elsewhere)."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.execenv import RunResult
from noeta.plugins.fs._read_formats import read_pdf

#: A one-page PDF (no fonts, an empty page).
_PDF = (b"%PDF-1.1\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 72 72]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n%%EOF\n")


class Recording(LocalExecEnv):
    def __init__(self, *args: Any, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.argvs: list[list[str]] = []

    async def run(self, argv: Sequence[str], **kw: Any) -> RunResult:
        self.argvs.append(list(argv))
        return await super().run(argv, **kw)


def _converter_paths(env: Recording) -> list[str]:
    return [a for argv in env.argvs if argv[0] in ("pdfinfo", "pdftoppm")
            for a in argv if a.endswith(".pdf")]


async def test_a_fenced_pdf_is_converted_from_a_copy(tmp_path: Path) -> None:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(_PDF)
    checked: list[str] = []

    def fence(path: str, write: bool) -> str | None:
        checked.append(path)
        return None

    env = Recording(str(tmp_path), fence=fence)
    out = await read_pdf("Read", env, str(doc), "doc.pdf", len(_PDF), None)
    if shutil.which("pdftoppm"):
        assert not out.is_error and len(out.content) == 2  # the text and one page
    used = _converter_paths(env)
    assert used and str(doc) not in used
    assert str(doc) in checked


async def test_an_unfenced_pdf_is_converted_in_place(tmp_path: Path) -> None:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(_PDF)
    env = Recording(str(tmp_path))
    await read_pdf("Read", env, str(doc), "doc.pdf", len(_PDF), None)
    assert set(_converter_paths(env)) == {str(doc)}


async def test_a_fenced_pdf_behind_a_link_into_the_fence_is_refused(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "doc.pdf").write_bytes(_PDF)
    link = tmp_path / "ws.pdf"
    link.symlink_to(secret / "doc.pdf")

    def fence(path: str, write: bool) -> str | None:
        return "fenced" if path.startswith(str(secret)) else None

    env = Recording(str(tmp_path), fence=fence)
    try:
        await read_pdf("Read", env, str(link), "ws.pdf", len(_PDF), None)
    except PermissionError:
        pass
    assert not _converter_paths(env)


async def test_a_tenants_copy_and_pages_stay_in_its_own_temporary_directory(
        tmp_path: Path) -> None:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(_PDF)
    own = tmp_path / "own-temp"
    own.mkdir()
    env = Recording(str(tmp_path), fence=lambda _p, _w: None, temp_dir=str(own))
    await read_pdf("Read", env, str(doc), "doc.pdf", len(_PDF), None)
    used = _converter_paths(env)
    assert used and all(p.startswith(f"{own}/") for p in used)
    assert list(own.iterdir()) == []  # removed after use
