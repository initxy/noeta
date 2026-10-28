"""The device id in ``metadata.user_id``: random, made once, kept."""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.cli import config


def test_made_once_and_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    first = config.device_id()
    assert first is not None and len(first) == 64
    assert config.device_id() == first
    assert (tmp_path / "home" / "device_id").read_text().strip() == first
