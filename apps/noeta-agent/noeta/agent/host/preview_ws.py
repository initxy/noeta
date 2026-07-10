"""preview_ws — minimal RFC 6455 WebSocket reverse-proxy transport.

Implements the *smallest* subset of RFC 6455 needed to proxy browser WebSocket
frames to a sandbox container and back:

* **Frame codec** — ``read_frame`` / ``write_frame`` handle the wire format
  (2/8-byte length, 4-byte client mask, FIN+opcode header byte). Opcodes
  (text/binary/ping/pong/close) and FIN bits are forwarded **verbatim** —
  the proxy never interprets payload semantics.
* **Handshake** — ``accept_handshake`` (server → browser: compute
  ``Sec-WebSocket-Accept``, send 101, echo negotiated subprotocol) and
  ``connect_upstream`` (proxy → container: generate random key, open TCP,
  send client handshake, verify 101).
* **Pump** — ``pump_bidirectional`` runs two blocking-read forward loops
  in daemon threads; each direction reads frames from socket A and
  writes them (re-masked for the direction) to socket B. Both sockets get
  TCP keepalive and a bounded send timeout so a frozen peer cannot wedge
  a pump thread (and its two file descriptors) forever.

The proxy is **transparent by design**: it offers no ``permessage-deflate``
(no compression negotiation), performs no UTF-8 validation on text frames,
and forwards control frames (ping/pong/close) unchanged. This keeps the
codec to ~200 lines and avoids the bugs a full-compliance implementation
would invite (R1 in the spec).

This module has **zero third-party dependencies** — stdlib only (D2).
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
import sys
import threading
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Constants — RFC 6455 opcodes + magic
# ---------------------------------------------------------------------------

OP_CONTINUATION = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
# 0x3–0x7 reserved for non-control frames
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA
# 0xB–0xF reserved for control frames

# RFC 6455 §1.3 — the GUID appended to Sec-WebSocket-Key before SHA-1.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# The "magic" line prefix for server handshake verification.
_WS_ACCEPT_PREFIX = b"HTTP/1.1 101"

# Upper bound on a single frame's declared payload length. The proxy buffers
# one whole frame at a time, so an endpoint declaring a huge length would
# otherwise grow the buffer until host memory is exhausted. 64 MiB is far
# above anything the real panels emit (a full-frame raw VNC update at
# 1920x1080x4 is ~8 MiB); a larger declaration is treated as a protocol
# error and closes the pipe.
_MAX_FRAME_BYTES = 64 * 1024 * 1024

# Pump socket tuning: a send that stalls this long (peer stopped reading —
# e.g. a frozen browser tab) fails the write and tears the pump down instead
# of blocking its thread forever. Keepalive reaps peers that vanished without
# a FIN (sleep/suspend, yanked network).
_SEND_TIMEOUT_S = 30
_KEEPALIVE_IDLE_S = 60
_KEEPALIVE_INTERVAL_S = 10
_KEEPALIVE_COUNT = 3


# ---------------------------------------------------------------------------
# Frame codec
# ---------------------------------------------------------------------------

def read_frame(sock: socket.socket) -> Optional[Tuple[int, int, bytes]]:
    """Read one WebSocket frame from ``sock``.

    Returns ``(fin, opcode, payload)`` or ``None`` on EOF / connection
    reset. The caller is responsible for closing the socket on ``None``.

    Handles the full RFC 6455 frame layout:
    ``[FIN+RSV+opcode][MASK+len][ext-len?][mask-key?][payload]``.
    Control frames (opcode ≥ 0x8) are required to have ``FIN=1`` and
    payload ≤ 125 bytes (RFC 6455 §5.5); the proxy doesn't enforce this
    — it just forwards what arrives, trusting the two real endpoints.
    The one thing it DOES enforce is :data:`_MAX_FRAME_BYTES`: a frame
    declaring a larger payload returns ``None`` (connection closed by the
    caller) instead of buffering unbounded memory.
    """
    try:
        header = _recv_exact(sock, 2)
    except (ConnectionError, OSError):
        return None
    if header is None:
        return None

    b0 = header[0]
    b1 = header[1]
    fin = (b0 & 0x80) != 0
    opcode = b0 & 0x0F
    masked = (b1 & 0x80) != 0
    length = b1 & 0x7F

    if length == 126:
        ext = _recv_exact(sock, 2)
        if ext is None:
            return None
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = _recv_exact(sock, 8)
        if ext is None:
            return None
        length = struct.unpack("!Q", ext)[0]

    # Bound the buffer BEFORE allocating: a huge declared length (malicious
    # or corrupt) must not grow host memory until it falls over.
    if length > _MAX_FRAME_BYTES:
        return None

    mask_key: Optional[bytes] = None
    if masked:
        mask_key = _recv_exact(sock, 4)
        if mask_key is None:
            return None

    payload = _recv_exact(sock, length) if length > 0 else b""
    if payload is None:
        return None

    if masked and mask_key is not None:
        payload = _apply_mask(payload, mask_key)

    return (fin, opcode, payload)


def write_frame(
    sock: socket.socket,
    fin: bool,
    opcode: int,
    payload: bytes,
    *,
    mask: bool = False,
) -> bool:
    """Write one WebSocket frame to ``sock``.

    When ``mask=True`` (browser→container direction, required by RFC 6455
    §5.3 for client-sent frames), a random 4-byte mask is generated and
    XOR'd onto the payload. Server→browser direction uses ``mask=False``.

    Returns ``True`` on success, ``False`` on write failure (caller
    should close the socket).
    """
    b0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)

    header = bytearray()
    header.append(b0)

    if mask:
        mask_key = os.urandom(4)
        payload = _apply_mask(payload, mask_key)
    else:
        mask_key = None

    if length < 126:
        header.append((0x80 if mask else 0x00) | length)
    elif length < 65536:
        header.append((0x80 if mask else 0x00) | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append((0x80 if mask else 0x00) | 127)
        header.extend(struct.pack("!Q", length))

    if mask_key is not None:
        header.extend(mask_key)

    try:
        sock.sendall(bytes(header))
        if payload:
            sock.sendall(payload)
        return True
    except (BrokenPipeError, ConnectionResetError, OSError):
        return False


def _apply_mask(payload: bytes, mask_key: bytes) -> bytes:
    """XOR ``payload`` with the 4-byte ``mask_key`` (RFC 6455 §5.3)."""
    return bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))


def _recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    """Read exactly ``n`` bytes from ``sock``, or ``None`` on EOF."""
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except (ConnectionError, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------

def compute_accept(key: str) -> str:
    """Compute the ``Sec-WebSocket-Accept`` value for a client key.

    RFC 6455 §1.3: ``base64(SHA-1(key + GUID))``.
    """
    digest = hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def accept_handshake(
    handler,
    *,
    subprotocols: Optional[list[str]] = None,
) -> Optional[str]:
    """Perform the server-side WebSocket accept handshake.

    ``handler`` is a ``BaseHTTPRequestHandler`` instance. Sends a 101
    response with ``Sec-WebSocket-Accept`` and (optionally) the negotiated
    ``Sec-WebSocket-Protocol``.

    Returns the negotiated subprotocol (or ``""`` if none), or ``None``
    if the request is not a valid WebSocket upgrade (the caller should
    fall through to normal HTTP handling).

    **Important**: after this returns successfully, the handler MUST NOT
    send any more HTTP responses — the connection has been "upgraded" and
    is now a raw byte pipe. The caller should set a flag (e.g.
    ``_response_started = True``) and hand the socket to ``pump_bidirectional``.
    """
    headers = handler.headers
    upgrade = headers.get("Upgrade", "")
    if not upgrade or "websocket" not in upgrade.lower():
        return None
    conn = headers.get("Connection", "")
    if "upgrade" not in conn.lower():
        return None
    key = headers.get("Sec-WebSocket-Key", "")
    if not key:
        return None

    accept_value = compute_accept(key)

    # Subprotocol negotiation: pick the first client-requested protocol
    # that the server supports. If none match, omit the header (RFC 6455 §4.1).
    requested_proto = headers.get("Sec-WebSocket-Protocol", "")
    negotiated: Optional[str] = None
    if requested_proto and subprotocols:
        for proto in (p.strip() for p in requested_proto.split(",")):
            if proto in subprotocols:
                negotiated = proto
                break

    # Build the 101 response manually (not via send_response — we need
    # raw control over the headers and no automatic Content-Length).
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Accept: {accept_value}",
    ]
    if negotiated:
        lines.append(f"Sec-WebSocket-Protocol: {negotiated}")

    raw = "\r\n".join(lines).encode("ascii") + b"\r\n\r\n"
    try:
        handler.connection.sendall(raw)
    except (BrokenPipeError, ConnectionResetError, OSError):
        return None

    return negotiated or ""


def connect_upstream(
    base_url: str,
    subpath: str,
    *,
    auth_headers: Optional[dict[str, str]] = None,
    subprotocol: Optional[str] = None,
) -> Optional[socket.socket]:
    """Open a WebSocket connection to the sandbox container.

    ``base_url`` is like ``http://127.0.0.1:12345`` (the sandbox's
    ``base_url``); ``subpath`` is like ``vnc/websockify``.

    ``auth_headers`` (e.g. ``{"X-AIO-API-Key": "..."}``) are added to the
    upstream handshake — the browser never sees these.

    Returns the connected TCP socket (ready for ``read_frame`` /
    ``write_frame``), or ``None`` on failure. The caller is responsible
    for closing the socket.
    """
    # Parse host:port from base_url.
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80

    # Normalise subpath: ensure it starts with '/'.
    if not subpath.startswith("/"):
        subpath = "/" + subpath

    # Generate a random client key.
    key = base64.b64encode(os.urandom(16)).decode("ascii")

    # Build client handshake.
    lines = [
        f"GET {subpath} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    if subprotocol:
        lines.append(f"Sec-WebSocket-Protocol: {subprotocol}")
    if auth_headers:
        for hdr_name, hdr_val in auth_headers.items():
            lines.append(f"{hdr_name}: {hdr_val}")

    raw = "\r\n".join(lines).encode("ascii") + b"\r\n\r\n"

    # Connect and send.
    try:
        sock = socket.create_connection((host, port), timeout=10)
        sock.sendall(raw)
    except (socket.error, OSError):
        return None

    # Read server response (must be 101).
    try:
        sock.settimeout(10)
        response = _recv_http_response(sock)
    except (socket.error, OSError):
        sock.close()
        return None

    if response is None:
        sock.close()
        return None

    status_line = response.split("\r\n")[0]
    if "101" not in status_line:
        sock.close()
        return None

    # Switch to blocking mode for the pump.
    sock.settimeout(None)
    return sock


def _recv_http_response(sock: socket.socket) -> Optional[str]:
    """Read a complete HTTP response (headers only) from ``sock``.

    Reads until ``\\r\\n\\r\\n`` is found. Returns the decoded header
    block, or ``None`` on read failure.
    """
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        try:
            chunk = sock.recv(4096)
        except (socket.error, OSError):
            return None
        if not chunk:
            return None
        buf.extend(chunk)
        if len(buf) > 65536:  # sanity limit for headers
            return None
    return buf.decode("latin-1")


# ---------------------------------------------------------------------------
# Bidirectional pump
# ---------------------------------------------------------------------------

def _tune_pump_socket(sock: socket.socket) -> None:
    """Best-effort keepalive + bounded send on a pump socket.

    ``SO_SNDTIMEO`` (rather than ``settimeout``) bounds only the SEND side:
    the read side must stay fully blocking — an idle-but-healthy panel (a
    VNC session nobody is touching) legitimately goes minutes between
    frames. Every option is individually best-effort: test doubles are
    ``AF_UNIX`` socketpairs where the TCP-level options don't apply.
    """
    for level, opt, value in (
        (socket.SOL_SOCKET, getattr(socket, "SO_KEEPALIVE", None), 1),
        (socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPIDLE", None), _KEEPALIVE_IDLE_S),
        (socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPINTVL", None), _KEEPALIVE_INTERVAL_S),
        (socket.IPPROTO_TCP, getattr(socket, "TCP_KEEPCNT", None), _KEEPALIVE_COUNT),
    ):
        if opt is None:
            continue
        try:
            sock.setsockopt(level, opt, value)
        except OSError:
            pass
    # POSIX ``struct timeval``; Windows wants a DWORD of milliseconds instead,
    # so skip there (the proxy ships on Linux — D2's stdlib-only posture).
    if sys.platform != "win32" and hasattr(socket, "SO_SNDTIMEO"):
        try:
            sock.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_SNDTIMEO,
                struct.pack("@ll", _SEND_TIMEOUT_S, 0),
            )
        except OSError:
            pass


def pump_bidirectional(
    browser_sock: socket.socket,
    upstream_sock: socket.socket,
    *,
    upstream_mask: bool = True,
) -> None:
    """Forward WebSocket frames between ``browser_sock`` and ``upstream_sock``.

    Spawns two daemon threads:

    * **downstream** — reads frames from upstream (container → proxy →
      browser), writes them ``mask=False`` (server frames are unmasked).
    * **upstream** — reads frames from browser (browser → proxy →
      container), writes them ``mask=upstream_mask`` (client frames must
      be masked per RFC 6455 §5.3; set ``False`` if the upstream is also
      a server and doesn't require masking).

    Both threads forward ``(fin, opcode, payload)`` *verbatim* — no
    interpretation of text/binary/control semantics. When either leg
    returns ``None`` from ``read_frame`` (EOF), both sockets are closed
    and the threads exit.

    This function blocks until both threads have exited (caller should
    invoke it in its own thread or after a ``ThreadingHTTPServer``
    handler has hijacked the connection).
    """
    _tune_pump_socket(browser_sock)
    _tune_pump_socket(upstream_sock)
    stop_event = threading.Event()

    def forward(src: socket.socket, dst: socket.socket, mask_out: bool) -> None:
        """Forward frames from ``src`` to ``dst`` until EOF."""
        try:
            while not stop_event.is_set():
                frame = read_frame(src)
                if frame is None:
                    break
                fin, opcode, payload = frame
                if not write_frame(dst, fin, opcode, payload, mask=mask_out):
                    break
                # Forward close frame → close both legs.
                if opcode == OP_CLOSE:
                    break
        finally:
            stop_event.set()
            # Best-effort shutdown of both sockets to unblock the other thread.
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    t_down = threading.Thread(
        target=forward,
        args=(upstream_sock, browser_sock, False),
        name="ws-pump-downstream",
        daemon=True,
    )
    t_up = threading.Thread(
        target=forward,
        args=(browser_sock, upstream_sock, upstream_mask),
        name="ws-pump-upstream",
        daemon=True,
    )

    t_down.start()
    t_up.start()

    t_down.join()
    t_up.join()

    # Final cleanup.
    for s in (browser_sock, upstream_sock):
        try:
            s.close()
        except OSError:
            pass
