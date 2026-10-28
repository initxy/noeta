"""MCP servers from settings files and a workspace's ``.mcp.json``: Claude
Code's format, environment expansion, consent for workspace servers."""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.store.sqlite import SqliteStore
from noeta.plugins.mcp import Mcp, McpPool, McpServer
from noeta.plugins.mcp.settings import alias_for, read_servers
from noeta.testing import ScriptedProvider, reply

FAKE = str(Path(__file__).with_name("fake_server.py"))


def write(path: Path, data: Any) -> str:
    path.write_text(json.dumps(data))
    return str(path)


def test_the_claude_code_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "t0k")
    monkeypatch.delenv("UNSET", raising=False)
    f = write(tmp_path / "s.json", {"mcpServers": {
        "Git Hub": {"type": "http", "url": "https://x/${UNSET:-mcp}",
                    "headers": {"Authorization": "Bearer ${TOKEN}"}},
        "files": {"command": "npx", "args": ["-y", "srv", "${UNSET}"], "env": {"K": "${TOKEN}"}},
        "old": {"type": "sse", "url": "https://x/sse"},
        "bad": {"type": "ws"},
        "broken": "nope",
    }})
    http, stdio = read_servers([f])
    assert http == McpServer(alias="git_hub", url="https://x/mcp",
                             headers={"Authorization": "Bearer t0k"})
    assert stdio == McpServer(alias="files", command=["npx", "-y", "srv", ""], env={"K": "t0k"})
    assert read_servers([tmp_path / "missing.json"]) == []
    (tmp_path / "junk.json").write_text("{")
    assert read_servers([tmp_path / "junk.json"]) == []
    assert alias_for("") == "_" and len(alias_for("x" * 50)) == 32


def test_workspace_servers_need_consent(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    write(ws / ".mcp.json", {"mcpServers": {"a": {"command": "a"}, "b": {"command": "b"},
                                            "user": {"command": "from-ws"}}})
    settings = tmp_path / "s.json"
    write(settings, {"mcpServers": {"user": {"command": "from-user"}}})
    assert [s.alias for s in read_servers([settings], [str(ws)])] == ["user"]
    write(settings, {"mcpServers": {"user": {"command": "from-user"}},
                     "enabledMcpjsonServers": ["b", "user"]})
    got = read_servers([settings], [str(ws)])
    assert [(s.alias, s.command[0]) for s in got] == [
        ("b", "b"), ("user", "from-ws"), ("user", "from-user")]  # the first of an alias wins
    write(settings, {"enableAllProjectMcpServers": True, "disabledMcpjsonServers": ["a"]})
    assert [s.alias for s in read_servers([settings], [str(ws)])] == ["b", "user"]


@pytest.fixture
async def store(tmp_path: Path) -> AsyncIterator[SqliteStore]:
    s = SqliteStore(str(tmp_path / "t.db"))
    yield s
    await s.close()


async def test_a_task_gets_the_settings_servers(store: SqliteStore, tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    write(ws / ".mcp.json", {"mcpServers": {"proj": {"command": sys.executable,
                                                     "args": [FAKE]}}})
    settings = write(tmp_path / "s.json", {
        "mcpServers": {"fake": {"command": sys.executable, "args": [FAKE]}},
        "enabledMcpjsonServers": ["proj"]})
    pool = McpPool()
    provider = ScriptedProvider([reply("done")])
    given = McpServer(alias="fake", command=[sys.executable, FAKE], env={"GIVEN": "1"})
    agent = Agent(name="a", model="m", extensions=[
        Mcp([given], settings_files=[settings], project=True, pool=pool)])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: None)  # type: ignore[arg-type,return-value]
    try:
        await Client(store).create("a", "go", config={"cwd": str(ws)})
        await rt.run_until_idle()
    finally:
        await pool.close()
    names = {t.name for t in provider.requests[0].tools}
    assert {"mcp__fake__echo", "mcp__proj__echo"} <= names
