"""Images made to fit a request with Claude Code's limits: 2000 x 2000 px and
5 MB of base64, shrunk with Pillow when it is installed, refused before the
history otherwise."""

from __future__ import annotations

import io
import os
import sys

import pytest

from noeta.toolkit.images import (
    MAX_BASE64_BYTES,
    TARGET_RAW_BYTES,
    ImageTooLarge,
    base64_size,
    fit_image,
    image_dimensions,
)
from tests.images import png


def test_limits_and_base64_size() -> None:
    assert MAX_BASE64_BYTES == 5_242_880 and TARGET_RAW_BYTES == 3_932_160
    assert base64_size(TARGET_RAW_BYTES) == MAX_BASE64_BYTES
    assert base64_size(4) == 8


def test_header_dimensions() -> None:
    assert image_dimensions(png(7, 3)) == (7, 3)
    assert image_dimensions(b"GIF89a\x0a\x00\x05\x00") == (10, 5)
    assert image_dimensions(b"not an image") is None
    pil = pytest.importorskip("PIL.Image")
    for fmt in ("JPEG", "WEBP", "GIF"):
        buf = io.BytesIO()
        pil.new("RGB", (33, 17)).save(buf, fmt)
        assert image_dimensions(buf.getvalue()) == (33, 17), fmt
    buf = io.BytesIO()
    pil.new("RGBA", (33, 17)).save(buf, "WEBP", lossless=True)
    assert image_dimensions(buf.getvalue()) == (33, 17)


def test_a_small_image_passes_unchanged() -> None:
    raw = png(10, 10)
    fitted = fit_image(raw, "image/png")
    assert fitted.data == raw and not fitted.resized and fitted.note() is None


def test_without_pillow_an_oversized_image_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "PIL", None)
    assert fit_image(png(3000, 10), "image/png").data == png(3000, 10)  # API takes 8000
    with pytest.raises(ImageTooLarge, match="9000x10 px"):
        fit_image(png(9000, 10), "image/png")
    big = png(10, 10) + b"\x00" * TARGET_RAW_BYTES
    with pytest.raises(ImageTooLarge, match=r"in base64, over the 5\.0MB limit"):
        fit_image(big, "image/png")
    with pytest.raises(ImageTooLarge, match="dimensions could not be read"):
        fit_image(b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg")
    with pytest.raises(ImageTooLarge, match="empty"):
        fit_image(b"", "image/png")


def test_pillow_shrinks_into_the_box() -> None:
    pytest.importorskip("PIL")
    fitted = fit_image(png(1000, 6000), "image/png")
    assert fitted.media_type == "image/png"
    assert image_dimensions(fitted.data) == (333, 2000) == fitted.shown
    assert fitted.original == (1000, 6000)


def test_pillow_brings_a_heavy_image_under_the_byte_target() -> None:
    pil = pytest.importorskip("PIL.Image")
    img = pil.frombytes("RGB", (1500, 1500), os.urandom(1500 * 1500 * 3))  # noise: no compression
    buf = io.BytesIO()
    img.save(buf, "PNG")
    assert len(buf.getvalue()) > TARGET_RAW_BYTES
    fitted = fit_image(buf.getvalue(), "image/png")
    assert len(fitted.data) <= TARGET_RAW_BYTES
    assert fitted.media_type in ("image/png", "image/jpeg")


def test_pillow_refuses_what_it_cannot_decode() -> None:
    pytest.importorskip("PIL")
    with pytest.raises(ImageTooLarge, match="could not be decoded"):
        fit_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32, "image/png")
