"""WP-H: hooks and ``Principal`` on the public surface, plus the D5 retry.

* ``HostConfig.hooks`` validates at construction, feeds its pre-tool-use rules
  to the ``HookGuard``, and runs post-tool-use commands from one observer the
  Client starts and ``shutdown`` stops.
* ``Client(principal=)`` and the per-turn ``principal=`` gate the model
  selector and stamp ``ModelBound.principal_identity``.
* A ``send_goal`` retried after a failed post-switch rebuild records the goal
  once.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

from noeta.protocols.messages import ToolUseBlock
from noeta.sdk import (
    LOCAL_PRINCIPAL,
    Client,
    HooksConfig,
    HostConfig,
    LLMResponse,
    MatchArg,
    ModelSelectorError,
    NotificationRule,
    Options,
    PostToolUseRule,
    PreToolUseRule,
    Principal,
    TextBlock,
    ToolContext,
    ToolResult,
    Usage,
    tool,
)
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


_RUNS: list[str] = []


@tool(
    name="greet",
    version="1",
    risk_level="low",
    input_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}},
        "additionalProperties": False,
    },
)
def _greet(arguments: dict, ctx: ToolContext) -> ToolResult:
    _RUNS.append(str(arguments.get("name")))
    return ToolResult(success=True, output="hi")


def _bound(c: Client, tid: str) -> list[Any]:
    return [e.payload for e in c.events(tid) if e.type == "ModelBound"]


# ---------------------------------------------------------------------------
# H1 — HooksConfig validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"pre_tool_use": [PreToolUseRule("x", "deny")]}, "must be a tuple"),
        ({"pre_tool_use": (PreToolUseRule("x", "block"),)}, "action"),  # type: ignore[arg-type]
        ({"pre_tool_use": (PreToolUseRule("", "deny"),)}, "match_tool"),
        (
            {"pre_tool_use": (PreToolUseRule(
                "x", "deny", match_arg=MatchArg(path=(), op="equals")),)},
            "path",
        ),
        (
            {"pre_tool_use": (PreToolUseRule(
                "x", "deny", match_arg=MatchArg(path=("a",), op="contains", value=3)),)},
            "contains",
        ),
        (
            {"pre_tool_use": (PreToolUseRule(
                "x", "deny", match_arg=MatchArg(path=("a",), op="regex", value="(")),)},
            "invalid regex",
        ),
        ({"post_tool_use": (PostToolUseRule("x"),)}, "does nothing"),
        ({"post_tool_use": (PostToolUseRule("x", command=["echo"]),)}, "argv"),  # type: ignore[arg-type]
        ({"post_tool_use": (PostToolUseRule("x", command=()),)}, "argv"),
        ({"notification": (NotificationRule("done", log=True),)}, "approval"),
        ({"post_tool_use": (PreToolUseRule("x", "deny"),)}, "PostToolUseRule"),  # type: ignore[arg-type]
        ({"command_timeout_s": 0}, "command_timeout_s"),
        ({"max_queue": 0}, "max_queue"),
        ({"max_queue": True}, "max_queue"),
    ],
)
def test_hooks_config_rejects_meaningless_values(
    kwargs: dict[str, Any], needle: str
) -> None:
    with pytest.raises(ValueError, match=re.escape(needle)):
        HooksConfig(**kwargs)


def test_hooks_config_compiles_a_regex_given_as_text() -> None:
    cfg = HooksConfig(
        pre_tool_use=(
            PreToolUseRule(
                "Bash", "deny",
                match_arg=MatchArg(path=("command",), op="regex", value=r"^rm "),
            ),
        )
    )
    ma = cfg.pre_tool_use[0].match_arg
    assert ma is not None and ma.pattern is not None
    assert ma.pattern.search("rm -rf /")


def test_host_config_rejects_a_non_hooks_config() -> None:
    with pytest.raises(ValueError, match="HooksConfig"):
        HostConfig(hooks=(PreToolUseRule("x", "deny"),))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# H1 — wiring
# ---------------------------------------------------------------------------


def test_pre_tool_use_rule_reaches_the_guard(tmp_path: Path) -> None:
    _RUNS.clear()
    p = _Rec(responses=[_call("greet", {"name": "bob"}), _say("done")])
    hc = HostConfig(hooks=HooksConfig(pre_tool_use=(
        PreToolUseRule(
            "greet", "deny",
            match_arg=MatchArg(path=("name",), op="equals", value="bob"),
            reason="no greeting bob",
        ),
    )))
    c = Client(Options(system_prompt="x", allowed_tools=(_greet,)),
               provider=p, workspace_dir=tmp_path, host_config=hc)
    try:
        tid = c.start(goal="greet bob").task_id
        assert _RUNS == []
        types = [e.type for e in c.events(tid)]
        assert "ToolCallDenied" in types
        assert c._hook_observer is None  # no post / notification rules
    finally:
        c.shutdown()


def test_post_tool_use_command_runs_and_shutdown_stops_the_observer(
    tmp_path: Path,
) -> None:
    _RUNS.clear()
    marker = tmp_path / "hook-ran"
    p = _Rec(responses=[_call("greet", {"name": "ann"}), _say("done")])
    hc = HostConfig(hooks=HooksConfig(
        post_tool_use=(PostToolUseRule("gr*", command=("touch", str(marker))),),
        command_timeout_s=5,
    ))
    c = Client(Options(system_prompt="x", allowed_tools=(_greet,)),
               provider=p, workspace_dir=tmp_path, host_config=hc)
    observer = c._hook_observer
    try:
        assert observer is not None
        c.start(goal="greet ann")
        assert _RUNS == ["ann"]
        deadline = time.monotonic() + 5.0
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
    finally:
        c.shutdown()
    assert not observer._worker.is_alive()  # type: ignore[attr-defined]


def test_no_hooks_builds_no_observer(tmp_path: Path) -> None:
    c = Client(Options(system_prompt="x"), provider=_Rec(responses=[_say()]),
               workspace_dir=tmp_path,
               host_config=HostConfig(hooks=HooksConfig()))
    try:
        assert c._hook_observer is None
    finally:
        c.shutdown()


# ---------------------------------------------------------------------------
# H2 — Principal
# ---------------------------------------------------------------------------


def test_client_principal_gates_and_stamps(tmp_path: Path) -> None:
    alice = Principal(identity="alice", allowed_models=frozenset({"opus"}))
    p = _Rec(responder=lambda r: _say())
    c = Client(Options(system_prompt="x"), provider=p, workspace_dir=tmp_path,
               principal=alice)
    try:
        with pytest.raises(ModelSelectorError):
            c.start(goal="g", model_selector="sonnet")
        tid = c.start(goal="g", model_selector="opus").task_id
        assert [b.principal_identity for b in _bound(c, tid)] == ["alice"]
    finally:
        c.shutdown()


def test_per_turn_principal_overrides_the_client_default(tmp_path: Path) -> None:
    bob = Principal(identity="bob", allowed_models=frozenset({"haiku"}))
    p = _Rec(responder=lambda r: _say())
    c = Client(Options(system_prompt="x"), provider=p, workspace_dir=tmp_path)
    try:
        tid = c.start(goal="g").task_id
        assert _bound(c, tid)[0].principal_identity == LOCAL_PRINCIPAL.identity
        before = len(c.events(tid))
        # bob may not bind opus even though the Client's own ⊤ principal may.
        with pytest.raises(ModelSelectorError):
            c.send_goal(tid, goal="g2", model_selector="opus", principal=bob)
        assert len(c.events(tid)) == before
        c.send_goal(tid, goal="g2", model_selector="haiku", principal=bob)
        assert _bound(c, tid)[-1].principal_identity == "bob"
        # The seed twins carry it too, through drive_seeded.
        seeded = c.seed_start(goal="s", model_selector="haiku", principal=bob)
        c.drive_seeded(seeded)
        assert _bound(c, seeded.task_id)[0].principal_identity == "bob"
        seeded2 = c.seed_send_goal(
            seeded.task_id, goal="s2", model_selector="haiku",
            principal=Principal(identity="carol", allows_any=True),
        )
        c.drive_seeded(seeded2)
        assert _bound(c, seeded.task_id)[-1].principal_identity == "carol"
    finally:
        c.shutdown()


# ---------------------------------------------------------------------------
# D5 — a retried send_goal records the goal once
# ---------------------------------------------------------------------------


def _user_texts(c: Client, tid: str) -> list[str]:
    from noeta.core.fold import fold

    host = c._host
    task = fold(host.event_log, host.content_store, tid)
    return [
        b.text
        for m in task.runtime.messages
        if m.role == "user" and m.origin is None
        for b in m.content
        if isinstance(b, TextBlock)
    ]


def test_retry_after_failed_post_switch_rebuild_records_the_goal_once(
    tmp_path: Path,
) -> None:
    p = _Rec(responses=[_say("a"), _say("b"), _say("c")])
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
        assert _user_texts(c, tid) == ["first", "second"]
        reason = c.suspend_reason(tid)
        assert reason is not None and reason.kind == "seed_failed"
        bound_before = len(_bound(c, tid))

        out = c.send_goal(tid, goal="second", model_selector="opus")
        assert out.status == "suspended"
        assert _user_texts(c, tid) == ["first", "second"]
        # The failed seed already wrote this switch; the retry adds none.
        assert len(_bound(c, tid)) == bound_before
        # And the retried turn is answered on the switched model.
        assert p.requests[-1].model != "claude-sonnet-5"
        # A later identical goal after an answer is a new turn, not a retry.
        assert c.send_goal(tid, goal="second").status == "suspended"
        assert len(p.requests) == 3
        assert _user_texts(c, tid) == ["first", "second", "second"]
    finally:
        c.shutdown()
