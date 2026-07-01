"""Phase 4.5 F2 — `McpStdioClient` lifecycle + deadlock/cap guards.

Exercises the real subprocess against the in-tree fake MCP server: the
happy initialize→list→call path, the per-call timeout (no hang), a
server that dies mid-call, an oversized line cap, and bounded shutdown
that leaves no leaked process.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

from noeta.tools.mcp import McpError, McpStdioClient


_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")


def _argv(mode: str = "echo") -> list[str]:
    return [sys.executable, "-u", _FAKE, mode]


def _client(mode: str = "echo", **kw: object) -> McpStdioClient:
    return McpStdioClient(argv=_argv(mode), **kw)  # type: ignore[arg-type]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_initialize_list_call_echo() -> None:
    c = _client("echo")
    c.start()
    try:
        tools = c.list_tools()
        assert [t["name"] for t in tools] == ["echo"]
        result = c.call_tool("echo", {"msg": "hi"})
        text = result["content"][0]["text"]
        assert '"msg":"hi"' in text.replace(" ", "")
        assert not result.get("isError")
    finally:
        c.shutdown()


def test_call_timeout_does_not_hang() -> None:
    c = _client("slow", timeout_s=0.5)
    c.start()
    try:
        start = time.monotonic()
        with pytest.raises(McpError):
            c.call_tool("echo", {})
        assert time.monotonic() - start < 4.0  # nowhere near the 5s server sleep
    finally:
        c.shutdown()


def test_server_dies_mid_call_is_typed_error() -> None:
    c = _client("die")
    c.start()
    try:
        with pytest.raises(McpError):
            c.call_tool("echo", {})
    finally:
        c.shutdown()


def test_oversized_line_hits_cap() -> None:
    c = _client("bigline", line_cap=64 * 1024)
    c.start()
    try:
        with pytest.raises(McpError):
            c.call_tool("echo", {})
    finally:
        c.shutdown()


def test_notification_flood_hits_cumulative_cap() -> None:
    # The server streams 40 small (50 KB) notification lines — each below
    # line_cap — before the real response. With a 256 KB total cap the
    # per-request byte budget must trip (typed failure) well before they
    # all arrive: no unbounded buffering, no hang.
    c = _client("flood", line_cap=128 * 1024, total_cap=256 * 1024, timeout_s=5.0)
    c.start()
    try:
        start = time.monotonic()
        with pytest.raises(McpError):
            c.call_tool("echo", {})
        assert time.monotonic() - start < 4.0
    finally:
        c.shutdown()


def test_handshake_failure_is_fail_fast() -> None:
    c = _client("die_init")
    with pytest.raises(McpError):
        c.start()
    c.shutdown()


def test_spawn_oserror_is_typed() -> None:
    c = McpStdioClient(argv=["/nonexistent/noeta-mcp-binary-xyz"])
    with pytest.raises(McpError):
        c.start()


def test_shutdown_reaps_process() -> None:
    c = _client("echo")
    c.start()
    pid = c.pid
    assert pid is not None
    c.shutdown()
    # Give the OS a moment, then assert the child is gone.
    for _ in range(50):
        if not _pid_alive(pid):
            break
        time.sleep(0.02)
    assert not _pid_alive(pid)


def test_shutdown_is_idempotent() -> None:
    c = _client("echo")
    c.start()
    c.shutdown()
    c.shutdown()  # no raise
