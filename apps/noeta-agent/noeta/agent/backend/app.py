"""app — the new backend's HTTP/SSE application + routing root.

HTTP lives only in the app
layer (the browser ↔ Python-backend bridge). This module owns the
``ThreadingHTTPServer`` and the route table; it translates HTTP into
:class:`~noeta.agent.backend.engine_room.EngineRoom` calls and projects the
canonical EventEnvelope stream back out.

T4 lands the skeleton: the server, a ``GET /health`` liveness probe, and the
routing seam (:class:`Router`). The core task protocol — the SSE multiplexed
envelope stream + the command endpoints (T5) — and the ancillary resource services
(T6) register onto this same router. Until then unknown routes return 404.
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional

from noeta.sdk import CodedError

from noeta.agent.backend.engine_room import EngineRoom


_log = logging.getLogger(__name__)

#: A route handler: ``(handler, params) -> None``. ``handler`` is the live
#: request handler (it owns ``send_response`` / ``wfile`` / the engine room);
#: ``params`` are the matched path params. Registered by T5/T6 onto the Router.
RouteHandler = Callable[["BackendHandler", dict[str, str]], None]


class _Pattern:
    """A ``/``-segmented path pattern with ``{name}`` params.

    ``/tasks/{id}/messages`` matches ``/tasks/abc/messages`` capturing
    ``{"id": "abc"}``. Segment counts must match exactly; an empty captured
    segment is a non-match.
    """

    __slots__ = ("segments",)

    def __init__(self, pattern: str) -> None:
        self.segments = pattern.strip("/").split("/")

    def match(self, path: str) -> Optional[dict[str, str]]:
        parts = path.strip("/").split("/")
        if len(parts) != len(self.segments):
            return None
        params: dict[str, str] = {}
        for seg, part in zip(self.segments, parts):
            if seg.startswith("{") and seg.endswith("}"):
                if not part:
                    return None
                params[seg[1:-1]] = part
            elif seg != part:
                return None
        return params


class Router:
    """An ordered ``(method, pattern) -> handler`` table with ``{param}`` paths.

    Routes are matched top-to-bottom (registration order); the first match
    wins, so register more specific patterns before broader ones.
    """

    def __init__(self) -> None:
        self._routes: list[tuple[str, _Pattern, RouteHandler]] = []

    def add(self, method: str, path: str, handler: RouteHandler) -> None:
        self._routes.append((method.upper(), _Pattern(path), handler))

    def resolve(
        self, method: str, path: str
    ) -> Optional[tuple[RouteHandler, dict[str, str]]]:
        method = method.upper()
        for route_method, pattern, handler in self._routes:
            if route_method != method:
                continue
            params = pattern.match(path)
            if params is not None:
                return handler, params
        return None


class BackendHandler(BaseHTTPRequestHandler):
    """stdlib request handler dispatching through the shared :class:`Router`.

    The server instance carries the :class:`EngineRoom` and :class:`Router`
    (set in :func:`make_http_server`); each request looks them up off
    ``self.server``.
    """

    server_version = "noeta-agent-backend/0.1"

    # -- helpers used by route handlers (T5/T6) ----------------------------

    @property
    def engine_room(self) -> EngineRoom:
        return self.server.engine_room  # type: ignore[attr-defined]

    @property
    def app_gateway(self) -> Optional[Any]:
        """The HTML-app preview gateway (T6), or ``None`` if not configured."""
        return getattr(self.server, "app_gateway", None)

    @property
    def sandbox_preview_gateway(self) -> Optional[Any]:
        """The per-session sandbox live-preview gateway, or ``None`` if absent.

        Backs ONLY the ``GET /tasks/{id}/preview`` discovery route on this
        server: the preview traffic itself is served on the gateway's own
        dedicated port (origin isolation — see
        ``noeta.agent.host.sandbox_preview_gateway``), never the main port.
        """
        return getattr(self.server, "sandbox_preview_gateway", None)

    @property
    def mcp_registry(self) -> Optional[Any]:
        """The MCP connector config registry (T6), or ``None`` if absent."""
        return getattr(self.server, "mcp_registry", None)

    @property
    def workspace_registry(self) -> Optional[Any]:
        """The workspace (project) config registry, or ``None`` if absent.

        Backs the
        ``/workspaces`` CRUD + the ``/capabilities`` workspace list.
        """
        return getattr(self.server, "workspace_registry", None)

    @property
    def web_assets(self) -> Optional[Any]:
        """The bundled SPA assets root (T7), or ``None`` if no build present."""
        return getattr(self.server, "web_assets", None)

    #: Set once any response writer has emitted its status line + headers, so
    #: ``_handle_handler_error`` never writes a SECOND response into a stream
    #: (e.g. an SSE handler that raises mid-body) and corrupts the wire.
    _response_started = False

    def send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._response_started = True
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_bytes(
        self, body: bytes, content_type: str, status: int = 200
    ) -> None:
        self.send_response(status)
        self._response_started = True
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_raw_body(self) -> bytes:
        """Read the raw request body bytes (``b""`` when empty)."""
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def read_json_body(self) -> dict[str, Any]:
        """Parse the request body as a JSON object (``{}`` when empty)."""
        raw = self.read_raw_body()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}

    def query_params(self) -> dict[str, str]:
        from urllib.parse import parse_qs, urlsplit

        q = urlsplit(self.path).query
        return {k: v[0] for k, v in parse_qs(q).items()}

    def stream_sse(self, frames: Any) -> None:
        """Write an iterable of pre-formatted SSE byte frames until the client
        disconnects (a write error breaks the loop and the generator's
        ``finally`` unsubscribes)."""
        self.send_response(200)
        self._response_started = True
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            for chunk in frames:
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            close = getattr(frames, "close", None)
            if callable(close):
                close()

    # -- preview gateway (T6, prefix-routed) -------------------------------

    def send_preview(self, resp: Any) -> None:
        """Send a :class:`PreviewResponse` (the ``/api`` proxy carries CORS).

        The preview iframe runs in a sandboxed null origin (docs/.../06-...md),
        so its ``/api`` fetch is cross-origin → answer ``cors`` responses with
        permissive headers, echoing the preflight's requested headers so
        app-specific ones pass.
        """
        self.send_response(resp.status)
        self._response_started = True
        if resp.content_type:
            self.send_header("Content-Type", resp.content_type)
        if resp.cors:
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header(
                "Access-Control-Allow-Methods",
                "GET, POST, PUT, DELETE, PATCH, OPTIONS",
            )
            requested = self.headers.get("Access-Control-Request-Headers")
            self.send_header("Access-Control-Allow-Headers", requested or "*")
            self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", str(len(resp.body)))
        self.end_headers()
        if resp.body:
            self.wfile.write(resp.body)

    def _maybe_preview(self, method: str, path: str) -> bool:
        """Route a ``/preview/<token>/...`` request into the gateway (T6).

        Single-port design: the preview is served from THIS server (no second
        port), so it is reachable wherever the noeta UI is. ``True`` if handled;
        ``None`` gateway / non-preview path ⇒ ``False`` (fall through to routing).
        """
        gw = self.app_gateway
        if gw is None or not path.startswith("/preview/"):
            return False
        from urllib.parse import urlsplit

        split = urlsplit(self.path)
        body = (
            self.read_raw_body()
            if method in ("POST", "PUT", "PATCH", "DELETE")
            else None
        )
        resp = gw.route(
            method,
            split.path,
            split.query,
            body=body,
            headers={k: v for k, v in self.headers.items()},
        )
        if resp is None:
            return False
        self.send_preview(resp)
        return True

    # -- static SPA (T7, prefix-routed) ------------------------------------

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _maybe_static(self, method: str, path: str) -> bool:
        """Serve the bundled SPA / static assets (T7); ``True`` if handled.

        GET-only head-of-pipeline special case (matches the legacy host): ``/``
        redirects to the chat surface, the explicit HTML routes + the validated
        ``/assets/`` prefix serve from the injected :class:`WebAssetRoot`. With
        no build injected, SPA routes 404 (handled here so they never reach the
        API router); any other path falls through (``False``).
        """
        if method != "GET":
            return False
        if path == "/":
            self._redirect("/chat")
            return True
        from noeta.agent.backend.static_assets import read_asset, resolve_static

        match = resolve_static(path)
        if match is None:
            return False
        filename, content_type = match
        assets = self.web_assets
        if assets is None:
            self.send_json({"error": "no frontend bundle"}, status=404)
            return True
        try:
            body = read_asset(assets, filename)
        except (OSError, FileNotFoundError, ModuleNotFoundError):
            self.send_json({"error": "not found", "path": path}, status=404)
            return True
        self.send_bytes(body, content_type)
        return True

    # -- dispatch ----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0]
        if self._maybe_preview(method, path):
            return
        if self._maybe_static(method, path):
            return
        if method == "GET" and path == "/health":
            self.send_json({"status": "ok", "backend": "new"})
            return
        router: Router = self.server.router  # type: ignore[attr-defined]
        match = router.resolve(method, path)
        if match is None:
            self.send_json({"error": "not found", "path": path}, status=404)
            return
        handler, params = match
        try:
            handler(self, params)
        except Exception as exc:  # noqa: BLE001 — see below
            self._handle_handler_error(exc)

    #: Stable engine error ``code`` → HTTP status. The engine verbs raise
    #: ``noeta.sdk.CodedError`` subclasses carrying a byte-stable ``code``; the
    #: backend switches on that STRUCTURALLY (``isinstance`` + ``exc.code``)
    #: rather than the class-name string / ``"already terminal"`` message
    #: substring it matched before — HTTP status is an app concern, so the map
    #: lives here keyed by the public code. Any unlisted error is an
    #: unexpected 500.
    _ERROR_CODE_STATUS = {
        "model_selector_rejected": 400,
        "provider_selector_rejected": 400,
        "not_resumable": 409,
        "unsupported_subtask_suspend": 409,
        "task_already_terminal": 409,
    }

    def _handle_handler_error(self, exc: Exception) -> None:
        """Turn a handler raise into an HTTP response instead of a dropped
        socket. ``BaseHTTPRequestHandler`` does NOT convert an exception into a
        response — an unhandled raise just closes the connection after a stderr
        traceback, so a typed engine error (bad model, non-resumable task, …)
        reached the client as a reset connection rather than a 4xx."""
        if self._response_started:
            # A streaming handler (or any writer) already committed a status
            # line + headers; a second ``send_json`` here would write a bogus
            # second response into the same socket. Log and bail — the partial
            # response is the client's to reconcile (the stream just ends).
            _log.exception("backend handler error after response started")
            return
        status = (
            self._ERROR_CODE_STATUS.get(exc.code)
            if isinstance(exc, CodedError)
            else None
        )
        if status is None:
            # Unexpected — log the full traceback server-side, return an opaque
            # 500 (never leak internals / a stack trace to the client).
            _log.exception("backend handler error")
            self.send_json({"error": "internal error"}, status=500)
            return
        self.send_json({"error": str(exc), "code": exc.code}, status=status)

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler convention
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def do_PUT(self) -> None:  # noqa: N802
        self._dispatch("PUT")

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch("DELETE")

    def do_PATCH(self) -> None:  # noqa: N802 — for the preview /api proxy
        self._dispatch("PATCH")

    def do_OPTIONS(self) -> None:  # noqa: N802 — preview /api CORS preflight
        self._dispatch("OPTIONS")

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A002
        _log.debug("backend http: " + fmt, *args)


class _BackendHttpServer(ThreadingHTTPServer):
    """ThreadingHTTPServer carrying the engine room + router for handlers.

    ``app_gateway`` (the HTML-app preview gateway) and ``mcp_registry`` (the MCP
    connector config store) are the T6 ancillary services; ``None`` when a deployment
    runs without preview / MCP.
    """

    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        addr: tuple[str, int],
        *,
        engine_room: EngineRoom,
        router: Router,
        app_gateway: Optional[Any] = None,
        sandbox_preview_gateway: Optional[Any] = None,
        mcp_registry: Optional[Any] = None,
        workspace_registry: Optional[Any] = None,
        web_assets: Optional[Any] = None,
    ) -> None:
        super().__init__(addr, BackendHandler)
        self.engine_room = engine_room
        self.router = router
        self.app_gateway = app_gateway
        self.sandbox_preview_gateway = sandbox_preview_gateway
        self.mcp_registry = mcp_registry
        self.workspace_registry = workspace_registry
        self.web_assets = web_assets


def make_http_server(
    engine_room: EngineRoom,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    router: Optional[Router] = None,
    app_gateway: Optional[Any] = None,
    sandbox_preview_gateway: Optional[Any] = None,
    mcp_registry: Optional[Any] = None,
    workspace_registry: Optional[Any] = None,
    web_assets: Optional[Any] = None,
) -> _BackendHttpServer:
    """Build the backend HTTP server over an :class:`EngineRoom`.

    ``router`` defaults to an empty table (only ``/health`` works until T5/T6
    register the task protocol + resource routes). ``app_gateway`` /
    ``sandbox_preview_gateway`` / ``mcp_registry`` / ``workspace_registry``
    enable the T6 preview + sandbox-live-preview + MCP + workspace services
    (``None`` ⇒ off). ``web_assets`` enables serving the bundled SPA (``None``
    ⇒ SPA routes 404).
    """
    return _BackendHttpServer(
        (host, port),
        engine_room=engine_room,
        router=router or Router(),
        app_gateway=app_gateway,
        sandbox_preview_gateway=sandbox_preview_gateway,
        mcp_registry=mcp_registry,
        workspace_registry=workspace_registry,
        web_assets=web_assets,
    )
