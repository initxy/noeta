"""``noeta serve`` always has a bearer token unless told otherwise: the
configured one, or one made once and kept owner-only."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from noeta.cli import config, main


def test_a_token_is_made_once_and_kept_private(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, "HOME", tmp_path / "home")
    monkeypatch.delenv("NOETA_SERVER_TOKEN", raising=False)
    assert config.server_token() is None
    token = config.server_token(create=True)
    assert token and len(token) >= 32
    path = tmp_path / "home" / "server-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert config.server_token() == config.server_token(create=True) == token
    monkeypatch.setenv("NOETA_SERVER_TOKEN", "configured")
    assert config.server_token(create=True) == "configured"


def test_no_token_only_on_a_loopback_address(monkeypatch: pytest.MonkeyPatch) -> None:
    served: list[object] = []
    monkeypatch.setattr(main, "_settings", lambda _a: object())
    monkeypatch.setattr(main, "_serve", lambda *a: served.append(a) or _done())
    with pytest.raises(SystemExit, match="loopback"):
        main.main(["serve", "--host", "0.0.0.0", "--no-token"])
    main.main(["serve", "--no-token"])
    assert len(served) == 1


async def _done() -> None:
    return None
