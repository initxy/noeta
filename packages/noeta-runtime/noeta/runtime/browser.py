"""``BrowserBackend`` — the kernel seam under the noeta-owned browser tool pack.

The browser tools (``browser_navigate`` / ``browser_click`` / ``browser_type``
/ ``browser_extract`` / ``browser_screenshot``) are **noeta-owned** at the
model-facing surface: their name / schema / description are pinned by noeta so
the stable prefix never drifts when the container image changes (see the
sandbox-browser-subsystem spec, D1). This module is the kernel band's half of
that split (microkernel M3 — sunk here from ``noeta.tools.browser``, the M1
``runtime.exec_env`` precedent): only the narrow :class:`BrowserBackend`
Protocol (the injection point tools call through, the builder's
``browser_backend`` parameter type, and the seam tests' substitute). The tool
pack lives in the ``browser`` built-in plugin
(``noeta.builtins.browser.impl``); the one concrete backend — the AIO Sandbox
``/mcp`` adapter — lives in the ``sandbox`` built-in plugin
(``noeta.builtins.sandbox.impl.browser``), both resolved through the loader's
dynamic-import doorway.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


__all__ = [
    "BrowserBackend",
]


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
