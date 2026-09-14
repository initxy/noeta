"""The Engine is a per-turn value: what that buys, end to end.

* A skill installed while the process runs is in the ``skill`` tool's roster
  on every task's next turn, with a recorded note naming it; the turn that
  came before is untouched; a removed skill leaves the roster silently; a
  task's opening turn announces nothing.
* Two tasks naming the same MCP server call through ONE pooled connection;
  ``reconnect_mcp`` never pulls a connection from under a turn; the pool
  closes with the Client.
* MCP provenance is recorded once per task, again when the enabled aliases
  change, and a dead server is reported once per outage, not once per turn.
"""

from __future__ import annotations

import gc
import json
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from noeta.core.fold import fold
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.protocols.tool import ToolContext
from noeta.runtime.mcp import McpServerSpec
from noeta.runtime.shell_policy import ShellMode
from noeta.runtime.workspace import FsWriteMode
from noeta.sdk import Client, HostConfig, Options
from noeta.storage.memory import InMemoryContentStore
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.builtins.skills.impl import SKILL_TOOL

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec
from tests._skill_fixtures import write_skill


_FAKE = str(Path(__file__).parent / "_fixtures" / "fake_mcp_server.py")


def _end(text: str = "done") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
        raw={"id": text},
    )


def _call(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _tool_name(tool: dict[str, Any]) -> str:
    return str(tool.get("name") or tool["function"]["name"])


def _skill_enum(request: Any) -> list[str]:
    """The ``skill`` tool's roster names as the model saw them in ``request``."""
    for tool in request.tools:
        if _tool_name(tool) == SKILL_TOOL:
            params = tool.get("parameters") or tool.get("input_schema") or tool["function"]["parameters"]
            return list(params["properties"]["skill"]["enum"])
    raise AssertionError("skill schema missing from the request")


def _system_texts(host: Any, task_id: str) -> list[str]:
    task = fold(host.event_log, host.content_store, task_id)
    return [
        " ".join(getattr(b, "text", "") for b in m.content)
        for m in task.runtime.messages
        if m.origin == "system"
    ]


def _skills_host(tmp_path: Path, *, responses: int = 6):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    write_skill(ws, "alpha", "the first skill")
    provider = FakeLLMProvider(responses=[_end(f"t{i}") for i in range(responses)])
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=provider,
        model="stub-model",
        multi_turn=True,
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
    )
    return host, make_driver(host), provider, ws


# ---------------------------------------------------------------------------
# skills: next turn, every task, with a note
# ---------------------------------------------------------------------------


def test_skill_installed_between_turns_shows_next_turn_with_a_note(
    tmp_path: Path,
) -> None:
    host, driver, provider, ws = _skills_host(tmp_path)
    started = driver.start(goal="first", agent="main")
    assert _skill_enum(provider.received_requests[0]) == ["alpha"]
    assert not any("New skills" in t for t in _system_texts(host, started.task_id))

    write_skill(ws, "beta", "installed mid-process")
    driver.send_goal(started.task_id, goal="second")
    second = provider.received_requests[1]
    assert _skill_enum(second) == ["alpha", "beta"]
    # The earlier turn's recorded request is what it was.
    assert _skill_enum(provider.received_requests[0]) == ["alpha"]
    notes = [t for t in _system_texts(host, started.task_id) if "New skills" in t]
    assert len(notes) == 1 and "beta" in notes[0] and "alpha" not in notes[0]

    # Nothing new: no second note. Removing a skill: gone, silently.
    driver.send_goal(started.task_id, goal="third")
    assert _skill_enum(provider.received_requests[2]) == ["alpha", "beta"]
    shutil.rmtree(ws / ".noeta" / "skills" / "alpha")
    driver.send_goal(started.task_id, goal="fourth")
    assert _skill_enum(provider.received_requests[3]) == ["beta"]
    notes = [t for t in _system_texts(host, started.task_id) if "New skills" in t]
    assert len(notes) == 1


def test_every_task_sees_the_new_skill_on_its_own_next_turn(tmp_path: Path) -> None:
    host, driver, provider, ws = _skills_host(tmp_path)
    one = driver.start(goal="one", agent="main")
    two = driver.start(goal="two", agent="main")
    write_skill(ws, "beta", "installed mid-process")
    # A task opened AFTER the install lists it at once and announces nothing:
    # its opening roster is its baseline.
    three = driver.start(goal="three", agent="main")
    assert _skill_enum(provider.received_requests[2]) == ["alpha", "beta"]
    assert not any("New skills" in t for t in _system_texts(host, three.task_id))
    # Both earlier tasks get it on their next turn, each with its own note.
    driver.send_goal(two.task_id, goal="again")
    driver.send_goal(one.task_id, goal="again")
    assert _skill_enum(provider.received_requests[3]) == ["alpha", "beta"]
    assert _skill_enum(provider.received_requests[4]) == ["alpha", "beta"]
    for task_id in (one.task_id, two.task_id):
        notes = [t for t in _system_texts(host, task_id) if "New skills" in t]
        assert len(notes) == 1 and "beta" in notes[0]


# ---------------------------------------------------------------------------
# MCP: one pooled connection per server, shared across tasks
# ---------------------------------------------------------------------------


def _echo_spec(alias: str = "fake", mode: str = "echo") -> McpServerSpec:
    return McpServerSpec(alias=alias, argv=(sys.executable, "-u", _FAKE, mode))


def _mcp_host(tmp_path: Path, responses: list[LLMResponse], *specs: McpServerSpec, **knobs: Any):
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    table = {s.alias: s for s in specs}
    provider = FakeLLMProvider(responses=responses)
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=provider,
        model="gpt-test",
        write_mode=FsWriteMode.DRY_RUN,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
        mcp_server_resolver=table.get,
        **knobs,
    )
    return host, make_driver(host), provider


def test_two_tasks_share_one_pooled_mcp_connection(tmp_path: Path) -> None:
    host, driver, _ = _mcp_host(
        tmp_path,
        [
            _call("c1", "mcp__fake__echo", {"msg": "one"}),
            _end(),
            _call("c2", "mcp__fake__echo", {"msg": "two"}),
            _end(),
        ],
        _echo_spec(),
        multi_turn=False,
    )
    one = driver.start(goal="use it", agent="main", enabled_mcp=("fake",))
    two = driver.start(goal="use it", agent="main", enabled_mcp=("fake",))
    assert one.status == "terminal" and two.status == "terminal"
    for out in (one, two):
        results = [
            e.payload
            for e in host.event_log.read(out.task_id)
            if e.type == "ToolResultRecorded"
        ]
        assert results and all(r.success for r in results)
    pool = host._mcp_pool
    assert pool is not None and pool.live_count() == 1
    # Both turns are over: no holder is left, the connection idles in the pool.
    gc.collect()
    (client,) = list(pool._by_client.values())
    assert client.holders == 0
    host.shutdown_mcp()
    assert pool.live_count() == 0


def test_reconnect_keeps_a_turns_connection_and_reconnects_the_next(
    tmp_path: Path,
) -> None:
    host, driver, _ = _mcp_host(tmp_path, [_end()], _echo_spec(), multi_turn=True)
    started = driver.start(goal="hello", agent="main", enabled_mcp=("fake",))
    host.note_turn_mcp(started.task_id, ("fake",))
    task = fold(host.event_log, host.content_store, started.task_id)
    engine = host.resolve_engine(task)
    tool = engine._tools["mcp__fake__echo"]
    pool = host._mcp_pool
    before = pool.live_count()
    host.reconnect_mcp()
    # The turn's connection is retired, not closed: its tools still answer.
    ctx = ToolContext(artifact_store=InMemoryContentStore())
    result = tool.invoke({"msg": "still here"}, ctx)
    assert result.success, result.summary
    assert json.loads(result.output["text"]) == {"msg": "still here"}
    # The next TURN's build connects afresh while the old one is still held
    # (within the turn, ``resolve_engine`` hands back the same Engine) …
    assert host.resolve_engine(task) is engine
    host.forget_turn_engine(task.task_id)
    fresh = host.resolve_engine(task)
    assert fresh is not engine
    assert fresh._tools["mcp__fake__echo"]._client is not tool._client
    assert pool.live_count() == before + 1
    # … and the old one closes once its Engine goes.
    del engine, tool
    gc.collect()
    assert pool.live_count() == before
    del fresh
    gc.collect()


def test_scope_resolver_partitions_the_pool_per_task(tmp_path: Path) -> None:
    """``HostConfig.mcp_scope_resolver``: two tasks the host maps to
    different scopes get their own connections to the same server; two in
    one scope share; a task the resolver declines uses the shared one."""
    scopes: dict[str, str] = {}
    host, driver, _ = _mcp_host(
        tmp_path,
        [
            _call("c1", "mcp__fake__echo", {"msg": "a"}), _end(),
            _call("c2", "mcp__fake__echo", {"msg": "b"}), _end(),
            _call("c3", "mcp__fake__echo", {"msg": "a2"}), _end(),
            _call("c4", "mcp__fake__echo", {"msg": "shared"}), _end(),
        ],
        _echo_spec(),
        multi_turn=False,
        mcp_scope_resolver=scopes.get,
    )

    def run(scope: Optional[str]) -> str:
        seeded = driver.seed_start(goal="use it", agent="main", enabled_mcp=("fake",))
        if scope is not None:
            scopes[seeded.task_id] = scope
        out = driver.drive_seeded(seeded)
        assert out.status == "terminal"
        (result,) = [
            e.payload
            for e in host.event_log.read(out.task_id)
            if e.type == "ToolResultRecorded"
        ]
        assert result.success, result.summary
        return out.task_id

    run("tenant-a")
    run("tenant-b")
    run("tenant-a")
    run(None)
    pool = host._mcp_pool
    assert pool is not None and pool.live_count() == 3
    host.shutdown_mcp()


def test_client_shutdown_closes_the_pool(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    spec = _echo_spec()
    client = Client(
        Options(
            system_prompt="finish at once",
            name="main",
            allowed_tools=(),
            plugins=("fs", "web", "mcp"),
            permission_mode="bypassPermissions",
        ),
        provider=FakeLLMProvider(
            responses=[_call("c1", "mcp__fake__echo", {"msg": "hi"}), _end()]
        ),
        workspace_dir=ws,
        host_config=HostConfig(mcp_server_resolver={"fake": spec}.get),
    )
    out = client.start(goal="use it", enabled_mcp=("fake",))
    assert out.status in ("terminal", "suspended")
    pool = client._host._mcp_pool
    assert pool is not None and pool.live_count() == 1
    client.shutdown()
    assert pool.live_count() == 0


# ---------------------------------------------------------------------------
# MCP provenance and skips: once per change, once per outage
# ---------------------------------------------------------------------------


def _counts(host: Any, task_id: str) -> tuple[int, int]:
    events = host.event_log.read(task_id)
    return (
        sum(1 for e in events if e.type == "McpProvenanceRecorded"),
        sum(1 for e in events if e.type == "McpServerSkipped"),
    )


def test_provenance_once_per_task_and_again_when_the_aliases_change(
    tmp_path: Path,
) -> None:
    host, driver, _ = _mcp_host(
        tmp_path,
        [_end("t1"), _end("t2"), _end("t3")],
        _echo_spec("fake"),
        _echo_spec("dead", mode="die_init"),
        multi_turn=True,
    )
    started = driver.start(goal="one", agent="main", enabled_mcp=("fake", "dead"))
    assert _counts(host, started.task_id) == (1, 1)
    driver.send_goal(started.task_id, goal="two", enabled_mcp=("fake", "dead"))
    # Same servers, same outage: nothing new on the stream.
    assert _counts(host, started.task_id) == (1, 1)
    driver.send_goal(started.task_id, goal="three", enabled_mcp=("fake",))
    # The enabled set changed: provenance again; the dead server is no longer
    # enabled, so no new skip either.
    assert _counts(host, started.task_id) == (2, 1)
    provenance = [
        e.payload.servers
        for e in host.event_log.read(started.task_id)
        if e.type == "McpProvenanceRecorded"
    ]
    assert [row["alias"] for row in provenance[0]] == ["dead", "fake"]
    assert [row["alias"] for row in provenance[1]] == ["fake"]
