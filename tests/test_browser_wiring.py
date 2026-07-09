"""Browser subsystem — wiring the browser pack into ``build_session_inputs`` (B4).

The browser pack is a per-session, flag-gated tool set (the twin of the memory /
open_app packs): it is merged into the tool set only when BOTH a browser backend
is present (the SDK host built one off the session's sandbox handle) AND the
agent opens the ``browser`` capability. It is NOT whitelist-filtered — a
capability gates it, not the ``allowed_tools`` set — and it never touches the
tool set when either input is absent, so a non-sandbox / non-browser session's
schemas + stable prefix are byte-identical.
"""

from __future__ import annotations

from pathlib import Path

from noeta.execution.builder import build_session_inputs, derive_compaction_config
from noeta.storage.memory import InMemoryContentStore
from noeta.tools.browser import BROWSER_TOOL_NAMES

from tests._sdk_session import coding_replay_budget


_SYSTEM = "you are a coding agent"


class _FakeBrowser:
    """A structural :class:`~noeta.tools.browser.BrowserBackend` — no socket."""

    def navigate(self, url: str) -> str:
        return f"nav {url}"

    def click(self, index: int) -> str:
        return f"click {index}"

    def type(self, index: int, text: str, *, submit: bool = False) -> str:
        return f"type {index} {text} {submit}"

    def extract(self) -> str:
        return "page snapshot"

    def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\n"


def _session(
    workspace_dir: Path,
    *,
    browser_backend,
    browser_enabled,
    allowed_tools=frozenset({"read"}),
):
    return build_session_inputs(
        workspace_dir=workspace_dir,
        system_prompt=_SYSTEM,
        allowed_tools=allowed_tools,
        content_store=InMemoryContentStore(),
        model="stub-model",
        compaction=derive_compaction_config("stub-model"),
        budget=coding_replay_budget(None),
        browser_backend=browser_backend,
        browser_enabled=browser_enabled,
    )


def test_browser_tools_present_when_backend_and_enabled(tmp_path: Path) -> None:
    inputs = _session(tmp_path, browser_backend=_FakeBrowser(), browser_enabled=True)
    for name in BROWSER_TOOL_NAMES:
        assert name in inputs.tools, f"{name} should be merged"
    # all high-risk (B7 force-gates them through approval)
    for name in BROWSER_TOOL_NAMES:
        assert inputs.tools[name].risk_level == "high"


def test_browser_tools_absent_without_backend(tmp_path: Path) -> None:
    # capability on, but no sandbox backend (every non-sandbox session) ⇒ nothing.
    inputs = _session(tmp_path, browser_backend=None, browser_enabled=True)
    for name in BROWSER_TOOL_NAMES:
        assert name not in inputs.tools


def test_browser_tools_absent_when_capability_disabled(tmp_path: Path) -> None:
    # a live backend, but the agent does not open the capability ⇒ nothing.
    inputs = _session(tmp_path, browser_backend=_FakeBrowser(), browser_enabled=False)
    for name in BROWSER_TOOL_NAMES:
        assert name not in inputs.tools


def test_browser_default_session_has_no_browser_tools(tmp_path: Path) -> None:
    # the default (resume / SDK / non-browser) path: byte-identical tool set.
    inputs = _session(tmp_path, browser_backend=None, browser_enabled=False)
    assert not any(n in inputs.tools for n in BROWSER_TOOL_NAMES)


def test_browser_tools_are_flag_gated_not_whitelist_filtered(tmp_path: Path) -> None:
    # browser_* are NOT in allowed_tools, yet they appear — proving the pack is
    # gated by the capability (like memory / open_app), not the whitelist.
    inputs = _session(
        tmp_path,
        browser_backend=_FakeBrowser(),
        browser_enabled=True,
        allowed_tools=frozenset({"read"}),
    )
    assert "read" in inputs.tools
    for name in BROWSER_TOOL_NAMES:
        assert name in inputs.tools


def test_browser_pack_delegates_to_the_injected_backend(tmp_path: Path) -> None:
    from noeta.protocols.tool import ToolContext

    inputs = _session(tmp_path, browser_backend=_FakeBrowser(), browser_enabled=True)
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    result = inputs.tools["browser_navigate"].invoke({"url": "https://x.test"}, ctx=ctx)
    assert result.success
