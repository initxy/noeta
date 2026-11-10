"""``noeta web``: a real ``noeta serve`` started in the background on
loopback with its own token, found again by a second ``noeta web`` and by
``status``, stopped by ``stop``; a dead server's record is dropped."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("uvicorn")

import httpx

import noeta.cli.main as cli
import noeta.host.http as http
from noeta.cli import home_dir, web_command
from noeta.toolkit.http import DIRECT_LOOPBACK


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
    return port


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    where = tmp_path / "home"
    monkeypatch.setenv("NOETA_HOME", str(where))
    monkeypatch.setattr(home_dir, "HOME", where)
    monkeypatch.setenv("HOME", str(tmp_path / "user"))
    (tmp_path / "user").mkdir()
    # A provider that is never reached: nothing here calls the model.
    monkeypatch.setenv("NOETA_PROVIDER", "openai_compat")
    monkeypatch.setenv("NOETA_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("NOETA_API_KEY", "unused")
    monkeypatch.setenv("NOETA_MODEL", "some-model")
    monkeypatch.setenv("NOETA_STORE", f"sqlite:///{tmp_path}/store.db")
    monkeypatch.delenv("NOETA_SERVER_TOKEN", raising=False)
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text("<!doctype html><title>noeta web app</title>")
    monkeypatch.setattr(http, "bundled_web_dir", lambda: str(web))
    monkeypatch.chdir(tmp_path)
    return where


def _main(argv: list[str]) -> int:
    with pytest.raises(SystemExit) as done:
        cli.main(argv)
    return int(done.value.code or 0)


def test_start_status_reuse_and_stop(home: Path, monkeypatch: pytest.MonkeyPatch,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    opened: list[str] = []
    monkeypatch.setattr(web_command.webbrowser, "open", opened.append)
    port = _free_port()
    try:
        assert _main(["web", "--no-open", "--port", str(port)]) == 0
        out = capsys.readouterr().out
        state = json.loads((home / "web.json").read_text())
        assert (home / "web.json").stat().st_mode & 0o777 == 0o600
        url = f"http://127.0.0.1:{port}/#token={state['token']}"
        assert out.splitlines() == [f"noeta web is running at {url}",
                                    "Stop it with: noeta web stop"]
        assert not opened
        # The token reaches the server through its environment, never argv.
        args = subprocess.run(["ps", "-o", "args=", "-p", str(state["pid"])],
                              capture_output=True, text=True, check=False).stdout
        assert state["token"] not in args and "--allow-root" in args
        with httpx.Client(mounts=DIRECT_LOOPBACK, timeout=10) as client:
            page = client.get(f"http://127.0.0.1:{port}/")
            assert page.status_code == 200 and "noeta web app" in page.text
            api = client.get(f"http://127.0.0.1:{port}/projects",
                             headers={"authorization": f"Bearer {state['token']}"})
            assert api.status_code == 200 and api.json() == []
            assert client.get(f"http://127.0.0.1:{port}/projects").status_code == 401

        assert _main(["web", "status"]) == 0
        assert capsys.readouterr().out.startswith(
            f"noeta web is running at http://127.0.0.1:{port}/ (pid {state['pid']}")
        # A second noeta web reuses the server, whatever port it is asked for.
        assert _main(["web", "--port", str(_free_port())]) == 0
        assert capsys.readouterr().out == f"noeta web is already running at {url}\n"
        assert opened == [url]
        assert json.loads((home / "web.json").read_text())["pid"] == state["pid"]
    finally:
        assert _main(["web", "stop"]) == 0
    assert capsys.readouterr().out == "noeta web stopped\n"
    assert not (home / "web.json").exists()
    assert not web_command._alive(state["pid"])
    assert _main(["web", "status"]) == 1
    assert capsys.readouterr().out == "noeta web is not running\n"
    assert _main(["web", "stop"]) == 0
    assert capsys.readouterr().out == "noeta web is not running\n"


def test_a_server_that_does_not_start_shows_its_log(
        home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(web_command, "_serve_command", lambda *_a: [
        sys.executable, "-c", "import sys; print('cannot open the store'); sys.exit(3)"])
    assert _main(["web", "--no-open", "--port", str(_free_port())]) == 1
    err = capsys.readouterr().err
    assert "the server did not start" in err and "cannot open the store" in err
    assert not (home / "web.json").exists()


def test_a_dead_servers_record_is_dropped_silently(home: Path) -> None:
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    home.mkdir()
    (home / "web.json").write_text(json.dumps(
        {"pid": dead.pid, "port": 1, "token": "t", "log": "x"}))
    assert web_command.running() is None
    assert not (home / "web.json").exists()
    (home / "web.json").write_text("{broken")
    assert web_command.running() is None and not (home / "web.json").exists()


def test_without_the_web_app_or_with_its_port_taken_it_says_so(
        home: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        assert _main(["web", "--no-open", "--port", str(port)]) == 1
        assert f"port {port} is in use" in capsys.readouterr().err
    monkeypatch.setattr(http, "bundled_web_dir", lambda: None)
    assert _main(["web", "--no-open"]) == 1
    assert "no web app built in" in capsys.readouterr().err


def test_a_new_server_needs_a_model(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOETA_MODEL")
    with pytest.raises(SystemExit, match="no model configured"):
        cli.main(["web", "--no-open"])


def test_the_server_command_and_whose_pid_it_is(
        home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cmd = web_command._serve_command(9, "/w", {"model": "opus", "base_url": "http://x"})
    assert cmd[:3] == [sys.executable, "-m", "noeta.cli.main"]
    assert cmd[3:] == ["--model", "opus", "--base-url", "http://x", "serve", "--host",
                       "127.0.0.1", "--port", "9", "--allow-root", os.environ["HOME"],
                       "--web", "/w", "--for-noeta-web"]
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        server = web_command.Server(pid=stranger.pid, port=_free_port(), token="t", log="x")
        assert not web_command._ours(server)
        web_command._keep(server)
        # Not noeta's: the record goes, the process is left alone.
        assert web_command.stop(quiet=True) == server
        assert stranger.poll() is None
    finally:
        stranger.kill()
        stranger.wait()


def test_terminate_kills_what_ignores_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    stubborn = subprocess.Popen(
        [sys.executable, "-c", "import signal, time; signal.signal(signal.SIGTERM, "
         "signal.SIG_IGN); print('ready', flush=True); time.sleep(60)"],
        stdout=subprocess.PIPE, start_new_session=True)
    assert stubborn.stdout is not None and stubborn.stdout.readline().strip() == b"ready"
    monkeypatch.setattr(web_command, "STOP_SECONDS", 0.3)
    web_command._terminate(stubborn.pid)
    # _terminate returns once the process is gone (it reaps it: no exit code left).
    assert not web_command._alive(stubborn.pid)
    stubborn.wait(timeout=5)


