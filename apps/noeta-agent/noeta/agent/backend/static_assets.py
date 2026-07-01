"""static_assets — serve the bundled ``apps/web`` SPA from the new backend.

HTTP — including serving the
built frontend — lives only in the app layer (D5). The legacy runner
(``noeta.agent.host.runner_cli`` + ``host.http``) served the SPA; that path was
deleted in T8, so this module is the backend's self-contained home for the
same concern: locate the Vite ``dist/`` bundle and map the two product URLs
(``/chat`` + ``/trace``) plus the hashed ``/assets/`` files onto it.

The SPA *source* lives in the standalone ``apps/web`` project; the product wheel
``force-include``s the built bundle at ``noeta/agent/static`` (the frontend never
leaks into the ``noeta-sdk`` wheel). React must be served from Vite ``dist/``
because browsers cannot execute the source JSX / bare imports directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


__all__ = ["WebAssetRoot", "locate_web_assets", "resolve_static", "read_asset"]


# HTML routes are explicit; built Vite assets under ``/assets/`` are served from
# a narrow URL prefix after path validation. ``/`` redirects to the chat surface,
# so the product has two pages: /chat and /trace.
_STATIC_FILES: dict[str, tuple[str, str]] = {
    "/chat": ("chat.html", "text/html; charset=utf-8"),
    "/chat.html": ("chat.html", "text/html; charset=utf-8"),
    "/trace": ("trace.html", "text/html; charset=utf-8"),
    "/trace.html": ("trace.html", "text/html; charset=utf-8"),
}
_STATIC_PREFIXES: tuple[tuple[str, str], ...] = (
    ("/assets/", "assets/"),
    ("/src/", "src/"),
)
_STATIC_CONTENT_TYPES: dict[str, str] = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


@dataclass(frozen=True)
class WebAssetRoot:
    """The SPA assets root — a built ``dist/`` over its source tree.

    ``joinpath`` resolves a non-``src/`` filename from ``dist`` first (the
    hashed Vite output) and falls back to ``source`` so an asset checked into
    source but not built still serves in dev.
    """

    source: Path
    dist: Path

    def joinpath(self, filename: str) -> Path:
        if not filename.startswith("src/"):
            candidate = self.dist / filename
            if candidate.is_file():
                return candidate
        return self.source / filename


def locate_web_assets() -> Optional[WebAssetRoot]:
    """Locate the coding SPA's assets root, or ``None`` if no build is present.

    Resolution order:

    * **installed wheel (primary):** the force-included resource at
      ``noeta/agent/static`` (anchored via ``Path(__file__)`` — its
      namespace-dir sibling — because ``importlib.resources.files()`` only
      supports namespace packages from Python 3.12 and this project runs 3.11+).
    * **source checkout (dev fallback):** the repo's ``apps/web`` source tree,
      which still requires ``apps/web/dist`` for the Python-hosted UI. During
      frontend development use Vite directly (``npm run dev``).

    Presence is probed via the ``dist/chat.html`` sentinel so an unbuilt source
    tree does not falsely satisfy the check. ``None`` ⇒ the server serves a
    clean 404 for SPA / static routes.
    """
    agent_dir = Path(__file__).resolve().parents[1]  # noeta/agent
    res = agent_dir / "static"
    if (res / "dist" / "chat.html").is_file():
        return WebAssetRoot(source=res, dist=res / "dist")

    # Dev fallback: backend/static_assets.py → parents[5] is the repo root, so
    # apps/web is parents[5]/apps/web.
    dev = Path(__file__).resolve().parents[5] / "apps" / "web"
    if (dev / "dist" / "chat.html").is_file():
        return WebAssetRoot(source=dev, dist=dev / "dist")
    return None


def resolve_static(path: str) -> Optional[tuple[str, str]]:
    """Map a request path onto ``(asset_filename, content_type)``, or ``None``.

    Covers the explicit HTML routes (``/chat`` / ``/trace`` and their
    ``.html`` forms) and the validated ``/assets/`` + ``/src/`` prefixes.
    Returns ``None`` for any other path (it falls through to API routing) and
    for prefix paths that fail traversal validation.
    """
    if path in _STATIC_FILES:
        return _STATIC_FILES[path]
    prefixed = _static_prefixed_file(path)
    if prefixed is not None:
        return prefixed, _static_content_type(prefixed)
    return None


def read_asset(assets: WebAssetRoot, filename: str) -> bytes:
    """Read a bundled static asset's bytes (wheel-safe via the provider).

    ``filename`` is always one of the hard-coded :data:`_STATIC_FILES` values
    or a traversal-validated static-prefix path.
    """
    return assets.joinpath(filename).read_bytes()


def _static_content_type(filename: str) -> str:
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _STATIC_CONTENT_TYPES.get(suffix, "application/octet-stream")


def _static_prefixed_file(path: str) -> Optional[str]:
    for prefix, root in _STATIC_PREFIXES:
        if not path.startswith(prefix):
            continue
        rest = path[len(prefix):]
        if (
            not rest
            or rest.startswith("/")
            or ".." in rest.split("/")
            or "\\" in rest
        ):
            return None
        return root + rest
    return None
