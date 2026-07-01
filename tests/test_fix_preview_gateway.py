"""Regression tests for PreviewGateway review fixes.

Covers three findings:

* **percent-decode static subpaths** — an asset whose name has a space / '+' /
  non-ASCII char must be reachable (the browser percent-encodes the request);
  decoding must NOT weaken the sandbox (``%2e%2e`` traversal still 404s).
* **per-task mount eviction** — re-``mount`` from the same task replaces its
  prior mount(s) so they don't leak and exhaust the global limit.
* **OPTIONS /api preflight before token-404** — a preflight against an
  unknown/expired token returns a clean CORS 204, not a bare 404.
"""

from __future__ import annotations

from pathlib import Path

from noeta.agent.host.preview_gateway import PreviewGateway


def _make_app(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "app").mkdir(parents=True)
    (ws / "app" / "index.html").write_text("<h1>live</h1>", encoding="utf-8")
    (ws / "secret.txt").write_text("nope", encoding="utf-8")
    return ws


# -- percent-decode ---------------------------------------------------------


def test_static_asset_with_space_is_percent_decoded(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    (ws / "app" / "my photo.png").write_bytes(b"PNGDATA")
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    # Browser encodes the space; the gateway must decode before the disk lookup.
    resp = gw.route("GET", f"{m.url}my%20photo.png", "")
    assert resp.status == 200
    assert resp.content_type == "image/png"
    assert resp.body == b"PNGDATA"


def test_static_asset_with_plus_and_unicode_is_decoded(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    (ws / "app" / "ré+sumé.txt").write_text("ok", encoding="utf-8")
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    resp = gw.route("GET", f"{m.url}r%C3%A9%2Bsum%C3%A9.txt", "")
    assert resp.status == 200
    assert resp.body == b"ok"


def test_percent_encoded_traversal_still_404s(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    # Decoding must happen BEFORE resolve, so %2e%2e cannot slip past the sandbox.
    resp = gw.route("GET", f"{m.url}%2e%2e/secret.txt", "")
    assert resp.status == 404


# -- per-task mount eviction ------------------------------------------------


def test_remount_same_task_evicts_prior_mount(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    first = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    second = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    # One active slot per session: the old token is gone, only the new one lives.
    assert first.token != second.token
    assert gw.mount_count == 1
    assert gw.route("GET", first.url, "").status == 404
    assert gw.route("GET", second.url, "").status == 200


def test_remount_does_not_evict_other_tasks(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    other = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T2")
    gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T1")
    gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T1")
    # T1 re-mount evicts only T1's prior slot; T2's mount is untouched.
    assert gw.mount_count == 2
    assert gw.route("GET", other.url, "").status == 200


def test_repeated_remount_never_exhausts_limit(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway(mount_limit=2)
    last = None
    for _ in range(10):
        last = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    # One long task re-opening repeatedly stays at a single slot — no MountLimit.
    assert gw.mount_count == 1
    assert gw.route("GET", last.url, "").status == 200


# -- OPTIONS preflight before token-404 -------------------------------------


def test_options_preflight_unknown_token_returns_cors_204(tmp_path: Path) -> None:
    gw = PreviewGateway()
    # No mount with this token (e.g. just-expired); the preflight must still
    # carry CORS so the sandboxed page sees a clean failure, not an opaque error.
    resp = gw.route("OPTIONS", "/preview/deadbeef/api/users", "")
    assert resp.status == 204
    assert resp.cors is True


def test_options_preflight_known_token_still_204(tmp_path: Path) -> None:
    ws = _make_app(tmp_path)
    gw = PreviewGateway()
    m = gw.mount(workspace_dir=ws, app_rel="app", proxy_to="http://x", task_id="T")
    resp = gw.route("OPTIONS", f"{m.url}api/users", "")
    assert resp.status == 204
    assert resp.cors is True


def test_non_options_unknown_token_still_404(tmp_path: Path) -> None:
    gw = PreviewGateway()
    # A real GET to /api against an unknown token is still a 404 (not a preflight).
    resp = gw.route("GET", "/preview/deadbeef/api/users", "")
    assert resp.status == 404
