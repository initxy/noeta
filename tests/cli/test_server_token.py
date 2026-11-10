"""``noeta serve`` always has a bearer token unless told otherwise: the
configured one, or one made once into ``~/.noeta/server-token`` (owner-only),
which clients on the same machine read; and every start prints the addresses
to open, the token in their fragment."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from noeta.cli import home_dir, main, serve_address, server_token


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    monkeypatch.delenv("NOETA_SERVER_TOKEN", raising=False)
    return tmp_path / "home"


def test_a_token_is_made_once_kept_owner_only_and_read_by_clients(home: Path) -> None:
    assert server_token.server_token() is None
    made = server_token.serve_token()
    assert made.made and len(made.token) >= 32
    path = home / "server-token"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text().strip() == made.token
    again = server_token.serve_token()
    assert again == server_token.ServerToken(made.token)
    assert server_token.server_token() == made.token


def test_an_older_digest_only_file_gets_a_new_token(home: Path) -> None:
    home.mkdir()
    path = home / "server-token"
    path.write_text("sha256:" + "0" * 64 + "\n")
    assert server_token.server_token() is None  # a digest is nothing a client can send
    made = server_token.serve_token()
    assert made.made and path.read_text().strip() == made.token
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_the_configured_token_wins_and_is_written_nowhere(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOETA_SERVER_TOKEN", "configured")
    assert server_token.serve_token() == server_token.ServerToken("configured", from_env=True)
    assert server_token.server_token() == "configured"
    assert not (home / "server-token").exists()


class Started(BaseException):  # not an Exception: past main()'s crash report
    pass


def _serve_until_listening(monkeypatch: pytest.MonkeyPatch, argv: list[str],
                           machine: list[str] | None = None) -> None:
    """``noeta serve`` run up to uvicorn, which stops it."""
    import uvicorn

    class _App:
        deltas = None
        plugin_admin = None
        client = type("C", (), {"task_config": {}})()
        runtime = type("R", (), {"agents": {}})()

        async def start(self, worker: bool) -> None:
            return None

        async def stop(self) -> None:
            return None

    class _Server:
        def __init__(self, config: object) -> None:
            pass

        async def serve(self) -> None:
            raise Started

    monkeypatch.setattr(main, "_app", lambda *_a, **_kw: _App())
    monkeypatch.setattr(uvicorn, "Server", _Server)
    monkeypatch.setattr(serve_address, "machine_names", lambda: list(machine or []))
    with pytest.raises(Started):
        main.main(argv)


def test_every_start_prints_the_address_with_the_token(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    argv = ["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db", "serve", "--port", "9100"]
    _serve_until_listening(monkeypatch, argv)
    first = capsys.readouterr().err
    token = (home / "server-token").read_text().strip()
    assert "made a new bearer token" in first
    assert f"at http://127.0.0.1:9100/#token={token}" in first
    _serve_until_listening(monkeypatch, argv)
    second = capsys.readouterr().err
    assert "made a new bearer token" not in second  # the same token on every start
    assert f"at http://127.0.0.1:9100/#token={token}" in second
    _serve_until_listening(monkeypatch, [*argv, "--no-token"])
    assert "at http://127.0.0.1:9100/\n" in capsys.readouterr().err


def test_a_start_on_every_address_prints_the_machines_names(
        home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NOETA_SERVER_TOKEN", "tok")
    argv = ["--model", "m", "--store", f"sqlite:///{tmp_path}/t.db", "serve", "--host",
            "0.0.0.0", "--allow-host", "proxy.example"]
    _serve_until_listening(monkeypatch, argv, machine=["box", "box.lan", "10.0.0.5", "fd00::5"])
    err = capsys.readouterr().err
    urls = [line.rpartition(" at ")[2] for line in err.splitlines() if " at http" in line]
    assert urls == ["http://box:8642/#token=tok", "http://box.lan:8642/#token=tok",
                    "http://10.0.0.5:8642/#token=tok", "http://[fd00::5]:8642/#token=tok",
                    "http://proxy.example:8642/#token=tok", "http://127.0.0.1:8642/#token=tok"]
    assert "any Host header" not in err
    assert not (home / "server-token").exists()


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


def test_allowed_hosts_add_the_machines_names_off_loopback() -> None:
    from noeta.host.http import LOOPBACK_HOSTS

    machine = ["box", "10.0.0.5"]
    assert serve_address.allowed_hosts("127.0.0.1", [], machine) == list(LOOPBACK_HOSTS)
    assert serve_address.allowed_hosts("127.0.0.1", ["noeta.example"], machine) == [
        *LOOPBACK_HOSTS, "noeta.example"]
    assert serve_address.allowed_hosts("0.0.0.0", ["noeta.example", "box"], machine) == [
        *LOOPBACK_HOSTS, "box", "10.0.0.5", "noeta.example"]
    assert serve_address.addresses("10.0.0.5", 1, None, ["n.example"], machine) == [
        "http://10.0.0.5:1/", "http://n.example:1/"]


def test_machine_names_leave_out_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "box")
    monkeypatch.setattr(socket, "getfqdn", lambda: "box.lan")
    monkeypatch.setattr(serve_address, "own_addresses", lambda: ["10.0.0.5", "fe80::1%eth0"])
    monkeypatch.setattr(socket, "getaddrinfo", lambda *_a: [
        (0, 0, 0, "", ("127.0.1.1", 0)), (0, 0, 0, "", ("10.0.0.5", 0)),
        (0, 0, 0, "", ("192.168.1.9", 0))])
    assert serve_address.machine_names() == ["box", "box.lan", "10.0.0.5", "192.168.1.9"]
