"""sandbox_preview_gateway — per-session sandbox live-preview reverse proxy.

Mirrors :class:`noeta.agent.host.preview_gateway.PreviewGateway` (mount
registry + lock + limit pattern) but for **container** preview surfaces
(noVNC browser / terminal PTY / code-server) rather than model-opened HTML
apps. Three panels all ride the same gateway:

* **browser** — noVNC iframe at ``/vnc/index.html``; its websockify WS lives
  at container root ``/websockify`` and is steered inside the token prefix
  via noVNC's ``?path=`` query param (pinned live in W7)
* **terminal** — xterm.js page at ``/terminal`` (no trailing slash — the page
  resolves its PTY WS relative to the URL; container serves ``/v1/shell/ws``)
* **code** — code-server at ``/code-server/`` (page HTTP + internal WS)

The gateway provides:

* **registry** — ``token -> {base_url, auth_headers, root_task_id}``,
  registered on container allocate, unregistered on release.
* **HTTP passthrough** — ``/sandbox-preview/<token>/<sub>`` → ``http://base/<sub>``
  with auth injected upstream. Used for noVNC/code-server static pages.
* **WS reverse proxy** — ``/sandbox-preview/<token>/<sub>`` with
  ``Upgrade: websocket`` → RFC 6455 frame pump to ``ws://base/<sub>``
  (auth in upstream handshake only).
* **dedicated origin** — :func:`make_preview_server` serves the gateway on
  its OWN port, never noeta's main port. The panels need
  ``allow-same-origin`` (noVNC localStorage, code-server service worker),
  which makes iframe content same-origin with whatever host serves it —
  container-controlled JS must therefore land on an origin that holds no
  noeta state (no cookies, no control API), or a compromised container
  could drive the agent's own control plane. The preview port is that
  blank origin; discovery (``preview_info``) advertises it to the frontend.

**v1 DEMO BOUNDARY**: localhost binding, unguessable token only, no
browser-leg auth, credentials never injected to browser (D6 / ADR
execution-environment-seam §"Browser subsystem").
"""

from __future__ import annotations

import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional

from noeta.agent.host.preview_ws import (
    accept_handshake,
    connect_upstream,
    pump_bidirectional,
)

__all__ = [
    "SandboxPreviewGateway",
    "SandboxPreviewMount",
    "make_preview_server",
]

_PREFIX = "/sandbox-preview/"
_DEFAULT_MOUNT_LIMIT = 32
_PROXY_TIMEOUT = 30.0

# Headers NOT forwarded upstream (hop-by-hop / urllib-recomputed /
# browser-supplied junk from the sandboxed iframe). Mirrors PreviewGateway.
_DROP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "proxy-connection",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "accept-encoding",
        "origin",
        "referer",
        "cookie",
    }
)


@dataclass(frozen=True, slots=True)
class _MountEntry:
    base_url: str
    auth_headers: dict[str, str]
    root_task_id: str


@dataclass(frozen=True, slots=True)
class SandboxPreviewMount:
    """Result of :meth:`SandboxPreviewGateway.mount_root`."""

    token: str
    root_task_id: str


class SandboxPreviewGateway:
    """Registry + HTTP passthrough + WS reverse proxy for per-session sandbox live preview.

    Lifecycle:
    * ``mount_root(root_task_id, base_url, auth_headers)`` — called when a
      container is allocated (W3 lifecycle wiring). Returns a
      :class:`SandboxPreviewMount` with an unguessable token.
    * ``unmount_root(root_task_id)`` — called on container release / task
      terminal. Returns count removed.

    Routing (called by the dedicated preview server, ``make_preview_server``):
    * ``is_preview_path(path)`` — quick prefix check.
    * ``route_http(method, path, query, *, content_type, body, headers)``
      — HTTP passthrough (noVNC / code-server static assets). Returns
      ``(status, content_type, body_bytes)`` or ``None`` if not a
      preview path.
    * ``try_handle_ws(handler, path)`` — if the request is a WS upgrade to
      a valid preview token, connects the container leg, then performs the
      accept handshake and starts the bidirectional pump. Returns ``True``
      if handled — including an unreachable-upstream 502 sent BEFORE any
      101 (the connection is otherwise a raw WS pipe; the caller must NOT
      send further HTTP responses).
    """

    def __init__(self, *, mount_limit: int = _DEFAULT_MOUNT_LIMIT) -> None:
        self._mount_limit = mount_limit
        self._lock = threading.Lock()
        self._mounts: dict[str, _MountEntry] = {}  # token -> entry
        self._tokens_by_root: dict[str, str] = {}  # root_task_id -> token
        # The dedicated preview server's bound port, advertised to the
        # frontend via ``preview_info`` (set by the product after
        # :func:`make_preview_server` binds). ``None`` until then.
        self._advertised_port: Optional[int] = None

    def set_advertised_port(self, port: int) -> None:
        """Record the dedicated preview server's bound port for discovery."""
        self._advertised_port = port

    # -- registry ------------------------------------------------------------

    def mount_root(
        self,
        root_task_id: str,
        base_url: str,
        auth_headers: dict[str, str],
    ) -> SandboxPreviewMount:
        """Register a sandbox preview mount for ``root_task_id``.

        Called when a per-session container is allocated. If the root
        already has a mount (e.g. re-drive after reconnect), it is
        replaced (idempotent upgrade — the old token is invalidated).
        """
        token = secrets.token_urlsafe(16)
        entry = _MountEntry(
            base_url=base_url.rstrip("/"),
            auth_headers=dict(auth_headers),
            root_task_id=root_task_id,
        )
        with self._lock:
            # Replace existing mount for this root (re-connect / re-drive).
            old_token = self._tokens_by_root.get(root_task_id)
            if old_token and old_token in self._mounts:
                del self._mounts[old_token]
            # Enforce mount limit.
            if len(self._mounts) >= self._mount_limit and old_token is None:
                # Evict the oldest root's mount (LRU-ish: first by insertion
                # order dict iteration = oldest inserted).
                for tok in list(self._mounts):
                    doomed = self._mounts[tok]
                    self._tokens_by_root.pop(doomed.root_task_id, None)
                    del self._mounts[tok]
                    break
            self._mounts[token] = entry
            self._tokens_by_root[root_task_id] = token
        return SandboxPreviewMount(token=token, root_task_id=root_task_id)

    def unmount_root(self, root_task_id: str) -> int:
        """Remove a root's preview mount; return count removed (0 or 1)."""
        with self._lock:
            token = self._tokens_by_root.pop(root_task_id, None)
            if token is None:
                return 0
            self._mounts.pop(token, None)
            return 1

    @property
    def mount_count(self) -> int:
        with self._lock:
            return len(self._mounts)

    def token_for_root(self, root_task_id: str) -> Optional[str]:
        """Return the preview token for a root, or ``None`` if not mounted."""
        with self._lock:
            return self._tokens_by_root.get(root_task_id)

    def _lookup(self, token: str) -> Optional[_MountEntry]:
        with self._lock:
            return self._mounts.get(token)

    # -- path helpers --------------------------------------------------------

    @staticmethod
    def is_preview_path(path: str) -> bool:
        """Quick prefix check: is ``path`` under ``/sandbox-preview/``?"""
        return path.startswith(_PREFIX)

    @staticmethod
    def parse_preview_path(path: str) -> Optional[tuple[str, str]]:
        """Split ``/sandbox-preview/<token>/<sub>`` → ``(token, sub)``.

        Returns ``None`` if not a valid preview path.
        """
        if not path.startswith(_PREFIX):
            return None
        rest = path[len(_PREFIX):]
        token, sep, sub = rest.partition("/")
        if not token:
            return None
        return (token, sub if sep else "")

    # -- HTTP passthrough -----------------------------------------------------

    def route_http(
        self,
        method: str,
        path: str,
        query: str,
        *,
        content_type: Optional[str] = None,
        body: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Optional[tuple[int, str, bytes]]:
        """Pass an HTTP request through to the sandbox container.

        Returns ``(status, content_type, body)`` or ``None`` if not a
        preview path. No CORS headers are involved: the preview is served
        on its own origin (see module docstring), so every fetch a panel
        page makes is same-origin from the browser's point of view.
        """
        parsed = self.parse_preview_path(path)
        if parsed is None:
            return None
        token, sub = parsed

        entry = self._lookup(token)
        if entry is None:
            return (404, "text/plain; charset=utf-8", b"unknown preview token")

        # Build upstream URL.
        upstream_url = entry.base_url
        if sub:
            upstream_url += "/" + sub
        if query:
            upstream_url += "?" + query

        # Forward headers (minus drop list), inject auth.
        fwd = {k: v for k, v in (headers or {}).items()}
        lowered = {k.lower() for k in fwd}
        if content_type is not None and "content-type" not in lowered:
            fwd["content-type"] = content_type
        for hdr_name, hdr_val in entry.auth_headers.items():
            fwd[hdr_name] = hdr_val

        req = urllib.request.Request(upstream_url, data=body, method=method)
        for name, value in fwd.items():
            if name.lower() in _DROP_REQUEST_HEADERS:
                continue
            req.add_header(name, value)

        try:
            with urllib.request.urlopen(req, timeout=_PROXY_TIMEOUT) as upstream:
                status = upstream.status
                resp_body = upstream.read()
                ctype = upstream.headers.get(
                    "Content-Type", "application/octet-stream"
                )
        except urllib.error.HTTPError as exc:
            status = exc.code
            resp_body = exc.read()
            ctype = exc.headers.get("Content-Type", "application/octet-stream")
        except (urllib.error.URLError, OSError, TimeoutError):
            return (
                502,
                "application/json; charset=utf-8",
                b'{"error":"sandbox unreachable"}',
            )
        return (status, ctype, resp_body)

    # -- WS reverse proxy -----------------------------------------------------

    def try_handle_ws(
        self,
        handler: Any,
        path: str,
    ) -> bool:
        """Attempt a WebSocket upgrade + pump for a preview path.

        Returns ``True`` if the connection was upgraded and the pump ran to
        completion (the caller's thread was blocked for the duration — the
        ``ThreadingHTTPServer`` gives each connection its own thread, so this
        is the correct ownership model). Returns ``False`` if this is not a WS
        upgrade request or not a valid preview path (caller falls through to
        normal HTTP handling).

        The handler's ``_response_started`` flag is set to ``True`` on
        successful upgrade so the error handler doesn't try to write a
        second response.
        """
        parsed = self.parse_preview_path(path)
        if parsed is None:
            return False
        token, sub = parsed

        entry = self._lookup(token)
        if entry is None:
            return False

        # Check if this is actually a WS upgrade request. Validate the FULL
        # upgrade header set here (mirroring accept_handshake's checks) so a
        # malformed request neither dials the container nor gets a 101.
        headers = handler.headers
        if "websocket" not in headers.get("Upgrade", "").lower():
            return False
        if "upgrade" not in headers.get("Connection", "").lower():
            return False
        if not headers.get("Sec-WebSocket-Key", ""):
            return False

        requested_proto = headers.get("Sec-WebSocket-Protocol", "")
        subprotocols = [p.strip() for p in requested_proto.split(",") if p.strip()] if requested_proto else None
        # accept_handshake picks the first requested protocol we offer — we
        # offer them all, so precompute the same choice for the upstream dial.
        negotiated = subprotocols[0] if subprotocols else None

        # Connect to the upstream container WS BEFORE sending 101: an
        # unreachable container must surface as a real HTTP error the client
        # can handle, not a 101 followed by an abrupt close (noVNC/xterm.js
        # get no close frame from that and can't tell what happened).
        upstream_sock = connect_upstream(
            entry.base_url,
            sub,
            auth_headers=entry.auth_headers,
            subprotocol=negotiated,
        )
        if upstream_sock is None:
            self._send_plain_error(handler, 502, b"sandbox upstream unreachable")
            return True

        # Perform the server-side accept handshake (sends the 101).
        accepted = accept_handshake(handler, subprotocols=subprotocols)
        if accepted is None:
            # Headers validated above, so only a browser-leg send failure
            # lands here — nothing more to say to the browser; drop upstream.
            try:
                upstream_sock.close()
            except OSError:
                pass
            handler._response_started = True
            return True

        # Mark response as started so the error handler doesn't double-write.
        handler._response_started = True

        # Get the raw browser socket and ensure it's in blocking mode.
        browser_sock = handler.connection
        try:
            browser_sock.settimeout(None)
        except OSError:
            pass

        # Run the pump SYNCHRONOUSLY in this handler thread.
        # ``ThreadingHTTPServer`` gives each connection its own thread, so
        # blocking here is correct: the socket lives for the pump's lifetime,
        # and ``shutdown_request`` fires after we return (at which point both
        # legs are already closed by the pump).
        pump_bidirectional(browser_sock, upstream_sock)
        return True

    @staticmethod
    def _send_plain_error(handler: Any, status: int, body: bytes) -> None:
        """Write a plain-text HTTP error on a not-yet-upgraded connection."""
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", "text/plain; charset=utf-8")
            handler.send_header("Content-Length", str(len(body)))
            handler.send_header("Connection", "close")
            handler.end_headers()
            handler.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        handler._response_started = True

    # -- discovery (GET /tasks/{id}/preview) ---------------------------------

    def preview_info(self, root_task_id: str) -> Optional[dict[str, Any]]:
        """Return the preview discovery payload for a root task.

        Shape: ``{"token": str, "port": int|None, "panels": {"browser": str,
        "terminal": str, "code": str}}`` or ``None`` if no sandbox is
        mounted for this root.

        ``port`` is the dedicated preview server's bound port (see module
        docstring — the panels live on their own origin); the frontend
        builds ``http://<same-hostname>:<port>/sandbox-preview/<token>/``
        and appends the panel ``<sub>`` paths to open each surface.
        """
        token = self.token_for_root(root_task_id)
        if token is None:
            return None
        return {
            "token": token,
            "port": self._advertised_port,
            "panels": {
                # noVNC's standard UI connects to ws://<host>/websockify by
                # default — an absolute path that would escape the token
                # prefix. Its `path=` query param redirects the WS inside the
                # proxy (the container serves websockify at root /websockify).
                "browser": (
                    "vnc/index.html?autoconnect=true&resize=scale"
                    f"&path=sandbox-preview/{token}/websockify"
                ),
                # No trailing slash: the terminal page resolves its PTY WS as
                # new URL('v1/shell/ws', '.') — served at .../terminal/ it
                # would aim at terminal/v1/shell/ws (404 upstream); served at
                # .../terminal it lands on <prefix>/v1/shell/ws, which the
                # container serves at root /v1/shell/ws.
                "terminal": "terminal",
                "code": "code-server/",
            },
        }


# ---------------------------------------------------------------------------
# Dedicated preview server (origin isolation)
# ---------------------------------------------------------------------------

class _SandboxPreviewHandler(BaseHTTPRequestHandler):
    """Slim handler for the dedicated preview port: gateway traffic ONLY.

    Serves nothing but ``/sandbox-preview/<token>/...`` (HTTP passthrough + WS reverse proxy).
    There is deliberately no router, no SPA, no control API on this origin —
    that blankness IS the security property (see the gateway module
    docstring): the panels' iframes run ``allow-same-origin`` against this
    origin, so container-controlled JS gains nothing beyond its own preview.
    """

    protocol_version = "HTTP/1.1"
    _response_started = False

    @property
    def gateway(self) -> "SandboxPreviewGateway":
        return self.server.gateway  # type: ignore[attr-defined]

    def _send_plain(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self._response_started = True
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def _dispatch(self, method: str) -> None:
        gw = self.gateway
        split = urllib.parse.urlsplit(self.path)
        if not gw.is_preview_path(split.path):
            self._send_plain(404, b"not found")
            return

        # WS upgrade → hijack the connection into the pump. Pass the RAW
        # request target (query intact): the terminal PTY WS carries
        # ``?session_id=...``, which must reach the container.
        if "websocket" in self.headers.get("Upgrade", "").lower():
            if gw.try_handle_ws(self, self.path):
                return
            self._send_plain(400, b"bad websocket request")
            return

        body: Optional[bytes] = None
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            length = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(length) if length > 0 else b""
        result = gw.route_http(
            method,
            split.path,
            split.query,
            content_type=self.headers.get("Content-Type"),
            body=body,
            headers={k: v for k, v in self.headers.items()},
        )
        if result is None:
            self._send_plain(404, b"not found")
            return
        status, content_type, resp_body = result
        self.send_response(status)
        self._response_started = True
        if content_type:
            self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(resp_body)))
        self.end_headers()
        if resp_body:
            try:
                self.wfile.write(resp_body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler convention
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch("PATCH")

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        """Silence per-request stderr logging (matches the backend server)."""


class _SandboxPreviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr: tuple[str, int], gateway: SandboxPreviewGateway) -> None:
        super().__init__(addr, _SandboxPreviewHandler)
        self.gateway = gateway


def make_preview_server(
    gateway: SandboxPreviewGateway,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """Bind the dedicated preview server and advertise its port for discovery.

    ``port=0`` (default) picks an ephemeral port — the frontend never
    hardcodes it, it reads ``preview_info``'s ``port``. The caller owns the
    server: start ``serve_forever`` on a daemon thread and ``shutdown()`` /
    ``server_close()`` it alongside the main backend server.
    """
    server = _SandboxPreviewServer((host, port), gateway)
    gateway.set_advertised_port(server.server_address[1])
    return server
