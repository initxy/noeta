"""T1 — ``noeta.sdk.Client`` per-session workspace / per-turn model+effort widen.

The thin Client
re-exposes what the runtime/SdkHost always had — per-session ``workspace_dir``
welded into durable ``TaskHostBound`` (zero mapping: a follow-up turn
fold-resolves it), per-turn ``effort``, and a local-deployment ``allowed_models``
that widens the driver's per-turn model-selector allowlist (LOCAL_PRINCIPAL is ⊤,
so the configured model list IS the authorized set).

Acceptance:
* ``allowed_models`` lets a real (non-stub) model selector pass; absent it the
  driver's STUB allowlist still rejects it (default byte-identical).
* ``Client.start(workspace_dir=...)`` welds the absolute path into durable, and a
  follow-up ``send_goal`` (no workspace) keeps the same binding (zero mapping).
* ``effort`` flows into every turn's ``LLMRequest`` (start + send_goal).
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
    """Default Client keeps the driver's STUB allowlist — an unlisted real
    selector is refused, byte-identical to every pre-widening caller."""
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
