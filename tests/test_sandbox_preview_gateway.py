"""Sandbox preview gateway — dedicated-origin server + WS handshake ordering.

Pins the security/robustness contract of the live-preview transport:

* The preview is served on its OWN port (origin isolation): the panels'
  iframes run ``allow-same-origin``, so container-controlled content must
  land on an origin holding no noeta state. The dedicated server serves
  ``/sandbox-preview/<token>/...`` and nothing else, and emits NO CORS
  headers (every panel fetch is same-origin on that port).
* ``preview_info`` advertises the bound port for frontend discovery.
* A WS upgrade to an unreachable container answers a real HTTP 502 —
  never a 101 followed by an abrupt close (the client can't interpret
  that); a reachable container still gets the 101 + pump.
"""

from __future__ import annotations

import http.client
import http.server
import socket
import threading

from noeta.agent.host.preview_ws import compute_accept
from noeta.agent.host.sandbox_preview_gateway import (
    SandboxPreviewGateway,
    make_preview_server,
)


def _serve(server: http.server.ThreadingHTTPServer) -> threading.Thread:
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return t


def _free_port() -> int:
    """A port with no listener (bound momentarily, then released)."""
    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _UpstreamHandler(http.server.BaseHTTPRequestHandler):
    """Fake container endpoint: records headers, answers a fixed body."""

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
    """make_preview_server — the blank preview origin."""

    def test_proxies_and_advertises_port_without_cors(self) -> None:
        _UpstreamHandler.seen_headers = []
        upstream = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), _UpstreamHandler
        )
        _serve(upstream)
        gw = SandboxPreviewGateway()
        preview = make_preview_server(gw, host="127.0.0.1")
        _serve(preview)
        try:
            up_port = upstream.server_address[1]
            pv_port = preview.server_address[1]
            mount = gw.mount_root(
                "root-1", f"http://127.0.0.1:{up_port}", {"X-AIO-API-Key": "k"}
            )

            # Discovery advertises the dedicated port.
            info = gw.preview_info("root-1")
            assert info is not None and info["port"] == pv_port

            conn = http.client.HTTPConnection("127.0.0.1", pv_port, timeout=5)
            conn.request("GET", f"/sandbox-preview/{mount.token}/some/page")
            resp = conn.getresponse()
            body = resp.read()
            assert resp.status == 200
            assert body == b"hello-from-container"
            # Same-origin by construction ⇒ no CORS headers on this origin.
            assert resp.getheader("Access-Control-Allow-Origin") is None
            # Auth was injected on the noeta→container leg only (urllib
            # normalizes header casing, so compare case-insensitively).
            seen = {k.lower(): v for k, v in _UpstreamHandler.seen_headers[-1].items()}
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
            for path in ("/sandbox-preview/not-a-token/x", "/", "/tasks"):
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
    """101 is sent only AFTER the upstream leg is connected."""

    def test_unreachable_upstream_gets_502_not_101(self) -> None:
        gw = SandboxPreviewGateway()
        mount = gw.mount_root("root-1", f"http://127.0.0.1:{_free_port()}", {})
        preview = make_preview_server(gw, host="127.0.0.1")
        _serve(preview)
        try:
            pv_port = preview.server_address[1]
            with socket.create_connection(("127.0.0.1", pv_port), timeout=5) as s:
                s.sendall(_ws_upgrade_request(pv_port, mount.token))
                response = _recv_headers(s)
            assert response.startswith("HTTP/1.1 502"), response.splitlines()[:1]
            assert "101" not in response.split("\r\n")[0]
        finally:
            preview.shutdown()
            preview.server_close()

    def test_reachable_upstream_gets_101(self) -> None:
        # Minimal upstream WS endpoint: accept one TCP connection, read the
        # client handshake, answer a valid 101.
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

        t = threading.Thread(target=upstream_accept, daemon=True)
        t.start()

        gw = SandboxPreviewGateway()
        mount = gw.mount_root("root-1", f"http://127.0.0.1:{up_port}", {})
        preview = make_preview_server(gw, host="127.0.0.1")
        _serve(preview)
        try:
            pv_port = preview.server_address[1]
            with socket.create_connection(("127.0.0.1", pv_port), timeout=5) as s:
                s.sendall(_ws_upgrade_request(pv_port, mount.token))
                response = _recv_headers(s)
            assert response.startswith("HTTP/1.1 101"), response.splitlines()[:1]
        finally:
            preview.shutdown()
            preview.server_close()
            upstream.close()
