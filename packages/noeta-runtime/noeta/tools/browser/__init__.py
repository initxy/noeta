"""``noeta.tools.browser`` — the noeta-owned browser tool pack (spec layer 3).

Five tools — ``browser_navigate`` / ``browser_click`` / ``browser_type`` /
``browser_extract`` / ``browser_screenshot`` — whose **name / schema /
description are pinned by noeta** so the model-facing contract (and therefore the
stable-prefix KV-cache bytes) never drifts when the AIO Sandbox container image
changes its own tool names (spec D1, CONTEXT.md Stable Prefix). Each tool's
``invoke`` delegates to a :class:`~noeta.tools.browser._backend.BrowserBackend`
(``AioBrowserBackend`` in production) which is the single place the container
``/mcp`` browser wire is pinned.

The pack is closure-constructed like the fs pack: :func:`build_browser_tools`
takes one backend and returns exactly the five tools keyed by name. It is a
per-session tool pack (mounted only in sandbox mode with a browser-capable
agent), injected the way fs tools are — NOT an MCP connector (spec D2).

Perception v1 (spec D4): the four text tools return a page snapshot (page text +
numbered interactive elements); ``browser_screenshot`` stores the PNG as a
workspace artifact and returns its ref — it does **not** feed the screenshot to
the model as vision (see the tool's own note on increment-2).
"""

from __future__ import annotations

from typing import Any

from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.tools._limits import (
    INLINE_CONTENT_MAX_BYTES,
    fit_output_fields,
)
from noeta.tools._refs import ref_json
from noeta.tools.browser._backend import (
    AioBrowserBackend,
    AioBrowserError,
    BrowserBackend,
)
from noeta.tools.descriptions import load_tool_description


__all__ = [
    "AioBrowserBackend",
    "AioBrowserError",
    "BROWSER_TOOL_NAMES",
    "BrowserBackend",
    "build_browser_tools",
]


#: The five noeta-owned browser tool names, in a fixed order. The public roster
#: the main-agent integration codes against; keep this exact tuple stable.
BROWSER_TOOL_NAMES: tuple[str, ...] = (
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_extract",
    "browser_screenshot",
)

_TEXT_MEDIA_TYPE = "text/plain"
_PNG_MEDIA_TYPE = "image/png"


def _fail(name: str, message: str) -> ToolResult:
    """A tool failure — never raised out of ``invoke``, always a ``ToolResult``."""
    return ToolResult(success=False, summary=f"{name}: {message}")


def _require_str(
    arguments: dict[str, Any], key: str, name: str, *, message: str
) -> "str | ToolResult":
    """Return ``arguments[key]`` as a non-empty ``str``, or a failed ``ToolResult``.

    Inlined (rather than importing ``noeta.tools._invocation.require_str``) so the
    browser pack stays self-contained and free of the ``_invocation`` ↔
    ``noeta.tools.fs`` import cycle when it is imported standalone. Same shape as
    the shared helper: the failure ``summary`` is ``"{name}: {message}"``.
    """
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        return _fail(name, message)
    return value


def _require_int(
    arguments: dict[str, Any], key: str, name: str, *, message: str
) -> "int | ToolResult":
    """Return ``arguments[key]`` as an ``int``, or a failed ``ToolResult``.

    ``bool`` is rejected even though it is an ``int`` subclass — an element index
    is never a boolean. Same failure shape as :func:`_require_str`.
    """
    value = arguments.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        return _fail(name, message)
    return value


def _snapshot_result(name: str, snapshot: str, ctx: ToolContext) -> ToolResult:
    """Wrap a page-snapshot string as a success ``ToolResult``, offloading a large
    snapshot to a ContentStore artifact (mirrors ``_result_to_tool_result``).

    A snapshot under the inline byte budget rides inline as ``{"snapshot": ...}``;
    a larger one is stored as a ``text/plain`` artifact and the model gets a
    bounded excerpt plus ``snapshot_ref`` so the context stays lean (spec R4).
    """
    output: dict[str, Any] = {"snapshot": snapshot}
    artifacts = []
    if len(snapshot.encode("utf-8")) > INLINE_CONTENT_MAX_BYTES:
        ref = ctx.artifact_store.put(
            snapshot.encode("utf-8"), media_type=_TEXT_MEDIA_TYPE
        )
        artifacts.append(ref)
        output = fit_output_fields(
            {"snapshot": snapshot, "snapshot_ref": ref_json(ref)},
            shrink_order=["snapshot"],
            max_bytes=INLINE_CONTENT_MAX_BYTES,
        )
    return ToolResult(
        success=True,
        output=output,
        summary=f"{name}: {len(snapshot)} chars",
        artifacts=artifacts,
    )


class _BrowserTool:
    """Shared base for the five browser tools: holds the backend + high risk.

    Every browser action can egress to any site, so ``risk_level="high"`` routes
    each call through the same approval predicate as ``shell_run`` (spec D5).
    Subclasses set ``name`` / ``description`` / ``input_schema`` and implement
    ``invoke``; the base only carries the backend.
    """

    risk_level: str = "high"

    def __init__(self, backend: BrowserBackend) -> None:
        self._backend = backend


class BrowserNavigateTool(_BrowserTool):
    name = "browser_navigate"
    description = load_tool_description("browser_navigate")
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
        "additionalProperties": False,
    }

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        url = _require_str(
            arguments, "url", self.name, message="requires non-empty 'url'"
        )
        if isinstance(url, ToolResult):
            return url
        try:
            snapshot = self._backend.navigate(url)
        except OSError as exc:
            return _fail(self.name, str(exc))
        return _snapshot_result(self.name, snapshot, ctx)


class BrowserClickTool(_BrowserTool):
    name = "browser_click"
    description = load_tool_description("browser_click")
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "required": ["index"],
        "additionalProperties": False,
    }

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        index = _require_int(
            arguments, "index", self.name, message="requires integer 'index'"
        )
        if isinstance(index, ToolResult):
            return index
        try:
            snapshot = self._backend.click(index)
        except OSError as exc:
            return _fail(self.name, str(exc))
        return _snapshot_result(self.name, snapshot, ctx)


class BrowserTypeTool(_BrowserTool):
    name = "browser_type"
    description = load_tool_description("browser_type")
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "text": {"type": "string"},
            "submit": {"type": "boolean"},
        },
        "required": ["index", "text"],
        "additionalProperties": False,
    }

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        index = _require_int(
            arguments, "index", self.name, message="requires integer 'index'"
        )
        if isinstance(index, ToolResult):
            return index
        text = arguments.get("text")
        if not isinstance(text, str):
            return _fail(self.name, "requires string 'text'")
        submit_raw = arguments.get("submit", False)
        submit = submit_raw if isinstance(submit_raw, bool) else False
        try:
            snapshot = self._backend.type(index, text, submit=submit)
        except OSError as exc:
            return _fail(self.name, str(exc))
        return _snapshot_result(self.name, snapshot, ctx)


class BrowserExtractTool(_BrowserTool):
    name = "browser_extract"
    description = load_tool_description("browser_extract")
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        del arguments
        try:
            snapshot = self._backend.extract()
        except OSError as exc:
            return _fail(self.name, str(exc))
        return _snapshot_result(self.name, snapshot, ctx)


class BrowserScreenshotTool(_BrowserTool):
    name = "browser_screenshot"
    description = load_tool_description("browser_screenshot")
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        del arguments
        try:
            png = self._backend.screenshot()
        except OSError as exc:
            return _fail(self.name, str(exc))
        # v1 (spec D4): the screenshot is a workspace artifact (viewable in the
        # file panel / Lightbox), NOT vision fed to the model — so the ref goes in
        # ``artifacts``, not ``images``. increment-2 (vision) flips this ref from
        # ``artifacts`` to ``images`` behind a config toggle; the schema is
        # unchanged (whether the model sees the image is a runtime behaviour, not
        # a stable-prefix byte).
        ref = ctx.artifact_store.put(png, media_type=_PNG_MEDIA_TYPE)
        return ToolResult(
            success=True,
            artifacts=[ref],
            output={"screenshot_ref": ref_json(ref)},
            summary=f"screenshot captured ({len(png)} bytes)",
        )


#: name → tool class, in the fixed ``BROWSER_TOOL_NAMES`` order.
_TOOL_CLASSES: tuple[type[_BrowserTool], ...] = (
    BrowserNavigateTool,
    BrowserClickTool,
    BrowserTypeTool,
    BrowserExtractTool,
    BrowserScreenshotTool,
)


def build_browser_tools(backend: BrowserBackend) -> dict[str, Tool]:
    """Build the five noeta-owned browser tools, keyed by name.

    Closure-constructs each tool over ``backend`` (the seam that hides the
    container ``/mcp`` wire) and returns exactly the ``BROWSER_TOOL_NAMES`` set.
    The caller (the engine build in sandbox mode with a browser-capable agent)
    merges this dict into the session tool set the same way it merges the fs pack.
    """
    return {cls.name: cls(backend) for cls in _TOOL_CLASSES}
