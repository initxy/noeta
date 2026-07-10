"""``AioBrowserBackend`` — the AIO Sandbox ``/mcp`` browser wire contract.

These pin the wire the backend is coded against: given a fake ``McpHttpClient``,
every :class:`BrowserBackend` method must issue the exact ``call_tool(name,
args)`` and parse the documented ``tools/call`` result shape. They never open a
socket — the live-container round-trip is a separate, gated e2e (B8) that
re-pins the AIO tool-name constants. If the live wire differs, this file is what
re-pins the one-file backend change (mirrors ``test_aio_sandbox_exec_env.py``).
"""

from __future__ import annotations

import base64
from typing import Any, Optional

import pytest

from noeta.tools.browser import AioBrowserBackend, AioBrowserError


BASE = "http://sandbox.local:8080"


class FakeMcpClient:
    """A scripted MCP transport that records every ``call_tool`` + start.

    ``results`` maps an AIO tool name to the ``tools/call`` result dict it
    returns. ``fail_on`` / ``fail_start`` make the client raise so the fault path
    can be asserted. Exposes only the ``start()`` / ``call_tool(name, args)``
    surface :class:`AioBrowserBackend` depends on.
    """

    def __init__(
        self,
        results: Optional[dict[str, Any]] = None,
        *,
        fail_on: Optional[str] = None,
        fail_start: bool = False,
    ) -> None:
        self.results = results or {}
        self.fail_on = fail_on
        self.fail_start = fail_start
        self.started = 0
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def start(self) -> None:
        self.started += 1
        if self.fail_start:
            raise RuntimeError("handshake refused")

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((name, arguments))
        if self.fail_on == name:
            raise RuntimeError("transport blew up")
        return self.results.get(name, {"content": []})


def _text(text: str) -> dict[str, Any]:
    """A ``tools/call`` result carrying a single text content block."""
    return {"content": [{"type": "text", "text": text}]}


def _backend(fake: FakeMcpClient) -> AioBrowserBackend:
    return AioBrowserBackend(base_url=BASE, client=fake)


# -- construction ----------------------------------------------------------- #


def test_empty_base_url_raises_at_construction() -> None:
    with pytest.raises(AioBrowserError):
        AioBrowserBackend(base_url="")


def test_aio_browser_error_is_oserror() -> None:
    # The tool try/except is uniform because the backend fault subclasses OSError.
    assert issubclass(AioBrowserError, OSError)


# -- wire: exact call_tool(name, args) + snapshot parsing ------------------- #


def test_navigate_issues_wire_and_returns_snapshot() -> None:
    fake = FakeMcpClient({"browser_navigate": _text("PAGE\n[1] Home link")})
    out = _backend(fake).navigate("https://example.com")
    assert fake.calls == [("browser_navigate", {"url": "https://example.com"})]
    assert out == "PAGE\n[1] Home link"


def test_click_issues_index_keyed_wire() -> None:
    fake = FakeMcpClient({"browser_click": _text("Clicked element: 7")})
    out = _backend(fake).click(7)
    assert fake.calls == [("browser_click", {"index": 7})]
    assert out == "Clicked element: 7"


def test_type_fills_then_presses_enter_on_submit() -> None:
    # noeta ``type`` fans out to the container's fill (+ Enter when submitting);
    # there is no single ``browser_type`` tool.
    fake = FakeMcpClient(
        {
            "browser_form_input_fill": _text("Successfully filled index 3"),
            "browser_press_key": _text("Pressed Enter"),
        }
    )
    out = _backend(fake).type(3, "hello world", submit=True)
    assert fake.calls == [
        ("browser_form_input_fill", {"index": 3, "value": "hello world", "clear": True}),
        ("browser_press_key", {"key": "Enter"}),
    ]
    assert out == "Successfully filled index 3\nPressed Enter"


def test_type_without_submit_only_fills() -> None:
    fake = FakeMcpClient({"browser_form_input_fill": _text("filled")})
    out = _backend(fake).type(3, "hi")
    assert fake.calls == [
        ("browser_form_input_fill", {"index": 3, "value": "hi", "clear": True})
    ]
    assert out == "filled"


def test_extract_composes_markdown_and_clickable_elements() -> None:
    # noeta ``extract`` fans out to page markdown + the numbered element list;
    # there is no single ``browser_extract`` tool.
    fake = FakeMcpClient(
        {
            "browser_get_markdown": _text("BODY TEXT"),
            "browser_get_clickable_elements": _text("[1] a\n[2] b"),
        }
    )
    out = _backend(fake).extract()
    assert fake.calls == [
        ("browser_get_markdown", {}),
        ("browser_get_clickable_elements", {}),
    ]
    assert out == "BODY TEXT\n\n# Interactive elements\n[1] a\n[2] b"


def test_text_blocks_are_concatenated_and_non_text_ignored() -> None:
    result = {
        "content": [
            {"type": "text", "text": "first"},
            {"type": "image", "data": "AAAA"},  # ignored by a text method
            {"type": "text", "text": "second"},
        ]
    }
    fake = FakeMcpClient({"browser_navigate": result})
    assert _backend(fake).navigate("https://x") == "first\nsecond"


# -- screenshot: image content block → raw bytes ---------------------------- #


def test_screenshot_decodes_base64_image_block_to_bytes() -> None:
    png = b"\x89PNG\r\n\x1a\nfake-bytes"
    result = {
        "content": [
            {"type": "image", "data": base64.b64encode(png).decode("ascii"),
             "mimeType": "image/png"}
        ]
    }
    fake = FakeMcpClient({"browser_screenshot": result})
    out = _backend(fake).screenshot()
    assert fake.calls == [("browser_screenshot", {})]
    assert out == png


def test_screenshot_without_image_block_raises() -> None:
    fake = FakeMcpClient({"browser_screenshot": _text("no image here")})
    with pytest.raises(AioBrowserError):
        _backend(fake).screenshot()


def test_screenshot_bad_base64_raises() -> None:
    result = {"content": [{"type": "image", "data": "not!base64!"}]}
    fake = FakeMcpClient({"browser_screenshot": result})
    with pytest.raises(AioBrowserError):
        _backend(fake).screenshot()


# -- lazy handshake --------------------------------------------------------- #


def test_start_is_lazy_and_runs_once() -> None:
    fake = FakeMcpClient(
        {
            "browser_get_markdown": _text("x"),
            "browser_get_clickable_elements": _text("y"),
        }
    )
    backend = _backend(fake)
    assert fake.started == 0  # not started at construction
    backend.extract()  # two container calls, one handshake
    backend.navigate("https://a")  # a later action does not re-handshake
    assert fake.started == 1


# -- fault mapping ---------------------------------------------------------- #


def test_transport_fault_raises_aio_browser_error() -> None:
    fake = FakeMcpClient(fail_on="browser_navigate")
    with pytest.raises(AioBrowserError):
        _backend(fake).navigate("https://example.com")


def test_handshake_fault_raises_aio_browser_error() -> None:
    fake = FakeMcpClient(fail_start=True)
    with pytest.raises(AioBrowserError):
        _backend(fake).extract()


def test_is_error_result_raises_aio_browser_error() -> None:
    result = {"isError": True, "content": [{"type": "text", "text": "boom"}]}
    fake = FakeMcpClient({"browser_navigate": result})
    with pytest.raises(AioBrowserError):
        _backend(fake).navigate("https://example.com")


def test_default_client_targets_mcp_endpoint() -> None:
    # Without an injected client the backend builds a real McpHttpClient aimed at
    # the container's ``/mcp`` endpoint (no socket opened — we only read its url).
    backend = AioBrowserBackend(base_url=BASE + "/")
    assert backend._client._url == BASE + "/mcp"  # noqa: SLF001 — wire assertion


def test_auth_headers_folded_into_client_once() -> None:
    calls = {"n": 0}

    def auth() -> dict[str, str]:
        calls["n"] += 1
        return {"X-AIO-API-Key": "secret"}

    backend = AioBrowserBackend(base_url=BASE, auth_headers=auth)
    # Resolved exactly once, at construction, and folded into the client headers.
    assert calls["n"] == 1
    assert backend._client._headers["X-AIO-API-Key"] == "secret"  # noqa: SLF001
