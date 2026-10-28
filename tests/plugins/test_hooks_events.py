"""Every hook event through the real runner, with a stand-in for running
commands: what each event receives and what each answer does."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.agent import Agent
from noeta.core.client import Client
from noeta.core.events import ExtensionRecorded, TaskEnded, ToolFinished, Waiting
from noeta.core.extension import Ask, Deny, Extension, Gate, TaskView
from noeta.core.runner import Runtime
from noeta.core.types import Text, ToolResult, ToolUse
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.extension import command_text
from noeta.plugins.fs import FileTools
from noeta.plugins.hooks import HookCommand, HookResult, Hooks
from noeta.plugins.permissions import Permissions
from noeta.plugins.subtasks import SubagentSpec, Subtasks
from noeta.testing import ScriptedProvider, reply

Answer = HookResult | Callable[[dict[str, Any]], HookResult]


class FakeRunner:
    """Answers command hooks by command name and keeps every payload."""

    def __init__(self, answers: Mapping[str, Answer] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, hook: HookCommand, payload: dict[str, Any],
                       cwd: str | None) -> HookResult:
        self.calls.append((hook.command, payload))
        answer = self.answers.get(hook.command, HookResult(0))
        return answer(payload) if callable(answer) else answer

    def payloads(self, command: str) -> list[dict[str, Any]]:
        return [p for c, p in self.calls if c == command]


def ok(data: dict[str, Any] | str = "") -> HookResult:
    return HookResult(0, stdout=json.dumps(data) if isinstance(data, dict) else data)


def specific(event: str, **fields: Any) -> HookResult:
    return ok({"hookSpecificOutput": {"hookEventName": event, **fields}})


def hook(command: str, matcher: str = "", **extra: Any) -> list[dict[str, Any]]:
    return [{"matcher": matcher, "hooks": [{"type": "command", "command": command, **extra}]}]


async def _no_sleep(_: float) -> None:
    return None


class Setup:
    def __init__(self, tmp_path: Path, hooks: dict[str, Any], steps: list[Any], *,
                 answers: Mapping[str, Answer] | None = None,
                 extensions: tuple[Extension, ...] = (), one_shot: bool = False,
                 http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.work = tmp_path / "w"
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "a.txt").write_text("hello\n")
        (self.work / "b.txt").write_text("other\n")
        self.store = SqliteStore(str(tmp_path / "t.db"))
        self.provider = ScriptedProvider(steps)
        self.runner = FakeRunner(answers)
        self.hooks = Hooks(hooks=hooks, runner=self.runner, http_transport=http_transport)
        self.agent = Agent(name="a", model="m", one_shot=one_shot,
                           extensions=[*extensions, self.hooks, FileTools()])
        self.client = Client(self.store)
        self.tid = ""

    def runtime(self) -> Runtime:
        return Runtime(store=self.store, agents={"a": self.agent},
                       provider_for=lambda _m: self.provider,
                       env_for=lambda _s: LocalExecEnv(str(self.work)), sleep=_no_sleep)

    async def start(self, prompt: str = "go", **config: Any) -> Setup:
        self.tid = await self.client.create("a", prompt, config={"cwd": str(self.work),
                                                                  **config})
        await self.runtime().run_until_idle()
        return self

    async def send(self, text: str) -> None:
        await self.client.send(self.tid, text)
        await self.runtime().run_until_idle()

    async def events(self) -> list[Any]:
        return [r.event for r in await self.client.events(self.tid)]

    async def results(self) -> list[tuple[str, bool]]:
        return [(e.result.text, e.result.is_error) for e in await self.events()
                if isinstance(e, ToolFinished)]

    async def notices(self) -> list[str]:
        return [e.data["text"] for e in await self.events()
                if isinstance(e, ExtensionRecorded) and e.kind == "notice"]

    def system_texts(self, i: int) -> list[str]:
        return [m.text for m in self.provider.requests[i].messages if m.origin == "system"]


READ_A = ("Read", {"file_path": "a.txt"})


class AskAll(Extension):
    """A check that asks about every call (``firm``: as an ask rule does)."""

    name = "ask_all"

    def __init__(self, *, firm: bool = False) -> None:
        self.firm = firm

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        return Ask(prompt="may I?", firm=self.firm)


# ---------------------------------------------------------------- PreToolUse


async def test_pre_tool_use_input_and_allow_skips_a_plain_ask(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PreToolUse": hook("pre", "Read")},
                    [reply("", READ_A), reply("done")], extensions=(AskAll(),),
                    answers={"pre": specific("PreToolUse", permissionDecision="allow")}
                    ).start(permission_mode="acceptEdits")
    [(text, is_error)] = await s.results()
    assert not is_error and "hello" in text
    [payload] = s.runner.payloads("pre")
    call_id = s.provider.requests[1].messages[-2].tool_uses[0].id
    assert payload == {
        "session_id": s.tid, "transcript_path": "", "cwd": str(s.work),
        "permission_mode": "acceptEdits", "hook_event_name": "PreToolUse", "task_id": s.tid,
        "tool_name": "Read", "tool_input": {"file_path": "a.txt"}, "tool_use_id": call_id}


async def test_pre_tool_use_allow_does_not_beat_ask_or_deny_rules(tmp_path: Path) -> None:
    allow = {"pre": specific("PreToolUse", permissionDecision="allow")}
    s = await Setup(tmp_path, {"PreToolUse": hook("pre")}, [reply("", READ_A)],
                    extensions=(AskAll(firm=True),), answers=allow).start()
    waits = [e for e in await s.events() if isinstance(e, Waiting)]
    assert waits[-1].on == "approval"
    s2 = await Setup(tmp_path / "2", {"PreToolUse": hook("pre")},
                     [reply("", READ_A), reply("ok")],
                     extensions=(Permissions(deny=["Read"]),), answers=allow).start()
    [(_text, is_error)] = await s2.results()
    assert is_error


async def test_pre_tool_use_ask_is_firm(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PreToolUse": hook("pre")}, [reply("", READ_A)],
                    extensions=(Permissions(mode="bypassPermissions"),),
                    answers={"pre": specific("PreToolUse", permissionDecision="ask",
                                             permissionDecisionReason="Really read it?")}
                    ).start()
    waits = [e for e in await s.events() if isinstance(e, Waiting)]
    assert waits[-1].on == "approval"
    assert next(iter(waits[-1].detail.values()))["prompt"] == "Really read it?"


async def test_pre_tool_use_deny_forms(tmp_path: Path) -> None:
    outcomes = iter([
        specific("PreToolUse", permissionDecision="deny", permissionDecisionReason="no reads"),
        ok({"decision": "block", "reason": "blocked here"}),
        HookResult(2, stderr="exit two"),
    ])
    s = await Setup(tmp_path, {"PreToolUse": hook("pre")},
                    [reply("", READ_A, READ_A, READ_A), reply("ok")],
                    answers={"pre": lambda _p: next(outcomes)}).start()
    assert [(t, e) for t, e in await s.results()] == [
        ("no reads", True), ("blocked here", True), ("exit two", True)]


async def test_pre_tool_use_rewrites_once_and_adds_context(tmp_path: Path) -> None:
    rewrite = specific("PreToolUse", permissionDecision="allow",
                       updatedInput={"file_path": "b.txt"}, additionalContext="Read b instead.")
    s = await Setup(tmp_path, {"PreToolUse": hook("pre")}, [reply("", READ_A), reply("ok")],
                    extensions=(AskAll(),), answers={"pre": rewrite}).start()
    [(text, is_error)] = await s.results()
    assert not is_error and "other" in text  # b.txt ran, without asking
    assert len(s.runner.payloads("pre")) == 1  # not asked again about its own rewrite
    second = s.provider.requests[1].messages
    assert second[-1].text == "PreToolUse hook (Read): Read b instead."
    assert second[-1].origin == "system"


async def test_if_condition_and_once(tmp_path: Path) -> None:
    hooks = {"PreToolUse": [{"hooks": [
        {"type": "command", "command": "only-b", "if": "Read(b.txt)"},
        {"type": "command", "command": "first-time", "once": True},
        {"type": "command", "command": "first-time", "once": True},  # identical: one run
    ]}], "Stop": [{"hooks": [{"type": "command", "command": "never", "if": "Read"}]}]}
    s = await Setup(tmp_path, hooks,
                    [reply("", READ_A, ("Read", {"file_path": "b.txt"})), reply("", READ_A),
                     reply("done")]).start()
    assert [p["tool_input"] for p in s.runner.payloads("only-b")] == [{"file_path": "b.txt"}]
    assert len(s.runner.payloads("first-time")) == 1
    assert s.runner.payloads("never") == []


async def test_on_failure_block_and_system_message(tmp_path: Path) -> None:
    hooks = {"PreToolUse": [{"hooks": [
        {"type": "command", "command": "broken", "onFailure": "block"},
        {"type": "command", "command": "chatty"}]}]}
    s = await Setup(tmp_path, hooks, [reply("", READ_A), reply("ok")],
                    answers={"broken": HookResult(1, stderr="lint crashed"),
                             "chatty": ok({"systemMessage": "Checked by policy."})}).start()
    [(text, is_error)] = await s.results()
    assert is_error and text == "lint crashed"
    assert "Checked by policy." in await s.notices()


async def test_a_failing_hook_is_reported_and_ignored(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PreToolUse": hook("broken")}, [reply("", READ_A), reply("ok")],
                    answers={"broken": HookResult(1, stderr="oops")}).start()
    [(_text, is_error)] = await s.results()
    assert not is_error
    assert "PreToolUse hook failed: oops" in await s.notices()


async def test_an_answer_for_another_event_is_ignored(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PreToolUse": hook("pre")}, [reply("", READ_A), reply("ok")],
                    answers={"pre": specific("PostToolUse", permissionDecision="deny")}).start()
    [(_text, is_error)] = await s.results()
    assert not is_error


async def test_continue_false_stops_the_turn(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PreToolUse": hook("pre")}, [reply("", READ_A), reply("never")],
                    answers={"pre": ok({"continue": False, "stopReason": "Freeze is on."})}
                    ).start()
    assert len(s.provider.requests) == 1
    [(_text, is_error)] = await s.results()
    assert is_error


async def test_async_hooks_are_not_waited_for(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def slow(hook: HookCommand, payload: dict[str, Any], cwd: str | None) -> HookResult:
        await release.wait()
        return HookResult(2, stderr="too late to block")

    s = Setup(tmp_path, {"PreToolUse": [{"hooks": [{"type": "command", "command": "bg", "async": True}]}]},
              [reply("", READ_A), reply("ok")])
    s.hooks.runner = slow
    await s.start()
    [(_text, is_error)] = await s.results()
    assert not is_error
    release.set()


# ---------------------------------------------------------------- PostToolUse


async def test_post_tool_use_replaces_output_and_adds_context(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PostToolUse": hook("post", "Read")},
                    [reply("", READ_A), reply("ok")],
                    answers={"post": specific("PostToolUse", updatedToolOutput="[redacted]",
                                              additionalContext="Treat as secret.")}).start()
    assert await s.results() == [("[redacted]", False)]
    [payload] = s.runner.payloads("post")
    assert payload["tool_response"]["is_error"] is False and "hello" in \
        payload["tool_response"]["text"]
    assert s.provider.requests[1].messages[-1].text == \
        "PostToolUse hook (Read): Treat as secret."


async def test_post_tool_use_failure(tmp_path: Path) -> None:
    hooks = {"PostToolUse": hook("post"), "PostToolUseFailure": hook("failed")}
    s = await Setup(tmp_path, hooks,
                    [reply("", ("Read", {"file_path": "missing.txt"})), reply("ok")],
                    answers={"failed": HookResult(2, stderr="Check the path first.")}).start()
    assert s.runner.payloads("post") == []
    [payload] = s.runner.payloads("failed")
    assert payload["hook_event_name"] == "PostToolUseFailure" and payload["error"]
    assert payload["is_interrupt"] is False and "tool_response" not in payload
    assert s.provider.requests[1].messages[-1].text == \
        "PostToolUseFailure hook (Read): Check the path first."


async def test_updated_mcp_tool_output_only_for_mcp_tools(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PostToolUse": hook("post")}, [reply("", READ_A), reply("ok")],
                    answers={"post": specific("PostToolUse",
                                              updatedMCPToolOutput={"x": 1})}).start()
    [(text, _e)] = await s.results()
    assert "hello" in text


# ---------------------------------------------------------------- permissions


async def test_permission_request_allows_with_new_input_once(tmp_path: Path) -> None:
    answer = specific("PermissionRequest",
                      decision={"behavior": "allow", "updatedInput": {"file_path": "b.txt"}})
    s = await Setup(tmp_path, {"PermissionRequest": hook("perm", "Read")},
                    [reply("", READ_A), reply("ok")], extensions=(AskAll(),),
                    answers={"perm": answer}).start()
    [(text, is_error)] = await s.results()
    assert not is_error and "other" in text
    [payload] = s.runner.payloads("perm")  # the rewritten call was not asked about again
    assert payload["tool_name"] == "Read" and payload["tool_input"] == {"file_path": "a.txt"}
    recorded = [e.data for e in await s.events()
                if isinstance(e, ExtensionRecorded) and e.kind == "permission"]
    assert recorded[0]["behavior"] == "allow"


async def test_permission_request_denies_and_can_interrupt(tmp_path: Path) -> None:
    deny = specific("PermissionRequest", decision={"behavior": "deny", "message": "Not today."})
    s = await Setup(tmp_path, {"PermissionRequest": hook("perm")},
                    [reply("", READ_A), reply("ok")], extensions=(AskAll(),),
                    answers={"perm": deny}).start()
    assert await s.results() == [("Not today.", True)]
    stop = specific("PermissionRequest", decision={"behavior": "deny", "interrupt": True})
    s2 = await Setup(tmp_path / "2", {"PermissionRequest": hook("perm")},
                     [reply("", READ_A), reply("never")], extensions=(AskAll(),),
                     answers={"perm": stop}).start()
    assert len(s2.provider.requests) == 1


async def test_permission_request_without_a_decision_asks_the_person(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PermissionRequest": hook("perm")}, [reply("", READ_A)],
                    extensions=(AskAll(),)).start()
    waits = [e for e in await s.events() if isinstance(e, Waiting)]
    assert waits[-1].on == "approval"


class Classifier(Extension):
    """Denies every call the way the auto-mode classifier records it."""

    name = "classifier"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        view.record("permissions", "classified", {"call_id": call.id, "decision": "deny",
                                                  "reason": "looks risky"})
        return Deny(reason="denied by the classifier")


async def test_permission_denied_can_allow_a_retry(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PermissionDenied": hook("denied", "Read")},
                    [reply("", READ_A), reply("ok")], extensions=(Classifier(),),
                    answers={"denied": specific("PermissionDenied", retry=True)}).start()
    [payload] = s.runner.payloads("denied")
    assert payload["reason"] == "looks risky" and payload["tool_name"] == "Read"
    last = s.provider.requests[1].messages[-1]
    assert last.origin == "system" and "Read call may be retried" in last.text


# ---------------------------------------------------------------- turn edges


async def test_session_start_startup_and_resume(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"SessionStart": hook("start", "startup|resume")},
                    [reply("a"), reply("b"), reply("c")],
                    answers={"start": lambda p: ok(f"source was {p['source']}")}).start()
    assert s.system_texts(0) == ["SessionStart hook (startup): source was startup"]
    await s.send("again")  # the same process: no new SessionStart
    assert [p["source"] for p in s.runner.payloads("start")] == ["startup"]
    s.hooks._seen.clear()  # as if this process had restarted
    await s.send("after a restart")
    assert [p["source"] for p in s.runner.payloads("start")] == ["startup", "resume"]
    assert s.provider.requests[2].messages[-2].text == \
        "SessionStart hook (resume): source was resume"


async def test_compaction_events(tmp_path: Path) -> None:
    hooks = {"PreCompact": hook("pre", "manual"), "PostCompact": hook("post"),
             "SessionStart": hook("start", "compact")}
    answers: dict[str, Answer] = {
        "pre": ok("Keep the file names."), "post": ok("Compacted, all good."),
        "start": specific("SessionStart", additionalContext="Re-read NOTES.md.")}
    s = await Setup(tmp_path, hooks, [reply("first"), reply("summary text"), reply("next")],
                    answers=answers, extensions=(Compaction(auto=False),)).start()
    await s.send(command_text("focus on tests"))
    [pre] = s.runner.payloads("pre")
    assert pre["trigger"] == "manual" and pre["custom_instructions"] == "focus on tests"
    summarize = s.provider.requests[1].messages[-1].text
    assert "focus on tests\n\nKeep the file names." in summarize
    [post] = s.runner.payloads("post")
    assert post["trigger"] == "manual" and post["compact_summary"]
    assert "Compacted, all good." in await s.notices()
    await s.send("go on")
    assert [p["source"] for p in s.runner.payloads("start")] == ["compact"]
    assert "SessionStart hook (compact): Re-read NOTES.md." in s.system_texts(2)


async def test_pre_compact_can_refuse(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PreCompact": hook("pre")}, [reply("first")],
                    answers={"pre": HookResult(2, stderr="Not during a deploy.")},
                    extensions=(Compaction(auto=False),)).start()
    await s.send(command_text(""))
    assert len(s.provider.requests) == 1  # no summarize call
    assert "Compaction skipped: Not during a deploy." in await s.notices()


async def test_stop_additional_context_continues_and_continue_false_stops(
        tmp_path: Path) -> None:
    outcomes = iter([specific("Stop", additionalContext="Also update the changelog."),
                     ok({"continue": False, "decision": "block", "reason": "ignored"})])
    s = await Setup(tmp_path, {"Stop": hook("stop")}, [reply("done"), reply("updated")],
                    answers={"stop": lambda _p: next(outcomes)}).start()
    assert len(s.provider.requests) == 2
    assert s.provider.requests[1].messages[-1].text == "Also update the changelog."
    first, second = s.runner.payloads("stop")
    assert (first["stop_hook_active"], first["last_assistant_message"]) == (False, "done")
    assert (second["stop_hook_active"], second["last_assistant_message"]) == (True, "updated")


async def test_session_end_and_notification(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"SessionEnd": hook("end"), "Notification": hook("note")},
                    [reply("bye")], one_shot=True).start()
    assert isinstance((await s.events())[-1], TaskEnded)
    [end] = s.runner.payloads("end")
    assert end["reason"] == "other"
    s2 = await Setup(tmp_path / "2", {"Notification": hook("note", "permission_prompt")},
                     [reply("", READ_A)], extensions=(AskAll(),)).start()
    for _ in range(50):
        if s2.runner.payloads("note"):
            break
        await asyncio.sleep(0.01)
    [note] = s2.runner.payloads("note")
    assert note["notification_type"] == "permission_prompt"
    assert note["message"] == "Noeta needs your permission to use Read"


async def test_http_hook_end_to_end(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "the policy server says no"}})

    hooks = {"PreToolUse": [{"hooks": [{"type": "http", "url": "https://policy.example/h"}]}]}
    s = await Setup(tmp_path, hooks, [reply("", READ_A), reply("ok")],
                    http_transport=httpx.MockTransport(handle)).start()
    assert await s.results() == [("the policy server says no", True)]
    assert seen[0]["tool_name"] == "Read"


async def test_prompt_hook_end_to_end(tmp_path: Path) -> None:
    hooks = {"PreToolUse": [{"hooks": [{"type": "prompt", "prompt": "Reads of a.txt are fine",
                                        "continueOnBlock": True}]}]}
    s = await Setup(tmp_path, hooks,
                    [reply("", READ_A), reply('{"ok": false, "reason": "not today"}'),
                     reply("ok")]).start()
    assert await s.results() == [("not today", True)]
    judge = s.provider.requests[1]
    assert not judge.tools and "Reads of a.txt are fine" in judge.messages[-1].text


async def test_sub_agent_start_and_stop(tmp_path: Path) -> None:
    stops = iter([HookResult(2, stderr="Run the tests first."), HookResult(0)])
    runner = FakeRunner({"start": specific("SubagentStart", additionalContext="Be brief."),
                         "stop": lambda _p: next(stops)})
    hooks = Hooks(hooks={"SubagentStart": hook("start", "scout"),
                         "SubagentStop": hook("stop")}, runner=runner)
    roster = [SubagentSpec(name="scout", description="Looks around.", agent="worker")]
    parent = Agent(name="main", model="p", extensions=[Subtasks(roster), hooks])
    worker = Agent(name="worker", model="c", extensions=[Subtasks(()), hooks])
    p = ScriptedProvider([reply("", ("Agent", {"description": "look", "prompt": "count",
                                               "subagent_type": "scout"})),
                          reply("launched"), reply("thanks")])
    c = ScriptedProvider([reply("", ("SubagentHandback", {"message": "draft"})),
                          reply("", ("SubagentHandback", {"message": "1 2 3"}))])
    store = SqliteStore(str(tmp_path / "t.db"))
    providers = {"p": p, "c": c}
    rt = Runtime(store=store, agents={"main": parent, "worker": worker},
                 provider_for=lambda m: providers[m], env_for=lambda _s: LocalExecEnv(str(tmp_path)),
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    [start] = runner.payloads("start")
    assert (start["agent_id"], start["agent_type"]) == (child.task_id, "scout")
    assert start["session_id"] == tid
    assert c.requests[0].messages[0].text == "SubagentStart hook: Be brief."
    first, second = runner.payloads("stop")
    assert (first["last_assistant_message"], first["stop_hook_active"]) == ("draft", False)
    assert second["stop_hook_active"] is True and second["agent_type"] == "scout"
    [denied] = c.requests[1].messages[-1].content
    assert isinstance(denied, ToolResult) and denied.is_error
    assert denied.content == [Text(text="Run the tests first.")]
    end = [r.event for r in await client.events(child.task_id)][-1]
    assert isinstance(end, TaskEnded) and end.answer == "1 2 3"
