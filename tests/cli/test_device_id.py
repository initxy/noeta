"""The device id in ``metadata.user_id``: random, made once, kept."""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.cli import home_dir


def test_made_once_and_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    first = home_dir.device_id()
    assert first is not None and len(first) == 64
    assert home_dir.device_id() == first
    assert (tmp_path / "home" / "device_id").read_text().strip() == first
