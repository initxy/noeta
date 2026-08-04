"""Real-LLM web tools, end to end (live marker + network opt-in).

The web pack's two tools, driven by a real model through the product path:

1. **WebFetch** — the model fetches a stable public page and the read-only GET
   round-trips as a successful tool result.
2. **WebSearch** — the model runs a query through the configured search backend
   (Tavily) and gets ranked hits back.

Unlike the rest of the live suite — which talks only to the configured gateway —
these reach arbitrary public hosts, so they carry a SECOND opt-in beyond the
usual ``NOETA_LIVE_*`` env: ``requires_live_web`` gates the whole module on
``NOETA_LIVE_WEB=1`` (see ``tests._live_env``). WebSearch additionally needs
``NOETA_WEB_SEARCH_API_KEY`` — without it the tool is not even built into the
set, so that test skips independently. Default ``uv run pytest -m live`` skips
both; CI never runs them::

    NOETA_LIVE_WEB=1 uv run pytest -m live tests/test_live_web_e2e.py
    NOETA_LIVE_WEB=1 NOETA_WEB_SEARCH_API_KEY=<tavily> \\
        uv run pytest -m live tests/test_live_web_e2e.py

WebFetch targets ``https://example.com`` — IANA-maintained, tiny, maximally
stable. Assertions watch **structural** invariants (tool success, >= 1 hit),
never page content, which changes out from under a test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode

from tests import _live_env
from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    runner_main_spec,
)

# The whole module is both live-marked (deselected by default) AND network-gated.
pytestmark = [pytest.mark.live, _live_env.requires_live_web]


def _model() -> str:
    return _live_env.live_model() or ""


def _web_session(ws: Path):
    """A one-shot main session; the web pack rides main's default tool set."""
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=_live_env.build_anthropic_provider(),
        model=_model(),
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
    )
    return host, make_driver(host)


def _tool_succeeded(host, task_id: str, tool_name: str) -> bool:
    """True iff a call to ``tool_name`` on this stream recorded a success."""
    events = list(host.event_log.read(task_id))
    ids = {
        e.payload.call_id
        for e in events
        if e.type == "ToolCallStarted" and e.payload.tool_name == tool_name
    }
    return any(
        e.type == "ToolResultRecorded"
        and e.payload.call_id in ids
        and e.payload.success
        for e in events
    )


# ---------------------------------------------------------------------------
# Loop 1 — WebFetch (read-only GET, no key needed)
# ---------------------------------------------------------------------------


def test_live_web_fetch(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _web_session(ws)
    out = driver.start(
        goal=(
            "Use the WebFetch tool to fetch https://example.com and then tell me "
            "the page's main heading."
        ),
        agent="main",
    )
    assert out.status == "terminal", out.status
    assert _tool_succeeded(host, out.task_id, "WebFetch"), (
        "model never made a successful WebFetch call"
    )


# ---------------------------------------------------------------------------
# Loop 2 — WebSearch (needs NOETA_WEB_SEARCH_API_KEY, else the tool is absent)
# ---------------------------------------------------------------------------


@_live_env.requires_live_web_search
def test_live_web_search(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    host, driver = _web_session(ws)
    out = driver.start(
        goal=(
            "Use the WebSearch tool to search for 'official Python website', then "
            "reply with the top result's URL."
        ),
        agent="main",
    )
    assert out.status == "terminal", out.status
    assert _tool_succeeded(host, out.task_id, "WebSearch"), (
        "model never made a successful WebSearch call"
    )
