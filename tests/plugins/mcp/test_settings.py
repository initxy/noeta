"""MCP servers from settings files and a workspace's ``.noeta/mcp.json``: Claude
Code's format, environment expansion, consent for workspace servers."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.plugins.mcp import Mcp, McpPool, McpServer
from noeta.plugins.mcp.config import (
    MAX_TIMEOUT_S,
    OAuthSettings,
    request_timeout_s,
    tool_timeout_s,
)
from noeta.plugins.mcp.settings import alias_for, read_servers
from noeta.testing import ScriptedProvider, reply

FAKE = str(Path(__file__).with_name("fake_server.py"))


def write(path: Path, data: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return str(path)


def test_the_claude_code_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "t0k")
    monkeypatch.delenv("UNSET", raising=False)
    f = write(tmp_path / "s.json", {"mcpServers": {
        "Git Hub": {"type": "http", "url": "https://x/${UNSET:-mcp}",
                    "headers": {"Authorization": "Bearer ${TOKEN}"}},
        "files": {"command": "npx", "args": ["-y", "srv", "${UNSET}"], "env": {"K": "${TOKEN}"}},
        "old": {"type": "sse", "url": "https://x/sse", "headersHelper": "echo ${TOKEN}",
                "oauth": {"clientId": "cid", "callbackPort": 8765, "scopes": "read write",
                          "authServerMetadataUrl": "https://auth/.well-known/x"}},
        "stream": {"type": "streamable-http", "url": "https://x/s", "alwaysLoad": True},
        "sock": {"type": "ws", "url": "wss://x/ws", "bareElicitationCapability": True},
        "bad": {"type": "ws"},
        "ide": {"type": "sse-ide", "url": "http://x"},
        "plain": {"type": "http", "url": "http://x", "oauth": {"authServerMetadataUrl": "http://a"}},
        "broken": "nope",
    }})
    http, stdio, sse, stream, ws = read_servers([f])
    assert http == McpServer(alias="git_hub", url="https://x/mcp",
                             headers={"Authorization": "Bearer t0k"})
    assert http.transport == "http"
    assert stdio == McpServer(alias="files", command=["npx", "-y", "srv", ""], env={"K": "t0k"})
    assert sse == McpServer(alias="old", type="sse", url="https://x/sse",
                            headers_helper="echo t0k",
                            oauth=OAuthSettings(client_id="cid", callback_port=8765,
                                                scopes="read write",
                                                auth_server_metadata_url="https://auth/.well-known/x"))
    assert (stream.transport, stream.deferred) == ("http", False)
    assert (ws.transport, ws.bare_elicitation, ws.oauth) == ("ws", True, None)
    assert read_servers([tmp_path / "missing.json"]) == []
    (tmp_path / "junk.json").write_text("{")
    assert read_servers([tmp_path / "junk.json"]) == []
    assert alias_for("") == "_" and len(alias_for("x" * 50)) == 32


def test_a_server_timeout_in_milliseconds(tmp_path: Path) -> None:
    f = write(tmp_path / "s.json", {"mcpServers": {
        "slow": {"command": "srv", "timeout": 120_000},
        "tiny": {"command": "srv", "timeout": 500},  # under 1 s: ignored, as in Claude Code
        "web": {"url": "https://x", "timeout": 2500},
    }})
    slow, tiny, web = read_servers([f])
    assert (slow.call_timeout, tiny.call_timeout, web.call_timeout) == (120.0, None, 2.5)


def test_timeouts_follow_claude_codes_defaults_and_variables(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOETA_MCP_TIMEOUT", raising=False)
    monkeypatch.delenv("NOETA_MCP_TOOL_TIMEOUT", raising=False)
    assert request_timeout_s() == 30.0
    assert tool_timeout_s(None) == 100_000.0  # 1e8 ms: no practical limit
    assert tool_timeout_s(5.0) == 5.0 and tool_timeout_s(0.2) == 1.0
    monkeypatch.setenv("NOETA_MCP_TIMEOUT", "60000")
    monkeypatch.setenv("NOETA_MCP_TOOL_TIMEOUT", "90000")
    assert request_timeout_s() == 60.0
    assert tool_timeout_s(None) == 90.0
    assert tool_timeout_s(5.0) == 5.0  # the server's own wins
    monkeypatch.setenv("NOETA_MCP_TOOL_TIMEOUT", "1e20")
    assert tool_timeout_s(None) == MAX_TIMEOUT_S
    monkeypatch.setenv("NOETA_MCP_TIMEOUT", "junk")
    assert request_timeout_s() == 30.0


def test_workspace_servers_need_consent(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    write(ws / ".noeta" / "mcp.json", {"mcpServers": {
        "a": {"command": "a"}, "b": {"command": "b"}, "user": {"command": "from-ws"}}})
    settings = tmp_path / "s.json"
    local = ws / ".noeta" / "settings.local.json"
    write(settings, {"mcpServers": {"user": {"command": "from-user"}}})
    assert [s.alias for s in read_servers([settings], [str(ws)])] == ["user"]
    write(local, {"enabledMcpjsonServers": ["b", "user"]})
    got = read_servers([settings, local], [str(ws)])
    assert [(s.alias, s.command[0]) for s in got] == [
        ("b", "b"), ("user", "from-ws"), ("user", "from-user")]  # the first of an alias wins
    write(local, {"enableAllProjectMcpServers": True})
    write(settings, {"disabledMcpjsonServers": ["a"]})
    assert [s.alias for s in read_servers([settings, local], [str(ws)])] == ["b", "user"]


def test_consent_is_per_project(tmp_path: Path) -> None:
    """Consent in the user's settings, or in another project's, starts no
    server of a cloned repository that reuses the name."""
    clone, other = tmp_path / "clone", tmp_path / "other"
    write(clone / ".noeta" / "mcp.json", {"mcpServers": {"github": {"command": "evil"}}})
    user = write(tmp_path / "user.json", {"enabledMcpjsonServers": ["github"],
                                          "enableAllProjectMcpServers": True})
    elsewhere = write(other / ".noeta" / "settings.local.json",
                      {"enabledMcpjsonServers": ["github"]})
    assert read_servers([user, elsewhere], [str(clone)]) == []


async def test_an_untrusted_workspace_starts_no_server(store: SqliteStore,
                                                       tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    write(ws / ".noeta" / "mcp.json", {"mcpServers": {"proj": {"command": "never-run"}}})
    local = write(ws / ".noeta" / "settings.local.json", {"enableAllProjectMcpServers": True})
    asked: list[str] = []

    def trusted(directory: str) -> bool:
        asked.append(directory)
        return False

    provider = ScriptedProvider([reply("done")])
    agent = Agent(name="a", model="m", extensions=[
        Mcp(settings_files=[local], project=trusted, pool=McpPool())])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    tid = await Client(store).create("a", "go", config={"cwd": str(ws)})
    await rt.run_until_idle()
    assert asked == [str(ws)]
    recorded = [(r.event.kind, r.event.data) for r in await Client(store).events(tid)
                if getattr(r.event, "extension", "") == "mcp"]
    assert "proj" not in repr(recorded)


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def test_a_task_gets_the_settings_servers(store: SqliteStore, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    write(ws / ".noeta" / "mcp.json", {"mcpServers": {"proj": {"command": sys.executable,
                                                     "args": [FAKE]}}})
    settings = write(tmp_path / "s.json", {
        "mcpServers": {"fake": {"command": sys.executable, "args": [FAKE]}}})
    local = write(ws / ".noeta" / "settings.local.json", {"enabledMcpjsonServers": ["proj"]})
    pool = McpPool()
    provider = ScriptedProvider([reply("done")])
    given = McpServer(alias="fake", command=[sys.executable, FAKE], env={"GIVEN": "1"})
    agent = Agent(name="a", model="m", extensions=[
        Mcp([given], settings_files=[settings, local], project=True, pool=pool)])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    try:
        tid = await Client(store).create("a", "go", config={"cwd": str(ws)})
        await rt.run_until_idle()
    finally:
        await pool.close()
    # Server tools are deferred: named for ToolSearch, not sent yet.
    assert [t.name for t in provider.requests[0].tools] == [
        "DeferredToolPlaceholder", "ToolSearch"]
    st = (await Client(store).events(tid))
    listed = [r.event for r in st if getattr(r.event, "kind", "") == "listed"][-1]
    names = {t["name"] for srv in listed.data["servers"] for t in srv["tools"]}
    assert {"mcp__fake__echo", "mcp__proj__echo"} <= names
