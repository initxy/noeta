"""Hook configuration and the three hook types on their own: parsing,
matchers, ``if``, the HTTP and prompt runners, the command's exec form."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.core.types import Message, ModelRequest, ModelResponse, Text
from noeta.plugins.hooks import HookCommand, Hooks, parse_hooks, run_command, run_http, run_prompt
from noeta.plugins.hooks.config import COMMAND_TIMEOUT_S, PROMPT_TIMEOUT_S, Matcher
from noeta.plugins.hooks.runners import header_value


def _one(event: str, hook: dict[str, Any], matcher: str = "") -> HookCommand:
    [m] = parse_hooks({event: [{"matcher": matcher, "hooks": [hook]}]})[event]
    [h] = m.hooks
    return h


def test_unknown_and_unsupported_events_and_types_are_skipped(
        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, "noeta.hooks")
    parsed = parse_hooks({
        "Bogus": [{"hooks": [{"type": "command", "command": "x"}]}],
        "PreModelSwitch": [{"hooks": [{"type": "command", "command": "x"}]}],
        "Stop": [{"hooks": [{"type": "script", "file": "x"}]}],
        "PermissionRequest": [{"hooks": [{"type": "agent", "prompt": "allow?"}]}],
    })
    assert parsed == {}
    text = caplog.text
    assert "unknown event 'Bogus'" in text and "does not fire PreModelSwitch" in text
    assert "'script' hooks" in text and "permission request" in text


def test_hook_fields_and_defaults() -> None:
    command = _one("PreToolUse", {"type": "command", "command": "c", "args": ["a", "b"],
                                  "if": "Bash(git *)", "once": True, "async": True,
                                  "onFailure": "block"})
    assert command.args == ("a", "b") and command.timeout == COMMAND_TIMEOUT_S == 600
    assert command.condition is not None and command.condition.text == "Bash(git *)"
    assert command.once and command.background and command.block_on_failure
    http = _one("Stop", {"type": "http", "url": "https://h/x", "timeout": 5,
                         "headers": {"A": "$T"}, "allowedEnvVars": ["T"]})
    assert (http.type, http.timeout, http.headers, http.allowed_env_vars) == \
        ("http", 5.0, (("A", "$T"),), ("T",))
    prompt = _one("Stop", {"type": "prompt", "prompt": "done?", "model": "m2",
                           "continueOnBlock": True})
    assert prompt.timeout == PROMPT_TIMEOUT_S == 30 and prompt.model == "m2"
    assert prompt.continue_on_block
    agent = _one("Stop", {"type": "agent", "prompt": "tests ran?", "continueOnBlock": True})
    assert (agent.type, agent.timeout, agent.label) == ("agent", 60.0, "tests ran?")
    assert not agent.continue_on_block  # a prompt hook's field only
    tool = _one("PostToolUse", {"type": "mcp_tool", "server": "s", "tool": "t",
                                "input": {"p": "${tool_input.file_path}"}})
    assert (tool.label, tool.arguments, tool.timeout) == \
        ("s/t", {"p": "${tool_input.file_path}"}, COMMAND_TIMEOUT_S)
    assert tool.key != _one("PostToolUse", {"type": "mcp_tool", "server": "s",
                                            "tool": "t"}).key
    for bad in ({"type": "command"}, {"type": "http", "url": "ftp://x"},
                {"type": "prompt"}, {"type": "command", "command": "c", "if": "Bash("},
                {"type": "agent"}, {"type": "mcp_tool", "server": "s"},
                {"type": "mcp_tool", "server": "s", "tool": "t", "input": "x"},
                {"type": "command", "command": "c", "timeout": 0},
                {"type": "command", "command": "c", "args": "x"}):
        with pytest.raises(ValueError):
            parse_hooks({"Stop": [{"hooks": [bad]}]})


def test_matchers() -> None:
    def m(pattern: str, lenient: bool = True) -> Matcher:
        return Matcher(pattern, (), lenient=lenient)

    assert m("").matches("x") and m("*").matches("x") and m("Edit").matches(None)
    assert m("Edit|Write").matches("Write") and not m("Edit|Write").matches("EditX")
    assert m("Edit, Write").matches("Write")
    assert not m("Edit, Write", lenient=False).matches("Write")  # a regex there
    assert m("Task").matches("Agent") and m("Agent").matches("Task")
    assert m("mcp__.*").matches("mcp__srv__tool") and m("Note").matches("Note")
    assert m("Notebook.*").matches("NotebookEdit")
    assert m("^Edit$|^Write$").matches("Write") and not m("^Edit$").matches("MultiEdit")
    assert m(".*Edit").matches("MultiEdit")  # searched, as Claude Code's RegExp.test
    assert not m("(").matches("(")


def test_common_input_fields() -> None:
    hooks = Hooks()
    child = TaskView(state=TaskState(task_id="r.c1.1", agent="Explore", parent_id="r",
                                     root_id="r", config={"cwd": "/w",
                                                          "permission_mode": "plan"}))
    payload = hooks._d.payload("PreToolUse", child, {"tool_name": "Read"})
    assert payload == {
        "session_id": "r", "transcript_path": "", "cwd": "/w", "permission_mode": "plan",
        "hook_event_name": "PreToolUse", "task_id": "r.c1.1", "agent_id": "r.c1.1",
        "agent_type": "Explore", "tool_name": "Read"}
    root = TaskView(state=TaskState(task_id="r", agent="main"))
    payload = hooks._d.payload("Stop", root, {})
    assert payload["session_id"] == "r" and payload["permission_mode"] == "default"
    assert "agent_id" not in payload and "agent_type" not in payload


def test_settings_files(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "true"}]}]}}))
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    hooks = Hooks(settings_files=[settings, broken, tmp_path / "missing.json"],
                  hooks={"Stop": [{"hooks": [{"type": "command", "command": "false"}]}]})
    assert [h.command for m in hooks.matchers["Stop"] for h in m.hooks] == ["true", "false"]


# ---------------------------------------------------------------- command


async def test_exec_form_runs_without_a_shell(tmp_path: Path) -> None:
    out = tmp_path / "argv.txt"
    hook = HookCommand(command="sh", args=("-c", 'printf "%s|%s" "$1" "$2" > "$3"', "sh",
                                           "${CLAUDE_PROJECT_DIR}", "a b; $(whoami)", str(out)))
    result = await run_command(hook, {"x": 1}, str(tmp_path))
    assert result.exit_code == 0
    assert out.read_text() == f"{tmp_path}|a b; $(whoami)"


async def test_a_command_that_cannot_start_or_overruns_fails() -> None:
    missing = await run_command(HookCommand(command="/no/such/program", args=()), {}, None)
    assert missing.failed and missing.exit_code == -1
    slow = await run_command(HookCommand(command="sleep 5; echo late", timeout=0.1), {}, None)
    assert slow.failed and "timed out" in slow.stderr


@pytest.mark.parametrize("cancelled", [False, True])
def test_a_killed_command_is_reaped_before_the_call_returns(cancelled: bool) -> None:
    """Left unreaped, the killed process is closed by the garbage collector
    once its event loop has closed ("Event loop is closed")."""
    ignored: list[Any] = []
    hook = sys.unraisablehook
    sys.unraisablehook = ignored.append
    loop = asyncio.new_event_loop()
    try:
        task = loop.create_task(run_command(
            HookCommand(command="sleep 5", timeout=30 if cancelled else 0.2), {}, None))
        if cancelled:
            loop.run_until_complete(asyncio.sleep(0.3))
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            loop.run_until_complete(task)
        loop.close()
        del task
        gc.collect()
    finally:
        sys.unraisablehook = hook
    assert not ignored, [str(u.exc_value) for u in ignored]


# ---------------------------------------------------------------- http


async def test_http_hook_posts_the_event_with_allowed_env_headers(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOOK_TOKEN", "s3cret")
    monkeypatch.setenv("OTHER_SECRET", "nope")
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"decision": "block", "reason": "no"})

    hook = HookCommand(type="http", url="https://hooks.example/pre",
                       headers=(("Authorization", "Bearer ${HOOK_TOKEN}"),
                                ("X-Other", "[$OTHER_SECRET]")),
                       allowed_env_vars=("HOOK_TOKEN",))
    result = await run_http(hook, {"hook_event_name": "PreToolUse"},
                            httpx.MockTransport(handle))
    assert result.exit_code == 0 and result.data == {"decision": "block", "reason": "no"}
    [request] = seen
    assert request.method == "POST" and json.loads(request.content) == {
        "hook_event_name": "PreToolUse"}
    assert request.headers["authorization"] == "Bearer s3cret"
    assert request.headers["x-other"] == "[]"
    assert request.headers["content-type"] == "application/json"


async def test_http_hook_failures() -> None:
    def answer(status: int, body: str) -> httpx.MockTransport:
        return httpx.MockTransport(lambda _r: httpx.Response(status, text=body))

    hook = HookCommand(type="http", url="https://hooks.example/x")
    assert (await run_http(hook, {}, answer(500, "boom"))).failed
    assert (await run_http(hook, {}, answer(200, "plain text"))).failed
    assert (await run_http(hook, {}, answer(302, ""))).failed  # redirects are not followed
    empty = await run_http(hook, {}, answer(204, ""))
    assert empty.exit_code == 0 and empty.data == {}

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert (await run_http(hook, {}, httpx.MockTransport(unreachable))).failed


def test_header_value_strips_line_breaks() -> None:
    assert header_value("a$X\r\nb", ("X",), {"X": "1"}) == "a1b"
    assert header_value("${Y}", ("X",), {"Y": "2"}) == ""


# ---------------------------------------------------------------- prompt


def _model(*texts: str) -> tuple[list[ModelRequest], Any]:
    asked: list[ModelRequest] = []
    answers = list(texts)

    async def complete(request: ModelRequest) -> ModelResponse:
        asked.append(request)
        return ModelResponse(message=Message(role="assistant",
                                             content=[Text(text=answers.pop(0))]),
                             stop_reason="end_turn")

    return asked, complete


async def test_prompt_hook_asks_the_model() -> None:
    asked, complete = _model('{"ok": true, "reason": "fine"}',
                             '```json\n{"ok": false, "reason": "rm is not allowed"}\n```',
                             "I think so")
    hook = HookCommand(type="prompt", prompt="Is this safe? $ARGUMENTS", model="judge")
    ok = await run_prompt(hook, {"tool_name": "Bash"}, complete, "main")
    assert ok.exit_code == 0 and ok.data == {}
    request = asked[0]
    assert request.model == "judge" and not request.tools and request.thinking is False
    assert request.messages[-1].text == 'Is this safe? {"tool_name": "Bash"}'
    blocked = await run_prompt(hook, {}, complete, "main")
    assert blocked.data == {"decision": "block", "reason": "rm is not allowed",
                            "continue": False, "stopReason": "rm is not allowed"}
    assert (await run_prompt(hook, {}, complete, "main")).failed
    assert (await run_prompt(hook, {}, None, "main")).failed


async def test_prompt_stop_hook_reads_the_conversation() -> None:
    asked, complete = _model('{"ok": false, "reason": "tests not run"}',
                             '{"ok": false, "impossible": true, "reason": "no tests exist"}')
    hook = HookCommand(type="prompt", prompt="The tests ran.", continue_on_block=False)
    transcript = [Message(role="user", content=[Text(text="fix it")])]
    blocked = await run_prompt(hook, {}, complete, "main", transcript)
    assert blocked.data == {"decision": "block", "reason": "tests not run"}
    assert asked[0].model == "main" and asked[0].messages[0].text == "fix it"
    assert "Condition: The tests ran." in asked[0].messages[-1].text
    impossible = await run_prompt(hook, {}, complete, "main", transcript)
    assert impossible.data == {"systemMessage": "no tests exist"}


def test_a_matcher_with_an_mcp_tools_old_name_still_fires(
        caplog: pytest.LogCaptureFixture) -> None:
    """MCP names now keep the server's case: a matcher written under the
    old lower-cased naming, as a list or a regular expression, still
    matches, and the log says so."""
    assert Matcher("mcp__github__delete_repo", ()).matches("mcp__GitHub__delete_repo")
    assert Matcher("Bash|mcp__github__delete_repo", ()).matches("mcp__GitHub__delete_repo")
    assert Matcher("mcp__github__.*", ()).matches("mcp__GitHub__delete_repo")
    assert not Matcher("mcp__gitlab__.*", ()).matches("mcp__GitHub__delete_repo")
    assert "only by its old name" in caplog.text


def test_an_mcp_tool_hook_finds_the_tool_by_its_old_name() -> None:
    """``--permission-prompt-tool mcp__approver__ok`` and an ``mcp_tool``
    hook written before MCP names kept their case still reach the tool."""
    from noeta.core.extension import Extension
    from noeta.core.tool import ToolContext, ToolOutput, text_output
    from noeta.plugins.hooks.dispatch import _mcp_tool

    class Approve:
        name = "mcp__Approver__ok"
        description = "d"
        input_schema: dict[str, Any] = {"type": "object"}

        async def run(self, input: dict[str, Any], ctx: ToolContext) -> ToolOutput:
            return text_output("ok")

    tool = Approve()

    class Mcp(Extension):
        name = "mcp"

        def tools(self, view: TaskView) -> list[Any]:
            return [tool]

    view = TaskView(state=TaskState(), extensions=[Mcp()])
    hook = HookCommand(type="mcp_tool", server="approver", tool="ok")
    assert _mcp_tool(view, hook) is tool
    assert _mcp_tool(view, HookCommand(type="mcp_tool", server="Approver", tool="ok")) is tool
    assert _mcp_tool(view, HookCommand(type="mcp_tool", server="other", tool="ok")) is None
    denied = TaskView(state=TaskState(), extensions=[Mcp()],
                      denied_tools=["mcp__approver__ok"])
    assert _mcp_tool(denied, hook) is None


# ------------------------------------------------- old and former names narrow


def _answering(answer: dict[str, Any]) -> Any:
    from noeta.plugins.hooks.runners import HookResult

    async def runner(hook: HookCommand, payload: dict[str, Any], cwd: str | None) -> Any:
        return HookResult(exit_code=0, stdout=json.dumps(answer))

    return runner


def _pre(decision: str) -> dict[str, Any]:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                   "permissionDecision": decision}}


async def _gate(hooks: Hooks, name: str, input: dict[str, Any], *tools: Any) -> Any:
    from noeta.core.extension import Extension
    from noeta.core.types import ToolUse

    class Tools(Extension):
        name = "tools"

        def tools(self, view: TaskView) -> list[Any]:
            return list(tools)

    view = TaskView(state=TaskState(), extensions=[hooks, Tools()])
    return await hooks.before_tool(view, ToolUse(id="c", name=name, input=input))


async def test_a_hook_reached_by_an_mcp_tools_old_name_may_block_not_allow() -> None:
    """A PreToolUse matcher written as ``mcp__github__.*`` reaches a server
    ``GitHub``'s tools by the old naming: its deny holds, its allow is not
    taken (it may have been written for the user's own ``github``)."""
    from noeta.core.extension import Allow, Deny

    hook = {"matcher": "mcp__github__.*", "hooks": [{"type": "command", "command": "x"}]}
    old = Hooks(hooks={"PreToolUse": [hook]}, runner=_answering(_pre("allow")))
    assert await _gate(old, "mcp__GitHub__delete_repo", {}) is None
    assert isinstance(await _gate(old, "mcp__github__delete_repo", {}), Allow)
    blocking = Hooks(hooks={"PreToolUse": [hook]}, runner=_answering(_pre("deny")))
    assert isinstance(await _gate(blocking, "mcp__GitHub__delete_repo", {}), Deny)


async def test_a_hook_if_by_a_former_skill_name_allows_only_when_unambiguous() -> None:
    """``if: Skill(dup)``: met through a skill's former name. When another
    skill had that name too (the tool says an allow may not match), the
    hook may still block but its allow is not taken."""
    from noeta.core.extension import Allow, Deny

    class Skill:
        name = "Skill"
        description = "d"
        input_schema: dict[str, Any] = {"type": "object"}

        def __init__(self, unambiguous: bool) -> None:
            self.unambiguous = unambiguous

        async def rule_inputs(self, input: Any, view: Any) -> list[Any]:
            return [({**input, "skill": "dup"}, self.unambiguous)]

        async def run(self, input: dict[str, Any], ctx: Any) -> Any:
            raise AssertionError

    hook = {"matcher": "Skill", "hooks": [{"type": "command", "command": "x",
                                           "if": "Skill(dup)"}]}
    allows = Hooks(hooks={"PreToolUse": [hook]}, runner=_answering(_pre("allow")))
    call = {"skill": "one"}
    assert isinstance(await _gate(allows, "Skill", call, Skill(True)), Allow)
    assert await _gate(allows, "Skill", call, Skill(False)) is None
    denies = Hooks(hooks={"PreToolUse": [hook]}, runner=_answering(_pre("deny")))
    assert isinstance(await _gate(denies, "Skill", call, Skill(False)), Deny)


def test_an_mcp_tool_hook_by_an_old_name_two_tools_share_finds_neither() -> None:
    """``mcp_tool`` hooks and ``--permission-prompt-tool`` by an old name:
    found (and marked as found so) only when one tool had it."""
    from noeta.core.extension import Extension
    from noeta.plugins.hooks.dispatch import _find_mcp_tool

    def named(name: str) -> Any:
        return type("T", (), {"name": name, "description": "d", "input_schema": {}})()

    def view(*tools: Any) -> TaskView:
        class Mcp(Extension):
            name = "mcp"

            def tools(self, view: TaskView) -> list[Any]:
                return list(tools)

        return TaskView(state=TaskState(), extensions=[Mcp()])

    hook = HookCommand(type="mcp_tool", server="approver", tool="ok")
    one = named("mcp__Approver__ok")
    assert _find_mcp_tool(view(one), hook) == (one, True)
    for tools in ((one, named("mcp__APPROVER__ok")), (named("mcp__APPROVER__ok"), one)):
        assert _find_mcp_tool(view(*tools), hook) == (None, False)
    exact = named("mcp__approver__ok")
    assert _find_mcp_tool(view(one, exact), hook) == (exact, False)
    # Such a hook fails closed: the two owners are named, and the dispatch
    # answers it with a block (exit 2) instead of a failure that lets the
    # event through.
    from noeta.plugins.hooks.dispatch import shared_old_name
    assert shared_old_name(view(one, named("mcp__APPROVER__ok")), hook) == [
        "mcp__APPROVER__ok", "mcp__Approver__ok"]
    assert shared_old_name(view(one), hook) == []
    assert shared_old_name(view(one, exact), hook) == []


def test_a_narrowed_answer_keeps_its_deny_and_drops_its_allow() -> None:
    from noeta.plugins.hooks.dispatch import Read, narrowed

    allow = Read(specific={"permissionDecision": "allow", "additionalContext": "c"})
    assert narrowed("PreToolUse", allow).specific == {"additionalContext": "c"}
    assert narrowed("PreToolUse", Read(data={"decision": "approve"})).data == {}
    deny = Read(specific={"permissionDecision": "deny"})
    assert narrowed("PreToolUse", deny) is deny
    granted = Read(specific={"decision": {"behavior": "allow"}})
    assert narrowed("PermissionRequest", granted).specific == {}
    refused = Read(specific={"decision": {"behavior": "deny", "message": "no"}})
    assert narrowed("PermissionRequest", refused) is refused


async def test_slash_hooks_lists_the_hooks_by_event_read_only() -> None:
    from noeta.core.commands import Typed, run_typed
    from noeta.core.extension import CommandContext

    ctx = CommandContext(cwd="/w")
    hooks = Hooks(hooks={
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",
                                                      "command": "./check.sh"}]}],
        "Stop": [{"hooks": [{"type": "prompt", "prompt": "Done?"}]}]})
    assert await run_typed([hooks], ctx, "/hooks") == Typed("\n".join([
        "Hooks: 2 hooks on 2 events (read-only: change them in a settings file)",
        "PreToolUse", "  Bash · command · ./check.sh", "Stop", "  * · prompt · Done?"]),
        local=True)
    assert (await Hooks().run_command(ctx, "hooks", "")).startswith("No hooks configured.")
