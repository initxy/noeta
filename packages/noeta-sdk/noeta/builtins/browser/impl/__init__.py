"""The five noeta-owned browser tools — ``browser_navigate`` /
``browser_click`` / ``browser_type`` / ``browser_extract`` /
``browser_screenshot``.

Their names, schemas, and descriptions are pinned by noeta so the model-facing
contract — and with it the stable-prefix KV-cache bytes — never drifts when the
sandbox container image renames its own browser tools; every ``invoke``
delegates to a :class:`BrowserBackend`, the single place that wire is pinned.
This is a per-session tool pack, never an MCP connector: the tools never enter
``mcp_registry`` and never take an alias. The four text tools return a page
snapshot (page text plus numbered interactive elements), while
``browser_screenshot`` stores the PNG as a workspace artifact instead of
feeding it to the model as vision.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, cast, runtime_checkable

from noeta.execution.session_pack import (
    EMPTY_CONTRIBUTION,
    PackContribution,
    SessionBuildContext,
)
from noeta.protocols.tool import Tool, ToolContext, ToolResult
from noeta.tools.limits import (
    INLINE_CONTENT_MAX_BYTES,
    fit_output_fields,
)
from noeta.tools.refs import ref_json
from noeta.protocols.resources import load_markdown


@runtime_checkable
class BrowserBackend(Protocol):
    """The high-level, element-level browser surface the tool pack acts through.

    Deliberately narrow: the four text methods return a page snapshot or
    action-outcome string (``extract`` gives page text plus a numbered list of
    interactive elements) and ``screenshot`` returns raw PNG bytes. ``click`` /
    ``type`` address an element by the numeric ``index`` a prior ``extract``
    (or the list ``navigate`` returns inline) handed the model — never pixel
    coordinates. The ``sandbox`` plugin's ``AioBrowserBackend`` is the
    production implementation.
    """

    def navigate(self, url: str) -> str: ...

    def click(self, index: int) -> str: ...

    def type(self, index: int, text: str, *, submit: bool = False) -> str: ...

    def extract(self) -> str: ...

    def screenshot(self) -> bytes: ...


__all__ = [
    "BROWSER_TOOL_NAMES",
    "BrowserBackend",
    "build_browser_session_pack",
    "build_browser_tools",
]


#: The fixed roster a host's approval gating reads. ``build_browser_tools``
#: builds exactly these names, in this order.
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
    """Return ``arguments[key]`` as a non-empty ``str``, or a failed ``ToolResult``."""
    value = arguments.get(key)
    if not isinstance(value, str) or not value:
        return _fail(name, message)
    return value


def _require_int(
    arguments: dict[str, Any], key: str, name: str, *, message: str
) -> "int | ToolResult":
    """Return ``arguments[key]`` as a non-negative ``int``, or a failed ``ToolResult``.

    ``bool`` is rejected even though it is an ``int`` subclass — an element index
    is never a boolean. Negatives are rejected too: element indices come from a
    prior ``extract`` snapshot's numbered list, so a ``-1`` is always a model
    guess ("last element") that deserves a clear local error, not a container
    round-trip.
    """
    value = arguments.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        return _fail(name, message)
    return value


def _snapshot_result(name: str, snapshot: str, ctx: ToolContext) -> ToolResult:
    """Wrap a page snapshot as a success ``ToolResult``.

    A snapshot under the inline byte budget rides inline as ``{"snapshot": ...}``;
    a larger one is stored as a ``text/plain`` artifact and the model gets a
    bounded excerpt plus ``snapshot_ref``, so one huge page cannot flood the
    context.
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
    """Shared base for the five browser tools.

    Every browser action can egress to any site, so ``risk_level="high"`` routes
    each call through the same approval predicate as ``shell_run``.
    """

    risk_level: str = "high"

    def __init__(self, backend: BrowserBackend) -> None:
        self._backend = backend


class BrowserNavigateTool(_BrowserTool):
    name = "browser_navigate"
    description = load_markdown(__package__, "browser_navigate")
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
    description = load_markdown(__package__, "browser_click")
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "required": ["index"],
        "additionalProperties": False,
    }

    def invoke(self, arguments: dict[str, Any], ctx: ToolContext) -> ToolResult:
        index = _require_int(
            arguments, "index", self.name, message="requires non-negative integer 'index'"
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
    description = load_markdown(__package__, "browser_type")
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
            arguments, "index", self.name, message="requires non-negative integer 'index'"
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
    description = load_markdown(__package__, "browser_extract")
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
    description = load_markdown(__package__, "browser_screenshot")
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
        # The screenshot is a workspace artifact for the human to look at, NOT
        # vision fed to the model — hence ``artifacts``, not ``images``.
        ref = ctx.artifact_store.put(png, media_type=_PNG_MEDIA_TYPE)
        # ``output=None`` → the model sees ``null`` (4 bytes). The model has no
        # ref-deref tool, so putting the hash in the prompt would be dead token
        # weight; the frontend renders the artifact instead.
        return ToolResult(
            success=True,
            artifacts=[ref],
            output=None,
            summary=f"screenshot captured ({len(png)} bytes)",
        )


#: Build order — must stay aligned with ``BROWSER_TOOL_NAMES``.
_TOOL_CLASSES: tuple[type[_BrowserTool], ...] = (
    BrowserNavigateTool,
    BrowserClickTool,
    BrowserTypeTool,
    BrowserExtractTool,
    BrowserScreenshotTool,
)


def build_browser_tools(backend: BrowserBackend) -> dict[str, Tool]:
    """The five browser tools, keyed by name and closed over one backend."""
    return {cls.name: cls(backend) for cls in _TOOL_CLASSES}


def build_browser_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The manifest-declared ``session_pack`` factory (band 700).

    Applies only when the session carries a live ``"browser"`` backend in the
    context bag AND the agent opens the ``browser`` capability; either missing
    yields the empty contribution and a byte-identical tool set. The tool
    schemas are noeta-owned and fixed, so a browser session's stable prefix
    turns on the capability flag alone, never on the backend or the container
    image.
    """
    backend = cast(Optional[BrowserBackend], ctx.backends.get("browser"))
    if backend is None or not ctx.flag("browser"):
        return EMPTY_CONTRIBUTION
    return PackContribution(tools=build_browser_tools(backend))
