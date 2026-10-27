"""``noeta acp`` as a real subprocess, driven over stdio by the ACP SDK's own
client, against a live model (``-m live``; needs NOETA_LIVE_* in the repo
.env)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("acp")

import acp  # noqa: E402
from acp import schema  # noqa: E402

pytestmark = pytest.mark.live

ENV_FILE = Path(__file__).resolve().parents[4] / ".env"


def _live_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"')
    env.update({k: v for k, v in os.environ.items() if k.startswith("NOETA_LIVE_")})
    return env


class Editor:
    def __init__(self) -> None:
        self.updates: list[Any] = []

    async def session_update(self, session_id: str, update: Any, **kw: Any) -> None:
        self.updates.append(update)

    async def request_permission(self, session_id: str, tool_call: Any, options: Any,
                                 **kw: Any) -> Any:
        return schema.RequestPermissionResponse(
            outcome=schema.AllowedOutcome(outcome="selected", option_id="allow"))


async def test_noeta_acp_over_stdio(tmp_path: Path) -> None:
    live = _live_env()
    if not live.get("NOETA_LIVE_MODEL"):
        pytest.skip("no live model configured")
    (tmp_path / "notes.txt").write_text("the secret word is tangerine\n")
    env = {
        **os.environ,
        "NOETA_MODEL": live["NOETA_LIVE_MODEL"],
        "NOETA_BASE_URL": live["NOETA_LIVE_BASE_URL"],
        "NOETA_API_KEY": live["NOETA_LIVE_API_KEY"],
        "NOETA_HOME": str(tmp_path / "home"),
    }
    editor = Editor()
    async with acp.spawn_agent_process(lambda _agent: editor, sys.executable, "-m",
                                       "noeta.cli", "acp", env=env, cwd=str(tmp_path)) as (conn, _):
        init = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
        assert init.agent_capabilities.load_session
        session = await conn.new_session(cwd=str(tmp_path), mcp_servers=[])
        resp = await conn.prompt(session_id=session.session_id, prompt=[
            schema.TextContentBlock(type="text",
                                    text="Read notes.txt and tell me the secret word, one word.")])
    assert resp.stop_reason == "end_turn"
    kinds = [u.session_update for u in editor.updates]
    assert "tool_call" in kinds and "tool_call_update" in kinds
    said = "".join(u.content.text for u in editor.updates
                   if u.session_update == "agent_message_chunk")
    assert "tangerine" in said.lower()
