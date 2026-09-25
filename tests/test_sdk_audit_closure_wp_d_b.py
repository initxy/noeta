"""SDK audit closure 2026-09-25, WP-D second half: config validation, model
aliases, seed compensation, memory + delegation built-ins, views, exports.

One regression per reproduced defect (spec items D3, D4, D5, D9–D14).
"""

from __future__ import annotations

import tempfile
import warnings
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from noeta.builtins.delegation.impl import _maybe_spawn_decision
from noeta.builtins.memory.impl.judge import build_recall_judge, render_judge_system
from noeta.builtins.memory.impl.recall import recall_memories
from noeta.builtins.memory.impl.store import MemoryStore, MemoryWriteTool
from noeta.client import AgentDefinition
from noeta.client.host_config import HostConfig
from noeta.client.parts import resolve_model_alias
from noeta.policies.control_semantics import SPAWN_SUBAGENT_TOOL
from noeta.builtins.memory.impl import (
    MEMORY_POLICY_READ_ONLY_PROMPT,
    memory_policy_for,
)
from noeta.presets import MAIN_SYSTEM_PROMPT, MEMORY_POLICY_PROMPT
from noeta.protocols.decisions import SpawnSubtaskDecision
from noeta.protocols.errors import CodedError
from noeta.protocols.messages import Message, ToolUseBlock
from noeta.protocols.tool import ToolContext
from noeta.sdk import (
    Client,
    LLMResponse,
    Options,
    QueryFailedError,
    TextBlock,
    Usage,
)
from noeta.storage.memory import InMemoryContentStore
from noeta.testing.fake_llm import FakeLLMProvider


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class _Rec(FakeLLMProvider):
    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.requests: list[Any] = []

    def complete(self, request: Any, *a: Any, **k: Any) -> Any:
        self.requests.append(request)
        return super().complete(request, *a, **k)


def _say(text: str = "ok") -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text=text)],
        usage=Usage(uncached=1, output=1),
    )


def _call(name: str, args: dict[str, Any], cid: str = "c1") -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=cid, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
    )


def _tool_names(request: Any) -> set[str]:
    out: set[str] = set()
    for t in request.tools or ():
        name = getattr(t, "name", None)
        if name is None and isinstance(t, dict):
            name = t.get("name") or t.get("function", {}).get("name")
        out.add(str(name))
    return out


def _system_text(request: Any) -> str:
    return "".join(
        getattr(b, "text", "") for b in getattr(request.system, "content", ())
    )


def _ctx() -> ToolContext:
    return ToolContext(artifact_store=InMemoryContentStore())


# ---------------------------------------------------------------------------
# D3 — the memory policy fragment follows the pack's read_only
# ---------------------------------------------------------------------------


def test_memory_policy_for_swaps_only_a_read_only_pack() -> None:
    assert memory_policy_for(MAIN_SYSTEM_PROMPT, read_only=False) == MAIN_SYSTEM_PROMPT
    swapped = memory_policy_for(MAIN_SYSTEM_PROMPT, read_only=True)
    assert swapped.endswith(MEMORY_POLICY_READ_ONLY_PROMPT)
    assert "memory_write" not in swapped and "memory_archive" not in swapped
    assert len(swapped) < len(MAIN_SYSTEM_PROMPT)
    # A prompt without the fragment is left alone.
    assert memory_policy_for("custom", read_only=True) == "custom"


def test_read_only_host_sends_the_read_only_fragment(tmp_path: Path) -> None:
    from noeta.presets import main_options

    for read_only in (False, True):
        p = _Rec(responses=[_say()])
        c = Client(
            main_options(),
            provider=p,
            workspace_dir=tmp_path,
            host_config=HostConfig(
                memory_read_only=read_only, memory_dir=tmp_path / "mem"
            ),
        )
        try:
            c.start(goal="hi")
        finally:
            c.shutdown()
        system = _system_text(p.requests[0])
        if read_only:
            assert MEMORY_POLICY_READ_ONLY_PROMPT in system
            assert "memory_write" not in system
        else:
            assert MEMORY_POLICY_PROMPT in system


def test_operator_read_only_override_keeps_the_curator_writable(
    tmp_path: Path,
) -> None:
    from noeta.presets import CONSOLIDATION_AGENT_NAME, official_specs
    from noeta.runtime.shell_policy import ShellMode

    from tests._sdk_session import make_host, make_registry

    main = official_specs()["main"]
    import dataclasses

    curator = dataclasses.replace(main, name=CONSOLIDATION_AGENT_NAME)
    host = make_host(
        make_registry(main, curator),
        workspace_dir=tmp_path,
        provider=FakeLLMProvider(responses=[]),
        model="stub-model",
        plugin_config_overrides={"memory": {"read_only": True}},
    )
    bag_main = host._plugin_config(shell_mode=ShellMode.OFF, spec=main)
    bag_curator = host._plugin_config(shell_mode=ShellMode.OFF, spec=curator)
    assert bag_main["memory"]["read_only"] is True
    assert bag_curator["memory"]["read_only"] is False


def test_provider_headers_reach_the_web_pack(tmp_path: Path) -> None:
    from noeta.runtime.shell_policy import ShellMode

    from tests._sdk_session import make_host, make_registry, runner_main_spec

    def headers(ctx: Any) -> dict[str, str]:
        return {"x-tenant": "t"}

    spec = runner_main_spec("main")
    host = make_host(
        make_registry(spec),
        workspace_dir=tmp_path,
        provider=FakeLLMProvider(responses=[]),
        model="stub-model",
        provider_headers=headers,
    )
    bag = host._plugin_config(shell_mode=ShellMode.OFF, spec=spec)
    assert bag["web"]["provider_headers"] is headers
    bare = make_host(
        make_registry(spec),
        workspace_dir=tmp_path,
        provider=FakeLLMProvider(responses=[]),
        model="stub-model",
    )
    assert "provider_headers" not in bare._plugin_config(
        shell_mode=ShellMode.OFF, spec=spec
    ).get("web", {})


# ---------------------------------------------------------------------------
# D4 — model aliases resolve at Engine build
# ---------------------------------------------------------------------------


def test_subagent_declared_alias_is_sent_resolved(tmp_path: Path) -> None:
    p = _Rec(responses=[_call(SPAWN_SUBAGENT_TOOL, {"agent": "helper", "goal": "g"}),
                        _say("child"), _say("parent")])
    opts = Options(
        system_prompt="delegate",
        agents={"helper": AgentDefinition(description="h", prompt="h", model="haiku")},
    )
    c = Client(opts, provider=p, workspace_dir=tmp_path, multi_turn=False)
    try:
        out = c.start(goal="go")
        children = [
            e.payload.subtask_id
            for e in c.events(out.task_id)
            if e.type == "SubtaskSpawned"
        ]
        bound = [
            e.payload.model
            for e in c.events(str(children[0]))
            if e.type == "ModelBound"
        ]
    finally:
        c.shutdown()
    assert p.requests[1].model == resolve_model_alias("haiku") != "haiku"
    # The child's recorded binding is the resolved id too.
    assert bound == [resolve_model_alias("haiku")]


def test_legacy_alias_binding_resolves_on_resume(tmp_path: Path) -> None:
    from noeta.storage.memory import InMemoryDispatcher, InMemoryEventLog

    disp = InMemoryDispatcher()
    hc = HostConfig(
        event_log=InMemoryEventLog(lease_validator=disp),
        content_store=InMemoryContentStore(),
        dispatcher=disp,
    )
    opts = Options(system_prompt="x", allowed_tools=())
    # A 0.6.29 store: the opening ModelBound recorded the alias unresolved.
    with mock.patch("noeta.client.client.resolve_model_alias", lambda s: s):
        c1 = Client(opts, provider=_Rec(responses=[_say()]), workspace_dir=tmp_path,
                    host_config=hc)
        tid = c1.start(goal="first").task_id
    assert [
        e.payload.model for e in hc.event_log.read(tid) if e.type == "ModelBound"
    ] == ["sonnet"]
    p2 = _Rec(responses=[_say()])
    c2 = Client(opts, provider=p2, workspace_dir=tmp_path, host_config=hc)
    c2.send_goal(tid, goal="second")
    assert [r.model for r in p2.requests] == [resolve_model_alias("sonnet")]


def test_inherited_model_compares_resolved_ids(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from tests._sdk_session import make_host, make_registry, runner_main_spec

    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=tmp_path,
        provider=FakeLLMProvider(responses=[]),
        model=resolve_model_alias("sonnet"),
    )
    root = SimpleNamespace(governance=SimpleNamespace(model_binding="sonnet"))
    # An alias of the host default is the default, not a switch to inherit.
    assert host._inherited_model_of(root) is None
    root.governance.model_binding = "opus"
    assert host._inherited_model_of(root) == resolve_model_alias("opus")


# ---------------------------------------------------------------------------
# D5 — the post-switch rebuild is compensated; turn knobs validate at entry
# ---------------------------------------------------------------------------


def test_failed_post_switch_rebuild_resuspends_and_releases(tmp_path: Path) -> None:
    from noeta.core.fold import fold

    p = _Rec(responses=[_say("a"), _say("b")])
    c = Client(Options(system_prompt="x", allowed_tools=()), provider=p,
               workspace_dir=tmp_path, model="claude-sonnet-5")
    try:
        tid = c.start(goal="first").task_id
        host = c._host
        orig = host.resolve_engine
        calls = {"n": 0}

        def flaky(task: Any) -> Any:
            calls["n"] += 1
            if calls["n"] == 2:  # the rebuild after the ModelBound prelude
                raise OSError("transient")
            return orig(task)

        with mock.patch.object(host, "resolve_engine", flaky):
            with pytest.raises(OSError):
                c.send_goal(tid, goal="second", model_selector="opus")
        task = fold(host.event_log, host.content_store, tid)
        assert task.status == "suspended"
        assert not host.dispatcher.has_active_lease(tid)
        # The caller can retry.
        out = c.send_goal(tid, goal="retry", model_selector="opus")
        assert out.status == "suspended"
    finally:
        c.shutdown()


@pytest.mark.parametrize(
    "kwargs",
    [{"permission_mode": "plan"}, {"effort": "bogus"}],
)
def test_bad_turn_knob_is_refused_before_any_write(
    tmp_path: Path, kwargs: dict[str, Any]
) -> None:
    from noeta.sdk import InvalidTurnOptionError

    c = Client(Options(system_prompt="x"), provider=_Rec(responder=lambda r: _say()),
               workspace_dir=tmp_path)
    try:
        tid = c.start(goal="hi").task_id
        before = len(c.events(tid))
        for verb in (c.send_goal, c.seed_send_goal):
            with pytest.raises(InvalidTurnOptionError) as info:
                verb(tid, goal="g", **kwargs)
            assert isinstance(info.value, ValueError)
            assert info.value.code == "invalid_turn_option"
        with pytest.raises(InvalidTurnOptionError):
            c.start(goal="g", **kwargs)
        assert len(c.events(tid)) == before
        assert c.send_goal(tid, goal="fine").status == "suspended"
    finally:
        c.shutdown()


def test_turn_effort_respects_session_thinking(tmp_path: Path) -> None:
    from noeta.sdk import InvalidTurnOptionError

    c = Client(Options(system_prompt="x", thinking="disabled"),
               provider=_Rec(responder=lambda r: _say()), workspace_dir=tmp_path)
    try:
        with pytest.raises(InvalidTurnOptionError):
            c.start(goal="g", effort="max")
        assert c.start(goal="g", effort="high").status == "suspended"
    finally:
        c.shutdown()


# ---------------------------------------------------------------------------
# D9 — HostConfig validation, reliability_sink, lease_backoff_max_s
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"recall_exclude": "abc"},
        {"recall_exclude": (1, 2)},
        {"memory_max_bytes": 0},
        {"memory_index_budget_tokens": -5},
        {"memory_index_budget_tokens": 0},
        {"max_background_jobs_per_root_task": 0},
        {"max_background_subagents_per_root_task": -1},
        {"mcp_idle_ttl": -1.0},
        {"instructions_file": Path("/x/NOETA.md")},
        {"storage_path": ""},
    ],
)
def test_host_config_rejects_meaningless_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        HostConfig(**kwargs)


def test_host_config_accepts_the_legal_forms() -> None:
    HostConfig(
        recall_exclude=frozenset({"owner"}),
        memory_max_bytes=1,
        memory_index_budget_tokens=1,
        mcp_idle_ttl=0.0,
        instructions_enabled=True,
        instructions_file=Path("/x/NOETA.md"),
    )


def test_unknown_plugin_config_name_warns(tmp_path: Path) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        c = Client(Options(system_prompt="x"), provider=_Rec(responses=[]),
                   workspace_dir=tmp_path,
                   host_config=HostConfig(plugin_config={"no_such_plugin": {"a": 1},
                                                         "memory": {}}))
        c.shutdown()
    messages = [str(w.message) for w in caught if issubclass(w.category, UserWarning)]
    assert any("no_such_plugin" in m for m in messages)
    assert not any("'memory'" in m.split("known")[0] for m in messages)


def test_start_workers_forwards_reliability_sink_and_backoff(tmp_path: Path) -> None:
    sink = lambda event: None  # noqa: E731
    c = Client(Options(system_prompt="x"), provider=_Rec(responses=[]),
               workspace_dir=tmp_path, host_config=HostConfig(reliability_sink=sink))
    try:
        c.start_workers(1, lease_backoff_max_s=2.5)
        loop = c._worker_loops[0]
        assert loop._reliability_sink is sink
        assert loop._lease_backoff_max_s == 2.5
    finally:
        c.shutdown()


# ---------------------------------------------------------------------------
# D10 — typed, exported errors; QueryFailedError.detail
# ---------------------------------------------------------------------------


def test_errors_are_coded_and_exported() -> None:
    import noeta.sdk as sdk

    for name, code in (
        ("AnswerValidationError", "invalid_answer"),
        ("WorkspaceEscape", "workspace_escape"),
        ("InvalidTurnOptionError", "invalid_turn_option"),
        ("QuestionNotPendingError", "question_not_pending"),
    ):
        cls = getattr(sdk, name)
        assert issubclass(cls, CodedError) and cls.code == code
        assert name in sdk.__all__
    assert issubclass(sdk.WorkspaceEscape, ValueError)
    assert issubclass(sdk.AnswerValidationError, ValueError)
    from noeta.protocols.tool_args import resolve_tool_call_arguments
    from noeta.runtime.worker import WorkerLoop

    assert sdk.WorkerLoop is WorkerLoop
    assert sdk.resolve_tool_call_arguments is resolve_tool_call_arguments


def test_query_failed_error_carries_detail() -> None:
    err = QueryFailedError(task_id="t", status="failed", reason="llm_error",
                           detail="400: bad request body")
    assert err.detail == "400: bad request body"
    assert "llm_error" in str(err) and "400: bad request body" in str(err)
    plain = QueryFailedError(task_id="t", status="failed", reason="llm_error")
    assert plain.detail == "" and str(plain).endswith("llm_error")


# ---------------------------------------------------------------------------
# D11 — ToolResultView names its tool and carries the error
# ---------------------------------------------------------------------------


def test_tool_result_view_has_tool_name_and_error(tmp_path: Path) -> None:
    from noeta.sdk import ToolResultView

    p = _Rec(responses=[_call("Read", {"file_path": str(tmp_path / "missing.txt")}),
                        _say("done")])
    c = Client(Options(system_prompt="x", allowed_tools=("Read",)), provider=p,
               workspace_dir=tmp_path, multi_turn=False)
    try:
        tid = c.start(goal="read it").task_id
        views = [m for m in c.messages(tid) if isinstance(m, ToolResultView)]
    finally:
        c.shutdown()
    assert views and views[0].tool_name == "Read"
    assert views[0].success is False and views[0].error


# ---------------------------------------------------------------------------
# D12 — Task tool argument values
# ---------------------------------------------------------------------------


def _task_decision(args: dict[str, Any]) -> Any:
    block = ToolUseBlock(call_id="t1", tool_name=SPAWN_SUBAGENT_TOOL, arguments=args)
    response = LLMResponse(stop_reason="tool_use", content=[block],
                           usage=Usage(uncached=1, output=1))
    return _maybe_spawn_decision(response, Message(role="assistant", content=[block]))


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        ({"description": "d", "prompt": "p", "subagent_type": "a",
          "background": "false"}, "background"),
        ({"description": "d", "prompt": "   ", "subagent_type": "a"}, "prompt"),
    ],
)
def test_task_tool_refuses_unusable_values(args: dict[str, Any], needle: str) -> None:
    decision = _task_decision(args)
    assert not isinstance(decision, SpawnSubtaskDecision)
    block = decision.messages_after[0].content[0]
    assert block.success is False and needle in block.error


def test_task_tool_accepts_boolean_background() -> None:
    base = {"description": "d", "prompt": "p", "subagent_type": "a"}
    assert _task_decision({**base, "background": True}).background is True
    assert _task_decision({**base, "background": False}).background is False
    assert _task_decision(base).background is False


# ---------------------------------------------------------------------------
# D13 — memory: judge cap + headers, page cap, created, prose fences
# ---------------------------------------------------------------------------


def test_judge_index_is_capped_most_recent_first() -> None:
    entries = tuple((f"page-{i}", "x" * 200, "", "") for i in range(20))
    full = render_judge_system(entries)
    capped = render_judge_system(entries, budget_tokens=120)
    assert len(capped) < len(full)
    # The caller's order decides who stays: the first entries it offers.
    assert "page-0" in capped and "page-19" not in capped
    # Under budget the text is the whole index, byte for byte.
    assert render_judge_system(entries, budget_tokens=10**6) == full


def test_recall_offers_the_judge_most_recently_written_first(tmp_path: Path) -> None:
    store = MemoryStore(root=tmp_path)
    store.write("old", "---\nupdated: 2026-01-01\n---\nold page")
    store.write("new", "---\nupdated: 2026-09-01\n---\nnew page")
    seen: list[tuple[str, ...]] = []

    def judge(entries: Any, text: str) -> tuple[str, ...]:
        seen.append(tuple(e[0] for e in entries))
        return ()

    recall_memories(store, "unrelated words", judge=judge)
    assert seen == [("new", "old")]


class _HeaderProvider:
    def __init__(self) -> None:
        self.headers: list[Any] = []

    def complete(self, request: Any) -> LLMResponse:
        self.headers.append(None)
        return _say('["a"]')

    def complete_with_headers(self, request: Any, headers: Any) -> LLMResponse:
        self.headers.append(dict(headers or {}))
        return _say('["a"]')


def test_judge_sends_provider_headers() -> None:
    provider = _HeaderProvider()
    judge = build_recall_judge(provider, "m", request_headers=lambda: {"x-tenant": "t1"})
    assert judge((("a", "", "", ""),), "hi") == ("a",)
    assert provider.headers == [{"x-tenant": "t1"}]
    bare = build_recall_judge(provider, "m")
    bare((("a", "", "", ""),), "hi")
    assert provider.headers[-1] is None


def test_memory_write_created_is_tool_stamped(tmp_path: Path, monkeypatch: Any) -> None:
    import noeta.builtins.memory.impl.store as store_mod

    monkeypatch.setattr(store_mod, "_today", lambda: "2026-09-25")
    store = MemoryStore(root=tmp_path)
    tool = MemoryWriteTool(store=store)
    assert tool.invoke(
        {"name": "n", "text": "---\ncreated: 1999-01-01\n---\nbody"}, _ctx()
    ).success
    assert "created: 2026-09-25" in store.read("n")
    monkeypatch.setattr(store_mod, "_today", lambda: "2026-09-26")
    tool.invoke({"name": "n", "text": "---\ncreated: 2000-01-01\n---\nv2"}, _ctx())
    text = store.read("n")
    assert "created: 2026-09-25" in text and "updated: 2026-09-26" in text


def test_memory_write_prose_after_a_rule_is_body(tmp_path: Path) -> None:
    store = MemoryStore(root=tmp_path)
    tool = MemoryWriteTool(store=store)
    prose = "---\nStep 1: install\n---\nthen run it"
    assert tool.invoke({"name": "howto", "text": prose}, _ctx()).success
    assert store.read("howto").endswith(prose)
    # A lowercase field-name fence is still a fence (custom keys work).
    assert tool.invoke(
        {"name": "task", "text": "---\nstatus: open\n---\nbody"}, _ctx()
    ).success
    text = store.read("task")
    assert "status: open" in text and text.endswith("---\nbody")


# ---------------------------------------------------------------------------
# D14 — a sub-agent without the delegation activation gets no Task tool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("plugins", "has_task"), [((), False), (("delegation",), True)])
def test_child_gets_task_only_when_it_activates_delegation(
    tmp_path: Path, plugins: tuple[str, ...], has_task: bool
) -> None:
    p = _Rec(responses=[_call(SPAWN_SUBAGENT_TOOL, {"agent": "helper", "goal": "g"}),
                        _say("child"), _say("parent")])
    opts = Options(
        system_prompt="delegate",
        agents={
            "helper": AgentDefinition(description="h", prompt="h", plugins=plugins)
        },
    )
    c = Client(opts, provider=p, workspace_dir=Path(tempfile.mkdtemp(dir=tmp_path)),
               multi_turn=False)
    try:
        c.start(goal="go")
    finally:
        c.shutdown()
    assert SPAWN_SUBAGENT_TOOL in _tool_names(p.requests[0])
    assert (SPAWN_SUBAGENT_TOOL in _tool_names(p.requests[1])) is has_task
