"""Persist an oversized tool result on the way into the log, without losing it.

Past ``tool_output_inline_limit`` the model-facing output becomes Claude Code's
``<persisted-output>`` block (the file holding the whole output plus a 2 KB
preview), or — with nowhere to save it — a ``<truncated-output>`` block keeping
the start; ``ToolResultRecorded.output_ref`` still holds the full original for
resume and audit. ``Read`` is exempt. The kernel default is off (``None``), the
hosts' is Claude Code's 50,000; the limit must be positive and threads from
``build_session_inputs`` through ``SessionInputs`` into the ``Engine``
unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from tests._session_inputs import default_factory_kwargs
from noeta.context.composer import COMPOSER_VERSION, ThreeSegmentComposer
from noeta.core._decision_handlers import (
    _validate_tool_output_inline_limit,
    large_tool_output_message,
    result_size_threshold,
)
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.builtins.react.impl import ReActPolicy
from noeta.protocols.events import (
    EventEnvelope,
    ToolResultRecordedPayload,
)
from noeta.protocols.messages import (
    LLMResponse,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
)
from noeta.runtime.llm import RuntimeLLMClient
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fake import FakeTool

from tests._sdk_session import coding_replay_budget

# ---------------------------------------------------------------------------
# Pure-function unit tests
# ---------------------------------------------------------------------------


def test_persisted_output_is_claude_codes_block() -> None:
    body = "\n".join(f"line {i:04d}" for i in range(6000))  # 59,999 chars
    out = large_tool_output_message(body, 50_000, saved_to="/spill/t/c1.txt")
    head, preview_and_tail = out.split("Preview (first 2KB):\n")
    assert head == (
        "<persisted-output>\n"
        "Output too large (58.6KB). Full output saved to: /spill/t/c1.txt\n\n"
    )
    preview, tail = preview_and_tail.rsplit("\n...\n", 1)
    assert tail == "</persisted-output>"
    # The preview ends at the last whole line inside 2,000 characters.
    assert body.startswith(preview + "\n")
    assert 1_000 < len(preview) <= 2_000


def test_persisted_output_without_more_has_no_ellipsis() -> None:
    out = large_tool_output_message("abc", 2, saved_to="/f.txt")
    assert out == (
        "<persisted-output>\n"
        "Output too large (3 bytes). Full output saved to: /f.txt\n\n"
        "Preview (first 2KB):\nabc\n</persisted-output>"
    )


def test_unsaved_output_keeps_the_start_and_end_within_the_limit() -> None:
    body = "x" * 5_000 + "y" * 5_000
    out = large_tool_output_message(body, 1_000)
    assert out.startswith(
        "<truncated-output>\nOutput too large (9.8KB). It could not be saved, "
        "so only the first "
    )
    assert "the middle was dropped. If the tool can page or filter its results, "
    "call it again for the part you need." in out
    assert out.endswith("y\n</truncated-output>")
    assert len(out) <= 1_000
    assert "x" * 300 in out and "y" * 300 in out
    assert " characters dropped] ...\n" in out


def test_unsaved_output_keeps_the_trailing_error() -> None:
    """A failing test run prints its verdict last; cutting to the start
    would show only passing lines."""
    lines = [f"line {i} ok" for i in range(4000)] + ["FAILED test_x: AssertionError"]
    out = large_tool_output_message("\n".join(lines), 30_000)
    assert len(out) <= 30_000
    assert "line 0 ok\n" in out
    assert out.endswith("\nFAILED test_x: AssertionError\n</truncated-output>")


def test_size_labels_follow_claude_code() -> None:
    assert "(1023 bytes)" in large_tool_output_message("a" * 1023, 10, saved_to="p")
    assert "(1KB)" in large_tool_output_message("a" * 1024, 10, saved_to="p")
    assert "(1.5MB)" in large_tool_output_message(
        "a" * (3 * 512 * 1024), 10, saved_to="p"
    )


def test_threshold_lets_a_tool_opt_out_or_lower_it() -> None:
    assert result_size_threshold(None, None) is None
    assert result_size_threshold(50_000, None) == 50_000
    assert result_size_threshold(50_000, float("inf")) is None
    assert result_size_threshold(50_000, 20_000) == 20_000
    assert result_size_threshold(10_000, 20_000) == 10_000


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


def test_validate_rejects_zero_and_negative() -> None:
    with pytest.raises(ValueError, match="tool_output_inline_limit must be > 0"):
        _validate_tool_output_inline_limit(0)
    with pytest.raises(ValueError, match="tool_output_inline_limit must be > 0"):
        _validate_tool_output_inline_limit(-5)


def test_engine_constructor_rejects_bad_limit() -> None:
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    from noeta.core.composer import PassthroughComposer

    with pytest.raises(ValueError, match="tool_output_inline_limit must be > 0"):
        Engine(
            event_log=event_log,
            content_store=content_store,
            composer=PassthroughComposer(),
            tool_output_inline_limit=0,
        )


# ---------------------------------------------------------------------------
# Shared helper: build a ReActPolicy + Engine with a tool, run one turn whose
# tool produces oversized output
# ---------------------------------------------------------------------------

_SYSTEM = "You are an agent. Work the task then finish."


def _big_echo_tool(size: int = 2000) -> FakeTool:
    """FakeTool named echo that returns a string of the given size."""
    content = "~" * size
    return FakeTool(
        name="echo",
        script={("big",): content},
        input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
    )


def _engine_and_run(*, tool_output_inline_limit: int | None, tool_size: int = 2000):
    """Run both turns (tool -> end_turn), return (task, engine, event_log, content_store)."""
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    content_store = InMemoryContentStore()
    wire_default_observers(event_log, dispatcher)

    tool = _big_echo_tool(tool_size)
    tools = {tool.name: tool}

    def policy_fn(llm):
        return ReActPolicy(
            llm=llm,
            tools=tools,
            system_prompt=_SYSTEM,
            model="stub-model",
            max_steps=5,
            composer_version=COMPOSER_VERSION,
        )

    # Round 1: LLM decides to call echo. Round 2: end_turn.
    call_id = "call-big-1"
    provider = FakeLLMProvider(
        responses=[
            LLMResponse(
                stop_reason="tool_use",
                content=[
                    ToolUseBlock(
                        call_id=call_id,
                        tool_name="echo",
                        arguments={"text": "big"},
                    )
                ],
                usage=Usage(uncached=1, output=1),
                raw={"id": "r1"},
            ),
            LLMResponse(
                stop_reason="end_turn",
                content=[TextBlock(text="done")],
                usage=Usage(uncached=1, output=1),
                raw={"id": "r2"},
            ),
        ]
    )
    # Recording on: (c) below reads the request the model saw back from the
    # store rather than from the fake provider.
    llm = RuntimeLLMClient(
        provider=provider,
        event_log=event_log,
        content_store=content_store,
        record_requests=True,
    )
    policy = policy_fn(llm)
    composer = ThreeSegmentComposer(
        system_prompt=_SYSTEM,
        tools=tools,
        content_store=content_store,
    )
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=composer,
        policy=policy,
        tools=tools,
        tool_output_inline_limit=tool_output_inline_limit,
    )
    task = engine.create_task(goal="run the echo", policy_name="react")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    engine.append_user_message(task, content=[TextBlock(text="run the echo")], lease_id=lease.lease_id)
    engine.run_one_step(task, lease_id=lease.lease_id)
    return task, engine, event_log, content_store, call_id


# ---------------------------------------------------------------------------
# Default off, zero impact
# ---------------------------------------------------------------------------


def test_default_no_limit_keeps_dict_and_full_string() -> None:
    """None leaves output untouched — dict output stays dict, string stays full."""
    task, engine, event_log, content_store, _ = _engine_and_run(
        tool_output_inline_limit=None, tool_size=2000
    )
    assert task.status == "terminal"
    folded = fold(event_log, content_store, task.task_id)
    tool_msg = [m for m in folded.runtime.messages if m.role == "tool"][0]
    block: ToolResultBlock = tool_msg.content[0]  # type: ignore[assignment]
    # Default off: the string passes through whole.
    assert block.output == "~" * 2000


# ---------------------------------------------------------------------------
# End-to-end with limit on — truncated message / full recording / model request sees truncation
# ---------------------------------------------------------------------------


def test_limit_truncates_inline_but_keeps_recorded_full() -> None:
    tool_size = 2000
    limit = 1000
    task, engine, event_log, content_store, call_id = _engine_and_run(
        tool_output_inline_limit=limit, tool_size=tool_size
    )
    assert task.status == "terminal"

    # (a) Message stream: truncated form
    folded = fold(event_log, content_store, task.task_id)
    tool_msg = [m for m in folded.runtime.messages if m.role == "tool"][0]
    block: ToolResultBlock = tool_msg.content[0]  # type: ignore[assignment]
    inline = block.output
    assert isinstance(inline, str)
    assert inline.startswith("<truncated-output>\nOutput too large (2KB).")
    assert len(inline) <= limit
    # Lean marker: the model-facing inline form carries NO ContentStore hash
    # (the model has no ref-deref tool). The full body is recovered from the
    # ToolResultRecorded.output_ref event below, not from the prompt text.
    assert "content ref" not in inline

    # (b) ToolResultRecorded.output_ref deref = full original
    events = list(event_log.read(task.task_id))
    trr = [e for e in events if e.type == "ToolResultRecorded"]
    assert len(trr) == 1
    trr_p: ToolResultRecordedPayload = trr[0].payload
    raw = content_store.get(trr_p.output_ref)
    decoded = json.loads(raw.decode("utf-8"))
    assert decoded == "~" * tool_size  # JSON-encoded original output

    # (c) The model's next LLMRequest is also the truncated form — read the
    # request_ref in LLMRequestStarted (the request after the tool call, before end_turn)
    llm_request_bodies = _llm_request_bodies(events, content_store)
    assert len(llm_request_bodies) >= 2  # at least the tool -> end_turn two rounds
    second_req = llm_request_bodies[1]  # round 2 (after tool result, end_turn)
    # Flatten the user/tool-role message content in the request body, check for the truncated string
    req_text_blob = json.dumps(second_req, default=str)
    # The cut form must appear in the request (the model saw it)
    assert "It could not be saved" in req_text_blob
    # The full original must never appear in the next request (or truncation was pointless)
    assert ("~" * tool_size) not in req_text_blob


def _llm_request_bodies(events: list[EventEnvelope], cs) -> list[dict[str, Any]]:
    """Extract the JSON bodies deref'd from LLMRequestStarted.request_ref, in order of appearance."""
    out = []
    for e in events:
        if e.type != "LLMRequestStarted":
            continue
        p = e.payload
        ref = getattr(p, "request_ref", None)
        if ref is None:
            continue
        raw = cs.get(ref)
        out.append(json.loads(raw.decode("utf-8")))
    return out


# ---------------------------------------------------------------------------
# Product-layer pass-through — build_session_inputs -> Engine takes effect
# ---------------------------------------------------------------------------


def _end_resp(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _tool_resp(call_id: str, tool_name: str = "echo") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[
            ToolUseBlock(
                call_id=call_id, tool_name=tool_name, arguments={"text": "big"}
            )
        ],
        usage=Usage(uncached=1, output=1),
        raw={"id": "tu"},
    )


def test_product_limit_propagates_to_handler_via_custom_echo(tmp_path: Path) -> None:
    """tool_output_inline_limit -> engine -> handler takes effect.

    Drives the pass-through the production ``SdkHost`` runs:
    build_session_inputs -> SessionInputs -> Engine."""
    from noeta.client.parts import derive_compaction_config
    from noeta.execution.builder import build_session_inputs

    ws = tmp_path / "ws"
    ws.mkdir()
    cs = InMemoryContentStore()
    limit = 50
    big_echo = FakeTool(
        name="echo",
        script={("big",): "X" * 500},
    )

    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=ws,
        system_prompt=_SYSTEM,
        allowed_tools=frozenset({"echo"}),
        content_store=cs,
        model="stub-model",
        compaction=derive_compaction_config("stub-model"),
        budget=coding_replay_budget(None),
        custom_tools={"echo": big_echo},
        tool_output_inline_limit=limit,
    )
    assert inputs.tool_output_inline_limit == limit
    # SessionInputs -> Engine mirrors how SdkHost constructs the Engine.
    tools_d = dict(inputs.tools)
    dispatcher = InMemoryDispatcher()
    el = InMemoryEventLog(lease_validator=dispatcher)
    wire_default_observers(el, dispatcher)
    llm = RuntimeLLMClient(
        provider=FakeLLMProvider(
            responses=[
                _tool_resp("c1", "echo"),
                _end_resp("done"),
            ]
        ),
        event_log=el,
        content_store=cs,
    )
    engine = Engine(
        event_log=el,
        content_store=cs,
        composer=inputs.composer,
        policy=inputs.policy_factory(llm),
        tools=tools_d,
        hooks=inputs.hooks,
        tool_output_inline_limit=inputs.tool_output_inline_limit,
    )
    task = engine.create_task(goal="hi", policy_name="react")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None
    engine.append_user_message(task, content=[TextBlock(text="hi")], lease_id=lease.lease_id)
    final = engine.run_one_step(task, lease_id=lease.lease_id)
    assert final.status == "terminal"
    folded = fold(el, cs, task.task_id)
    tool_blocks = [
        b
        for m in folded.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    assert len(tool_blocks) == 1
    inline = tool_blocks[0].output
    assert isinstance(inline, str)
    assert inline.startswith("<truncated-output>\nOutput too large (500 bytes).")
    # Red line: ToolResultRecorded still records the full output
    trrs = [e for e in el.read(task.task_id) if e.type == "ToolResultRecorded"]
    assert len(trrs) == 1
    payload: ToolResultRecordedPayload = trrs[0].payload
    raw = cs.get(payload.output_ref)
    assert json.loads(raw.decode("utf-8")) == "X" * 500


def test_product_default_none_zero_impact(tmp_path: Path) -> None:
    """Product layer passes no explicit limit -> SessionInputs.tool_output_inline_limit is None."""
    from noeta.client.parts import derive_compaction_config
    from noeta.execution.builder import build_session_inputs

    ws = tmp_path / "ws"
    ws.mkdir()
    cs = InMemoryContentStore()
    big_echo = FakeTool(
        name="echo",
        script={("big",): "Y" * 500},
    )
    inputs = build_session_inputs(
        **default_factory_kwargs(),
        workspace_dir=ws,
        system_prompt=_SYSTEM,
        allowed_tools=frozenset({"echo"}),
        content_store=cs,
        model="stub-model",
        compaction=derive_compaction_config("stub-model"),
        budget=coding_replay_budget(None),
        custom_tools={"echo": big_echo},
        # <- Explicitly omitted, so the limit defaults to None
    )
    assert inputs.tool_output_inline_limit is None


# ---------------------------------------------------------------------------
# Extra guard: ToolResult.output_ref is populated by ToolRuntime (frozen-slot semantics)
# ---------------------------------------------------------------------------


def test_tool_runtime_populates_output_ref() -> None:
    """After ToolRuntime.invoke, the returned ToolResult.output_ref is not None."""
    from noeta.runtime.tool import ToolRuntime

    dispatcher = InMemoryDispatcher()
    el = InMemoryEventLog(lease_validator=dispatcher)
    cs = InMemoryContentStore()
    wire_default_observers(el, dispatcher)
    rt = ToolRuntime(event_log=el, content_store=cs)
    tool = FakeTool(
        name="t",
        script={tuple(): "hello"},
    )
    from noeta.protocols.decisions import ToolCall

    task_id = "tid"
    # ToolRuntime.emit goes through the lease-checked path -> must lease first (no
    # TaskCreated is fine, since InMemoryEventLog only checks that lease_id is in the
    # dispatcher; but the task must still be created). Simplest: register via
    # Engine.create_task + lease.
    from noeta.core.composer import PassthroughComposer
    from noeta.core.engine import Engine

    e = Engine(event_log=el, content_store=cs, composer=PassthroughComposer())
    e.create_task(goal="g", policy_name="p", task_id=task_id)
    dispatcher.enqueue(task_id)
    lease = dispatcher.lease(worker_id="w")
    assert lease is not None

    tc = ToolCall(call_id="c1", tool_name="t", arguments={})
    result = rt.invoke(
        tool, tc, task_id=task_id, lease_id=lease.lease_id, trace_id="trace"
    )
    assert result.output_ref is not None
    assert hasattr(result.output_ref, "hash")
    # Matches the ref in the event
    ev = [e for e in el.read("tid") if e.type == "ToolResultRecorded"][0]
    assert ev.payload.output_ref.hash == result.output_ref.hash


# ---------------------------------------------------------------------------
# HostConfig.tool_output_spill_dir — a cut result is saved whole, by path
# ---------------------------------------------------------------------------


def test_spiller_paths_are_a_function_of_task_and_call(tmp_path: Path) -> None:
    from noeta.core._decision_handlers import tool_output_spiller

    assert tool_output_spiller(None, "t1") is None
    spill = tool_output_spiller(tmp_path, "task/1")
    assert spill is not None
    path = spill("toolu_01:x", "whole body")
    assert path == str(tmp_path / "task_1" / "toolu_01_x.txt")
    assert Path(path).read_text(encoding="utf-8") == "whole body"


def test_spiller_that_cannot_write_cuts_without_a_path(tmp_path: Path) -> None:
    from noeta.core._decision_handlers import tool_output_spiller

    blocker = tmp_path / "file"
    blocker.write_text("x")
    spill = tool_output_spiller(blocker, "t1")  # a file where a dir must go
    assert spill is not None
    assert spill("c1", "body") is None


def _run_client(
    tmp_path: Path, call: ToolUseBlock, host_config: Any
) -> tuple[Any, str]:
    from noeta.sdk import Client, Options

    client = Client(
        Options(system_prompt="x", permission_mode="bypassPermissions"),
        provider=FakeLLMProvider(
            responses=[
                LLMResponse(
                    stop_reason="tool_use",
                    content=[call],
                    usage=Usage(uncached=1, output=1),
                ),
                _end_resp(),
            ]
        ),
        workspace_dir=tmp_path / "ws",
        model="stub-model",
        multi_turn=False,
        host_config=host_config,
    )
    try:
        outcome = client.start(goal="go")
        folded = fold(
            client._host.event_log, client._host.content_store, outcome.task_id
        )
    finally:
        client.shutdown()
    [block] = [
        b
        for m in folded.runtime.messages
        if m.role == "tool"
        for b in m.content
        if isinstance(b, ToolResultBlock)
    ]
    return block, outcome.task_id


def test_client_persists_a_large_bash_output_and_names_the_file(
    tmp_path: Path,
) -> None:
    from noeta.sdk import HostConfig

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.txt").write_text("\n".join(f"line {i:05d}" for i in range(2000)))
    spill_dir = tmp_path / "spill"
    block, task_id = _run_client(
        tmp_path,
        ToolUseBlock(
            call_id="c1", tool_name="Bash", arguments={"command": "cat big.txt"}
        ),
        HostConfig(tool_output_inline_limit=1_000, tool_output_spill_dir=spill_dir),
    )
    saved = spill_dir / task_id / "c1.txt"
    assert block.output.startswith(
        "<persisted-output>\nOutput too large ("
    )
    assert f"Full output saved to: {saved}\n" in block.output
    assert block.output.endswith("\n...\n</persisted-output>")
    body = saved.read_text(encoding="utf-8")
    assert "line 01999" in body and len(body) > 1_000


def test_read_is_never_persisted(tmp_path: Path) -> None:
    from noeta.sdk import HostConfig

    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "big.txt").write_text("\n".join(f"line {i:05d}" for i in range(2000)))
    block, _ = _run_client(
        tmp_path,
        ToolUseBlock(
            call_id="c1", tool_name="Read", arguments={"file_path": "big.txt"}
        ),
        HostConfig(tool_output_inline_limit=1_000, tool_output_spill_dir=tmp_path),
    )
    assert "persisted-output" not in block.output
    assert "line 01999" in block.output


def test_hosts_default_to_claude_codes_50000() -> None:
    from noeta.sdk import HostConfig

    assert HostConfig().tool_output_inline_limit == 50_000


def test_bash_persists_past_its_own_30000_under_the_host_default(
    tmp_path: Path,
) -> None:
    from noeta.sdk import HostConfig

    (tmp_path / "ws").mkdir()
    spill_dir = tmp_path / "spill"
    block, task_id = _run_client(
        tmp_path,
        ToolUseBlock(call_id="c1", tool_name="Bash", arguments={"command": "seq 1 8000"}),
        HostConfig(tool_output_spill_dir=spill_dir),  # host cap: 50,000
    )
    assert block.output.startswith("<persisted-output>\nOutput too large (")
    body = (spill_dir / task_id / "c1.txt").read_text(encoding="utf-8")
    # Saved whole — no middle cut.
    assert body == "\n".join(str(i) for i in range(1, 8001))


def test_bash_under_30000_stays_inline(tmp_path: Path) -> None:
    from noeta.sdk import HostConfig

    (tmp_path / "ws").mkdir()
    block, _ = _run_client(
        tmp_path,
        ToolUseBlock(call_id="c1", tool_name="Bash", arguments={"command": "seq 1 5000"}),
        HostConfig(tool_output_spill_dir=tmp_path / "spill"),
    )
    assert block.output == "\n".join(str(i) for i in range(1, 5001))


def test_builtin_tools_carry_claude_codes_result_sizes(tmp_path: Path) -> None:
    import math

    from noeta.builtins.fs.impl import build_fs_tools
    from noeta.runtime.shell_policy import ShellMode
    from noeta.runtime.workspace import WorkspaceRoot

    tools = build_fs_tools(
        WorkspaceRoot.from_path(tmp_path), shell_mode=ShellMode.ARBITRARY
    )
    sizes = {n: getattr(t, "max_result_size_chars") for n, t in tools.items()}
    assert sizes["Read"] == math.inf
    assert sizes["Bash"] == 30_000
    assert sizes["Grep"] == 20_000
    assert sizes["Glob"] == sizes["Edit"] == sizes["Write"] == 100_000
