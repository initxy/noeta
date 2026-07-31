"""``open_app`` — render a workspace HTML app in the right-side panel.

The model writes a small front end into a workspace subdir
(``app/index.html`` + assets), then calls ``open_app(dir, proxy_to)``; this
tool validates the directory, registers a mount on the host
:class:`AppPreviewGateway`, and returns the render URL both as ``output`` and
as an ``open_app`` side effect the frontend acts on. The gateway serves the
directory same-origin and proxies ``/api/*`` to ``proxy_to`` server-side, so
the page reaches its API without browser CORS. The tool and the gateway seam
it calls through live together here, so the kernel carries no app-preview
vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, cast

from noeta.execution.session_pack import (
    EMPTY_CONTRIBUTION,
    PackContribution,
    SessionBuildContext,
)
from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.protocols.resources import load_markdown
from noeta.tools.invocation import require_str
from noeta.runtime.workspace import WorkspaceRoot, resolve_or_error, tool_error


__all__ = [
    "AppMount",
    "AppPreviewGateway",
    "OpenAppTool",
    "build_app_session_pack",
    "build_app_tools",
]


@dataclass(frozen=True, slots=True)
class AppMount:
    """``token`` is the unguessable path segment the gateway routes on
    (``/apps/<token>/``); ``url`` is the absolute address the preview iframe
    loads.
    """

    token: str
    url: str


class AppPreviewGateway(Protocol):
    """Structural seam: register an app mount, get its render URL.

    A host gateway (an HTTP listener + a mount registry + the same-origin
    ``/api`` proxy) satisfies this structurally and rides the kernel's backend
    bag under ``"app_preview"``. ``mount`` is the only operation the tool
    needs; unmount and lifecycle stay the host's concern, keyed on ``task_id``.
    """

    def mount(
        self,
        *,
        workspace_dir: Path,
        app_rel: str,
        proxy_to: str,
        task_id: str,
    ) -> AppMount: ...

_ENTRY = "index.html"


@dataclass
class OpenAppTool:
    workspace: WorkspaceRoot
    gateway: AppPreviewGateway
    name: str = "open_app"
    description: str = field(default=load_markdown(__package__, "open_app"))
    risk_level: str = "low"
    input_schema: dict[str, Any] = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "dir": {"type": "string"},
                "proxy_to": {"type": "string"},
            },
            "required": ["dir", "proxy_to"],
            "additionalProperties": False,
        }
    )

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        err = lambda m: tool_error(self.name, m)  # noqa: E731
        dir_arg = require_str(arguments, "dir", err, message="requires non-empty 'dir'")
        if isinstance(dir_arg, ToolResult):
            return dir_arg
        proxy_to = require_str(
            arguments, "proxy_to", err, message="requires non-empty 'proxy_to'"
        )
        if isinstance(proxy_to, ToolResult):
            return proxy_to
        if not (proxy_to.startswith("http://") or proxy_to.startswith("https://")):
            return tool_error(self.name, "proxy_to must be an http(s) URL")

        resolved = resolve_or_error(self.workspace, self.name, dir_arg)
        if isinstance(resolved, ToolResult):
            return resolved
        if not resolved.is_dir():
            return tool_error(self.name, f"not a directory: {dir_arg!r}")
        if not (resolved / _ENTRY).is_file():
            return tool_error(self.name, f"missing {_ENTRY} in {dir_arg!r}")

        rel = self.workspace.relative(resolved)
        task_id = str(ctx.metadata.get("task_id", "")) if ctx.metadata else ""
        try:
            mount = self.gateway.mount(
                workspace_dir=self.workspace.root,
                app_rel=rel,
                proxy_to=proxy_to,
                task_id=task_id,
            )
        except Exception as exc:  # noqa: BLE001 — never crash the worker
            return tool_error(self.name, f"gateway mount failed: {exc}")

        return ToolResult(
            success=True,
            output={"path": rel, "url": mount.url, "proxy_to": proxy_to},
            summary=f"open_app {rel} → {mount.url} (/api proxied to {proxy_to})",
            side_effects=[{"type": "open_app", "url": mount.url, "dir": rel}],
        )


def build_app_tools(
    workspace: WorkspaceRoot, gateway: AppPreviewGateway
) -> dict[str, Tool]:
    """The app tool pack — one ``open_app`` closed over the live gateway."""
    return {"open_app": OpenAppTool(workspace=workspace, gateway=gateway)}


def build_app_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The manifest-declared ``session_pack`` factory (band 1000).

    Gated on a live ``"app_preview"`` gateway in the context bag; without one
    the empty contribution keeps the tool set and the stable-prefix hash
    byte-identical, so a resumed turn that wires no gateway rebuilds the same
    tool schemas.
    """
    gateway = cast(
        Optional[AppPreviewGateway], ctx.backends.get("app_preview")
    )
    if gateway is None:
        return EMPTY_CONTRIBUTION
    return PackContribution(tools=build_app_tools(ctx.workspace, gateway))
