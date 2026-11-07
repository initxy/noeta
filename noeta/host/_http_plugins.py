"""``noeta serve``'s plugin routes: the listing and switches, a task's UI
items, calls from a plugin's front-end code, its web files (served without a
token: an ES module import sends none, and the files hold no secret), and
installing and removing, allowed only on a loopback server or one started
with ``--allow-plugin-install``."""

from __future__ import annotations

import mimetypes
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import msgspec
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response

from noeta.core.agent import Agent
from noeta.host._http_guard import Guard, Refused, scope
from noeta.host.app import AppAPI
from noeta.host.plugins import UI_API, web_dir

_enc = msgspec.json.Encoder()

#: Media types a browser needs exactly (a module script is refused under
#: ``nosniff`` with any other).
_TYPES = {".js": "text/javascript", ".mjs": "text/javascript", ".css": "text/css",
          ".json": "application/json", ".map": "application/json", ".svg": "image/svg+xml",
          ".wasm": "application/wasm"}


def _json(value: Any, status: int = 200) -> Response:
    return Response(_enc.encode(value), status_code=status, media_type="application/json")


def _media_type(path: Path) -> str:
    return _TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] \
        or "application/octet-stream"


class PluginRoutes:
    """The handlers, on ``serving``; ``agents`` are the ones this server
    runs (a web part is served only for a plugin one of them runs);
    ``allow_install``: install and remove over HTTP."""

    def __init__(self, serving: AppAPI, agents: Callable[[], Mapping[str, Agent]],
                 guard: Guard, *, allow_install: bool,
                 headers: tuple[tuple[bytes, bytes], ...] = ()) -> None:
        self._serving = serving
        self._agents = agents
        self._guard = guard
        self._allow_install = allow_install
        self._headers = {k.decode(): v.decode() for k, v in headers}

    async def list(self, request: Request) -> Response:
        q = request.query_params
        cwd = scope(request).guarded.check_cwd(q.get("cwd")) or ""
        found = await self._serving.plugins(q.get("agent") or "main", str(cwd))
        return _json({"plugins": found, "ui_api": list(UI_API)})

    async def put(self, request: Request) -> Response:
        body = await request.json()
        enabled = body.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("'enabled' is true or false")
        try:
            await self._serving.set_plugin_default(request.path_params["name"], enabled)
        except NotImplementedError as exc:
            return _json({"error": str(exc)}, 501)
        return _json({"ok": True})

    async def call(self, request: Request) -> Response:
        body = await request.json()
        task_id = str(body["task_id"])
        await scope(request).client.get(task_id)  # an unknown task answers 404
        try:
            result = await self._serving.plugin_call(task_id, request.path_params["name"],
                                                     str(body["name"]), body.get("args"))
        except LookupError as exc:
            return _json({"error": f"no such plugin call: {exc}"}, 404)
        return _json({"result": result})

    async def ui(self, request: Request) -> Response:
        task_id = request.path_params["task_id"]
        await scope(request).client.get(task_id)
        return _json({"items": await self._serving.ui(task_id)})

    async def market(self, request: Request) -> Response:
        return _json({"entries": await self._serving.market()})

    def _check_install(self) -> None:
        if not self._allow_install:
            raise Refused("this server does not install or remove plugins over HTTP "
                          "(serve on a loopback address with no --allow-host or "
                          "--allow-origin, or start it with --allow-plugin-install)")

    async def install(self, request: Request) -> Response:
        self._check_install()
        body = await request.json()
        source = body.get("source")
        if not isinstance(source, str) or not source:
            raise ValueError("'source' names the plugin to install")
        try:
            info = await self._serving.install_plugin(source, body.get("marketplace"),
                                                      body.get("commit"))
        except NotImplementedError as exc:
            return _json({"error": str(exc)}, 501)
        return _json(info, 201)

    async def remove(self, request: Request) -> Response:
        self._check_install()
        try:
            await self._serving.remove_plugin(request.path_params["name"])
        except NotImplementedError as exc:
            return _json({"error": str(exc)}, 501)
        return _json({"ok": True})

    async def web(self, request: Request) -> Response:
        """A file of an enabled plugin's web part, inside its web directory."""
        if not self._guard.same_site(request.headers):
            return JSONResponse({"error": "cross-site request refused"}, status_code=403)
        name = request.path_params["name"]
        running = any(e.name == name for a in self._agents().values() for e in a.extensions)
        root = web_dir(name) if running else None
        if root is None:
            return JSONResponse({"error": f"no web part of an enabled plugin {name!r}"},
                                status_code=404)
        path = (root / request.path_params["path"]).resolve()
        if not path.is_relative_to(root.resolve()) or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path, media_type=_media_type(path),
                            headers={**self._headers, "cache-control": "no-cache"})
