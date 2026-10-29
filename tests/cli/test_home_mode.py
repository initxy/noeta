"""noeta's home holds the store, tokens and settings: made owner-only when
noeta creates it; an existing one keeps its mode, with a notice when it is
open to other users."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from noeta.cli import config


@pytest.fixture(autouse=True)
def usual_umask() -> Iterator[None]:
    old = os.umask(0o022)
    yield
    os.umask(old)


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_home_noeta_makes_is_owner_only(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "user" / ".noeta"
    monkeypatch.setattr(config, "HOME", home)
    config.device_id()
    assert _mode(home) == 0o700
    assert config.loose_permissions(f"sqlite:///{home / 'tasks.db'}") == []


def test_an_open_home_and_store_are_kept_and_named(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir(mode=0o755)
    db = tmp_path / "tasks.db"
    db.touch()
    db.chmod(0o644)
    monkeypatch.setattr(config, "HOME", home)
    config.trust_project(str(tmp_path))
    assert _mode(home) == 0o755  # never changed silently
    notices = config.loose_permissions(f"sqlite:///{db}")
    assert [n.split(" is open")[0] for n in notices] == [str(home), str(db)]
    assert "chmod 700" in notices[0] and "chmod 600" in notices[1]
