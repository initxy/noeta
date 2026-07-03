"""Tests for ``Options.provider`` + ``Client`` provider fallback.

Covers:

1. Provider is **not** part of identity: two Options differing only in
   ``provider`` compile to structurally equal specs.
2. ``Client`` accepts a provider via ``Options.provider`` and runs a happy-path
   query (no explicit ``provider=`` kwarg).
3. ``Client(provider=...)`` kwarg takes precedence over ``Options.provider``.
4. Both missing → ``ValueError``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.client import AgentDefinition, Client, Options, compile_options, query
from noeta.protocols.events import (
    ModelBoundPayload,
    TaskCompletedPayload,
)
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    Usage,
)
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _end_script(text: str) -> list[LLMResponse]:
    return [
        LLMResponse(
            stop_reason="end_turn",
            content=[TextBlock(text=text)],
            usage=Usage(uncached=1, output=1),
            raw={"id": f"end-{text}"},
        ),
    ]


def _make_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


# ---------------------------------------------------------------------------
# Case 1 — provider excluded from identity
# ---------------------------------------------------------------------------


def test_provider_excluded_from_identity(tmp_path: Path) -> None:
    """Two Options differing only in ``provider`` → structurally equal specs."""
    ws = _make_ws(tmp_path)
    provider_a = FakeLLMProvider(responses=_end_script("A"))
    opts_a = Options(system_prompt="be terse", name="main", provider=None)
    opts_b = Options(system_prompt="be terse", name="main", provider=provider_a)
    main_a, _ = compile_options(opts_a)
    main_b, _ = compile_options(opts_b)
    assert main_a == main_b
    # sanity: identity also doesn't depend on the *value* of provider
    provider_b = FakeLLMProvider(responses=_end_script("B"))
    opts_c = Options(system_prompt="be terse", name="main", provider=provider_b)
    main_c, _ = compile_options(opts_c)
    assert main_a == main_c


# ---------------------------------------------------------------------------
# Case 2 — Options.provider fallback runs happy path
# ---------------------------------------------------------------------------


def test_options_provider_fallback_runs_query(tmp_path: Path) -> None:
    """Client with ``Options.provider`` (no kwarg) drives a one-shot query."""
    ws = _make_ws(tmp_path)
    provider = FakeLLMProvider(responses=_end_script("hello from fallback"))
    opts = Options(
        system_prompt="be terse",
        name="main",
        provider=provider,
    )
    # query() sugar — no explicit provider= kwarg
    envelopes = query(opts, goal="say hi", workspace_dir=ws)
    completed = [e for e in envelopes if e.type == "TaskCompleted"]
    assert len(completed) == 1
    payload = completed[0].payload
    assert isinstance(payload, TaskCompletedPayload)
    assert "hello from fallback" in str(payload.answer)


# ---------------------------------------------------------------------------
# Case 3 — Client(provider=...) kwarg takes precedence
# ---------------------------------------------------------------------------


def test_client_provider_kwarg_takes_precedence(tmp_path: Path) -> None:
    """Explicit ``provider=`` kwarg wins over ``Options.provider``."""
    ws = _make_ws(tmp_path)
    opts_provider = FakeLLMProvider(responses=_end_script("OPTS_PROVIDER_WAS_USED"))
    kwarg_provider = FakeLLMProvider(responses=_end_script("KWARG_PROVIDER_WAS_USED"))
    opts = Options(
        system_prompt="be terse",
        name="main",
        provider=opts_provider,
    )
    envelopes = query(
        opts,
        goal="say hi",
        provider=kwarg_provider,  # explicit kwarg should win
        workspace_dir=ws,
    )
    completed = [e for e in envelopes if e.type == "TaskCompleted"]
    assert len(completed) == 1
    payload = completed[0].payload
    assert isinstance(payload, TaskCompletedPayload)
    assert "KWARG_PROVIDER_WAS_USED" in str(payload.answer)
    assert "OPTS_PROVIDER_WAS_USED" not in str(payload.answer)


# ---------------------------------------------------------------------------
# Case 4 — both missing → ValueError
# ---------------------------------------------------------------------------


def test_missing_provider_raises_value_error(tmp_path: Path) -> None:
    """Neither kwarg nor Options.provider → ValueError."""
    ws = _make_ws(tmp_path)
    opts = Options(system_prompt="be terse", name="main")
    with pytest.raises(ValueError, match="a provider is required"):
        Client(opts, workspace_dir=ws)
    # query() sugar, same contract
    with pytest.raises(ValueError, match="a provider is required"):
        query(opts, goal="hi", workspace_dir=ws)


# ---------------------------------------------------------------------------
# Per-agent default model → child ModelBound
# ---------------------------------------------------------------------------


def _spawn(agent: str, goal: str = "child goal") -> LLMResponse:
    from noeta.policies.react import SPAWN_SUBAGENT_TOOL
    from noeta.protocols.messages import ToolUseBlock

    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id="s1",
                tool_name=SPAWN_SUBAGENT_TOOL,
                arguments={"agent": agent, "goal": goal},
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "s1"},
    )


def test_subagent_default_model_binds_child(tmp_path: Path) -> None:
    """A flat-dict child declaring ``model="haiku-test"`` gets its own
    opening ModelBound (written by the drain before the goal seed) and its
    LLM traffic runs on that model; the parent keeps exactly its own
    opening binding — no leak in either direction."""
    ws = _make_ws(tmp_path)
    main = Options(
        system_prompt="delegate everything",
        agents={
            "helper": AgentDefinition(
                description="test helper",
                prompt="you are the helper",
                model="haiku-test",
            ),
        },
        provider=FakeLLMProvider(
            responses=[
                _spawn("helper"),  # parent turn 1: spawn the child
                *_end_script("child-out"),  # the child's single turn
                *_end_script("parent-out"),  # parent resumes post-drain
            ]
        ),
    )
    client = Client(main, workspace_dir=ws, multi_turn=False)
    try:
        outcome = client.start(goal="go")
        parent_events = client.events(outcome.task_id)
        spawned = [e for e in parent_events if e.type == "SubtaskSpawned"]
        assert spawned, "parent must have spawned the helper child"
        child_id = spawned[0].payload.subtask_id

        child_events = client.events(child_id)
        child_bound = [e for e in child_events if e.type == "ModelBound"]
        assert [b.payload.model for b in child_bound] == ["haiku-test"]
        assert child_bound[0].payload.principal_identity == "agent-default"
        # The child's LLM traffic actually runs on the declared model.
        child_reqs = [e for e in child_events if e.type == "LLMRequestStarted"]
        assert child_reqs and all(
            getattr(e.payload, "model", None) == "haiku-test"
            for e in child_reqs
        )
        # The parent keeps exactly its own opening binding.
        parent_models = [
            e.payload.model for e in parent_events if e.type == "ModelBound"
        ]
        assert "haiku-test" not in parent_models
    finally:
        client.shutdown()


def test_subagent_without_default_model_gets_no_extra_binding(
    tmp_path: Path,
) -> None:
    """No declared default → no ModelBound on the child (old behaviour,
    byte-identical recordings); the child runs on the host model."""
    ws = _make_ws(tmp_path)
    main = Options(
        system_prompt="delegate",
        agents={
            "helper": AgentDefinition(description="test helper", prompt="helper"),
        },
        provider=FakeLLMProvider(
            responses=[
                _spawn("helper"),
                *_end_script("child"),
                *_end_script("parent"),
            ]
        ),
    )
    client = Client(main, workspace_dir=ws, multi_turn=False)
    try:
        outcome = client.start(goal="go")
        parent_events = client.events(outcome.task_id)
        child_id = next(
            e.payload.subtask_id
            for e in parent_events
            if e.type == "SubtaskSpawned"
        )
        child_events = client.events(child_id)
        assert not [e for e in child_events if e.type == "ModelBound"]
    finally:
        client.shutdown()


def test_subagent_inherits_parent_session_model_binding(tmp_path: Path) -> None:
    """A session opened on a NON-default model selector propagates its
    binding down the delegation tree: a child with no declared default gets
    its own opening ModelBound (identity ``"inherited"``) and its LLM traffic
    runs on the session model, not the host default."""
    ws = _make_ws(tmp_path)
    main = Options(
        system_prompt="delegate",
        agents={
            "helper": AgentDefinition(description="test helper", prompt="helper"),
        },
        provider=FakeLLMProvider(
            responses=[
                _spawn("helper"),
                *_end_script("child"),
                *_end_script("parent"),
            ]
        ),
    )
    client = Client(
        main,
        workspace_dir=ws,
        multi_turn=False,
        allowed_models=["session-model"],
    )
    try:
        outcome = client.start(goal="go", model_selector="session-model")
        parent_events = client.events(outcome.task_id)
        child_id = next(
            e.payload.subtask_id
            for e in parent_events
            if e.type == "SubtaskSpawned"
        )
        child_events = client.events(child_id)
        child_bound = [e for e in child_events if e.type == "ModelBound"]
        assert [b.payload.model for b in child_bound] == ["session-model"]
        assert child_bound[0].payload.principal_identity == "inherited"
        # The child's LLM traffic actually runs on the inherited model.
        child_reqs = [e for e in child_events if e.type == "LLMRequestStarted"]
        assert child_reqs and all(
            getattr(e.payload, "model", None) == "session-model"
            for e in child_reqs
        )
    finally:
        client.shutdown()


def test_subagent_declared_default_beats_inherited_binding(
    tmp_path: Path,
) -> None:
    """A child agent's own declared default model wins over the parent's
    session binding."""
    ws = _make_ws(tmp_path)
    main = Options(
        system_prompt="delegate",
        agents={
            "helper": AgentDefinition(
                description="test helper",
                prompt="helper",
                model="haiku-test",
            ),
        },
        provider=FakeLLMProvider(
            responses=[
                _spawn("helper"),
                *_end_script("child"),
                *_end_script("parent"),
            ]
        ),
    )
    client = Client(
        main,
        workspace_dir=ws,
        multi_turn=False,
        allowed_models=["session-model"],
    )
    try:
        outcome = client.start(goal="go", model_selector="session-model")
        parent_events = client.events(outcome.task_id)
        child_id = next(
            e.payload.subtask_id
            for e in parent_events
            if e.type == "SubtaskSpawned"
        )
        child_events = client.events(child_id)
        child_bound = [e for e in child_events if e.type == "ModelBound"]
        assert [b.payload.model for b in child_bound] == ["haiku-test"]
        assert child_bound[0].payload.principal_identity == "agent-default"
    finally:
        client.shutdown()


# ---------------------------------------------------------------------------
# Case 6 — output_schema / thinking / effort injection chain + structured-answer parsing
# ---------------------------------------------------------------------------


def test_output_schema_e2e_valid_json_parsed_to_dict(tmp_path: Path) -> None:
    """Options.output_schema set → a valid-JSON answer is parsed into a dict."""
    ws = _make_ws(tmp_path)
    schema = {
        "type": "object",
        "properties": {"city": {"type": "string"}, "temp_c": {"type": "number"}},
    }
    provider = FakeLLMProvider(
        responses=_end_script(
            '{"city": "Shenzhen", "temp_c": 28.5, "tags": ["sunny", "humid"]}'
        )
    )
    opts = Options(
        system_prompt="be terse",
        name="main",
        provider=provider,
        output_schema=schema,
    )
    envelopes = query(opts, goal="What is the weather?", workspace_dir=ws)
    completed = [e for e in envelopes if e.type == "TaskCompleted"]
    assert len(completed) == 1
    payload = completed[0].payload
    assert isinstance(payload, TaskCompletedPayload)
    # answer has been parsed into a Python object
    assert payload.answer == {
        "city": "Shenzhen",
        "temp_c": 28.5,
        "tags": ["sunny", "humid"],
    }
    # Injection chain check: the request FakeLLMProvider received carried the schema
    assert len(provider.received_requests) == 1
    assert provider.received_requests[0].output_schema == schema


def test_output_schema_e2e_invalid_json_falls_back_to_string(tmp_path: Path) -> None:
    """Options.output_schema set → invalid JSON doesn't raise; answer stays the raw string."""
    ws = _make_ws(tmp_path)
    provider = FakeLLMProvider(responses=_end_script("{not valid json"))
    opts = Options(
        system_prompt="be terse",
        name="main",
        provider=provider,
        output_schema={"type": "object"},
    )
    envelopes = query(opts, goal="What is the weather?", workspace_dir=ws)
    completed = [e for e in envelopes if e.type == "TaskCompleted"]
    assert len(completed) == 1
    payload = completed[0].payload
    assert isinstance(payload, TaskCompletedPayload)
    # Parse failed, so the raw text is preserved
    assert payload.answer == "{not valid json"


def test_thinking_and_effort_propagated_to_llm_request(tmp_path: Path) -> None:
    """Options.thinking / effort → the LLMRequest FakeLLMProvider receives carries the same values."""
    ws = _make_ws(tmp_path)
    provider = FakeLLMProvider(responses=_end_script("hi"))
    opts = Options(
        system_prompt="be terse",
        name="main",
        provider=provider,
        thinking="adaptive",
        effort="high",
    )
    query(opts, goal="say hi", workspace_dir=ws)
    assert len(provider.received_requests) == 1
    req = provider.received_requests[0]
    assert req.thinking == "adaptive"
    assert req.effort == "high"
    assert req.output_schema is None  # unset field stays None (omit_none in effect)

