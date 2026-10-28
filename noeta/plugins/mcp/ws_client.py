"""The WebSocket MCP client (``"type": "ws"``): one JSON-RPC message per
text frame, subprotocol ``mcp``.

The WebSocket itself (RFC 6455, client side) is spoken here over asyncio
streams, with no extra dependency: the opening handshake (checked
``Sec-WebSocket-Accept`` and subprotocol), masked frames out, fragmented
frames in, ``ping`` answered, ``close`` returned. ``wss://`` uses the
system's default TLS context. Requests are multiplexed over the socket
(:class:`~noeta.plugins.mcp.client.StreamClient`); the socket closing
breaks the connection. Headers are credentials: sent on the wire only.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import ssl
import struct
import urllib.parse
from collections.abc import AsyncIterator, Mapping
from typing import TYPE_CHECKING, Any

from noeta.plugins.mcp.client import StreamClient
from noeta.plugins.mcp.config import DEFAULT_TIMEOUT_S, McpError
from noeta.plugins.mcp.stdio import LINE_CAP

if TYPE_CHECKING:
    from noeta.plugins.mcp.http_client import HeadersFn

__all__ = ["SUBPROTOCOL", "WsClient"]

SUBPROTOCOL = "mcp"
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_TEXT, _BINARY, _CONTINUATION, _CLOSE, _PING, _PONG = 0x1, 0x2, 0x0, 0x8, 0x9, 0xA
#: The largest message accepted (a whole, reassembled one).
MESSAGE_CAP = 8 * LINE_CAP
_HEADER_CAP = 64 * 1024


def accept_key(key: str) -> str:
    """The ``Sec-WebSocket-Accept`` a server answers ``key`` with."""
    return base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()


def frame(opcode: int, payload: bytes, *, mask: bool = True) -> bytes:
    """One final frame (a client masks every frame it sends)."""
    head = bytes([0x80 | opcode])
    size = len(payload)
    bit = 0x80 if mask else 0
    if size < 126:
        head += bytes([bit | size])
    elif size < 1 << 16:
        head += bytes([bit | 126]) + struct.pack("!H", size)
    else:
        head += bytes([bit | 127]) + struct.pack("!Q", size)
    if not mask:
        return head + payload
    key = os.urandom(4)
    return head + key + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


async def read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    """``(final, opcode, payload)`` of the next frame, unmasked."""
    first, second = await reader.readexactly(2)
    size = second & 0x7F
    if size == 126:
        (size,) = struct.unpack("!H", await reader.readexactly(2))
    elif size == 127:
        (size,) = struct.unpack("!Q", await reader.readexactly(8))
    if size > MESSAGE_CAP:
        raise McpError("server frame exceeded cap")
    key = await reader.readexactly(4) if second & 0x80 else b""
    payload = await reader.readexactly(size)
    if key:
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bool(first & 0x80), first & 0x0F, payload


class WsClient(StreamClient):
    """A connection to one WebSocket MCP endpoint."""

    def __init__(self, url: str, *, headers: Mapping[str, str] | None = None,
                 timeout: float = DEFAULT_TIMEOUT_S, headers_fn: HeadersFn | None = None,
                 bare_elicitation: bool = False,
                 ssl_context: ssl.SSLContext | None = None) -> None:
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("ws", "wss") or not parts.hostname:
            raise McpError(f"mcp ws server url must be ws:// or wss://, got {url!r}")
        super().__init__(timeout=timeout, bare_elicitation=bare_elicitation)
        self.url = url
        self.headers = dict(headers or {})
        self._headers_fn = headers_fn
        self._ssl = ssl_context
        self._stream_reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._write_lock = asyncio.Lock()

    async def _open(self) -> None:
        if self._headers_fn is not None:
            self.headers = {**self.headers, **await self._headers_fn()}
        parts = urllib.parse.urlsplit(self.url)
        secure = parts.scheme == "wss"
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_connection(
                    host, port,
                    ssl=(self._ssl or ssl.create_default_context()) if secure else None)
                self._stream_reader, self._writer = reader, writer
                await self._handshake(reader, writer, parts)
        except TimeoutError:
            raise McpError("timeout opening the WebSocket") from None
        except OSError as exc:
            raise McpError(f"connect failed: {exc}") from exc

    async def _handshake(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                         parts: urllib.parse.SplitResult) -> None:
        key = base64.b64encode(os.urandom(16)).decode()
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        lines = [f"GET {path} HTTP/1.1", f"Host: {parts.netloc}", "Upgrade: websocket",
                 "Connection: Upgrade", f"Sec-WebSocket-Key: {key}",
                 "Sec-WebSocket-Version: 13", f"Sec-WebSocket-Protocol: {SUBPROTOCOL}",
                 *(f"{k}: {v}" for k, v in self.headers.items())]
        writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("utf-8"))
        await writer.drain()
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            raise McpError("the server ended the WebSocket handshake") from exc
        if len(head) > _HEADER_CAP:
            raise McpError("WebSocket handshake response too large")
        status, *header_lines = head.decode("latin-1").split("\r\n")
        got = {}
        for line in header_lines:
            name, _, value = line.partition(":")
            if name:
                got[name.strip().lower()] = value.strip()
        if " 101 " not in f"{status} ":
            raise McpError(f"WebSocket handshake refused: {status.strip()}")
        if got.get("sec-websocket-accept") != accept_key(key):
            raise McpError("WebSocket handshake: bad Sec-WebSocket-Accept")
        if got.get("sec-websocket-protocol", SUBPROTOCOL) != SUBPROTOCOL:
            raise McpError("WebSocket handshake: the server chose another subprotocol")

    async def _write(self, msg: dict[str, Any]) -> None:
        await self._frame(_TEXT, json.dumps(msg, separators=(",", ":")).encode("utf-8"))

    async def _frame(self, opcode: int, payload: bytes) -> None:
        if self._writer is None:
            raise McpError("client not started")
        async with self._write_lock:
            self._writer.write(frame(opcode, payload))
            await self._writer.drain()

    async def _messages(self) -> AsyncIterator[dict[str, Any]]:
        reader = self._stream_reader
        assert reader is not None
        parts: list[bytes] = []
        while True:
            try:
                final, opcode, payload = await read_frame(reader)
            except asyncio.IncompleteReadError:
                raise McpError("server closed the WebSocket") from None
            if opcode == _PING:
                await self._frame(_PONG, payload)
                continue
            if opcode == _PONG:
                continue
            if opcode == _CLOSE:
                with contextlib.suppress(Exception):
                    await self._frame(_CLOSE, payload[:2])
                raise McpError("server closed the WebSocket")
            if opcode in (_TEXT, _BINARY, _CONTINUATION):
                parts.append(payload)
                if sum(len(p) for p in parts) > MESSAGE_CAP:
                    raise McpError("server message exceeded cap")
                if not final:
                    continue
                data, parts = b"".join(parts), []
                try:
                    obj = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if isinstance(obj, dict):
                    yield obj

    async def _shut(self) -> None:
        writer, self._writer = self._writer, None
        if writer is None:
            return
        with contextlib.suppress(Exception):
            writer.write(frame(_CLOSE, struct.pack("!H", 1000)))
            await asyncio.wait_for(writer.drain(), 2)
        writer.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(writer.wait_closed(), 2)
