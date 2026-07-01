"""T7 acceptance — the new backend serves the bundled ``apps/web`` SPA.

The legacy runner served ``/chat`` / ``/trace`` + the hashed ``/assets/`` bundle;
T8 deletes that path, so the new backend owns the same concern via
:mod:`noeta.agent.backend.static_assets`. Without this, ``python -m noeta.agent``
(now defaulting to the new backend) advertises ``/chat`` but 404s it — the SPA
can't load. Covered here:

* ``resolve_static`` — the pure URL → asset mapping incl. traversal rejection.
* end-to-end serving through ``serve_backend`` with an injected asset root.
* the no-build path — SPA routes 404 cleanly when no bundle is present.
"""

from __future__ import annotations

import http.client
from pathlib import Path
from typing import Optional

from noeta.agent.backend import BackendConfig, EngineRoom, serve_backend
from noeta.agent.backend.app import Router, make_http_server
from noeta.agent.backend.static_assets import (
    WebAssetRoot,
    locate_web_assets,
    resolve_static,
)
from noeta.sdk import Options
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider


def _room(workspace: Path) -> EngineRoom:
    return EngineRoom(
        Options(
            system_prompt="finish",
            name="main",
            allowed_tools=(),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="end_turn",
                    content=[TextBlock(text="ok")],
                    usage=Usage(uncached=1, output=1),
                )
            ]
        ),
        workspace_dir=workspace,
    )


def _build_assets(tmp_path: Path) -> WebAssetRoot:
    """A fabricated SPA bundle (no real Vite build needed)."""
    dist = tmp_path / "static" / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "chat.html").write_text("<!doctype html><title>chat</title>")
    (dist / "trace.html").write_text("<!doctype html><title>trace</title>")
    (dist / "assets" / "app.js").write_text("console.log('app')")
    return WebAssetRoot(source=tmp_path / "static", dist=dist)


def _get(
    host: str, port: int, path: str, follow: bool = False
) -> tuple[int, bytes, str, Optional[str]]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = resp.read()
    ctype = resp.getheader("Content-Type", "")
    location = resp.getheader("Location")
    conn.close()
    return resp.status, data, ctype, location


# ---------------------------------------------------------------------------
# pure mapping
# ---------------------------------------------------------------------------


def test_resolve_static_maps_html_and_assets() -> None:
    assert resolve_static("/chat") == ("chat.html", "text/html; charset=utf-8")
    assert resolve_static("/chat.html")[0] == "chat.html"
    assert resolve_static("/trace") == ("trace.html", "text/html; charset=utf-8")
    js = resolve_static("/assets/app-abc123.js")
    assert js == ("assets/app-abc123.js", "application/javascript; charset=utf-8")
    # Non-SPA paths fall through to API routing.
    assert resolve_static("/tasks") is None
    assert resolve_static("/capabilities") is None


def test_resolve_static_rejects_traversal() -> None:
    assert resolve_static("/assets/../secret") is None
    assert resolve_static("/assets/..%2f") is not None  # not decoded → literal name
    assert resolve_static("/assets/") is None


def test_web_asset_root_prefers_dist(tmp_path: Path) -> None:
    root = _build_assets(tmp_path)
    assert root.joinpath("chat.html") == root.dist / "chat.html"
    # src/ always resolves from source (Vite serves dev modules from there).
    assert root.joinpath("src/main.jsx") == root.source / "src" / "main.jsx"


# ---------------------------------------------------------------------------
# end-to-end serving
# ---------------------------------------------------------------------------


def test_serves_spa_routes_end_to_end(tmp_path: Path) -> None:
    assets = _build_assets(tmp_path)
    server, _url, shutdown = serve_backend(
        BackendConfig(host="127.0.0.1", port=0, workspace_dir=tmp_path),
        engine_room=_room(tmp_path),
        web_assets=assets,
    )
    host, port = server.server_address[:2]
    try:
        # Root redirects to the chat surface.
        status, _, _, loc = _get(host, port, "/")
        assert status == 302 and loc == "/chat"

        # HTML routes serve the bundle.
        status, body, ctype, _ = _get(host, port, "/chat")
        assert status == 200 and ctype.startswith("text/html")
        assert b"<title>chat</title>" in body

        status, body, _, _ = _get(host, port, "/trace")
        assert status == 200 and b"<title>trace</title>" in body

        # Hashed asset serves with a JS content type.
        status, body, ctype, _ = _get(host, port, "/assets/app.js")
        assert status == 200 and ctype.startswith("application/javascript")
        assert b"console.log" in body

        # Traversal + unknown asset 404 (don't escape the bundle).
        status, _, _, _ = _get(host, port, "/assets/nope.js")
        assert status == 404
    finally:
        shutdown()


def test_no_build_404s_spa_routes(tmp_path: Path) -> None:
    """With no bundle injected the SPA routes 404 cleanly (handled before the
    API router, so they never resolve as unknown API paths)."""
    server = make_http_server(
        _room(tmp_path),
        host="127.0.0.1",
        port=0,
        router=Router(),
        web_assets=None,
    )
    import threading

    threading.Thread(target=server.serve_forever, daemon=True).start()
    host, port = server.server_address[:2]
    try:
        status, body, ctype, _ = _get(host, port, "/chat")
        assert status == 404 and "json" in ctype
        assert b"frontend" in body
        # Root still redirects regardless of bundle presence.
        status, _, _, loc = _get(host, port, "/")
        assert status == 302 and loc == "/chat"
    finally:
        server.shutdown()
        server.server_close()


def test_locate_web_assets_returns_root_or_none() -> None:
    """Smoke: the locator returns a usable root in the dev checkout (where
    apps/web/dist is built) or ``None`` (no build) — never raises."""
    root = locate_web_assets()
    assert root is None or (root.dist / "chat.html").is_file()
