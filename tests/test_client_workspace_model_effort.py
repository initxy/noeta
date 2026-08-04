"""``Client`` per-session workspace and per-turn model + effort selection.

Three couplings must hold for a session to be steerable per turn:

* ``allowed_models`` widens the driver's per-turn model-selector allowlist.
  LOCAL_PRINCIPAL is ⊤, so the configured model list IS the authorized set; a
  real (non-stub) selector passes only when listed, otherwise the STUB
  allowlist rejects it.
* ``Client.start(workspace_dir=...)`` welds the absolute path into the durable
  fold, and a follow-up ``send_goal`` with no workspace re-resolves the same
  binding from the fold — no caller-side mapping to carry it forward.
* ``effort`` flows into every turn's ``LLMRequest`` (start and send_goal alike).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.client import Client, Options
from noeta.core.fold import fold
from noeta.execution.driver import ModelSelectorError
from noeta.protocols.messages import LLMResponse, TextBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider


def _end_turn(req=None) -> LLMResponse:  # noqa: ANN001 — responder shape
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="done")],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _ws(tmp_path: Path, name: str) -> Path:
    d = tmp_path / name
    d.mkdir()
    return d


def _client(tmp_path: Path, **kw) -> tuple[Client, FakeLLMProvider]:
    provider = FakeLLMProvider(responder=_end_turn)
    client = Client(
        Options(system_prompt="test agent", name="main"),
        provider=provider,
        workspace_dir=_ws(tmp_path, "default_ws"),
        model="gpt-test",
        **kw,
    )
    return client, provider


# ---------------------------------------------------------------------------
# allowed_models widens the per-turn model-selector allowlist
# ---------------------------------------------------------------------------


def test_allowed_models_lets_real_selector_pass(tmp_path: Path) -> None:
    client, _ = _client(tmp_path, allowed_models=["gpt-test", "gpt-other"])
    try:
        out = client.start(goal="hi", model_selector="gpt-other")
        assert out.status == "suspended"
        folded = fold(
            client._host.event_log, client._host.content_store, out.task_id
        )
        # gpt-other is not a known alias → resolve_alias passes it through.
        assert folded.governance.model_binding == "gpt-other"
    finally:
        client.shutdown()


def test_without_allowed_models_rejects_unlisted_selector(tmp_path: Path) -> None:
    """Without ``allowed_models`` the driver keeps the STUB allowlist, so an
    unlisted real selector is refused."""
    client, _ = _client(tmp_path)  # no allowed_models
    try:
        with pytest.raises(ModelSelectorError):
            client.start(goal="hi", model_selector="gpt-other")
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# per-session workspace welded into durable; resume fold-resolves it (zero map)
# ---------------------------------------------------------------------------


def test_start_welds_workspace_and_resume_keeps_binding(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    proj = _ws(tmp_path, "proj_a")
    try:
        out = client.start(goal="first", workspace_dir=str(proj))
        folded = fold(
            client._host.event_log, client._host.content_store, out.task_id
        )
        assert folded.governance.workspace == str(proj)

        # Follow-up turn passes NO workspace — the binding must persist
        # (the resolver re-reads it from the durable fold each turn).
        client.send_goal(out.task_id, goal="second")
        folded2 = fold(
            client._host.event_log, client._host.content_store, out.task_id
        )
        assert folded2.governance.workspace == str(proj)
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# per-turn effort flows into the LLMRequest (start + send_goal)
# ---------------------------------------------------------------------------


def test_effort_threads_into_every_turn_request(tmp_path: Path) -> None:
    client, provider = _client(tmp_path)
    try:
        out = client.start(goal="first", effort="high")
        assert provider.received_requests, "the opening turn made no LLM call"
        assert provider.received_requests[-1].effort == "high"

        client.send_goal(out.task_id, goal="second", effort="low")
        assert provider.received_requests[-1].effort == "low"
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# Options.compaction_model bridges Client -> SdkHost -> summarize request
# ---------------------------------------------------------------------------


def test_compaction_model_bridges_options_to_the_summarize_request(
    tmp_path: Path,
) -> None:
    """The one end-to-end proof of the D7 bridge: ``Options.compaction_model``
    reaches the ReActPolicy through ``Client`` -> ``SdkHost`` ->
    ``build_session_inputs`` with the ALIAS RESOLVED at the host boundary, and
    routes ONLY the summarize round-trip.

    Also exercises the unknown-model fallback: the uncatalogued ``gpt-test``
    still owns a compaction window (the conservative 128K), so proactive
    compaction is live for it. The FakeLLM reports ~1 input token per turn, so
    the density clamp floors at 0.25 and the protected tail spans ~146K
    estimate tokens — the loop below simply feeds big goals until the raw
    history outgrows that and the proactive pass produces a real summarize
    call (earlier passes short-circuit on ``compaction_no_progress``, which
    the driver converts to a next-goal suspend, so the conversation survives
    them)."""

    def _summarize_call(req) -> bool:  # noqa: ANN001 — request shape
        if req.system is None or not req.system.content:
            return False
        return getattr(req.system.content[0], "text", "").startswith(
            "Summarize the conversation"
        )

    def _responder(req):  # noqa: ANN001 — responder shape
        if _summarize_call(req):
            return LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="1. Primary Request & Intent: hi")],
                usage=Usage(uncached=1, output=1),
                raw={"id": "note"},
            )
        return _end_turn(req)

    provider = FakeLLMProvider(responder=_responder)
    client = Client(
        Options(
            system_prompt="test agent", name="main", compaction_model="haiku"
        ),
        provider=provider,
        workspace_dir=_ws(tmp_path, "compaction_ws"),
        model="gpt-test",
    )
    try:
        # ~12K estimate tokens per goal.
        filler = "lorem ipsum dolor sit amet " * 1800
        out = client.start(goal=filler)

        def _summaries() -> list:
            return [
                req
                for req in provider.received_requests
                if _summarize_call(req)
            ]

        for _ in range(24):
            if _summaries():
                break
            client.send_goal(out.task_id, goal=filler)
        summarize_calls = _summaries()
        models = [req.model for req in provider.received_requests]
        assert summarize_calls, f"no summarize round-trip observed: {models}"
        # The alias resolved at the host boundary, not the raw "haiku" string.
        assert all(
            req.model == "claude-haiku-4-5" for req in summarize_calls
        ), models
        # Every non-summarize (decide) turn keeps the session model.
        assert all(
            req.model == "gpt-test"
            for req in provider.received_requests
            if not _summarize_call(req)
        ), models
    finally:
        client.shutdown()
