"""``noeta mcp``: Claude Code's ``claude mcp`` commands, writing noeta's places."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from noeta.cli import config, home_dir
from noeta.cli.main import main
from noeta.plugins.mcp.config import McpServer
from noeta.plugins.mcp.oauth import store_for

FAKE = str(Path(__file__).parents[1] / "plugins" / "mcp" / "fake_server.py")


@pytest.fixture
def places(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    home, project = tmp_path / "home", tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr(home_dir, "HOME", home)
    monkeypatch.setattr(config, "managed_dir", lambda: tmp_path / "managed")
    monkeypatch.delenv("NOETA_MCP_CLIENT_SECRET", raising=False)
    return home, project


def mcp(project: Path, *args: str) -> int:
    with pytest.raises(SystemExit) as done:
        main(["--cwd", str(project), "mcp", *args])
    code = done.value.code
    return code if isinstance(code, int) else 1


def read(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_add_writes_each_scope_in_claude_codes_format(places: tuple[Path, Path],
                                                      capsys: pytest.CaptureFixture[str]) -> None:
    home, project = places
    assert mcp(project, "add", "files", "-e", "ROOT=/x", "--", "npx", "-y", "srv",
               "--flag") == 0
    assert read(project / ".noeta" / "settings.local.json")["mcpServers"]["files"] == {
        "type": "stdio", "command": "npx", "args": ["-y", "srv", "--flag"],
        "env": {"ROOT": "/x"}}
    assert mcp(project, "add", "-s", "user", "-t", "http", "-H", "Authorization: Bearer k",
               "--client-id", "cid", "--callback-port", "8765", "gh",
               "https://mcp.example/mcp") == 0
    assert read(home / "settings.json")["mcpServers"]["gh"] == {
        "type": "http", "url": "https://mcp.example/mcp",
        "headers": {"Authorization": "Bearer k"},
        "oauth": {"clientId": "cid", "callbackPort": 8765}}
    assert mcp(project, "add-json", "-s", "project", "docs",
               '{"type": "sse", "url": "https://x/sse"}') == 0
    assert read(project / ".noeta" / "mcp.json")["mcpServers"]["docs"]["type"] == "sse"
    out = capsys.readouterr().out
    assert "enabledMcpjsonServers" in out and "noeta trust" in out
    # Names are unique per scope.
    with pytest.raises(SystemExit, match="already exists"):
        main(["--cwd", str(project), "mcp", "add", "files", "x"])
    with pytest.raises(SystemExit, match="invalid header"):
        main(["--cwd", str(project), "mcp", "add", "-t", "http", "h", "u", "-H", "nocolon"])
    with pytest.raises(SystemExit, match="invalid JSON"):
        main(["--cwd", str(project), "mcp", "add-json", "j", "{"])


def test_a_client_secret_is_kept_with_the_sign_in(places: tuple[Path, Path],
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    home, project = places
    monkeypatch.setenv("NOETA_MCP_CLIENT_SECRET", "s3cret")
    assert mcp(project, "add", "-t", "http", "--client-id", "cid", "--client-secret", "gh",
               "https://mcp.example/mcp") == 0
    spec = read(project / ".noeta" / "settings.local.json")["mcpServers"]["gh"]
    assert "s3cret" not in json.dumps(spec)
    server = McpServer(alias="gh", url="https://mcp.example/mcp")
    assert store_for(server, home=home).load()["client"] == {"client_id": "cid",
                                                             "client_secret": "s3cret"}
    with pytest.raises(SystemExit, match="needs --client-id"):
        main(["--cwd", str(project), "mcp", "add", "-t", "http", "--client-secret", "x", "u"])


def test_list_get_and_remove(places: tuple[Path, Path],
                             capsys: pytest.CaptureFixture[str]) -> None:
    home, project = places
    assert mcp(project, "add", "-s", "user", "fake", sys.executable, FAKE) == 0
    assert mcp(project, "add", "-s", "project", "shared", sys.executable, FAKE) == 0
    assert mcp(project, "add", "-s", "local", "fake", sys.executable, FAKE) == 0
    assert mcp(project, "add", "-s", "user", "dead", "/no/such/server") == 0
    capsys.readouterr()
    assert mcp(project, "list") == 0
    out = capsys.readouterr().out
    assert f"fake: {sys.executable} {FAKE} (stdio, local) - connected" in out
    assert "shared:" in out and "pending approval" in out
    assert "dead:" in out and "failed" in out
    assert mcp(project, "get", "shared") == 0
    out = capsys.readouterr().out
    assert "Scope: project" in out and "noeta mcp remove shared -s project" in out
    with pytest.raises(SystemExit, match="several scopes"):
        main(["--cwd", str(project), "mcp", "remove", "fake"])
    assert mcp(project, "remove", "-s", "user", "fake") == 0
    assert "fake" not in read(home / "settings.json")["mcpServers"]
    assert mcp(project, "remove", "fake") == 0
    with pytest.raises(SystemExit, match="no MCP server"):
        main(["--cwd", str(project), "mcp", "get", "fake"])
    (home / "settings.json").write_text("{}")
    (project / ".noeta" / "mcp.json").unlink()
    (project / ".noeta" / "settings.local.json").unlink()
    capsys.readouterr()
    assert mcp(project, "list") == 0
    assert "No MCP servers configured" in capsys.readouterr().out


def test_login_and_logout(places: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
                          capsys: pytest.CaptureFixture[str]) -> None:
    home, project = places
    assert mcp(project, "add", "-t", "http", "gh", "https://mcp.example/mcp") == 0
    started: list[McpServer] = []

    class FakeSignIn:
        def __init__(self, server: McpServer, store: Any, **_: Any) -> None:
            self.server, self.store = server, store

        async def start(self) -> str:
            started.append(self.server)
            return "https://auth.example/authorize?x=1"

        async def finish(self) -> None:
            self.store.update(tokens={"access_token": "a"})

        async def close(self) -> None:
            return None

        def submit(self, url: str) -> bool:
            return True

    monkeypatch.setattr("noeta.plugins.mcp.admin.SignIn", FakeSignIn)
    monkeypatch.setattr("sys.stdin.readline", lambda: "")
    assert mcp(project, "login", "--no-browser", "gh") == 0
    out = capsys.readouterr().out
    assert "https://auth.example/authorize?x=1" in out and "Authentication successful" in out
    store = store_for(started[0], home=home)
    assert store.load()["tokens"] == {"access_token": "a"}
    assert mcp(project, "logout", "gh") == 0
    assert "tokens" not in store.load()
    assert mcp(project, "logout", "gh") == 0
    assert "No stored sign-in" in capsys.readouterr().out
    assert mcp(project, "add", "local-tool", "cmd") == 0
    with pytest.raises(SystemExit, match="does not sign in"):
        main(["--cwd", str(project), "mcp", "login", "local-tool"])
