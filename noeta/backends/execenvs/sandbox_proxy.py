"""The sandbox's network filter: an HTTP proxy (``CONNECT`` and plain
``http://`` requests) and a SOCKS5 proxy that connect only to the hosts the
sandbox settings allow, and remember the ones they refused.

Both run on one event loop in a daemon thread of their own, shared by every
sandbox of the process, so a slow upstream never stalls the agent's loop.
Each confined command gets its own pair of listeners (:meth:`FilterProxy.open`):
Unix sockets for a Linux sandbox (its network namespace reaches the host
only through them) or TCP ports on a given address (macOS, a container's
gateway). The refusals of one listener pair are that command's.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import os
import struct
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from noeta.toolkit.sandbox import domain_allowed, resolve_allowed

__all__ = ["FilterProxy", "Listeners", "default_proxy"]

log = logging.getLogger("noeta.sandbox")

#: Longest request head the HTTP proxy reads before giving up.
_HEAD_MAX = 64 * 1024
#: How long an upstream connect may take.
_CONNECT_TIMEOUT_S = 30.0
_CHUNK = 64 * 1024
_HOP_HEADERS = frozenset({"proxy-connection", "proxy-authorization", "connection",
                          "keep-alive"})


@dataclass
class Listeners:
    """One command's proxy pair. ``http`` / ``socks`` are the socket paths
    (Unix) or ``(host, port)`` (TCP)."""

    http: Any
    socks: Any
    allowed: tuple[str, ...]
    denied: tuple[str, ...]
    refused: list[str] = field(default_factory=list)
    servers: list[asyncio.base_events.Server] = field(default_factory=list)

    def check(self, host: str, port: int) -> bool:
        if domain_allowed(host, port, self.allowed, self.denied):
            return True
        self.refuse(host, port)
        return False

    def refuse(self, host: str, port: int) -> None:
        entry = f"{host}:{port}"
        if entry not in self.refused:
            self.refused.append(entry)

    async def connect(self, host: str, port: int,
                      ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        """A connection to an allowed ``host:port``, at the address that was
        checked; ``None`` when the name resolves only to addresses no
        command may reach (refused like a host not allowed). A failed
        connection raises."""
        try:
            address = await asyncio.wait_for(
                resolve_allowed(host, port, self.allowed, self.denied), _CONNECT_TIMEOUT_S)
        except PermissionError as exc:
            log.info("sandbox: refused %s:%s: %s", host, port, exc)
            self.refuse(host, port)
            return None
        return await _connect(address, port)


class FilterProxy:
    """The filtering proxies of this process (:func:`default_proxy`)."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock = threading.Lock()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            if self._loop is None or self._loop.is_closed():
                loop = asyncio.new_event_loop()
                ready = threading.Event()

                def serve() -> None:
                    asyncio.set_event_loop(loop)
                    loop.call_soon(ready.set)
                    loop.run_forever()

                threading.Thread(target=serve, name="noeta-sandbox-proxy", daemon=True).start()
                ready.wait()
                self._loop = loop
            return self._loop

    async def _call(self, coro: Awaitable[Any]) -> Any:
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(_as_coro(coro), loop)
        return await asyncio.wrap_future(future)

    async def open(self, *, allowed: Sequence[str], denied: Sequence[str] = (),
                   unix_dir: str | None = None, tcp_host: str | None = None,
                   tcp_ports: tuple[int, int] = (0, 0), upstream_http: int | None = None,
                   upstream_socks: int | None = None) -> Listeners:
        """A new HTTP + SOCKS5 listener pair: Unix sockets in ``unix_dir``,
        else TCP on ``tcp_host`` (``tcp_ports`` fixed, or ``0`` for any).
        ``upstream_*``: the user's own proxy on that loopback port takes the
        connections unfiltered instead (``httpProxyPort`` / ``socksProxyPort``)."""
        listeners: Listeners = await self._call(self._open(
            tuple(allowed), tuple(denied), unix_dir, tcp_host, tcp_ports,
            (upstream_http, upstream_socks)))
        return listeners

    async def _open(self, allowed: tuple[str, ...], denied: tuple[str, ...],
                    unix_dir: str | None, tcp_host: str | None,
                    tcp_ports: tuple[int, int],
                    upstream: tuple[int | None, int | None]) -> Listeners:
        listeners = Listeners(http=None, socks=None, allowed=allowed, denied=denied)
        Handler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]

        def pick(own: Handler, port: int | None) -> Handler:
            return own if port is None else (lambda r, w: _forward(port, r, w))

        handlers = [pick(lambda r, w: _http(listeners, r, w), upstream[0]),
                    pick(lambda r, w: _socks(listeners, r, w), upstream[1])]
        addresses: list[Any] = []
        try:
            for i, handler in enumerate(handlers):
                if unix_dir is not None:
                    path = os.path.join(unix_dir, ("http", "socks")[i] + ".sock")
                    with contextlib.suppress(FileNotFoundError):
                        os.unlink(path)
                    server = await asyncio.start_unix_server(handler, path=path)
                    os.chmod(path, 0o600)
                    addresses.append(path)
                else:
                    server = await asyncio.start_server(handler, host=tcp_host or "127.0.0.1",
                                                        port=tcp_ports[i])
                    addresses.append(server.sockets[0].getsockname()[:2])
                listeners.servers.append(server)
        except BaseException:
            await self._close(listeners)
            raise
        listeners.http, listeners.socks = addresses
        return listeners

    async def close(self, listeners: Listeners) -> None:
        await self._call(self._close(listeners))

    async def _close(self, listeners: Listeners) -> None:
        for server in listeners.servers:
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 1.0)
        for address in (listeners.http, listeners.socks):
            if isinstance(address, str):
                with contextlib.suppress(OSError):
                    os.unlink(address)
        listeners.servers.clear()


async def _as_coro(awaitable: Awaitable[Any]) -> Any:
    return await awaitable


_DEFAULT: FilterProxy | None = None


def default_proxy() -> FilterProxy:
    global _DEFAULT
    if _DEFAULT is None:
        _DEFAULT = FilterProxy()
    return _DEFAULT


# ---------------------------------------------------------------- piping


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            chunk = await reader.read(_CHUNK)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
        if writer.can_write_eof():
            writer.write_eof()
    except (ConnectionError, OSError):
        pass


async def _splice(client: tuple[asyncio.StreamReader, asyncio.StreamWriter],
                  upstream: tuple[asyncio.StreamReader, asyncio.StreamWriter]) -> None:
    try:
        await asyncio.gather(_pipe(client[0], upstream[1]), _pipe(upstream[0], client[1]))
    finally:
        for w in (client[1], upstream[1]):
            w.close()


async def _connect(host: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    return await asyncio.wait_for(asyncio.open_connection(host, port), _CONNECT_TIMEOUT_S)


async def _close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()


async def _forward(port: int, reader: asyncio.StreamReader,
                   writer: asyncio.StreamWriter) -> None:
    """Hand the connection, unread, to the user's proxy on ``port``."""
    try:
        upstream = await _connect("127.0.0.1", port)
    except (TimeoutError, OSError):
        await _close_writer(writer)
        return
    await _splice((reader, writer), upstream)


# ---------------------------------------------------------------- HTTP


def _refusal(host: str, port: int) -> bytes:
    body = (f"Connection to {host}:{port} blocked by the noeta sandbox: the host is not in "
            "sandbox.network.allowedDomains, or resolves only to a local, link-local or "
            "metadata address.\n").encode()
    return (b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\n"
            b"X-Proxy-Error: blocked-by-allowlist\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode() + body)


def _host_port(authority: str, default: int) -> tuple[str, int]:
    if authority.startswith("["):
        host, _, rest = authority[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = authority.rpartition(":") if authority.count(":") == 1 \
            else (authority, "", "")
    return host, int(port) if port.isdigit() else default


async def _http(ls: Listeners, reader: asyncio.StreamReader,
                writer: asyncio.StreamWriter) -> None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
        await _close_writer(writer)
        return
    if len(head) > _HEAD_MAX:
        await _close_writer(writer)
        return
    lines = head.decode("latin-1").split("\r\n")
    try:
        method, target, version = lines[0].split(" ", 2)
    except ValueError:
        writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        await _close_writer(writer)
        return
    if method.upper() == "CONNECT":
        host, port = _host_port(target, 443)
        if not ls.check(host, port):
            writer.write(_refusal(host, port))
            await _close_writer(writer)
            return
        try:
            upstream = await ls.connect(host, port)
        except (TimeoutError, OSError) as exc:
            writer.write(f"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n{exc}\n"
                         .encode())
            await _close_writer(writer)
            return
        if upstream is None:
            writer.write(_refusal(host, port))
            await _close_writer(writer)
            return
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await _splice((reader, writer), upstream)
        return
    if not target.lower().startswith("http://"):
        writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
        await _close_writer(writer)
        return
    rest = target[len("http://"):]
    authority, slash, path = rest.partition("/")
    host, port = _host_port(authority.rpartition("@")[2], 80)
    if not ls.check(host, port):
        writer.write(_refusal(host, port))
        await _close_writer(writer)
        return
    try:
        upstream = await ls.connect(host, port)
    except (TimeoutError, OSError) as exc:
        writer.write(f"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n{exc}\n".encode())
        await _close_writer(writer)
        return
    if upstream is None:
        writer.write(_refusal(host, port))
        await _close_writer(writer)
        return
    headers = [h for h in lines[1:] if h and h.split(":", 1)[0].strip().lower()
               not in _HOP_HEADERS]
    out = f"{method} /{path if slash else ''} {version}\r\n" + "".join(
        f"{h}\r\n" for h in headers) + "Connection: close\r\n\r\n"
    upstream[1].write(out.encode("latin-1"))
    await _splice((reader, writer), upstream)


# ---------------------------------------------------------------- SOCKS5


async def _socks(ls: Listeners, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter) -> None:
    def reply(code: int) -> bytes:
        return bytes([5, code, 0, 1, 0, 0, 0, 0, 0, 0])

    try:
        ver, count = await reader.readexactly(2)
        await reader.readexactly(count)
        if ver != 5:
            await _close_writer(writer)
            return
        writer.write(b"\x05\x00")
        ver, cmd, _rsv, atyp = await reader.readexactly(4)
        if atyp == 1:
            host = str(ipaddress.IPv4Address(await reader.readexactly(4)))
        elif atyp == 3:
            (size,) = await reader.readexactly(1)
            host = (await reader.readexactly(size)).decode("idna")
        elif atyp == 4:
            host = str(ipaddress.IPv6Address(await reader.readexactly(16)))
        else:
            writer.write(reply(8))
            await _close_writer(writer)
            return
        (port,) = struct.unpack("!H", await reader.readexactly(2))
    except (asyncio.IncompleteReadError, ConnectionError, ValueError, UnicodeError):
        await _close_writer(writer)
        return
    if cmd != 1:
        writer.write(reply(7))
        await _close_writer(writer)
        return
    if not ls.check(host, port):
        writer.write(reply(2))
        await _close_writer(writer)
        return
    try:
        upstream = await ls.connect(host, port)
    except (TimeoutError, OSError):
        writer.write(reply(5))
        await _close_writer(writer)
        return
    if upstream is None:
        writer.write(reply(2))
        await _close_writer(writer)
        return
    writer.write(reply(0))
    await writer.drain()
    await _splice((reader, writer), upstream)
