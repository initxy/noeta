"""Sandbox preview gateway: registry refcounting + dedicated-origin reverse
proxy + WS handshake ordering.

Pins three contract groups (adapted from noeta
tests/test_sandbox_preview_gateway.py, with the keying semantics switched to
this repo's session + root refcounting):

* registry: multiple roots of one session share the mount (only the last root
  released unmounts), a container rebuild rotates the token, lazy-mount
  fallback, session deletion force-unmounts.
* dedicated origin: the preview port serves only ``/sandbox-preview/*``, no
  CORS headers, auth rides only the gateway→container leg.
* WS: unreachable upstream gets a real 502 (dial first, then 101); reachable
  gets 101.
"""
from __future__ import annotations

import http.client
import http.server
import socket
import threading

from noeta.agent.host.preview_ws import compute_accept
from noeta.agent.host.sandbox_preview import SandboxPreviewGateway, make_preview_server


def _serve(server: http.server.ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


def _free_port() -> int:
    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _auth() -> dict[str, str]:
    return {"X-AIO-API-Key": "k"}


def _no_auth() -> dict[str, str]:
    return {}


class TestRegistry:
    """Session keying + root refcounting."""

    def test_shared_container_roots_share_token(self) -> None:
        gw = SandboxPreviewGateway()
        t1 = gw.mount_root("root-1", "sess-a", "http://127.0.0.1:1", _no_auth)
        t2 = gw.mount_root("root-2", "sess-a", "http://127.0.0.1:1", _no_auth)
        assert t1 == t2
        # first root released: the container is still referenced by root-2,
        # the mount stays
        assert gw.release_root("root-1") is False
        assert gw.preview_info("sess-a") is not None
        # last root released: unmount, the token dies
        assert gw.release_root("root-2") is True
        assert gw.preview_info("sess-a") is None

    def test_container_rebuild_rotates_token(self) -> None:
        gw = SandboxPreviewGateway()
        t1 = gw.mount_root("root-1", "sess-a", "http://127.0.0.1:1", _no_auth)
        # container rebuild changes the port → new token, the old one dies
        t2 = gw.mount_root("root-2", "sess-a", "http://127.0.0.1:2", _no_auth)
        assert t1 != t2
        assert gw.route_http("GET", f"/sandbox-preview/{t1}/x", "")[0] == 404
        info = gw.preview_info("sess-a")
        assert info is not None and info["token"] == t2

    def test_lazy_mount_and_fallback_release(self) -> None:
        gw = SandboxPreviewGateway()
        # the attach-after-restart path: the discovery endpoint lazily mounts
        # (no root reference)
        token = gw.mount_session("sess-a", "http://127.0.0.1:1", _no_auth)
        assert gw.preview_info("sess-a")["token"] == token
        # idempotent: the same base_url reuses the token
        assert gw.mount_session("sess-a", "http://127.0.0.1:1", _no_auth) == token
        # on release the root is not in the mapping (never mount_root'ed);
        # fall back to tearing down by session_id
        assert gw.release_root("root-x", session_id="sess-a") is True
        assert gw.preview_info("sess-a") is None

    def test_unmount_session_force(self) -> None:
        gw = SandboxPreviewGateway()
        gw.mount_root("root-1", "sess-a", "http://127.0.0.1:1", _no_auth)
        assert gw.unmount_session("sess-a") is True
        assert gw.preview_info("sess-a") is None
        assert gw.unmount_session("sess-a") is False

    def test_preview_info_shape(self) -> None:
        gw = SandboxPreviewGateway()
        gw.set_advertised_port(12345)
        token = gw.mount_session("sess-a", "http://127.0.0.1:1", _no_auth)
        info = gw.preview_info("sess-a")
        assert info["port"] == 12345
        # noVNC's websockify WS must be folded into the token prefix via ?path=
        assert f"path=sandbox-preview/{token}/websockify" in info["panels"]["browser"]
        # terminal has no trailing slash: the page resolves the PTY WS as a
        # relative URL (see the gateway comment)
        assert info["panels"]["terminal"] == "terminal"
        assert info["panels"]["code"].endswith("/")


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Fake container endpoint: records request headers, fixed reply."""

    seen_headers: list[dict[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802
        type(self).seen_headers.append({k: v for k, v in self.headers.items()})
        body = b"hello-from-container"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # noqa: A002
        pass


class TestDedicatedPreviewServer:
    """Dedicated preview origin: only gateway traffic, no CORS, auth only on
    the upstream leg."""

    def test_proxies_and_advertises_port_without_cors(self) -> None:
        _UpstreamHandler.seen_headers = []
        upstream = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        _serve(upstream)
        gw = SandboxPreviewGateway()
        preview = make_preview_server(gw, host="127.0.0.1")
        _serve(preview)
        try:
            up_port = upstream.server_address[1]
            pv_port = preview.server_address[1]
            token = gw.mount_root(
                "root-1", "sess-a", f"http://127.0.0.1:{up_port}", _auth
            )

            info = gw.preview_info("sess-a")
            assert info is not None and info["port"] == pv_port

            conn = http.client.HTTPConnection("127.0.0.1", pv_port, timeout=5)
            conn.request("GET", f"/sandbox-preview/{token}/some/page")
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 200
            assert body == b"hello-from-container"
            # same-origin construction ⇒ no CORS headers on this origin
            assert resp.getheader("Access-Control-Allow-Origin") is None
            # auth is injected only on the gateway→container leg (urllib
            # normalizes header case; compare case-insensitively)
            seen = {
                k.lower(): v for k, v in _UpstreamHandler.seen_headers[-1].items()
            }
            assert seen.get("x-aio-api-key") == "k"
            conn.close()
        finally:
            preview.shutdown()
            preview.server_close()
            upstream.shutdown()
            upstream.server_close()

    def test_unknown_token_and_foreign_paths_404(self) -> None:
        gw = SandboxPreviewGateway()
        preview = make_preview_server(gw, host="127.0.0.1")
        _serve(preview)
        try:
            pv_port = preview.server_address[1]
            for path in ("/sandbox-preview/not-a-token/x", "/", "/api/v1/sessions"):
                conn = http.client.HTTPConnection("127.0.0.1", pv_port, timeout=5)
                conn.request("GET", path)
                resp = conn.getresponse()
                resp.read()
                assert resp.status == 404, f"{path} should 404 on the preview origin"
                conn.close()
        finally:
            preview.shutdown()
            preview.server_close()


def _ws_upgrade_request(port: int, token: str) -> bytes:
    return (
        f"GET /sandbox-preview/{token}/websockify HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    ).encode("ascii")


def _recv_headers(sock: socket.socket) -> str:
    buf = bytearray()
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    return buf.decode("latin-1")


class TestWsHandshakeOrdering:
    """101 is sent only after the upstream leg dialed through."""

    def test_unreachable_upstream_gets_502_not_101(self) -> None:
        gw = SandboxPreviewGateway()
        token = gw.mount_root(
            "root-1", "sess-a", f"http://127.0.0.1:{_free_port()}", _no_auth
        )
        preview = make_preview_server(gw, host="127.0.0.1")
        _serve(preview)
        try:
            pv_port = preview.server_address[1]
            with socket.create_connection(("127.0.0.1", pv_port), timeout=5) as s:
                s.sendall(_ws_upgrade_request(pv_port, token))
                response = _recv_headers(s)
            assert response.startswith("HTTP/1.1 502"), response.splitlines()[:1]
        finally:
            preview.shutdown()
            preview.server_close()

    def test_reachable_upstream_gets_101(self) -> None:
        # Minimal upstream WS endpoint: accept one TCP connection, read the
        # client handshake, answer one valid 101.
        upstream = socket.create_server(("127.0.0.1", 0))
        up_port = upstream.getsockname()[1]

        def upstream_accept() -> None:
            conn, _ = upstream.accept()
            with conn:
                request = _recv_headers(conn)
                key = ""
                for line in request.split("\r\n"):
                    if line.lower().startswith("sec-websocket-key:"):
                        key = line.split(":", 1)[1].strip()
                accept = compute_accept(key)
                conn.sendall(
                    (
                        "HTTP/1.1 101 Switching Protocols\r\n"
                        "Upgrade: websocket\r\n"
                        "Connection: Upgrade\r\n"
                        f"Sec-WebSocket-Accept: {accept}\r\n"
                        "\r\n"
                    ).encode("ascii")
                )

        threading.Thread(target=upstream_accept, daemon=True).start()

        gw = SandboxPreviewGateway()
        token = gw.mount_root(
            "root-1", "sess-a", f"http://127.0.0.1:{up_port}", _no_auth
        )
        preview = make_preview_server(gw, host="127.0.0.1")
        _serve(preview)
        try:
            pv_port = preview.server_address[1]
            with socket.create_connection(("127.0.0.1", pv_port), timeout=5) as s:
                s.sendall(_ws_upgrade_request(pv_port, token))
                response = _recv_headers(s)
            assert response.startswith("HTTP/1.1 101"), response.splitlines()[:1]
        finally:
            preview.shutdown()
            preview.server_close()
            upstream.close()
