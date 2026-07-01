"""``AppPreviewGateway`` — the host seam the ``open_app`` tool registers a
mount against.

The tool lives in the SDK; the concrete preview gateway (a second HTTP
listener + a mount registry + the same-origin ``/api`` proxy) lives in the
noeta-agent product layer. The SDK must not import the product, so the tool
depends only on this narrow structural Protocol — exactly the pattern
``BackgroundRunner`` uses for the background-shell seam.

``mount`` is the only operation the tool needs: hand the gateway a workspace
directory + the app subdirectory + the forward target + the owning task, get
back an :class:`AppMount` (token + the URL to render). Unmount/lifecycle is the
host's concern (keyed on ``task_id``), never the tool's.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


__all__ = ["AppMount", "AppPreviewGateway"]


@dataclass(frozen=True, slots=True)
class AppMount:
    """What :meth:`AppPreviewGateway.mount` returns.

    ``token`` is the unguessable path segment the gateway routes on
    (``/apps/<token>/``); ``url`` is the absolute address the right-side
    "App" iframe loads (``http://<gateway-host>:<port>/apps/<token>/``).
    """

    token: str
    url: str


class AppPreviewGateway(Protocol):
    """Structural seam: register an app mount, get its render URL.

    The host's concrete gateway satisfies this structurally. A tool holding
    one of these never sees the HTTP server or the registry — only this single
    ``mount`` call.
    """

    def mount(
        self,
        *,
        workspace_dir: Path,
        app_rel: str,
        proxy_to: str,
        task_id: str,
    ) -> AppMount: ...
