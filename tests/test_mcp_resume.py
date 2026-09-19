"""A Task resumed on a fresh host keeps its MCP tools.

The enabled aliases ride a process-local per-turn carrier, so a restart — or
another machine claiming the Task — used to rebuild the Engine with zero
``mcp__`` tools even though the recording says which servers the Task was
given. The durable ``McpProvenanceRecorded`` fold is that record: a build that
finds the carrier empty reconnects from it through the host's own
``mcp_server_resolver``.

A restart is simulated the way the child-observer lineage test does it —
a second host constructed over the *same* stores, so every per-turn carrier
and task-local starts empty and the recording is all there is.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from noeta.client.host import SdkHost
from noeta.core.fold import fold
from noeta.protocols.events import TaskCreatedPayload
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL
from noeta.runtime.mcp import McpServerSpec
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.testing.fake_llm import FakeLLMProvider

from tests._sdk_session import (
    make_driver,
    make_host,
    make_registry,
    preset_spec,
    runner_main_spec,
)


_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")
_UNSET: Any = object()


def _spec(alias: str = "fake", mode: str = "echo") -> McpServerSpec:
    return McpServerSpec(alias=alias, argv=(sys.executable, "-u", _FAKE, mode))


def _call(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": text},
    )


def _mcp_host(
    tmp_path: Path,
    responses: list[LLMResponse],
    *specs: McpServerSpec,
    delegation: bool = False,
    **knobs: Any,
):
    """An SDK host wired for ``specs`` through the production resolver seam."""
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    table = {s.alias: s for s in specs}
    main = runner_main_spec(
        "main", delegation=delegation, spawnable=("general-purpose", "explore")
    )
    children = (
        [preset_spec("general-purpose"), preset_spec("explore")]
        if delegation
        else []
    )
    host = make_host(
        make_registry(main, *children),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=responses),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        mcp_server_resolver=table.get,
        **knobs,
    )
    return host, make_driver(host), table


def _restart(host: SdkHost, *, mcp_server_resolver: Any = _UNSET) -> SdkHost:
    """A fresh host over the SAME stores — a process restart, or another
    machine claiming the Task: no per-turn carrier, no task-local Engine."""
    return SdkHost(
        event_log=host.event_log,
        content_store=host.content_store,
        dispatcher=host.dispatcher,
        provider=host.providers[host.default_provider],
        model=host.model,
        workspace_dir=host.workspace_dir,
        registry=host.registry,
        aliases=host.aliases,
        policy_wrapper=host.policy_wrapper,
        write_mode=host.write_mode,
        shell_mode=host.shell_mode,
        require_approval_tools=host.require_approval_tools,
        mcp_server_resolver=(
            host.mcp_server_resolver
            if mcp_server_resolver is _UNSET
            else mcp_server_resolver
        ),
        mcp_scope_resolver=host.mcp_scope_resolver,
    )


def _counts(host: Any, task_id: str) -> tuple[int, int]:
    events = host.event_log.read(task_id)
    return (
        sum(1 for e in events if e.type == "McpProvenanceRecorded"),
        sum(1 for e in events if e.type == "McpServerSkipped"),
    )


def _suspended_on_approval(tmp_path: Path, **knobs: Any):
    """A Task parked on an ``mcp__fake__echo`` approval — the default-mode
    shape (MCP tools are high risk, so every call is gated)."""
    host, driver, _ = _mcp_host(
        tmp_path,
        [_call("c1", "mcp__fake__echo", {"msg": "hi"}), _end()],
        _spec(),
        require_approval_tools=("mcp__fake__echo",),
        **knobs,
    )
    out = driver.start(goal="use the mcp tool", agent="main", enabled_mcp=("fake",))
    assert out.status == "suspended" and out.wake_handle == "approval-c1"
    return host, out.task_id


def _engine_for(host: SdkHost, task_id: str) -> Any:
    return host.resolve_engine(fold(host.event_log, host.content_store, task_id))


def _mcp_tool_names(engine: Any) -> list[str]:
    return sorted(n for n in engine._tools if n.startswith("mcp__"))


# ---------------------------------------------------------------------------
# the Task itself
# ---------------------------------------------------------------------------


def test_a_resumed_task_keeps_its_mcp_tools(tmp_path: Path) -> None:
    host, task_id = _suspended_on_approval(tmp_path)
    restarted = _restart(host)

    engine = _engine_for(restarted, task_id)
    assert "mcp__fake__echo" in engine._tools

    # …and the pending approval executes instead of dying on an unknown tool.
    out = make_driver(restarted).approve(task_id, call_id="c1")
    assert out.status == "terminal"
    results = [
        e.payload
        for e in restarted.event_log.read(task_id)
        if e.type == "ToolResultRecorded"
    ]
    assert results and all(r.success for r in results)
    restarted.shutdown_mcp()


def test_the_rebuilt_tool_set_is_the_one_the_recording_names(
    tmp_path: Path,
) -> None:
    """Same aliases, same ticked subset — so the schema the resumed turn
    composes (and the stable prefix over it) is the one it opened with."""
    ticked = McpServerSpec(
        alias="fake",
        argv=(sys.executable, "-u", _FAKE, "multi"),
        tool_subset=("alpha", "gamma"),
    )
    host, driver, _ = _mcp_host(tmp_path, [_end()], ticked)
    out = driver.start(goal="use it", agent="main", enabled_mcp=("fake",))
    before = _mcp_tool_names(_engine_for(host, out.task_id))
    assert before == ["mcp__fake__alpha", "mcp__fake__gamma"]

    restarted = _restart(host)
    assert _mcp_tool_names(_engine_for(restarted, out.task_id)) == before
    restarted.shutdown_mcp()
    host.shutdown_mcp()


def test_the_resume_rebuild_records_no_second_provenance(tmp_path: Path) -> None:
    host, task_id = _suspended_on_approval(tmp_path)
    assert _counts(host, task_id) == (1, 0)
    restarted = _restart(host)

    _engine_for(restarted, task_id)
    # Same servers, same ticked tools: the recording already says so.
    assert _counts(restarted, task_id) == (1, 0)
    restarted.shutdown_mcp()


def test_a_host_without_a_resolver_resumes_exactly_as_before(
    tmp_path: Path,
) -> None:
    host, task_id = _suspended_on_approval(tmp_path)
    before = len(host.event_log.read(task_id))
    restarted = _restart(host, mcp_server_resolver=None)

    engine = _engine_for(restarted, task_id)
    assert not any(n.startswith("mcp__") for n in engine._tools)
    assert len(restarted.event_log.read(task_id)) == before


def test_a_server_unreachable_at_resume_is_skipped(tmp_path: Path) -> None:
    host, task_id = _suspended_on_approval(tmp_path)
    dead = {"fake": _spec("fake", mode="die_init")}
    restarted = _restart(host, mcp_server_resolver=dead.get)

    # Skip-on-failure, exactly as at a turn's open: the build survives, the
    # outage is recorded once.
    engine = _engine_for(restarted, task_id)
    assert not any(n.startswith("mcp__") for n in engine._tools)
    assert _counts(restarted, task_id) == (1, 1)


# ---------------------------------------------------------------------------
# delegated children
# ---------------------------------------------------------------------------


def _spawn(agent: str, goal: str) -> LLMResponse:
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


def _child_id(host: Any, parent_task_id: str) -> str:
    for env in host.event_log.read(parent_task_id):
        if env.type == "SubtaskSpawned":
            return str(env.payload.subtask_id)
    raise AssertionError("no SubtaskSpawned on the parent stream")


def _delegating_root(tmp_path: Path):
    host, driver, _ = _mcp_host(
        tmp_path,
        [_spawn("general-purpose", "look around"), _end("looked"), _end("done")],
        _spec(),
        delegation=True,
    )
    out = driver.start(goal="delegate it", agent="main", enabled_mcp=("fake",))
    assert out.status == "terminal"
    return host, out.task_id


def test_a_delegated_child_resumes_with_its_mcp_tools(tmp_path: Path) -> None:
    host, root_id = _delegating_root(tmp_path)
    child_id = _child_id(host, root_id)
    restarted = _restart(host)

    engine = _engine_for(restarted, child_id)
    assert "mcp__fake__echo" in engine._tools
    restarted.shutdown_mcp()


def test_a_child_claimed_after_the_restart_inherits_the_roots_record(
    tmp_path: Path,
) -> None:
    """A child created but never driven before the restart carries no
    provenance of its own; it inherits the root's recorded set — but only
    when its own spec opens the ``mcp`` capability."""
    host, root_id = _delegating_root(tmp_path)
    restarted = _restart(host)

    def _child(task_id: str, agent_name: str) -> Any:
        restarted.event_log.system_emit(
            task_id=task_id,
            type="TaskCreated",
            payload=TaskCreatedPayload(
                goal="do",
                policy_name="react",
                agent_name=agent_name,
                parent_task_id=root_id,
                subtask_depth=1,
            ),
            actor="test",
            origin="system",
        )
        return _engine_for(restarted, task_id)

    opted_in = _child("child-mcp", "general-purpose")
    assert "mcp__fake__echo" in opted_in._tools
    # explore does not activate ``mcp`` — inheritance stays per-spec opt-in.
    plain = _child("child-plain", "explore")
    assert not any(n.startswith("mcp__") for n in plain._tools)
    restarted.shutdown_mcp()
