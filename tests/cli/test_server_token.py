"""``noeta serve`` always has a bearer token unless told otherwise: the
configured one, or one made once and kept owner-only."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from noeta.cli import home_dir, main, server_token, tenants
from noeta.core.tenants import token_digest


def test_a_token_is_made_once_and_only_its_digest_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    monkeypatch.delenv("NOETA_SERVER_TOKEN", raising=False)
    assert server_token.server_token() is None
    made = server_token.server_token_digest()
    assert made.shown and len(made.shown) >= 32 and made.sha256 == token_digest(made.shown)
    path = tmp_path / "home" / "server-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert made.shown not in path.read_text() and made.sha256 in path.read_text()
    again = server_token.server_token_digest()
    assert again.sha256 == made.sha256 and again.shown is None
    assert server_token.server_token() is None  # a client needs NOETA_SERVER_TOKEN now
    lookup = tenants.tenant_tokens(server_sha256=made.sha256)
    assert lookup(made.shown) == "default" and lookup("wrong") is None
    monkeypatch.setenv("NOETA_SERVER_TOKEN", "configured")
    assert server_token.server_token() == "configured"
    assert server_token.server_token_digest() == server_token.ServerToken(token_digest("configured"))


def test_a_plain_text_token_file_is_moved_to_its_digest(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    monkeypatch.delenv("NOETA_SERVER_TOKEN", raising=False)
    (tmp_path / "home").mkdir()
    path = tmp_path / "home" / "server-token"
    path.write_text("old-token\n")
    assert server_token.server_token() == "old-token"  # what an older noeta left
    moved = server_token.server_token_digest()
    assert moved.shown == "old-token" and moved.sha256 == token_digest("old-token")
    assert "old-token" not in path.read_text() and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert server_token.server_token_digest().shown is None


def test_serve_shows_the_token_only_when_it_is_made(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    monkeypatch.delenv("NOETA_SERVER_TOKEN", raising=False)
    monkeypatch.chdir(tmp_path)

    def started(*_a: object, **_kw: object) -> None:
        raise RuntimeError("started")

    monkeypatch.setattr(main, "_app", started)
    argv = ["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db", "serve"]
    with pytest.raises(RuntimeError, match="started"):
        main.main(argv)
    first = capsys.readouterr().err
    with pytest.raises(RuntimeError, match="started"):
        main.main(argv)
    second = capsys.readouterr().err
    shown = first.rpartition(": ")[2].strip()
    assert "shown only now" in first and token_digest(shown) in (
        tmp_path / "home" / "server-token").read_text()
    assert shown not in second and "was made" in second


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


def test_allowed_hosts_are_checked_on_any_bind_when_named() -> None:
    from noeta.host.http import LOOPBACK_HOSTS

    def hosts(*argv: str) -> list[str] | None:
        return main._allowed_hosts(main._parser().parse_args(["serve", *argv]))

    assert hosts() == list(LOOPBACK_HOSTS)
    assert hosts("--host", "0.0.0.0") is None
    assert hosts("--host", "0.0.0.0", "--allow-host", "noeta.example",
                 "--allow-host", "10.0.0.5") == ["noeta.example", "10.0.0.5"]
    assert hosts("--allow-host", "noeta.example") == [*LOOPBACK_HOSTS, "noeta.example"]


@pytest.mark.parametrize("known", ["token", "directory"])
def test_no_token_is_refused_when_tenants_are_known(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, known: str) -> None:
    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    monkeypatch.chdir(tmp_path)
    if known == "token":
        tenants.add_token("acme")
    else:
        (tmp_path / "home" / "tenants" / "acme").mkdir(parents=True)
        (tmp_path / "home" / "tenants" / "acme" / "config.toml").write_text("")

    def started(*_a: object, **_kw: object) -> None:
        raise RuntimeError("started")

    monkeypatch.setattr(main, "_app", started)
    with pytest.raises(SystemExit, match=r"--no-token.*acme"):
        main.main(["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db", "serve",
                   "--no-token", "--no-worker"])
