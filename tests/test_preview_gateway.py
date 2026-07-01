"""PreviewGateway registry + route() tests (single-port amendment).

The gateway no longer owns an HTTP server; the noeta main server calls
:meth:`PreviewGateway.route`. So we test the registry (mount → relative url,
unmount, limit) and ``route`` directly (static serve, sandbox escape, unknown
token, the ``/api`` proxy against a stub upstream, OPTIONS preflight, 502).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from noeta.agent.host.preview_gateway import (
    MountLimitExceeded,
    PreviewGateway,
)


def _make_app(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "index.html").write_text("<h1>live</h1>", encoding="utf-8")
    (ws / "app" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (ws / "secret.txt").write_text("nope", encoding="utf-8")  # sibling, must not leak
    return ws


# -- registry ---------------------------------------------------------------


def test_mount_returns_relative_url(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://127.0.0.1:1", task_id="T1")
    assert m.token
    # Relative path (no scheme/host/port) — resolved by the browser against the
    # noeta origin, so reachable wherever the UI is (the whole point of the fix).
    assert m.url == f"/preview/{m.token}/"
    assert gw.mount_count == 1


def test_unmount_task_drops_and_404s(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://127.0.0.1:1", task_id="T1")
    gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://127.0.0.1:1", task_id="T2")
    assert gw.unmount_task("T1") == 1
    assert gw.mount_count == 1
    resp = gw.route("GET", m.url, "")
    assert resp is not None and resp.status == 404


def test_mount_limit(tmp_path: Path) -> None:
    # One active slot per session: a re-mount from the same task evicts its
    # prior mount, so the global ceiling is only reached across DISTINCT tasks.
    ws = _make_app(tmp_path)
    gw = PreviewGateway(mount_limit=2)
    gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T1")
    gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T2")
    with pytest.raises(MountLimitExceeded):
        gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T3")


def test_remount_same_task_evicts_prior_no_limit(tmp_path: Path) -> None:
    # Re-``open_app`` from one task never trips the ceiling: each remount
    # replaces that task's prior mount instead of leaking a new slot.
    ws = _make_app(tmp_path)
    gw = PreviewGateway(mount_limit=2)
    last = None
    for _ in range(5):
        last = gw.mount(
            workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T"
        )
    assert gw.mount_count == 1
    # Only the latest token resolves; earlier ones were evicted.
    assert gw.route("GET", last.url, "").status == 200


# -- route(): static --------------------------------------------------------


def test_route_none_for_non_preview_path(tmp_path: Path) -> None:
    gw = PreviewGateway()
    assert gw.route("GET", "/chat", "") is None
    assert gw.route("GET", "/tasks/abc/file", "path=x") is None


def test_route_serves_index_and_assets(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    root = gw.route("GET", m.url, "")  # trailing-slash root → index.html
    assert root.status == 200
    assert root.content_type == "text/html; charset=utf-8"
    # HTML responses get the /api rewrite shim spliced in; the original markup is
    # preserved and the shim carries THIS mount's prefix.
    assert b"<h1>live</h1>" in root.body
    assert b"data-noeta-api-shim" in root.body
    assert m.url.encode() in root.body
    assert root.cors is False
    js = gw.route("GET", f"{m.url}app.js", "")
    assert js.status == 200
    assert js.content_type == "application/javascript; charset=utf-8"
    assert js.body == b"console.log(1)"  # non-HTML assets are NOT rewritten


def test_route_injects_api_shim_inside_head(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "index.html").write_text(
        "<!doctype html><html><head><title>t</title></head>"
        "<body><script>fetch('/api/x')</script></body></html>",
        encoding="utf-8",
    )
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    body = gw.route("GET", m.url, "").body.decode("utf-8")
    # Shim sits right after <head> so it patches fetch before the body script runs.
    head_open = body.index("<head>") + len("<head>")
    shim_at = body.index("data-noeta-api-shim")
    app_script_at = body.index("fetch('/api/x')")
    assert head_open <= shim_at < app_script_at
    # The mount prefix is baked into the shim so /api/... rewrites carry the token.
    assert m.url in body


def test_route_unknown_token_and_missing_file_404(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    assert gw.route("GET", "/preview/deadbeef/", "").status == 404
    assert gw.route("GET", f"{m.url}nope.html", "").status == 404
    assert gw.route("GET", "/preview/", "").status == 404  # no token


def test_route_sandbox_escape_404(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    # ../secret.txt must NOT escape the mounted app/ dir into the workspace
    assert gw.route("GET", f"{m.url}../secret.txt", "").status == 404


def test_route_static_rejects_non_get(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    assert gw.route("POST", f"{m.url}index.html", "", body=b"x").status == 405


# -- route(): /api proxy ----------------------------------------------------


class _StubUpstream:
    """A tiny HTTP server recording the last request it received."""

    def __init__(self) -> None:
        self.last: dict = {}
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # noqa: D401
                return

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.last = {
                    "method": method,
                    "path": self.path,
                    "body": body,
                    "content_type": self.headers.get("Content-Type"),
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                }
                payload = json.dumps({"ok": True, "echo_path": self.path}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802
                self._handle("GET")

            def do_POST(self):  # noqa: N802
                self._handle("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_route_proxy_forwards_get_with_query_and_cors(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    up = _StubUpstream()
    try:
        gw = PreviewGateway()
        m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to=up.base, task_id="T")
        resp = gw.route("GET", f"{m.url}api/users", "limit=2")
        assert resp.status == 200
        assert resp.cors is True  # null-origin iframe fetch needs it
        assert up.last["method"] == "GET"
        assert up.last["path"] == "/users?limit=2"  # /api stripped, query kept
        assert json.loads(resp.body)["echo_path"] == "/users?limit=2"
    finally:
        up.stop()


def test_route_proxy_forwards_post_body(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    up = _StubUpstream()
    try:
        gw = PreviewGateway()
        m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to=up.base, task_id="T")
        resp = gw.route(
            "POST",
            f"{m.url}api/create_requirement",
            "",
            content_type="application/json",
            body=b'{"name":"x"}',
        )
        assert resp.status == 200
        assert up.last["method"] == "POST"
        assert up.last["path"] == "/create_requirement"
        assert up.last["body"] == b'{"name":"x"}'
        assert up.last["content_type"] == "application/json"
    finally:
        up.stop()


def test_route_proxy_forwards_page_headers(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    up = _StubUpstream()
    try:
        gw = PreviewGateway()
        m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to=up.base, task_id="T")
        # A runtime-supplied auth header on the page must reach the upstream.
        gw.route(
            "GET",
            f"{m.url}api/users",
            "",
            headers={"X-Jwt-Token": "abc123", "Accept": "application/json"},
        )
        assert up.last["headers"].get("x-jwt-token") == "abc123"
        assert up.last["headers"].get("accept") == "application/json"
    finally:
        up.stop()


def test_route_proxy_drops_denylisted_headers(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    up = _StubUpstream()
    try:
        gw = PreviewGateway()
        m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to=up.base, task_id="T")
        gw.route(
            "GET",
            f"{m.url}api/users",
            "",
            headers={
                "Cookie": "secret=1",
                "Origin": "null",
                "Referer": "http://evil",
                "Accept-Encoding": "gzip",
                "Host": "spoofed",
                "X-Keep": "yes",
            },
        )
        h = up.last["headers"]
        assert "cookie" not in h
        assert "origin" not in h
        assert "referer" not in h
        # The page's "gzip" must not pass through (we don't decode it). urllib
        # then defaults to its own "identity" → upstream sends uncompressed.
        assert h.get("accept-encoding") != "gzip"
        assert h.get("host") != "spoofed"  # urllib recomputes Host from the target
        assert h.get("x-keep") == "yes"  # non-denylisted header survives
    finally:
        up.stop()


def test_route_options_preflight(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://127.0.0.1:1", task_id="T")
    resp = gw.route("OPTIONS", f"{m.url}api/anything", "")
    assert resp.status == 204
    assert resp.cors is True  # preflight must carry CORS


def test_route_proxy_upstream_down_502(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    # nothing listening on :1 → URLError → 502 (still CORS so the browser sees it)
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://127.0.0.1:1", task_id="T")
    resp = gw.route("GET", f"{m.url}api/x", "")
    assert resp.status == 502
    assert resp.cors is True
