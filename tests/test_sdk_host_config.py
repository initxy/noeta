"""Stage A acceptance — the noeta.sdk ``HostConfig`` host-level wiring surface (D3).

D3: host-level wiring (durable storage + the preview/MCP runtime injections)
goes through HostConfig, NOT Options — so it never touches the agent identity.
``HostConfig()`` reproduces the historical in-memory, no-preview, no-MCP path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from noeta.sdk import Client, HostConfig, Options
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.tools.decorator import tool


def _finishing_provider() -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="ok")],
                usage=Usage(uncached=1, output=1),
            )
        ]
    )


def _options() -> Options:
    return Options(
        system_prompt="finish",
        name="main",
        allowed_tools=(),
        permission_mode="bypassPermissions",
    )


def test_empty_host_config_is_inert() -> None:
    hc = HostConfig()
    assert hc.storage_triple() is None
    assert hc.app_gateway is None
    assert hc.mcp_server_resolver is None
    assert hc.workflow_allowed is False
    assert hc.instructions_discovery is False
    assert hc.environment_enabled is True


def test_storage_triple_is_all_or_none() -> None:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    # Partial triple is a hard error: the three are constructed together.
    with pytest.raises(ValueError):
        HostConfig(event_log=log, dispatcher=disp).storage_triple()


def test_injected_storage_triple_is_used(tmp_path: Path) -> None:
    # Build an external in-memory triple and drive a turn through it; the events
    # must land in OUR log (proving the Client used the injected storage, not a
    # freshly-built internal one).
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    store = InMemoryContentStore()
    client = Client(
        _options(),
        provider=_finishing_provider(),
        workspace_dir=tmp_path,
        host_config=HostConfig(
            event_log=log, content_store=store, dispatcher=disp
        ),
    )
    try:
        outcome = client.start(goal="hi")
        # The injected log, read directly, sees this task's stream.
        injected = list(log.read(outcome.task_id))
        assert injected, "injected event_log holds no events"
        assert injected == client.events(outcome.task_id)
        assert client._host.event_log is log
        assert client._host.content_store is store
    finally:
        client.shutdown()


def test_storage_path_and_explicit_triple_are_mutually_exclusive() -> None:
    # Two ways to name a store would name two different stores; the loud error
    # is the whole point of the one-string form (D9).
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    with pytest.raises(ValueError, match="EITHER storage_path"):
        HostConfig(
            storage_path=":memory:",
            event_log=log,
            content_store=InMemoryContentStore(),
            dispatcher=disp,
        ).storage_triple()


def test_query_with_storage_path_round_trips_a_durable_sqlite_run(
    tmp_path: Path,
) -> None:
    """The one-string durable form, through the sugar path (D6 + D9).

    ``query()`` used to be mutually exclusive with durable storage, and
    ``HostConfig`` used to demand a hand-built triple. Together they mean a
    one-shot call can record to a sqlite file: the assertion re-opens that file
    with a SECOND stack and finds the run, which is what "durable" has to mean.
    """
    from noeta.sdk import query
    from noeta.sdk.storage import open_storage_stack

    db = tmp_path / "run.sqlite"
    result = query(
        _options(),
        goal="hi",
        provider=_finishing_provider(),
        workspace_dir=tmp_path,
        host_config=HostConfig(storage_path=str(db)),
    )
    assert result.answer() == "ok"
    assert db.exists(), "storage_path did not create the sqlite file"

    event_log, content_store, dispatcher = open_storage_stack(str(db))
    try:
        replayed = event_log.read(result.task_id)
        assert replayed, "the durable log holds no events for the query's task"
        assert [e.type for e in replayed][0] == "TaskCreated"
        assert "TaskCompleted" in {e.type for e in replayed}
    finally:
        for obj in (event_log, content_store, dispatcher):
            close = getattr(obj, "close", None)
            if callable(close):
                close()


def test_host_injections_reach_the_host(tmp_path: Path) -> None:
    # app_gateway / mcp_server_resolver / workflow flag are host-level (not
    # Options): they reach the SdkHost verbatim.
    sentinel_gateway = object()

    def resolver(alias: str):  # noqa: ANN202 — test stub
        return None

    def headers(ctx):  # noqa: ANN001, ANN202 — test stub
        return {"extra": ctx.task_id}

    client = Client(
        _options(),
        provider=_finishing_provider(),
        workspace_dir=tmp_path,
        host_config=HostConfig(
            app_gateway=sentinel_gateway,  # type: ignore[arg-type]
            mcp_server_resolver=resolver,
            provider_headers=headers,
            workflow_allowed=True,
            instructions_discovery=True,
            environment_enabled=False,
        ),
    )
    try:
        assert client._host.app_gateway is sentinel_gateway
        assert client._host.mcp_server_resolver is resolver
        assert client._host.provider_headers is headers
        assert client._host.workflow_allowed is True
        assert client._host.instructions_discovery is True
        assert client._host.environment_enabled is False
    finally:
        client.shutdown()


def test_host_config_excluded_from_agent_identity(tmp_path: Path) -> None:
    # Two clients differing only in HostConfig compile the SAME AgentSpec
    # identity (HostConfig is wiring, never identity).
    plain = Client(
        _options(), provider=_finishing_provider(), workspace_dir=tmp_path
    )
    wired = Client(
        _options(),
        provider=_finishing_provider(),
        workspace_dir=tmp_path,
        host_config=HostConfig(workflow_allowed=True),
    )
    try:
        plain_spec = plain.registry.resolve(plain.main_agent_name)
        wired_spec = wired.registry.resolve(wired.main_agent_name)
        assert plain_spec == wired_spec
    finally:
        plain.shutdown()
        wired.shutdown()


def test_write_mode_defaults_to_dry_run() -> None:
    # The safe default: no config means no real writes.
    assert HostConfig().write_mode == "dry_run"


def test_write_mode_accepts_the_two_legal_values() -> None:
    assert HostConfig(write_mode="dry_run").write_mode == "dry_run"
    assert HostConfig(write_mode="apply").write_mode == "apply"


def test_invalid_write_mode_raises_at_construction() -> None:
    # A typo used to fall back to dry-run silently — a user who meant real
    # writes got none, with no error. It now fails loudly, like Options
    # validates permission_mode / thinking / effort.
    with pytest.raises(ValueError, match="write_mode"):
        HostConfig(write_mode="Apply")
    with pytest.raises(ValueError, match="write_mode"):
        HostConfig(write_mode="apply ")


# ---------------------------------------------------------------------------
# Loop and tool-output bounds: both live on SdkHost and were unreachable from
# the public surface until HostConfig carried them.
# ---------------------------------------------------------------------------


_LOOP_SCHEMA = {
    "type": "object",
    "properties": {"k": {"type": "string"}},
    "additionalProperties": False,
}


@tool(
    name="loop_echo",
    version="1",
    risk_level="low",
    input_schema=_LOOP_SCHEMA,
)
def _loop_echo(arguments: dict, ctx: ToolContext) -> ToolResult:  # noqa: ARG001
    # 500 characters — long enough that a small inline limit has to cut it.
    return ToolResult(success=True, output="X" * 500)


def _loop_call(call_id: str) -> LLMResponse:
    """The SAME ``(tool, arguments)`` every time — what the guard watches."""
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id, tool_name="loop_echo", arguments={"k": "loop"}
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _looping_provider(calls: int = 2) -> FakeLLMProvider:
    return FakeLLMProvider(
        responses=[_loop_call(f"c{i}") for i in range(calls)]
        + [
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="done")],
                usage=Usage(uncached=1, output=1),
            )
        ]
    )


def _loop_options() -> Options:
    return Options(
        system_prompt="call loop_echo",
        name="main",
        allowed_tools=(_loop_echo,),
        permission_mode="bypassPermissions",
    )


def _tool_result_outputs(provider: FakeLLMProvider) -> list[str]:
    """The tool results as the MODEL saw them, off the last request."""
    return [
        block.output
        for message in provider.received_requests[-1].messages
        for block in message.content
        if isinstance(block, ToolResultBlock)
    ]


def test_loop_and_output_bounds_default_to_off() -> None:
    hc = HostConfig()
    assert hc.repetition_threshold is None
    assert hc.tool_output_inline_limit is None


def test_non_positive_loop_and_output_bounds_raise_at_construction() -> None:
    # ``None`` is how both spell "off"; a 0 or a negative would read as a
    # disabled guard to one consumer and an impossible cap to the other.
    with pytest.raises(ValueError, match="repetition_threshold"):
        HostConfig(repetition_threshold=0)
    with pytest.raises(ValueError, match="repetition_threshold"):
        HostConfig(repetition_threshold=-3)
    with pytest.raises(ValueError, match="tool_output_inline_limit"):
        HostConfig(tool_output_inline_limit=0)
    with pytest.raises(ValueError, match="tool_output_inline_limit"):
        HostConfig(tool_output_inline_limit=-1)


def test_repetition_threshold_registers_the_guard_and_trips(
    tmp_path: Path,
) -> None:
    # threshold=2: the first identical call runs, the second trips the guard
    # and routes through the HITL suspend path instead of looping forever.
    provider = _looping_provider()
    client = Client(
        _loop_options(),
        provider=provider,
        workspace_dir=tmp_path,
        model="stub-model",
        multi_turn=False,
        host_config=HostConfig(repetition_threshold=2),
    )
    try:
        assert client._host.repetition_threshold == 2
        outcome = client.start(goal="loop")
        types = [e.type for e in client.events(outcome.task_id)]
        assert types.count("ToolCallStarted") == 1
        assert "ToolCallApprovalRequested" in types
        assert outcome.status == "suspended"
    finally:
        client.shutdown()


def test_repetition_threshold_unset_leaves_every_call_running(
    tmp_path: Path,
) -> None:
    provider = _looping_provider()
    client = Client(
        _loop_options(),
        provider=provider,
        workspace_dir=tmp_path,
        model="stub-model",
        multi_turn=False,
    )
    try:
        assert client._host.repetition_threshold == 0  # the host's "off"
        outcome = client.start(goal="loop")
        types = [e.type for e in client.events(outcome.task_id)]
        assert types.count("ToolCallStarted") == 2
        assert "ToolCallApprovalRequested" not in types
        assert outcome.status == "terminal"
    finally:
        client.shutdown()


def test_tool_output_inline_limit_truncates_what_the_model_sees(
    tmp_path: Path,
) -> None:
    provider = _looping_provider(calls=1)
    client = Client(
        _loop_options(),
        provider=provider,
        workspace_dir=tmp_path,
        model="stub-model",
        multi_turn=False,
        host_config=HostConfig(tool_output_inline_limit=50),
    )
    try:
        assert client._host.tool_output_inline_limit == 50
        outcome = client.start(goal="loop")
        assert outcome.status == "terminal"
        outputs = _tool_result_outputs(provider)
        assert len(outputs) == 1
        assert outputs[0].startswith("X" * 50)
        assert "450 of 500 chars dropped" in outputs[0]
    finally:
        client.shutdown()


def test_tool_output_inline_limit_unset_keeps_the_whole_output(
    tmp_path: Path,
) -> None:
    provider = _looping_provider(calls=1)
    client = Client(
        _loop_options(),
        provider=provider,
        workspace_dir=tmp_path,
        model="stub-model",
        multi_turn=False,
    )
    try:
        assert client._host.tool_output_inline_limit is None
        client.start(goal="loop")
        assert _tool_result_outputs(provider) == ["X" * 500]
    finally:
        client.shutdown()
