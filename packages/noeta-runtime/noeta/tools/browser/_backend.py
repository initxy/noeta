"""``BrowserBackend`` — the seam under the noeta-owned browser tool pack, and
its one concrete implementation ``AioBrowserBackend`` (the AIO Sandbox wire).

The browser tools (``browser_navigate`` / ``browser_click`` / ``browser_type``
/ ``browser_extract`` / ``browser_screenshot``) are **noeta-owned** at the
model-facing surface: their name / schema / description are pinned by noeta so
the stable prefix never drifts when the container image changes (see the
sandbox-browser-subsystem spec, D1). This module is where the *implementation*
lives: a narrow :class:`BrowserBackend` Protocol (the injection point tools call
through, and the seam tests substitute) plus :class:`AioBrowserBackend`, the
single place the container's ``/mcp`` browser wire is pinned.

Mirrors :class:`~noeta.tools.fs.exec_env.AioSandboxExecEnv`: base_url + an
optional per-call auth-header factory + an injectable transport (the internal
:class:`~noeta.tools.mcp._http_client.McpHttpClient`), with the AIO tool
names / argument shapes captured as module-level constants so a wire drift is a
one-constant change caught by the fake-transport contract test — the model-facing
schema stays byte-identical. The MCP client is used purely as an **internal
transport** here; the browser tools are NOT an MCP connector (they never enter
``mcp_registry`` / take an alias — spec D2).
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Mapping, Optional, Protocol, runtime_checkable

from noeta.tools.mcp._http_client import McpHttpClient


__all__ = [
    "AioBrowserBackend",
    "AioBrowserError",
    "BrowserBackend",
]


# --------------------------------------------------------------------------- #
# AIO Sandbox browser MCP wire (pinned against the published server source)
# --------------------------------------------------------------------------- #
#
# These name the container ``/mcp`` browser-server tools + their argument shapes.
# They are the ONE place the AIO browser wire is pinned; the noeta model-facing
# schema (the ``.md``-described browser_* tools) never moves when these change.
#
# Pinned from the authoritative source of the server AIO Sandbox bundles,
# ``@agent-infra/mcp-server-browser`` (bytedance/UI-TARS-desktop, at
# ``packages/agent-infra/mcp-servers/browser/src/{server,tools}.ts``). Two
# noeta-owned tools have NO single container counterpart and fan out to the real
# element-level primitives here — this is exactly the seam D1 buys us:
#   * ``browser_type``    → ``browser_form_input_fill`` (+ ``browser_press_key``
#                            "Enter" when ``submit``).
#   * ``browser_extract`` → ``browser_get_markdown`` (page text) followed by
#                            ``browser_get_clickable_elements`` (the numbered
#                            interactive-element list).
# The server keys every element by a numeric ``index`` from a prior
# ``browser_get_clickable_elements`` (or the list ``browser_navigate`` returns
# inline) — NOT a string ref. A live-container e2e (B8) still confirms the
# runtime *return shapes* (it cannot change these names); the fake-transport
# contract test asserts exactly what we send, so a wire drift fails loudly here
# rather than perturbing the model.
_AIO_NAVIGATE = "browser_navigate"  # args: {"url": url}; returns clickable list inline
_AIO_CLICK = "browser_click"  # args: {"index": index}
_AIO_FORM_INPUT_FILL = "browser_form_input_fill"  # args: {"index", "value", "clear"}
_AIO_PRESS_KEY = "browser_press_key"  # args: {"key": key}
_AIO_GET_MARKDOWN = "browser_get_markdown"  # args: {}; page text as markdown
_AIO_GET_CLICKABLE = "browser_get_clickable_elements"  # args: {}; numbered elements
_AIO_SCREENSHOT = "browser_screenshot"  # args: {}; returns an image content block

#: The element-reference argument key: the server addresses elements by numeric
#: ``index`` (from ``browser_get_clickable_elements``). Used for both click and
#: fill so a drift is a single-constant flip.
_AIO_INDEX_KEY = "index"

#: The key name ``browser_press_key`` uses to submit a filled field (Enter is a
#: valid ``keyInputValues`` member in the server).
_AIO_ENTER_KEY = "Enter"

#: Header prefixed to the numbered element list in an ``extract`` snapshot so the
#: model can tell the page text from the actionable element indices (spec R3
#: sanctions this backend-side shaping — the model-facing schema is untouched).
_INTERACTIVE_ELEMENTS_HEADER = "# Interactive elements"


class AioBrowserError(OSError):
    """A failed AIO Sandbox browser call (transport, protocol, or tool error).

    Subclasses :class:`OSError` so the browser tools' ``except OSError`` sites
    treat a remote browser fault exactly like any other IO failure — the tool's
    ``invoke`` maps it to ``ToolResult(success=False, ...)`` uniformly and never
    lets it escape to crash the worker (mirrors
    :class:`~noeta.tools.fs.exec_env.AioSandboxError`).
    """


@runtime_checkable
class BrowserBackend(Protocol):
    """The high-level, element-level browser surface the tool pack acts through.

    Deliberately narrow (spec D4 perception-v1): the four text methods return a
    page snapshot / action-outcome string (``extract`` gives page text + a
    numbered list of interactive elements, browser-use style), and ``screenshot``
    returns raw PNG bytes. ``click`` / ``type`` address an element by the numeric
    ``index`` a prior ``extract`` (or the list ``navigate`` returns inline) handed
    the model — no pixel coordinates. ``AioBrowserBackend`` is the production
    implementation; a test substitutes a fake to prove the tools delegate without
    touching a container.
    """

    def navigate(self, url: str) -> str: ...

    def click(self, index: int) -> str: ...

    def type(self, index: int, text: str, *, submit: bool = False) -> str: ...

    def extract(self) -> str: ...

    def screenshot(self) -> bytes: ...


class AioBrowserBackend:
    """:class:`BrowserBackend` backed by an AIO Sandbox container's ``/mcp``
    browser server over HTTP.

    Every browser action is a ``tools/call`` against the container's aggregated
    ``/mcp`` endpoint via a synchronous :class:`~noeta.tools.mcp._http_client.
    McpHttpClient` (the same single-threaded, stdlib-only transport the MCP
    connectors use — reused here as a private transport, not as a connector).
    The AIO wire contract — which ``browser_*`` tools, their argument keys, and
    the returned ``content`` block shape — is captured *only here* and pinned by
    fake-transport tests; a contract drift is a one-file change.

    Auth (spec D8): when an ``auth_headers`` factory is supplied it is invoked
    **once at construction** and its headers become the client's static headers
    (the ``McpHttpClient`` holds static headers, unlike the per-call minting the
    file/shell backend does). The backend is rebuilt per session at engine-build
    time, so the credential is fresh for that session; a short-lived-token
    refresh mid-session is a later concern. The key rides only on the wire —
    never recorded (D5). The handshake (``start``) is lazy and runs at most once.
    """

    def __init__(
        self,
        *,
        base_url: str,
        auth_headers: Optional[Callable[[], Mapping[str, str]]] = None,
        client: Optional[McpHttpClient] = None,
        timeout_s: float = 60.0,
    ) -> None:
        if not base_url:
            raise AioBrowserError("aio browser base_url is empty")
        # An explicit ``client`` is injected by tests (a fake exposing
        # ``start()`` / ``call_tool(name, args) -> dict``); production builds the
        # real MCP HTTP transport aimed at the container's ``/mcp`` endpoint,
        # folding the (once-resolved) auth headers in as static headers.
        self._client = client or McpHttpClient(
            url=base_url.rstrip("/") + "/mcp",
            headers=dict(auth_headers()) if auth_headers is not None else {},
            timeout_s=timeout_s,
        )
        self._started = False

    # -- wire ------------------------------------------------------------- #

    def _ensure_started(self) -> None:
        """Complete the MCP handshake once (lazy). Any fault → ``AioBrowserError``."""
        if self._started:
            return
        try:
            self._client.start()
        except AioBrowserError:
            raise
        except Exception as exc:  # transport / protocol handshake fault
            raise AioBrowserError(f"browser handshake failed: {exc}") from exc
        self._started = True

    def _call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Issue one ``tools/call`` and return the raw result object.

        Raises :class:`AioBrowserError` on any transport / protocol fault, on a
        non-object result, or on an ``isError: true`` result (the container
        reporting the browser action itself failed).
        """
        self._ensure_started()
        try:
            result = self._client.call_tool(name, dict(arguments))
        except AioBrowserError:
            raise
        except Exception as exc:  # any transport / protocol fault
            raise AioBrowserError(f"{name}: transport error: {exc}") from exc
        if not isinstance(result, dict):
            raise AioBrowserError(f"{name}: result is not an object")
        if result.get("isError"):
            raise AioBrowserError(
                f"{name}: {self._text(result) or 'tool error'}"
            )
        return result

    @staticmethod
    def _text(result: dict[str, Any]) -> str:
        """Concatenate the text ``content`` blocks of a ``tools/call`` result.

        Mirrors :func:`~noeta.tools.mcp.tool._result_to_tool_result`'s parsing:
        text blocks are joined with newlines; non-text blocks are ignored. This
        is the text the text methods return — a page snapshot (``extract``), the
        inline element list (``navigate``), or an action outcome (``click`` /
        ``type``).
        """
        content = result.get("content")
        parts: list[str] = []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text = block.get("text")
                    if isinstance(text, str):
                        parts.append(text)
        return "\n".join(parts)

    def _text_call(self, name: str, arguments: dict[str, Any]) -> str:
        return self._text(self._call(name, arguments))

    # -- BrowserBackend --------------------------------------------------- #

    def navigate(self, url: str) -> str:
        return self._text_call(_AIO_NAVIGATE, {"url": url})

    def click(self, index: int) -> str:
        return self._text_call(_AIO_CLICK, {_AIO_INDEX_KEY: index})

    def type(self, index: int, text: str, *, submit: bool = False) -> str:
        # No single container "type": fill the field, then optionally press Enter.
        # ``clear`` replaces any existing value (the common intent — a search box
        # or a field being re-entered).
        outcome = self._text_call(
            _AIO_FORM_INPUT_FILL,
            {_AIO_INDEX_KEY: index, "value": text, "clear": True},
        )
        if submit:
            pressed = self._text_call(_AIO_PRESS_KEY, {"key": _AIO_ENTER_KEY})
            outcome = "\n".join(part for part in (outcome, pressed) if part)
        return outcome

    def extract(self) -> str:
        # No single container "extract": the page text (markdown) plus the
        # numbered interactive-element list the model addresses by index.
        markdown = self._text_call(_AIO_GET_MARKDOWN, {})
        elements = self._text_call(_AIO_GET_CLICKABLE, {})
        sections: list[str] = []
        if markdown:
            sections.append(markdown)
        if elements:
            sections.append(f"{_INTERACTIVE_ELEMENTS_HEADER}\n{elements}")
        return "\n\n".join(sections)

    def screenshot(self) -> bytes:
        result = self._call(_AIO_SCREENSHOT, {})
        content = result.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "image":
                    data = block.get("data")
                    if isinstance(data, str):
                        try:
                            return base64.b64decode(data)
                        except (ValueError, TypeError) as exc:
                            raise AioBrowserError(
                                f"screenshot: bad base64 image data: {exc}"
                            ) from exc
        raise AioBrowserError("screenshot: response missing image content block")
