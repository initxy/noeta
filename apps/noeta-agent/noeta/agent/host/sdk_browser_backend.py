"""``SdkBrowserBackend`` — the AIO Sandbox browser backend over the official SDK.

The product-layer replacement for :class:`~noeta.tools.browser._backend.AioBrowserBackend`
(the container ``/mcp`` browser wire). It implements the same narrow
:class:`~noeta.tools.browser._backend.BrowserBackend` surface
(``navigate`` / ``click`` / ``type`` / ``extract`` / ``screenshot``), so the
noeta-owned browser tool schemas — and the stable prefix — are unchanged; only
the wire moves to the official ``agent-sandbox`` ``browser_page`` REST client.

The one semantic point worth calling out: noeta addresses interactive elements
by the numeric ``index`` a prior ``extract`` (or ``navigate``) handed the model.
The SDK's ``browser_page.click`` / ``fill`` accept that ``index`` **natively**
(each element in ``get_elements`` carries its own ``index``), so there is no
selector bridge — ``click(index)`` maps straight to
``browser_page.click(index=index)``.

Auth (D8) and error mapping mirror :class:`~noeta.agent.host.sdk_sandbox_exec_env.SdkSandboxExecEnv`:
a per-call header factory, and SDK :class:`ApiError` / transport faults re-raised
as :class:`~noeta.tools.browser._backend.AioBrowserError` (an ``OSError``) so the
browser tools' ``except OSError`` maps every fault to a clean
``ToolResult(success=False)``. See
``docs/implementation-specs/2026-07-10-sandbox-sdk-adapters.md``.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional

import httpx
from agent_sandbox import Sandbox
from agent_sandbox.core.api_error import ApiError

# The AIO error type is runtime-internal (NOT on the ``noeta.sdk`` public
# surface). This module is one of the two pinned import-linter exemptions that
# may reach it directly (see the execution-environment-seam ADR, "SDK-adapter
# export surface", and the ``app-uses-only-sdk`` contract).
from noeta.tools.browser import AioBrowserError


__all__ = ["SdkBrowserBackend"]

#: Header prefixed to the numbered element list in an ``extract`` snapshot, kept
#: identical to :mod:`noeta.tools.browser._backend` so the shaping the model sees
#: does not move with the backend swap.
_INTERACTIVE_ELEMENTS_HEADER = "# Interactive elements"

_DEFAULT_BROWSER_TIMEOUT_S = 60.0


class SdkBrowserBackend:
    """:class:`~noeta.tools.browser._backend.BrowserBackend` over ``agent-sandbox``.

    Every action is a ``browser_page`` call on a per-session
    :class:`agent_sandbox.Sandbox`. ``client`` is injected by tests (a fake
    exposing ``.browser_page``); production builds the real client with a
    ``trust_env=False`` httpx client (never route a loopback container call
    through an ambient proxy).
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth_headers: Optional[Callable[[], Mapping[str, str]]] = None,
        timeout_s: float = _DEFAULT_BROWSER_TIMEOUT_S,
        client: Optional[Sandbox] = None,
    ) -> None:
        if not base_url:
            raise AioBrowserError("aio browser base_url is empty")
        self._auth_headers = auth_headers
        # ``_httpx_client`` is kept only when this instance built (and therefore
        # owns) the pool, so ``close`` never touches an injected test client.
        self._httpx_client: Optional[httpx.Client] = None
        if client is None:
            self._httpx_client = httpx.Client(timeout=timeout_s, trust_env=False)
            client = Sandbox(
                base_url=base_url.rstrip("/"), httpx_client=self._httpx_client
            )
        self._client: Sandbox = client

    def close(self) -> None:
        """Release the owned HTTP connection pool (idempotent, never raises).

        Called by ``SandboxExecEnvManager`` when a session's browser backend is
        evicted (release / teardown); a no-op for an injected test client.
        """
        if self._httpx_client is not None:
            try:
                self._httpx_client.close()
            except Exception:  # noqa: BLE001 — teardown path must not raise
                pass

    # -- request options + error mapping ---------------------------------- #

    def _request_options(self) -> Optional[dict[str, Any]]:
        if self._auth_headers is None:
            return None
        headers = self._auth_headers()
        return {"additional_headers": dict(headers)} if headers else None

    def _guard(self, action: str, fn: Callable[[], Any]) -> Any:
        """Run one SDK browser call, re-mapping every fault to AioBrowserError."""
        try:
            return fn()
        except AioBrowserError:
            raise
        except ApiError as exc:
            raise AioBrowserError(f"{action}: {self._api_error_message(exc)}") from exc
        except httpx.TimeoutException as exc:
            raise AioBrowserError(f"{action}: timed out: {exc}") from exc
        except Exception as exc:  # any other transport / protocol fault
            raise AioBrowserError(f"{action}: transport error: {exc}") from exc

    @staticmethod
    def _api_error_message(exc: ApiError) -> str:
        body = exc.body
        if isinstance(body, Mapping):
            message = body.get("message")
            if isinstance(message, str) and message:
                return message
        if isinstance(body, str) and body:
            return body
        return f"browser request failed (status {exc.status_code})"

    # -- element-list shaping --------------------------------------------- #

    def _elements_text(self) -> str:
        """The numbered interactive-element list the model addresses by index.

        Rendered from ``browser_page.get_elements`` — each element already
        carries its own ``index`` (the same key ``click`` / ``type`` take).
        """
        result = self._guard(
            "get_elements", lambda: self._client.browser_page.get_elements(
                request_options=self._request_options()
            )
        )
        elements = getattr(result, "data", None)
        if not isinstance(elements, list):
            return ""
        lines: list[str] = []
        for el in elements:
            if not isinstance(el, Mapping):
                continue
            index = el.get("index")
            tag = el.get("tag") or "element"
            text = (el.get("text") or el.get("placeholder") or "").strip()
            href = el.get("href")
            label = f"[{index}] <{tag}> {text}".rstrip()
            if href:
                label = f"{label} ({href})"
            lines.append(label)
        return "\n".join(lines)

    # -- BrowserBackend --------------------------------------------------- #

    def navigate(self, url: str) -> str:
        self._guard(
            "navigate", lambda: self._client.browser_page.navigate(
                url=url, request_options=self._request_options()
            )
        )
        # Mirror the old backend: navigate hands back the inline clickable list so
        # the model can act on the freshly loaded page without a second call.
        return self._elements_text()

    def click(self, index: int) -> str:
        self._guard(
            "click", lambda: self._client.browser_page.click(
                index=index, request_options=self._request_options()
            )
        )
        return f"clicked element {index}"

    def type(self, index: int, text: str, *, submit: bool = False) -> str:
        # No single container "type": fill the field, then optionally press Enter.
        self._guard(
            "fill", lambda: self._client.browser_page.fill(
                text=text, index=index, request_options=self._request_options()
            )
        )
        outcome = f"typed into element {index}"
        if submit:
            self._guard(
                "press_key", lambda: self._client.browser_page.press_key(
                    key="Enter", request_options=self._request_options()
                )
            )
            outcome = f"{outcome}; pressed Enter"
        return outcome

    def extract(self) -> str:
        result = self._guard(
            "get_markdown", lambda: self._client.browser_page.get_markdown(
                request_options=self._request_options()
            )
        )
        data = getattr(result, "data", None)
        markdown = ""
        if isinstance(data, Mapping):
            markdown = str(data.get("markdown") or "")
        elements = self._elements_text()
        sections: list[str] = []
        if markdown:
            sections.append(markdown)
        if elements:
            sections.append(f"{_INTERACTIVE_ELEMENTS_HEADER}\n{elements}")
        return "\n\n".join(sections)

    def screenshot(self) -> bytes:
        # ``browser_page.screenshot`` captures the PAGE — the old MCP
        # ``browser_screenshot`` semantics. (``browser.screenshot`` would capture
        # the container's virtual DISPLAY instead — wrong artifact.) It streams:
        # the HTTP fault surfaces during iteration, so the ``b"".join`` must run
        # INSIDE the guard for it to map to AioBrowserError.
        data = self._guard(
            "screenshot", lambda: b"".join(
                self._client.browser_page.screenshot(
                    request_options=self._request_options()
                )
            )
        )
        if not data:
            raise AioBrowserError("screenshot: empty response")
        return data
