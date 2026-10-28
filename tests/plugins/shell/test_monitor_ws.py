"""``Monitor`` with a WebSocket source, against a small server speaking the
protocol's server side over asyncio streams."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import struct
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from noeta.core.tool import ToolContext
from noeta.plugins.shell import Registry, Shell
from noeta.plugins.shell.websocket import parse_ws, run_ws
from tests.plugins.shell.test_background_tools import Harness, text

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
Script = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def frame(opcode: int, payload: bytes, *, fin: bool = True) -> bytes:
    """An unmasked server frame."""
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        return head + bytes([n]) + payload
    return head + bytes([126]) + struct.pack("!H", n) + payload


async def read_client_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    b0, b1 = await reader.readexactly(2)
    assert b1 & 0x80, "client frames are masked"
    n = b1 & 0x7F
    mask = await reader.readexactly(4)
    data = await reader.readexactly(n)
    return b0 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


@pytest.fixture
async def server() -> AsyncIterator[Callable[[Script], Awaitable[str]]]:
    servers: list[asyncio.Server] = []
    seen: dict[str, str] = {}

    async def start(script: Script) -> str:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            head = (await reader.readuntil(b"\r\n\r\n")).decode()
            fields = {k.strip().lower(): v.strip() for k, _, v in
                      (line.partition(":") for line in head.split("\r\n")[1:] if line)}
            seen.update(fields)
            accept = base64.b64encode(hashlib.sha1(
                (fields["sec-websocket-key"] + GUID).encode()).digest()).decode()
            writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n")
                         .encode())
            await writer.drain()
            await script(reader, writer)
            writer.close()

        srv = await asyncio.start_server(handle, "127.0.0.1", 0)
        servers.append(srv)
        return f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/stream?x=1"

    start.seen = seen  # type: ignore[attr-defined]
    yield start
    for srv in servers:
        srv.close()


async def test_text_binary_ping_and_close(server: Callable[[Script], Awaitable[str]]) -> None:
    pongs: list[bytes] = []

    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(frame(0x1, b"hello"))
        writer.write(frame(0x1, b"two ", fin=False) + frame(0x0, b"parts"))
        writer.write(frame(0x2, b"\x00\x01\x02"))
        writer.write(frame(0x9, b"hi"))
        await writer.drain()
        opcode, data = await read_client_frame(reader)
        assert opcode == 0xA
        pongs.append(data)
        writer.write(frame(0x8, struct.pack("!H", 1000)))
        await writer.drain()
        opcode, _ = await read_client_frame(reader)
        assert opcode == 0x8

    url = await server(script)
    events: list[str] = []
    end = await run_ws(url, ["v1"], events.append)
    assert events == ["hello", "two parts", "[binary frame, 3 bytes]"]
    assert pongs == [b"hi"] and end.exit_code == 1000
    assert server.seen["sec-websocket-protocol"] == "v1"  # type: ignore[attr-defined]


async def test_a_dropped_connection_is_reported() -> None:
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    srv = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        events: list[str] = []
        end = await run_ws(f"ws://127.0.0.1:{srv.sockets[0].getsockname()[1]}/", [],
                           events.append)
    finally:
        srv.close()
    assert end.exit_code == 1006 and events[0].startswith("WebSocket error:")


def test_parse_ws() -> None:
    assert parse_ws({"url": "wss://h/x", "protocols": ["v1"]}) == ("wss://h/x", ["v1"])
    assert "ws:// or wss://" in str(parse_ws({"url": "http://h"}))
    assert "protocol tokens" in str(parse_ws({"url": "ws://h", "protocols": ["a b"]}))


async def test_monitor_streams_a_websocket(tmp_path: Path,
                                           server: Callable[[Script], Awaitable[str]]) -> None:
    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.write(frame(0x1, b"deploy started"))
        await writer.drain()
        await asyncio.sleep(0.3)
        writer.write(frame(0x8, struct.pack("!H", 1000)))
        await writer.drain()
        await read_client_frame(reader)

    url = await server(script)
    registry = Registry(log_dir=str(tmp_path / "logs"))
    work = tmp_path / "work"
    work.mkdir()
    h = Harness(work, registry, Shell(registry=registry))
    assert "ws" in h.tool("Monitor").input_schema["properties"]
    both = await h.call("Monitor", command="true", ws={"url": url}, description="d")
    assert both.is_error
    out = await h.call("Monitor", ws={"url": url}, description="deploy events",
                       timeout_ms=60_000)
    assert not out.is_error and "unless the socket closes first" in text(out)
    job_id = out.meta["job_id"]
    await h.settle(job_id)
    texts = [t for _, t in h.posts]
    assert texts[0] == (f"<task-notification>\n<task-id>{job_id}</task-id>\n"
                        "<summary>Monitor event: \"deploy events\"</summary>\n"
                        "<event>deploy started</event>\n</task-notification>")
    assert "<status>completed</status>" in texts[-1]
    assert 'Monitor "deploy events" socket closed (code 1000)' in texts[-1]


async def test_monitor_keeps_to_the_sandbox_network_rules(
        tmp_path: Path, server: Callable[[Script], Awaitable[str]]) -> None:
    from noeta.toolkit.sandbox import SandboxSettings
    from tests.plugins.shell.test_sandbox import FakeSandbox

    connected: list[bool] = []

    async def script(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        connected.append(True)
        writer.write(frame(0x1, b"through") + frame(0x8, struct.pack("!H", 1000)))
        await writer.drain()
        await read_client_frame(reader)

    url = await server(script)
    port = url.split(":")[2].split("/")[0]
    registry = Registry(log_dir=str(tmp_path / "logs"))
    work = tmp_path / "work"
    work.mkdir()
    allowed: list[str] = []

    class Sandboxed(Harness):
        def ctx(self, task_id: str = "task", root_id: str = "task") -> ToolContext:
            ctx = super().ctx(task_id, root_id)
            ctx.env.sandbox = FakeSandbox(SandboxSettings(  # type: ignore[attr-defined]
                enabled=True, allowed_domains=tuple(allowed)))
            return ctx

    h = Sandboxed(work, registry, Shell(registry=registry))
    # Not in allowedDomains: refused before any connection.
    refused = await h.call("Monitor", ws={"url": url}, description="d", timeout_ms=60_000)
    assert refused.is_error and "allowedDomains" in text(refused)
    # Allowed by a wildcard, but the name resolves to this machine.
    allowed.append("*")
    out = await h.call("Monitor", ws={"url": f"ws://0x7f000001:{port}/"}, description="d",
                       timeout_ms=60_000)
    await h.settle(out.meta["job_id"])
    assert "resolves to 127.0.0.1 (a loopback address)" in h.posts[-1][1]
    assert not connected
    allowed[:] = [f"127.0.0.1:{port}"]
    out = await h.call("Monitor", ws={"url": url}, description="d", timeout_ms=60_000)
    await h.settle(out.meta["job_id"])
    assert connected and "<event>through</event>" in "".join(t for _, t in h.posts[1:])
