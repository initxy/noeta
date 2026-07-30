"""``browser`` built-in — the noeta-owned browser tool pack (impl).

Five tools — ``browser_navigate`` / ``browser_click`` / ``browser_type`` /
``browser_extract`` / ``browser_screenshot`` — whose **name / schema /
description are pinned by noeta** so the model-facing contract (and therefore the
stable-prefix KV-cache bytes) never drifts when the AIO Sandbox container image
changes its own tool names (spec D1, CONTEXT.md Stable Prefix). Each tool's
``invoke`` delegates to a :class:`BrowserBackend` (in production the AIO
adapter from the ``sandbox`` built-in plugin,
``noeta.builtins.sandbox.impl.browser``) which is the single place the
container ``/mcp`` browser wire is pinned.

The pack is closure-constructed like the fs pack: :func:`build_browser_tools`
takes one backend and returns exactly the five tools keyed by name. It is a
per-session tool pack (mounted only in sandbox mode with a browser-capable
agent) — NOT an MCP connector (spec D2). Microkernel phase 3: the
:class:`BrowserBackend` Protocol lives HERE (this plugin owns the seam its
five tools call through; the ``sandbox`` plugin implements it — a normal
one-directional plugin dependency), and the pack enters a session as this
plugin's ``session_pack`` manifest contribution
(:func:`build_browser_session_pack`, band 700) reading the ``"browser"``
entry of the kernel's backend bag — the kernel holds no browser vocabulary
at all.

Perception v1 (spec D4): the four text tools return a page snapshot (page text +
numbered interactive elements); ``browser_screenshot`` stores the PNG as a
workspace artifact and returns its ref — it does **not** feed the screenshot to
the model as vision (see the tool's own note on increment-2).
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

    This plugin's own seam (microkernel phase 3 — moved here from
    ``noeta.runtime.browser``; the five tools and the Protocol they call
    through live in one plugin, so the import is internal and the kernel
    holds no browser vocabulary). Deliberately narrow (spec D4
    perception-v1): the four text methods return a page snapshot /
    action-outcome string (``extract`` gives page text + a numbered list of
    interactive elements, browser-use style), and ``screenshot`` returns raw
    PNG bytes. ``click`` / ``type`` address an element by the numeric
    ``index`` a prior ``extract`` (or the list ``navigate`` returns inline)
    handed the model — no pixel coordinates. The ``sandbox`` plugin's
    ``AioBrowserBackend`` is the production implementation; a test
    substitutes a fake to prove the tools delegate without touching a
    container.
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

    Inlined (rather than importing ``noeta.tools.invocation.require_str``) so the
    browser pack stays self-contained and free of the ``_invocation`` ↔
    ``noeta.runtime.exec_env`` import cycle when it is imported standalone. Same shape as
    the shared helper: the failure ``summary`` is ``"{name}: {message}"``.
    """
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
    round-trip. Same failure shape as :func:`_require_str`.
    """
    value = arguments.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
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
        # v1 (spec D4): the screenshot is a workspace artifact (viewable in the
        # file panel / Lightbox), NOT vision fed to the model — so the ref goes in
        # ``artifacts``, not ``images``. increment-2 (vision) flips this ref from
        # ``artifacts`` to ``images`` behind a config toggle; the schema is
        # unchanged (whether the model sees the image is a runtime behaviour, not
        # a stable-prefix byte).
        ref = ctx.artifact_store.put(png, media_type=_PNG_MEDIA_TYPE)
        # ``output=None`` → the model sees ``null`` (4 bytes). The ref rides
        # ``artifacts`` only — the frontend renders it from there; the model has
        # no ref-deref tool, so a hash in the prompt would be dead token weight.
        return ToolResult(
            success=True,
            artifacts=[ref],
            output=None,
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


def build_browser_session_pack(ctx: SessionBuildContext) -> PackContribution:
    """The browser pack as a ``session_pack`` contribution (microkernel phase 3).

    The manifest-declared factory (band 700) — sandbox-only, flag-gated, NOT
    whitelist-filtered. Applies when the session both carries a live
    ``"browser"`` backend in the context bag (the SDK host vends one off the
    sandbox handle) AND the agent opens the ``browser`` capability. The tool
    schemas are noeta-owned and fixed, so a browser session's stable prefix
    depends only on the capability flag, never on the backend or the AIO
    image; absent backend OR flag off (resume with no sandbox / every
    non-browser agent) ⇒ the empty contribution, byte-identical tool set.
    """
    backend = cast(Optional[BrowserBackend], ctx.backends.get("browser"))
    if backend is None or not ctx.flag("browser"):
        return EMPTY_CONTRIBUTION
    return PackContribution(tools=build_browser_tools(backend))
