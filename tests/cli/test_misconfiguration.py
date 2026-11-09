"""Common misconfigurations end the command with one line naming what to set,
not a traceback."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from noeta.cli.server_token import server_problem


def _env(tmp_path: Path, **more: str) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700, exist_ok=True)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("NOETA_", "ANTHROPIC_", "OPENAI_"))}
    env.update(NOETA_HOME=str(home), HOME=str(tmp_path / "user"),
               NOETA_STORE=f"sqlite:///{tmp_path / 't.db'}", **more)
    return env


@pytest.mark.parametrize(("provider", "also"), [
    ({"NOETA_PROVIDER": "openai_compat", "NOETA_BASE_URL": "http://127.0.0.1:9/v1"},
     "OPENAI_API_KEY"),
    ({}, "ANTHROPIC_API_KEY"),
])
def test_no_api_key_names_the_variables_to_set(
        tmp_path: Path, provider: dict[str, str], also: str) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "noeta.cli.main", "-p", "hi", "--cwd", str(tmp_path)],
        capture_output=True, text=True, timeout=120, check=False,
        env=_env(tmp_path, NOETA_MODEL="sonnet", **provider))
    assert done.returncode == 1, done.stderr
    assert "Traceback" not in done.stderr, done.stderr
    line = done.stderr.strip().splitlines()[-1]
    assert line.startswith("noeta: no API key") and "NOETA_API_KEY" in line and also in line


def test_no_model_names_a_model_a_key_and_the_quickstart(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "noeta.cli.main", "-p", "hi", "--cwd", str(tmp_path)],
        capture_output=True, text=True, timeout=120, check=False, env=_env(tmp_path))
    assert done.returncode == 1, done.stderr
    assert "Traceback" not in done.stderr, done.stderr
    for wanted in ("no model configured", "API key", "NOETA_MODEL=", "ANTHROPIC_API_KEY=",
                   "NOETA_PROVIDER=openai_compat", "NOETA_BASE_URL=",
                   "https://initxy.github.io/noeta/start/quickstart"):
        assert wanted in done.stderr, wanted


@pytest.mark.parametrize(("config", "env", "wanted"), [
    ("model = sonnet\n", {}, "config.toml is not valid TOML: Invalid value (at line 1"),
    ("", {"NOETA_PROVIDER": "nope"}, "there is no provider 'nope'"),
    ('[plugins.fs]\nnope = 1\n', {}, "plugin 'fs': got an unexpected keyword argument 'nope'"),
    ("", {"NOETA_STORE": "mysql://x"}, "unknown store url 'mysql://x'"),
])
def test_a_setting_noeta_cannot_use_ends_with_one_line(
        tmp_path: Path, config: str, env: dict[str, str], wanted: str) -> None:
    env = {**_env(tmp_path, NOETA_MODEL="sonnet", NOETA_API_KEY="k"), **env}
    (tmp_path / "home" / "config.toml").write_text(config)
    done = subprocess.run(
        [sys.executable, "-m", "noeta.cli.main", "-p", "hi", "--cwd", str(tmp_path)],
        capture_output=True, text=True, timeout=120, check=False, env=env)
    assert done.returncode == 1, done.stderr
    assert "Traceback" not in done.stderr, done.stderr
    line = done.stderr.strip().splitlines()[-1]
    assert line.startswith("noeta: ") and wanted in line, line


def test_a_missing_key_stops_the_start_before_any_request(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No model listing is read without a key (it would only answer 401),
    and a model that is not Claude's on the default provider gets the way to
    an OpenAI-compatible endpoint."""
    from noeta.backends.providers import listing
    from noeta.cli import home_dir
    from noeta.cli.config import MissingAPIKey, from_settings, load_settings

    monkeypatch.setattr(home_dir, "HOME", tmp_path / "home")
    for var in ("NOETA_API_KEY", "ANTHROPIC_API_KEY", "NOETA_PROVIDER", "NOETA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    read: list[object] = []
    monkeypatch.setattr(listing, "fetch_listing", lambda *a, **kw: read.append(a) or [])
    s = load_settings(model="some-gateway-model", store=f"sqlite:///{tmp_path / 't.db'}")
    with pytest.raises(MissingAPIKey) as missing:
        from_settings(s, cwd=str(tmp_path))
    assert read == []
    assert "NOETA_PROVIDER=openai_compat" in str(missing.value)
    assert "NOETA_BASE_URL" in str(missing.value)
    s = load_settings(model="sonnet", store=f"sqlite:///{tmp_path / 't.db'}")
    with pytest.raises(MissingAPIKey) as missing:
        from_settings(s, cwd=str(tmp_path))
    assert "NOETA_PROVIDER" not in str(missing.value)


class _Agents(BaseHTTPRequestHandler):
    """``GET /agents`` of a server whose token is ``right``."""

    def do_GET(self) -> None:
        ok = self.headers.get("authorization") == "Bearer right"
        body = b'[{"name": "main"}]' if ok else b'{"error": "unauthorized"}'
        self.send_response(200 if ok else 401)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        pass


@pytest.fixture
def server() -> Iterator[str]:
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Agents)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()


@pytest.mark.parametrize("command", [[], ["acp"]], ids=["tui", "acp"])
@pytest.mark.parametrize("token", [None, "wrong"])
def test_a_front_end_on_a_server_without_its_token_names_the_variable(
        tmp_path: Path, server: str, token: str | None, command: list[str]) -> None:
    more = {"NOETA_SERVER_TOKEN": token} if token else {}
    done = subprocess.run(
        [sys.executable, "-m", "noeta.cli.main", *command, "--server", server],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, check=False,
        env=_env(tmp_path, **more))
    assert done.returncode == 1, done.stderr
    assert "Traceback" not in done.stdout + done.stderr, done.stdout
    line = done.stderr.strip().splitlines()[-1]
    assert line.startswith(f"noeta: the server at {server}") and "NOETA_SERVER_TOKEN" in line


def test_a_server_that_takes_the_token_has_no_problem(server: str) -> None:
    assert server_problem(server, "right") is None
    assert "cannot reach" in (server_problem("http://127.0.0.1:9", "right") or "")
