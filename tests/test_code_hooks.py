"""Phase 4.5 F3 — user hooks end-to-end through the code session + parser.

Covers: PreToolUse deny / require_approval through a real session;
PostToolUse observer behavior; and the strict-fail-fast parser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.agent.observe.hooks_config import HooksConfig, HooksConfigError, parse_hooks_obj
from noeta.guards.hook import PreToolUseRule
from noeta.observers.hook import (
    HookObserver,
    PostToolUseRule,
    make_subprocess_runner,
)
from noeta.protocols.messages import LLMResponse, TextBlock, ToolUseBlock, Usage
from noeta.testing.fake_llm import FakeLLMProvider
from noeta.tools.fs import FsWriteMode, ShellMode

from tests._sdk_session import make_driver, make_host, make_registry, runner_main_spec


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    return ws


def _call(call_id: str, name: str, args: dict[str, Any]) -> LLMResponse:
    return LLMResponse(
        stop_reason="tool_use",
        content=[ToolUseBlock(call_id=call_id, tool_name=name, arguments=args)],
        usage=Usage(uncached=1, output=1),
        raw={"id": call_id},
    )


def _end() -> LLMResponse:
    return LLMResponse(
        stop_reason="end_turn",
        content=[TextBlock(text="done")],
        usage=Usage(uncached=1, output=1),
        raw={"id": "end"},
    )


def _write_responses() -> list[LLMResponse]:
    return [_call("c1", "write", {"path": "new.py", "content": "x\n"}), _end()]


def _session(tmp_path: Path, *, hooks: HooksConfig):
    """A one-shot SDK session wired with ``hooks``.

    ``pre_tool_use`` rules are a guard → the host ``hooks_pre_tool_use`` field.
    ``post_tool_use`` / ``notification`` rules are a live-only ``HookObserver``
    (the SDK observer extension seam — it self-subscribes on the host event log).
    ``require_approval_tools=()`` so the host's default permission gate does not
    pre-empt the HookGuard's own deny / require_approval verdict on ``write``.
    """
    ws = _ws(tmp_path)
    host = make_host(
        make_registry(runner_main_spec("main")),
        workspace_dir=ws,
        provider=FakeLLMProvider(responses=_write_responses()),
        model="gpt-test",
        multi_turn=False,
        write_mode=FsWriteMode.APPLY,
        shell_mode=ShellMode.OFF,
        require_approval_tools=(),
        hooks_pre_tool_use=hooks.pre_tool_use,
    )
    if hooks.post_tool_use or hooks.notification:
        HookObserver(
            event_log=host.event_log,
            post_tool_use=hooks.post_tool_use,
            notification=hooks.notification,
            runner=make_subprocess_runner(cwd=str(ws)),
        )
    return host, make_driver(host), ws


# -- PreToolUse deny / require_approval (live) -------------------------------


def test_pre_tool_use_deny(tmp_path: Path) -> None:
    hooks = HooksConfig(
        pre_tool_use=(PreToolUseRule(match_tool="write", action="deny"),),
        post_tool_use=(),
        notification=(),
    )
    host, driver, ws = _session(tmp_path, hooks=hooks)
    out = driver.start(goal="write a file", agent="main")
    events = host.event_log.read(out.task_id)
    assert any(e.type == "ToolCallDenied" for e in events)
    assert not (ws / "new.py").exists()  # the write never ran
    assert out.status == "terminal"


def test_pre_tool_use_require_approval_round_trip(tmp_path: Path) -> None:
    hooks = HooksConfig(
        pre_tool_use=(
            PreToolUseRule(match_tool="write", action="require_approval"),
        ),
        post_tool_use=(),
        notification=(),
    )
    host, driver, ws = _session(tmp_path, hooks=hooks)
    out = driver.start(goal="write a file", agent="main")
    assert out.status == "suspended"
    assert out.wake_handle == "approval-c1"
    result = driver.approve(out.task_id, call_id="c1")
    assert result.status == "terminal"
    assert (ws / "new.py").read_text() == "x\n"


# -- PostToolUse observer (live) ---------------------------------------------


def test_post_tool_use_observer_records_normally(tmp_path: Path) -> None:
    """A session with a PostToolUse notify hook records normally; the
    observer is live-only and writes nothing to the EventLog."""
    hooks = HooksConfig(
        pre_tool_use=(),
        post_tool_use=(PostToolUseRule(match_tool="*", log=True),),
        notification=(),
    )
    host, driver, ws = _session(tmp_path, hooks=hooks)
    out = driver.start(goal="write a file", agent="main")
    events = host.event_log.read(out.task_id)
    assert out.status == "terminal"
    assert (ws / "new.py").read_text() == "x\n"
    # PostToolUse notify is an observer: no PostToolUse event type is folded.
    assert not any(e.type == "PostToolUse" for e in events)


# -- parser fail-fast --------------------------------------------------------


def test_parser_happy_path() -> None:
    cfg = parse_hooks_obj(
        {
            "pre_tool_use": [
                {"match_tool": "write", "action": "require_approval"},
                {
                    "match_tool": "shell_run",
                    "match_arg": {"path": "command", "contains": "rm -rf"},
                    "action": "deny",
                },
            ],
            "post_tool_use": [{"match_tool": "*", "notify": {"command": ["./x.sh"]}}],
            "notification": [{"on": "approval", "notify": {"log": True}}],
        }
    )
    assert len(cfg.pre_tool_use) == 2
    assert cfg.pre_tool_use[1].match_arg is not None
    assert cfg.post_tool_use[0].command == ("./x.sh",)
    assert cfg.notification[0].on == "approval"


@pytest.mark.parametrize(
    "obj",
    [
        {"bogus": []},  # unknown top-level key
        {"pre_tool_use": [{"match_tool": "x", "action": "nope"}]},  # bad action
        {"pre_tool_use": [{"match_tool": "x", "action": "deny", "typo": 1}]},  # unknown rule key
        {"pre_tool_use": [{"match_tool": "x", "action": "deny", "match_arg": {"path": "p", "regex": "("}}]},  # bad regex
        {"pre_tool_use": [{"match_tool": "x", "action": "deny", "match_arg": {"path": "p"}}]},  # no op
        {"pre_tool_use": [{"match_tool": "x", "action": "deny", "match_arg": {"path": "p", "equals": 1, "contains": "y"}}]},  # two ops
        {"notification": [{"on": "weird", "notify": {"log": True}}]},  # bad on
        {"post_tool_use": [{"match_tool": "*", "notify": {"command": []}}]},  # empty command
        {"post_tool_use": [{"match_tool": "*", "notify": {"command": ["", "x"]}}]},  # empty arg
        {"post_tool_use": [{"match_tool": "*", "notify": {}}]},  # neither command nor log
        {"post_tool_use": [{"match_tool": "*"}]},  # missing notify
    ],
)
def test_parser_fail_fast(obj: dict[str, Any]) -> None:
    with pytest.raises(HooksConfigError):
        parse_hooks_obj(obj)
