"""Where noeta names its own version to the other side (an ACP editor, an
MCP server, an OpenAPI reader), it names the installed package's."""

from __future__ import annotations

from importlib.metadata import version
from pathlib import Path

import pytest

from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.host.local import LocalApp
from noeta.testing import ScriptedProvider

INSTALLED = version("noeta")


async def test_acp_reports_the_installed_version(tmp_path: Path) -> None:
    pytest.importorskip("acp")
    from noeta.host.acp import NoetaAcpAgent

    app = LocalApp(SqliteStore(str(tmp_path / "t.db")), [Agent(name="main", model="m")],
                   ScriptedProvider([]), env_for=lambda _s: None)
    reply = await NoetaAcpAgent(app).initialize(protocol_version=1)
    await app.stop()
    assert reply.agent_info is not None and reply.agent_info.version == INSTALLED


def test_mcp_client_info_names_the_installed_version() -> None:
    from noeta.plugins.mcp.client import initialize_params

    assert initialize_params()["clientInfo"]["version"] == INSTALLED


def test_openapi_document_names_the_installed_version() -> None:
    pytest.importorskip("starlette")
    from noeta.host._http_openapi import OPENAPI

    assert OPENAPI["info"]["version"] == INSTALLED
