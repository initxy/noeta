"""Real-LLM MCP tool call, end to end (live marker).

A real model decides to call an MCP tool exposed by an external stdio server,
and the whole production MCP path runs: the host spawns the server subprocess,
handshakes JSON-RPC, discovers ``mcp__fake__echo``, offers it to the model, and
records the three-event call envelope when the model invokes it.

The transport half (real subprocess, real JSON-RPC) is already exercised by
``test_code_mcp.py`` against a scripted provider; the only newly-"live" part
here is a real model *choosing* to call the tool and threading a real argument.
The server is the in-tree fake stdio server (``tests/_fixtures/fake_mcp_server.py``
in ``echo`` mode), reused exactly as ``test_code_mcp.py`` wires it — no new
server, and no network.

Config comes from a git-ignored ``.env`` via ``tests._live_env``. Missing
base/key/model auto-skips; CI never runs these.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from noeta.runtime.mcp import McpServerSpec
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests import _live_env
from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    runner_main_spec,
)

pytestmark = pytest.mark.live

requires_live = _live_env.requires_live

_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")


def _model() -> str:
    return _live_env.live_model() or ""


@requires_live
def test_live_mcp_tool_call(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = McpServerSpec(alias="fake", argv=(sys.executable, "-u", _FAKE, "echo"))
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=_live_env.build_anthropic_provider(),
        model=_model(),
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        mcp_server_resolver={spec.alias: spec}.get,
    )
    driver = make_driver(host)
    out = driver.start(
        goal=(
            "Call the mcp__fake__echo tool with msg set to 'hello from live'. "
            "Then reply with whatever the tool returned."
        ),
        agent="main",
        enabled_mcp=("fake",),
    )
    assert out.status == "terminal", out.status
    events = list(host.event_log.read(out.task_id))
    starts = [
        e
        for e in events
        if e.type == "ToolCallStarted" and e.payload.tool_name == "mcp__fake__echo"
    ]
    assert len(starts) == 1, "model did not call the MCP echo tool exactly once"
    # The recorded result for that call succeeded (real subprocess round-trip).
    result_ids = {
        e.payload.call_id
        for e in events
        if e.type == "ToolResultRecorded" and e.payload.success
    }
    assert starts[0].payload.call_id in result_ids, "MCP call recorded no success"
