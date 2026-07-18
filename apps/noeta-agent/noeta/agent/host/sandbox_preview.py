"""sandbox_preview — per-session container live-preview reverse proxy (browser / terminal / code).

Adapted from noeta ``apps/noeta-agent/noeta/agent/host/sandbox_preview_gateway.py``
(the apps layer is not distributed with the PyPI noeta-sdk). The only
differences from upstream are keying and lifecycle:

- **Keyed by app session**: in this repository containers are shared per app
  session (``resolve_container_id``), so multiple root tasks of one session
  land in the same container. Mounts are keyed by session_id and
  reference-counted over root tasks — ``on_allocate`` fires for every root
  and ``on_release`` fires at every root's terminal state, but only the
  session's last root releasing actually unmounts (mirroring the refcount
  semantics of ``LocalDockerSandboxProvider.release``).
- **Lazy mount**: after a process restart, requeued tasks go down the
  manager's ``attach`` path, which fires no ``on_allocate`` — when the
  discovery endpoint finds no mount, :meth:`mount_session` re-mounts as a
  fallback (AgentService looks the live container handle back up from the
  provider).

Everything else matches upstream:

- **registry** ``token -> {session_id, base_url, auth}``; tokens are
  unguessable (``secrets.token_urlsafe``). auth stores a policy object
  (``SandboxAuth``) whose ``connect_headers()`` is fetched fresh per upstream
  request; the secret rides only the gateway→container leg, never visible to
  the browser.
- **HTTP passthrough** ``/sandbox-preview/<token>/<sub>`` → ``http://base/<sub>``.
- **WS reverse proxy** on the same path with ``Upgrade: websocket`` →
  RFC 6455 frame pump (preview_ws).
- **Dedicated-port origin isolation**: the panel iframes need
  ``allow-same-origin`` (noVNC localStorage / code-server service worker),
  making iframe content same-origin with whatever serves it — served from
  the main port, a compromised container's JS would hold the main API origin
  (cookies, control plane). The preview server is a "blank origin": nothing
  but ``/sandbox-preview/*``, no cookies, no API.

**v1 red lines** (matching the noeta demo boundary): unguessable-token
gating only, no browser-leg auth, credentials never injected into the
browser, the container is the isolation boundary; no arbitration when the
panels and the web subagent drive the browser at the same time.
"""
from __future__ import annotations

import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from noeta.agent.host.preview_ws import (
    accept_handshake,
    connect_upstream,
    pump_bidirectional,
)

__all__ = [
    "SandboxPreviewGateway",
    "make_preview_server",
]

_PREFIX = "/sandbox-preview/"
_DEFAULT_MOUNT_LIMIT = 64
_PROXY_TIMEOUT = 30.0

#: Headers NOT forwarded upstream (hop-by-hop / urllib-recomputed /
#: browser-supplied junk from the sandboxed iframe).
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

#: ``() -> dict[str,str]`` — the fetch-fresh policy for upstream auth headers
#: (SandboxAuth.connect_headers).
AuthHeaders = Callable[[], dict[str, str]]


@dataclass
class _MountEntry:
    session_id: str
    base_url: str
    auth: AuthHeaders
    token: str
    #: Set of root task ids referencing this mount; empty for a lazy mount.
    roots: set[str] = field(default_factory=set)


class SandboxPreviewGateway:
    """Registry + HTTP passthrough + WS reverse proxy, keyed by app session with root-task refcounts."""

    def __init__(self, *, mount_limit: int = _DEFAULT_MOUNT_LIMIT) -> None:
        self._mount_limit = mount_limit
        self._lock = threading.Lock()
        self._mounts: dict[str, _MountEntry] = {}  # token -> entry
        self._by_session: dict[str, _MountEntry] = {}  # session_id -> entry
        self._session_by_root: dict[str, str] = {}  # root task id -> session_id
        self._advertised_port: Optional[int] = None

    def set_advertised_port(self, port: int) -> None:
        """Record the dedicated preview server's bound port, advertised to the frontend via the discovery endpoint."""
        self._advertised_port = port

    # -- registry ------------------------------------------------------------

    def mount_root(
        self,
        root_task_id: str,
        session_id: str,
        base_url: str,
        auth: AuthHeaders,
    ) -> str:
        """Mount at container allocate time (the lifecycle-listener path); returns the token.

        A repeat allocate for the same session (a later workflow node / a
        container rebuild): with an unchanged base_url the token is reused
        (idempotent); a changed one (rebuilt container on a new port) mints a
        new token and invalidates the old.
        """
        with self._lock:
            self._session_by_root[root_task_id] = session_id
            entry = self._by_session.get(session_id)
            if entry is not None and entry.base_url == base_url.rstrip("/"):
                entry.roots.add(root_task_id)
                entry.auth = auth
                return entry.token
            if entry is not None:
                self._mounts.pop(entry.token, None)
            return self._mount_locked(
                session_id, base_url, auth, roots={root_task_id}
            ).token

    def mount_session(
        self, session_id: str, base_url: str, auth: AuthHeaders
    ) -> str:
        """The discovery endpoint's lazy fallback: after a process restart the attach path fires no on_allocate."""
        with self._lock:
            entry = self._by_session.get(session_id)
            if entry is not None and entry.base_url == base_url.rstrip("/"):
                return entry.token
            if entry is not None:
                self._mounts.pop(entry.token, None)
            return self._mount_locked(session_id, base_url, auth, roots=set()).token

    def _mount_locked(
        self,
        session_id: str,
        base_url: str,
        auth: AuthHeaders,
        *,
        roots: set[str],
    ) -> _MountEntry:
        if (
            len(self._mounts) >= self._mount_limit
            and session_id not in self._by_session
        ):
            # Evict the oldest mount (dict iteration order = insertion order).
            for tok in list(self._mounts):
                doomed = self._mounts.pop(tok)
                self._by_session.pop(doomed.session_id, None)
                break
        entry = _MountEntry(
            session_id=session_id,
            base_url=base_url.rstrip("/"),
            auth=auth,
            token=secrets.token_urlsafe(16),
            roots=roots,
        )
        self._mounts[entry.token] = entry
        self._by_session[session_id] = entry
        return entry

    def release_root(
        self, root_task_id: str, *, session_id: Optional[str] = None
    ) -> bool:
        """Root task terminal state: decrement the refcount; only the session's last root releasing unmounts.

        ``session_id`` is the caller's fallback (a root on the attach path
        never went through mount_root, so ``_session_by_root`` misses);
        aligned with the provider's refcount semantics — after a restart any
        root's terminal state tears the container down, and the preview mount
        goes with it. Returns whether it actually unmounted.
        """
        with self._lock:
            sid = self._session_by_root.pop(root_task_id, None) or session_id
            if sid is None:
                return False
            entry = self._by_session.get(sid)
            if entry is None:
                return False
            entry.roots.discard(root_task_id)
            if entry.roots:
                return False
            self._by_session.pop(sid, None)
            self._mounts.pop(entry.token, None)
            return True

    def unmount_session(self, session_id: str) -> bool:
        """Force-unmount by session (the counterpart of force_release on session deletion)."""
        with self._lock:
            entry = self._by_session.pop(session_id, None)
            if entry is None:
                return False
            self._mounts.pop(entry.token, None)
            for root in [
                r for r, s in self._session_by_root.items() if s == session_id
            ]:
                self._session_by_root.pop(root, None)
            return True

    @property
    def mount_count(self) -> int:
        with self._lock:
            return len(self._mounts)

    def _lookup(self, token: str) -> Optional[_MountEntry]:
        with self._lock:
            return self._mounts.get(token)

    # -- discovery -------------------------------------------------------------

    def preview_info(self, session_id: str) -> Optional[dict[str, Any]]:
        """The discovery payload ``{token, port, panels}``; None when this session has no mount.

        The panel sub-paths were pinned by noeta against the live AIO
        container:

        - browser: the noVNC page + ``?path=`` steering the websockify WS
          inside the token prefix (the container serves the WS at root
          ``/websockify``; noVNC's default absolute path would escape the
          prefix).
        - terminal: **no trailing slash** — the page resolves its PTY WS
          relative to the URL, and only ``.../terminal`` lands on
          ``<prefix>/v1/shell/ws`` (the container serves it at root
          ``/v1/shell/ws``).
        - code: the code-server page + its internal WS.
        """
        with self._lock:
            entry = self._by_session.get(session_id)
            if entry is None:
                return None
            token = entry.token
        return {
            "token": token,
            "port": self._advertised_port,
            "panels": {
                "browser": (
                    "vnc/index.html?autoconnect=true&resize=scale"
                    f"&path=sandbox-preview/{token}/websockify"
                ),
                "terminal": "terminal",
                "code": "code-server/",
            },
        }

    # -- path helpers ----------------------------------------------------------

    @staticmethod
    def is_preview_path(path: str) -> bool:
        return path.startswith(_PREFIX)

    @staticmethod
    def parse_preview_path(path: str) -> Optional[tuple[str, str]]:
        """``/sandbox-preview/<token>/<sub>`` → ``(token, sub)``; None when invalid."""
        if not path.startswith(_PREFIX):
            return None
        rest = path[len(_PREFIX):]
        token, sep, sub = rest.partition("/")
        if not token:
            return None
        return (token, sub if sep else "")

    # -- HTTP passthrough --------------------------------------------------------

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
        """Pass one HTTP request through to the container; None for non-preview paths.

        No CORS headers: the preview has its own origin, so every fetch a
        panel page makes is a same-origin request from the browser's point of
        view.
        """
        parsed = self.parse_preview_path(path)
        if parsed is None:
            return None
        token, sub = parsed

        entry = self._lookup(token)
        if entry is None:
            return (404, "text/plain; charset=utf-8", b"unknown preview token")

        upstream_url = entry.base_url
        if sub:
            upstream_url += "/" + sub
        if query:
            upstream_url += "?" + query

        fwd = {k: v for k, v in (headers or {}).items()}
        lowered = {k.lower() for k in fwd}
        if content_type is not None and "content-type" not in lowered:
            fwd["content-type"] = content_type
        for hdr_name, hdr_val in entry.auth().items():
            fwd[hdr_name] = hdr_val

        req = urllib.request.Request(upstream_url, data=body, method=method)
        for name, value in fwd.items():
            if name.lower() in _DROP_REQUEST_HEADERS:
                continue
            req.add_header(name, value)

        try:
            with urllib.request.urlopen(req, timeout=_PROXY_TIMEOUT) as upstream:  # noqa: S310 — base_url comes from the local provider
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

    # -- WS reverse proxy ---------------------------------------------------------

    def try_handle_ws(self, handler: Any, path: str) -> bool:
        """WS upgrade + bidirectional pump for a preview path; returns True when handled (502 included).

        After a True return the caller must **not** write any further HTTP
        response (the connection is upgraded / already answered). The
        upstream leg dials before the 101 goes out: an unreachable container
        must surface to the client as a real HTTP error, not a 101 followed
        by an abrupt close (noVNC / xterm.js get no close frame from that and
        cannot tell what happened).
        """
        parsed = self.parse_preview_path(path)
        if parsed is None:
            return False
        token, sub = parsed

        entry = self._lookup(token)
        if entry is None:
            return False

        headers = handler.headers
        if "websocket" not in headers.get("Upgrade", "").lower():
            return False
        if "upgrade" not in headers.get("Connection", "").lower():
            return False
        if not headers.get("Sec-WebSocket-Key", ""):
            return False

        requested_proto = headers.get("Sec-WebSocket-Protocol", "")
        subprotocols = (
            [p.strip() for p in requested_proto.split(",") if p.strip()]
            if requested_proto
            else None
        )
        # accept_handshake picks the first protocol in the client's requested
        # list that we support — we support them all, so precompute the same
        # choice for the upstream dial.
        negotiated = subprotocols[0] if subprotocols else None

        upstream_sock = connect_upstream(
            entry.base_url,
            sub,
            auth_headers=entry.auth(),
            subprotocol=negotiated,
        )
        if upstream_sock is None:
            self._send_plain_error(handler, 502, b"sandbox upstream unreachable")
            return True

        accepted = accept_handshake(handler, subprotocols=subprotocols)
        if accepted is None:
            try:
                upstream_sock.close()
            except OSError:
                pass
            handler._response_started = True
            return True

        handler._response_started = True

        browser_sock = handler.connection
        try:
            browser_sock.settimeout(None)
        except OSError:
            pass

        # The pump runs synchronously on this handler thread:
        # ThreadingHTTPServer gives each connection its own thread, and
        # blocking here is the correct ownership model (the thread is held as
        # long as the socket lives).
        pump_bidirectional(browser_sock, upstream_sock)
        return True

    @staticmethod
    def _send_plain_error(handler: Any, status: int, body: bytes) -> None:
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


# ---------------------------------------------------------------------------
# Dedicated preview server (origin isolation)
# ---------------------------------------------------------------------------

class _SandboxPreviewHandler(BaseHTTPRequestHandler):
    """Slim handler for the dedicated preview port: gateway traffic only.

    There is deliberately no router, no SPA, no API of any kind on this
    origin — the "blankness" is itself the security property: the panel
    iframes run with ``allow-same-origin`` against this origin, so a
    compromised container's JS gains nothing here beyond its own preview
    surface.
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

        # WS upgrade → hijack the connection into the pump. Pass the raw
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
        """Silence per-request stderr logging."""


class _SandboxPreviewServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self, addr: tuple[str, int], gateway: SandboxPreviewGateway
    ) -> None:
        super().__init__(addr, _SandboxPreviewHandler)
        self.gateway = gateway


def make_preview_server(
    gateway: SandboxPreviewGateway,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> ThreadingHTTPServer:
    """Bind the dedicated preview server and record its port in the gateway for the discovery endpoint.

    ``port=0`` (default) picks an ephemeral port — the frontend never
    hardcodes it, it reads the discovery payload's ``port``. The caller owns
    the server: ``serve_forever`` on a daemon thread, ``shutdown()`` /
    ``server_close()`` alongside the main backend.
    """
    server = _SandboxPreviewServer((host, port), gateway)
    gateway.set_advertised_port(server.server_address[1])
    return server
