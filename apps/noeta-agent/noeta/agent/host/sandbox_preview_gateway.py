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
* **HTTP 透传** — ``/sandbox-preview/<token>/<sub>`` → ``http://base/<sub>``
  with auth injected upstream. Used for noVNC/code-server static pages.
* **WS 反代** — ``/sandbox-preview/<token>/<sub>`` with
  ``Upgrade: websocket`` → RFC 6455 frame pump to ``ws://base/<sub>``
  (auth in upstream handshake only).

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
from typing import Any, Optional

from noeta.agent.host.preview_ws import (
    accept_handshake,
    connect_upstream,
    pump_bidirectional,
)

__all__ = [
    "SandboxPreviewGateway",
    "SandboxPreviewMount",
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
    """Registry + HTTP透传 + WS反代 for per-session sandbox live preview.

    Lifecycle:
    * ``mount_root(root_task_id, base_url, auth_headers)`` — called when a
      container is allocated (W3 lifecycle wiring). Returns a
      :class:`SandboxPreviewMount` with an unguessable token.
    * ``unmount_root(root_task_id)`` — called on container release / task
      terminal. Returns count removed.

    Routing (called by backend ``app.py``):
    * ``is_preview_path(path)`` — quick prefix check.
    * ``route_http(method, path, query, *, content_type, body, headers)``
      — HTTP透传 (noVNC / code-server static assets). Returns
      ``(status, content_type, body_bytes, cors)`` or ``None`` if not a
      preview path.
    * ``try_handle_ws(handler, path)`` — if the request is a WS upgrade to
      a valid preview token, performs the accept handshake and starts the
      bidirectional pump. Returns ``True`` if handled (the connection is
      now a raw WS pipe — caller must NOT send any further HTTP responses).
    """

    def __init__(self, *, mount_limit: int = _DEFAULT_MOUNT_LIMIT) -> None:
        self._mount_limit = mount_limit
        self._lock = threading.Lock()
        self._mounts: dict[str, _MountEntry] = {}  # token -> entry
        self._tokens_by_root: dict[str, str] = {}  # root_task_id -> token

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

    # -- HTTP 透传 -----------------------------------------------------------

    def route_http(
        self,
        method: str,
        path: str,
        query: str,
        *,
        content_type: Optional[str] = None,
        body: Optional[bytes] = None,
        headers: Optional[dict[str, str]] = None,
    ) -> Optional[tuple[int, str, bytes, bool]]:
        """透传 an HTTP request to the sandbox container.

        Returns ``(status, content_type, body, cors)`` or ``None`` if not
        a preview path / unknown token. ``cors=True`` adds
        ``Access-Control-Allow-Origin: *`` (sandboxed iframes need it).
        """
        parsed = self.parse_preview_path(path)
        if parsed is None:
            return None
        token, sub = parsed

        entry = self._lookup(token)
        if entry is None:
            return (404, "text/plain; charset=utf-8", b"unknown preview token", False)

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
                True,
            )
        # Sandboxed iframes need CORS for fetch/XHR to same-origin proxy.
        return (status, ctype, resp_body, True)

    # -- WS 反代 -------------------------------------------------------------

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

        # Check if this is actually a WS upgrade request.
        upgrade = handler.headers.get("Upgrade", "")
        if "websocket" not in upgrade.lower():
            return False

        # Perform the server-side accept handshake.
        requested_proto = handler.headers.get("Sec-WebSocket-Protocol", "")
        subprotocols = [p.strip() for p in requested_proto.split(",") if p.strip()] if requested_proto else None

        negotiated = accept_handshake(handler, subprotocols=subprotocols)
        if negotiated is None:
            return False  # handshake failed

        # Mark response as started so the error handler doesn't double-write.
        handler._response_started = True

        # Connect to upstream container WS.
        upstream_sock = connect_upstream(
            entry.base_url,
            sub,
            auth_headers=entry.auth_headers,
            subprotocol=negotiated if negotiated else None,
        )
        if upstream_sock is None:
            # Upstream refused — close the browser leg.
            try:
                handler.connection.close()
            except OSError:
                pass
            return True

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

    # -- discovery (GET /tasks/{id}/preview) ---------------------------------

    def preview_info(self, root_task_id: str) -> Optional[dict[str, Any]]:
        """Return the preview discovery payload for a root task.

        Shape: ``{"token": str, "panels": {"browser": str, "terminal": str, "code": str}}``
        or ``None`` if no sandbox is mounted for this root.

        The panel values are ``<sub>`` paths the frontend appends to
        ``/sandbox-preview/<token>/`` to open each surface.
        """
        token = self.token_for_root(root_task_id)
        if token is None:
            return None
        return {
            "token": token,
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
