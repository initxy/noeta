"""A fenced env's PDF reaches the page converter only as bytes read through
the fence, on stdin: the converter, run by the worker, never opens a path
(a link the tenant's commands swap in after the check would carry it
elsewhere), and nothing is written where those commands can reach."""

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


def _pdf(marker: bytes) -> bytes:
    """A one-page PDF whose page box differs by ``marker`` (so its rendered
    page does too)."""
    return _PDF.replace(b"[0 0 72 72]", b"[0 0 " + marker + b"]")


async def test_a_fenced_pdf_is_fed_to_the_converters_on_stdin(tmp_path: Path) -> None:
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
    converters = [argv for argv in env.argvs if argv[0] in ("pdfinfo", "pdftoppm")]
    assert converters and all(argv[-1] == "-" for argv in converters)
    assert not _converter_paths(env)
    assert str(doc) in checked


async def test_an_unfenced_pdf_is_converted_in_place(tmp_path: Path) -> None:
    doc = tmp_path / "doc.pdf"
    doc.write_bytes(_PDF)
    env = Recording(str(tmp_path))
    await read_pdf("Read", env, str(doc), "doc.pdf", len(_PDF), None)
    assert set(_converter_paths(env)) == {str(doc)}


async def test_a_fenced_pdf_behind_a_link_into_the_fence_is_an_error(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "doc.pdf").write_bytes(_PDF)
    link = tmp_path / "ws.pdf"
    link.symlink_to(secret / "doc.pdf")

    def fence(path: str, write: bool) -> str | None:
        return "fenced" if path.startswith(str(secret)) else None

    env = Recording(str(tmp_path), fence=fence)
    out = await read_pdf("Read", env, str(link), "ws.pdf", len(_PDF), None)
    assert out.is_error and "read failed" in str(out.content[0])
    assert not _converter_paths(env)


class Swapping(Recording):
    """The tenant's background command at its worst: every file a converter
    is about to open in the tenant's temporary directory is first replaced
    by a link to another tenant's PDF, and every directory it writes into by
    a link to another tenant's workspace."""

    def __init__(self, *args: Any, own: Path, other: Path, **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.own, self.other = own, other

    async def run(self, argv: Sequence[str], **kw: Any) -> RunResult:
        if argv[0] in ("pdfinfo", "pdftoppm"):
            for a in argv:
                target = Path(a)
                if not a.startswith(f"{self.own}/"):
                    continue
                if a.endswith(".pdf") and target.is_file():
                    target.unlink()
                    target.symlink_to(self.other / "secret.pdf")
                elif target.parent.is_dir() and not target.parent.is_symlink():
                    shutil.rmtree(target.parent)
                    target.parent.symlink_to(self.other)
        return await super().run(argv, **kw)


async def test_a_swapped_link_in_the_tenants_temporary_directory_reaches_nothing(
        tmp_path: Path) -> None:
    mine, other, own = tmp_path / "mine", tmp_path / "other", tmp_path / "own-temp"
    for d in (mine, other, own / "tmp"):
        d.mkdir(parents=True)
    (mine / "doc.pdf").write_bytes(_pdf(b"72 72"))
    (other / "secret.pdf").write_bytes(_pdf(b"300 300"))

    def fence(path: str, write: bool) -> str | None:
        return None if path.startswith(f"{mine}/") or path.startswith(f"{own}/") else "fenced"

    env = Swapping(str(mine), fence=fence, temp_dir=str(own), own=own, other=other)
    out = await read_pdf("Read", env, str(mine / "doc.pdf"), "doc.pdf", 100, None)
    assert sorted(p.name for p in other.iterdir()) == ["secret.pdf"]
    assert list((own / "tmp").iterdir()) == []
    if shutil.which("pdftoppm"):
        assert not out.is_error
        page = out.content[1]
        clean = await read_pdf("Read", LocalExecEnv(str(mine)), str(mine / "doc.pdf"),
                               "doc.pdf", 100, None)
        assert page == clean.content[1]  # tenant a's own page, not the other's
