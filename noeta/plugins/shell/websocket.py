"""The WebSocket source of ``Monitor``: a minimal RFC 6455 client that turns
each text frame the server sends into an event.

It runs in the worker process (no shell, no external program), so it needs
no dependency: the opening handshake over ``asyncio`` streams (TLS for
``wss``), then frames read one at a time. Client frames (pong, close) are
masked as the protocol requires; fragmented messages are joined; a binary
message is reported by its size, never passed through. The close code the
server sends (or 1006 when the connection drops without one) ends the watch.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import ssl
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

__all__ = ["WsExit", "parse_ws", "run_ws"]

#: RFC 6455's key suffix for ``Sec-WebSocket-Accept``.
_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
#: A message larger than this ends the watch (the protocol has no cap).
MAX_MESSAGE_BYTES = 16 * 1024 * 1024
#: The handshake response must fit in this.
_MAX_HEADER_BYTES = 64 * 1024
#: Close code for a connection that ended without a close frame, and for a
#: close frame that carries no code.
ABNORMAL_CLOSE = 1006
NO_STATUS = 1005
#: Close codes that count as a normal end of the stream.
NORMAL_CLOSES = (1000, 1001)
HANDSHAKE_TIMEOUT_S = 30.0
#: A subprotocol token (RFC 7230 ``token``).
_TOKEN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")

_CONT, _TEXT, _BINARY, _CLOSE, _PING, _PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


@dataclass(frozen=True)
class WsExit:
    """How the socket ended, in the shape a job's run reports: the close
    code stands for the exit code."""

    exit_code: int


def parse_ws(value: Any) -> tuple[str, list[str]] | str:
    """``(url, protocols)`` from ``Monitor``'s ``ws`` argument, or why it is
    unusable."""
    if not isinstance(value, Mapping):
        return "'ws' must be an object with a 'url'"
    url = value.get("url")
    if not isinstance(url, str) or not url.strip():
        return "'ws.url' must be a non-empty string"
    parts = urlsplit(url.strip())
    if parts.scheme not in ("ws", "wss") or not parts.hostname:
        return f"'ws.url' must be a ws:// or wss:// URL; got {url!r}"
    protocols = value.get("protocols") or []
    if not isinstance(protocols, list) or not all(
            isinstance(p, str) and _TOKEN.match(p) for p in protocols):
        return "'ws.protocols' must be a list of protocol tokens"
    return url.strip(), list(protocols)


def _frame(opcode: int, payload: bytes = b"") -> bytes:
    """A masked client frame (final, unfragmented)."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack("!H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack("!Q", n)
    mask = os.urandom(4)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


async def _handshake(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     host: str, target: str, protocols: Sequence[str]) -> None:
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [f"GET {target} HTTP/1.1", f"Host: {host}", "Upgrade: websocket",
             "Connection: Upgrade", f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
    if protocols:
        lines.append(f"Sec-WebSocket-Protocol: {', '.join(protocols)}")
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
    await writer.drain()
    raw = await reader.readuntil(b"\r\n\r\n")
    if len(raw) > _MAX_HEADER_BYTES:
        raise ConnectionError("the handshake response is too large")
    status, *headers = raw.decode("latin-1").split("\r\n")
    if len(status.split()) < 2 or status.split()[1] != "101":
        raise ConnectionError(f"the server did not switch protocols ({status.strip()})")
    fields = {k.strip().lower(): v.strip() for k, _, v in
              (h.partition(":") for h in headers if h)}
    expected = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
    if fields.get("sec-websocket-accept") != expected:
        raise ConnectionError("the server's Sec-WebSocket-Accept does not match")


async def _read_frame(reader: asyncio.StreamReader) -> tuple[bool, int, bytes]:
    b0, b1 = await reader.readexactly(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", await reader.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", await reader.readexactly(8))[0]
    if n > MAX_MESSAGE_BYTES:
        raise ConnectionError(f"a frame of {n} bytes is over the {MAX_MESSAGE_BYTES}-byte cap")
    mask = await reader.readexactly(4) if b1 & 0x80 else b""
    data = await reader.readexactly(n)
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return bool(b0 & 0x80), b0 & 0x0F, data


async def _stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  emit: Callable[[str], None]) -> WsExit:
    """Messages until the server's close frame."""
    kind: int | None = None
    parts: list[bytes] = []
    while True:
        fin, opcode, data = await _read_frame(reader)
        if opcode == _PING:
            writer.write(_frame(_PONG, data))
            await writer.drain()
            continue
        if opcode == _PONG:
            continue
        if opcode == _CLOSE:
            code = struct.unpack("!H", data[:2])[0] if len(data) >= 2 else NO_STATUS
            with contextlib.suppress(OSError):
                writer.write(_frame(_CLOSE, data[:2]))
                await writer.drain()
            return WsExit(code)
        if opcode in (_TEXT, _BINARY):
            kind, parts = opcode, [data]
        elif opcode == _CONT and kind is not None:
            parts.append(data)
        else:
            raise ConnectionError(f"unexpected frame opcode {opcode}")
        if sum(len(p) for p in parts) > MAX_MESSAGE_BYTES:
            raise ConnectionError(f"a message is over the {MAX_MESSAGE_BYTES}-byte cap")
        if not fin:
            continue
        message = b"".join(parts)
        if kind == _TEXT:
            emit(message.decode("utf-8", errors="replace"))
        else:
            emit(f"[binary frame, {len(message)} bytes]")
        kind, parts = None, []


async def run_ws(url: str, protocols: Sequence[str], emit: Callable[[str], None]) -> WsExit:
    """Stream ``url``'s messages to ``emit`` (one call per text message, a
    ``[binary frame, N bytes]`` line per binary one) until the socket
    closes; an error is emitted before the end. Cancelling closes the
    socket."""
    parts = urlsplit(url)
    secure = parts.scheme == "wss"
    host = parts.hostname or ""
    port = parts.port or (443 if secure else 80)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    host_header = parts.netloc.rpartition("@")[2]
    writer: asyncio.StreamWriter | None = None
    try:
        reader, opened = await asyncio.wait_for(asyncio.open_connection(
            host, port, ssl=ssl.create_default_context() if secure else None,
            limit=_MAX_HEADER_BYTES), HANDSHAKE_TIMEOUT_S)
        writer = opened
        await asyncio.wait_for(_handshake(reader, opened, host_header, target, protocols),
                               HANDSHAKE_TIMEOUT_S)
        return await _stream(reader, opened, emit)
    except asyncio.IncompleteReadError:
        emit("WebSocket error: the connection closed without a close frame")
        return WsExit(ABNORMAL_CLOSE)
    except (TimeoutError, OSError, ConnectionError, ssl.SSLError, asyncio.LimitOverrunError,
            ValueError) as exc:
        emit(f"WebSocket error: {exc or type(exc).__name__}")
        return WsExit(ABNORMAL_CLOSE)
    finally:
        if writer is not None:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
