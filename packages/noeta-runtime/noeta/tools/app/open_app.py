"""``open_app`` — render a workspace HTML app in the right-side panel.

The model first ``write``s a small front-end into a workspace subdir
(``app/index.html`` + assets), then calls ``open_app(dir, proxy_to)``. This
tool validates the directory (inside the workspace, has an ``index.html``),
registers a mount on the host :class:`AppPreviewGateway`, and returns the
render URL — both as ``output`` (for the model) and as an ``open_app``
``side_effect`` (the signal the frontend uses to open the panel + point the
iframe). The gateway serves the dir same-origin and proxies ``/api/*`` to
``proxy_to`` server-side, so the page calls its API with no browser CORS.

The tool is only ever constructed when a real gateway is injected (the
noeta-agent live serving path); every other build path — including the SDK/test
fixtures — passes no gateway, so the tool is absent and the tool set (hence
the prompt's tool list) is unchanged on those paths.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.tools.app._gateway import AppPreviewGateway
from noeta.tools.descriptions import load_tool_description
from noeta.tools._invocation import require_str
from noeta.tools.fs._workspace import WorkspaceRoot, resolve_or_error, tool_error


__all__ = ["OpenAppTool", "build_app_tools"]

_ENTRY = "index.html"


@dataclass
class OpenAppTool:
    """Mount a workspace HTML app on the preview gateway and open it."""

    workspace: WorkspaceRoot
    gateway: AppPreviewGateway
    name: str = "open_app"
    description: str = field(default=load_tool_description("open_app"))
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
    """The app-tool pack: just ``open_app``, gateway-injected.

    Returned only when the host has a live preview gateway; merged into the
    session tool set via ``build_session_inputs(app_gateway=...)``.
    """
    return {"open_app": OpenAppTool(workspace=workspace, gateway=gateway)}
