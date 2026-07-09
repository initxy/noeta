"""``build_browser_tools`` — the noeta-owned browser tool pack (spec layer 3).

The pack's model-facing contract is noeta's, not the container's: these assert
the exact roster, schemas, risk level, and descriptions (the stable-prefix
bytes), then that each tool delegates to the injected backend and maps a backend
fault to ``ToolResult(success=False, ...)`` without raising. The stable-prefix
guarantee (spec acceptance #2) is exercised by ``FakeBackend`` renaming the AIO
wire freely — the tool schema below never moves.
"""

from __future__ import annotations

from typing import Any

from noeta.protocols.tool import ToolContext
from noeta.storage.memory import InMemoryContentStore
from noeta.tools._limits import INLINE_CONTENT_MAX_BYTES
from noeta.tools.browser import (
    BROWSER_TOOL_NAMES,
    AioBrowserError,
    build_browser_tools,
)


class FakeBackend:
    """A recording ``BrowserBackend`` stand-in — no container involved.

    Each method records its call and returns a canned snapshot / PNG; ``fail``
    names a method that should raise :class:`AioBrowserError` so the tool's
    error-mapping path can be asserted.
    """

    def __init__(self, *, snapshot: str = "SNAP", png: bytes = b"PNGDATA",
                 fail: str = "") -> None:
        self.snapshot = snapshot
        self.png = png
        self.fail = fail
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _maybe_fail(self, name: str) -> None:
        if self.fail == name:
            raise AioBrowserError(f"{name} refused")

    def navigate(self, url: str) -> str:
        self.calls.append(("navigate", (url,), {}))
        self._maybe_fail("navigate")
        return self.snapshot

    def click(self, index: int) -> str:
        self.calls.append(("click", (index,), {}))
        self._maybe_fail("click")
        return self.snapshot

    def type(self, index: int, text: str, *, submit: bool = False) -> str:
        self.calls.append(("type", (index, text), {"submit": submit}))
        self._maybe_fail("type")
        return self.snapshot

    def extract(self) -> str:
        self.calls.append(("extract", (), {}))
        self._maybe_fail("extract")
        return self.snapshot

    def screenshot(self) -> bytes:
        self.calls.append(("screenshot", (), {}))
        self._maybe_fail("screenshot")
        return self.png


def _ctx() -> tuple[ToolContext, InMemoryContentStore]:
    store = InMemoryContentStore()
    return ToolContext(artifact_store=store), store


# -- roster / schemas / risk / descriptions (the stable prefix) ------------- #


def test_build_returns_exactly_the_named_tools() -> None:
    tools = build_browser_tools(FakeBackend())
    assert tuple(tools) == BROWSER_TOOL_NAMES
    assert all(tools[name].name == name for name in BROWSER_TOOL_NAMES)


def test_all_tools_are_high_risk_with_nonempty_descriptions() -> None:
    tools = build_browser_tools(FakeBackend())
    for tool in tools.values():
        assert tool.risk_level == "high"
        assert isinstance(tool.description, str) and tool.description.strip()


def test_input_schemas_are_the_pinned_noeta_bytes() -> None:
    tools = build_browser_tools(FakeBackend())
    assert tools["browser_navigate"].input_schema == {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
        "additionalProperties": False,
    }
    assert tools["browser_click"].input_schema == {
        "type": "object",
        "properties": {"index": {"type": "integer"}},
        "required": ["index"],
        "additionalProperties": False,
    }
    assert tools["browser_type"].input_schema == {
        "type": "object",
        "properties": {
            "index": {"type": "integer"},
            "text": {"type": "string"},
            "submit": {"type": "boolean"},
        },
        "required": ["index", "text"],
        "additionalProperties": False,
    }
    assert tools["browser_extract"].input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert tools["browser_screenshot"].input_schema == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }


# -- delegation ------------------------------------------------------------- #


def test_navigate_delegates_and_returns_snapshot() -> None:
    backend = FakeBackend(snapshot="HOME PAGE")
    ctx, _ = _ctx()
    result = build_browser_tools(backend)["browser_navigate"].invoke(
        {"url": "https://example.com"}, ctx
    )
    assert backend.calls == [("navigate", ("https://example.com",), {})]
    assert result.success is True
    assert result.output["snapshot"] == "HOME PAGE"


def test_click_delegates_by_index() -> None:
    backend = FakeBackend()
    ctx, _ = _ctx()
    build_browser_tools(backend)["browser_click"].invoke({"index": 9}, ctx)
    assert backend.calls == [("click", (9,), {})]


def test_type_delegates_index_text_submit() -> None:
    backend = FakeBackend()
    ctx, _ = _ctx()
    build_browser_tools(backend)["browser_type"].invoke(
        {"index": 2, "text": "query", "submit": True}, ctx
    )
    assert backend.calls == [("type", (2, "query"), {"submit": True})]


def test_extract_delegates() -> None:
    backend = FakeBackend(snapshot="BODY\n[1] link")
    ctx, _ = _ctx()
    result = build_browser_tools(backend)["browser_extract"].invoke({}, ctx)
    assert backend.calls == [("extract", (), {})]
    assert result.output["snapshot"] == "BODY\n[1] link"


def test_large_snapshot_offloads_to_artifact() -> None:
    big = "x" * (INLINE_CONTENT_MAX_BYTES + 1000)
    backend = FakeBackend(snapshot=big)
    ctx, store = _ctx()
    result = build_browser_tools(backend)["browser_extract"].invoke({}, ctx)
    assert result.success is True
    assert len(result.artifacts) == 1
    assert "snapshot_ref" in result.output
    assert store.get(result.artifacts[0]) == big.encode("utf-8")


# -- error mapping ---------------------------------------------------------- #


def test_backend_fault_maps_to_failed_result_not_raise() -> None:
    backend = FakeBackend(fail="navigate")
    ctx, _ = _ctx()
    result = build_browser_tools(backend)["browser_navigate"].invoke(
        {"url": "https://example.com"}, ctx
    )
    assert result.success is False
    assert "navigate refused" in result.summary


def test_missing_url_is_a_failed_result() -> None:
    ctx, _ = _ctx()
    result = build_browser_tools(FakeBackend())["browser_navigate"].invoke({}, ctx)
    assert result.success is False


def test_type_missing_text_is_a_failed_result() -> None:
    ctx, _ = _ctx()
    result = build_browser_tools(FakeBackend())["browser_type"].invoke(
        {"index": 1}, ctx
    )
    assert result.success is False


def test_click_missing_index_is_a_failed_result() -> None:
    ctx, _ = _ctx()
    result = build_browser_tools(FakeBackend())["browser_click"].invoke({}, ctx)
    assert result.success is False


def test_click_non_integer_index_is_a_failed_result() -> None:
    # a string (or bool) index never reaches the backend — the tool rejects it.
    ctx, _ = _ctx()
    backend = FakeBackend()
    result = build_browser_tools(backend)["browser_click"].invoke(
        {"index": "9"}, ctx
    )
    assert result.success is False
    assert backend.calls == []


# -- screenshot v1: artifact, NOT vision ------------------------------------ #


def test_screenshot_puts_png_in_artifacts_not_images() -> None:
    backend = FakeBackend(png=b"\x89PNGscreenshot")
    ctx, store = _ctx()
    result = build_browser_tools(backend)["browser_screenshot"].invoke({}, ctx)
    assert result.success is True
    # v1: the PNG rides in ``artifacts`` (file panel), NEVER ``images`` (vision).
    assert len(result.artifacts) == 1
    assert result.images == []
    assert store.get(result.artifacts[0]) == b"\x89PNGscreenshot"
    assert result.artifacts[0].media_type == "image/png"
    # v1: output is None — the ref rides artifacts only. The model has no
    # ref-deref tool, so a hash in the prompt would be dead token weight.
    assert result.output is None


def test_screenshot_fault_maps_to_failed_result() -> None:
    backend = FakeBackend(fail="screenshot")
    ctx, _ = _ctx()
    result = build_browser_tools(backend)["browser_screenshot"].invoke({}, ctx)
    assert result.success is False
