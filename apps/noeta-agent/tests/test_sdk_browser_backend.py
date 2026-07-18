"""``SdkBrowserBackend`` — the ``agent-sandbox`` SDK browser wire contract.

vendored from noeta ``tests/test_sdk_browser_backend.py`` (noeta 0.2.3); only
the import is repointed at this repo's vendored adapter. Sync it along with an
upstream re-sync.

These pin the mapping the backend is coded against: given a fake SDK client,
every :class:`BrowserBackend` method must issue the right ``browser_page`` /
``browser`` call (index-addressed, no selector bridge) and shape the result into
the string / bytes the tool pack expects. They never open a socket; the
live-container round-trip is exercised separately. Mirrors
``test_browser_backend.py``.
"""

from __future__ import annotations

from typing import Any, Optional

import pytest
from agent_sandbox.core.api_error import ApiError

from noeta.agent.host.sdk_browser_backend import SdkBrowserBackend
from noeta.tools.browser._backend import AioBrowserError


BASE = "http://sandbox.local:8080"


class _Resp:
    def __init__(self, data: Any) -> None:
        self.data = data


class FakeBrowserPage:
    def __init__(
        self,
        *,
        elements: Optional[list[dict[str, Any]]] = None,
        markdown: str = "",
        png: bytes = b"\x89PNG\r\n",
        fail_on: Optional[str] = None,
    ) -> None:
        self._elements = elements or []
        self._markdown = markdown
        self._png = png
        self._fail_on = fail_on
        self.navigated: Optional[str] = None
        self.clicked: Optional[int] = None
        self.filled: Optional[tuple[Optional[int], str]] = None
        self.keys: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        if self._fail_on == name:
            raise ApiError(status_code=500, headers={}, body={"message": "boom"})

    def navigate(self, *, url: str, request_options: Any = None) -> _Resp:
        self._maybe_fail("navigate")
        self.navigated = url
        return _Resp({})

    def get_elements(self, *, request_options: Any = None) -> _Resp:
        self._maybe_fail("get_elements")
        return _Resp(self._elements)

    def get_markdown(self, *, request_options: Any = None) -> _Resp:
        self._maybe_fail("get_markdown")
        return _Resp({"title": "T", "markdown": self._markdown})

    def click(self, *, index: Optional[int] = None, request_options: Any = None) -> _Resp:
        self._maybe_fail("click")
        self.clicked = index
        return _Resp({})

    def fill(self, *, text: str, index: Optional[int] = None, request_options: Any = None) -> _Resp:
        self._maybe_fail("fill")
        self.filled = (index, text)
        return _Resp({})

    def press_key(self, *, key: str, request_options: Any = None) -> _Resp:
        self._maybe_fail("press_key")
        self.keys.append(key)
        return _Resp({})

    def screenshot(self, *, request_options: Any = None):
        # PAGE screenshot — the adapter must hit ``browser_page.screenshot``,
        # NOT ``browser.screenshot`` (the container display).
        self._maybe_fail("screenshot")
        yield self._png[: len(self._png) // 2]
        yield self._png[len(self._png) // 2 :]


class FakeSandbox:
    def __init__(self, page: FakeBrowserPage) -> None:
        self.browser_page = page


_ELEMENTS = [
    {"index": 0, "tag": "a", "text": "Learn more", "href": "https://x/y"},
    {"index": 1, "tag": "button", "text": "Go"},
]


def _backend(page: FakeBrowserPage) -> SdkBrowserBackend:
    return SdkBrowserBackend(base_url=BASE, client=FakeSandbox(page))


# -- construction ----------------------------------------------------------- #


def test_empty_base_url_raises() -> None:
    with pytest.raises(AioBrowserError):
        SdkBrowserBackend(base_url="", client=FakeSandbox(FakeBrowserPage()))


# -- navigate --------------------------------------------------------------- #


def test_navigate_calls_page_and_returns_indexed_elements() -> None:
    page = FakeBrowserPage(elements=_ELEMENTS)
    out = _backend(page).navigate("https://example.com")
    assert page.navigated == "https://example.com"
    assert "[0] <a> Learn more (https://x/y)" in out
    assert "[1] <button> Go" in out


# -- click / type (index-addressed, no selector bridge) --------------------- #


def test_click_passes_index_natively() -> None:
    page = FakeBrowserPage(elements=_ELEMENTS)
    out = _backend(page).click(1)
    assert page.clicked == 1
    assert "1" in out


def test_type_fills_then_optionally_presses_enter() -> None:
    page = FakeBrowserPage()
    b = _backend(page)
    b.type(2, "hello", submit=False)
    assert page.filled == (2, "hello")
    assert page.keys == []
    b.type(2, "query", submit=True)
    assert page.keys == ["Enter"]


# -- extract ---------------------------------------------------------------- #


def test_extract_combines_markdown_and_elements() -> None:
    page = FakeBrowserPage(elements=_ELEMENTS, markdown="# Example\nbody")
    out = _backend(page).extract()
    assert "# Example\nbody" in out
    assert "# Interactive elements" in out
    assert "[0] <a> Learn more" in out


# -- screenshot ------------------------------------------------------------- #


def test_screenshot_joins_page_stream_bytes() -> None:
    out = _backend(FakeBrowserPage(png=b"\x89PNGdata")).screenshot()
    assert out == b"\x89PNGdata"


def test_screenshot_empty_raises() -> None:
    with pytest.raises(AioBrowserError):
        _backend(FakeBrowserPage(png=b"")).screenshot()


# -- fault mapping ---------------------------------------------------------- #


def test_navigate_apierror_maps_to_browsererror() -> None:
    with pytest.raises(AioBrowserError):
        _backend(FakeBrowserPage(fail_on="navigate")).navigate("https://x")


def test_screenshot_apierror_maps_to_browsererror() -> None:
    with pytest.raises(AioBrowserError):
        _backend(FakeBrowserPage(fail_on="screenshot")).screenshot()


# -- close ------------------------------------------------------------------- #


def test_close_is_noop_for_injected_client() -> None:
    backend = _backend(FakeBrowserPage())
    backend.close()  # owned pool is None with an injected client — must not raise
    backend.close()  # idempotent
