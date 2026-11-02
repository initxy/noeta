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
from noeta.core.tool import ToolOutput
from noeta.core.types import Text, ToolResult, ToolUse
from noeta.plugins.compaction import Compaction
from noeta.plugins.compaction.extension import command_text
from noeta.plugins.fs import FileTools
from noeta.plugins.hooks import HookCommand, HookResult, Hooks
from noeta.plugins.permissions import Permissions
from noeta.plugins.subtasks import SubagentSpec, Subtasks
from noeta.testing import ScriptedProvider, reply
from noeta.toolkit.signals import RESUMED_SIGNAL

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


async def test_a_task_without_a_configured_cwd_runs_its_hooks_in_the_env_cwd(
        tmp_path: Path) -> None:
    s = Setup(tmp_path, {"PreToolUse": hook("pre", "Read")}, [reply("", READ_A), reply("done")])
    s.tid = await s.client.create("a", "go")  # no config cwd: the exec env's is the task's
    await s.runtime().run_until_idle()
    [payload] = s.runner.payloads("pre")
    assert payload["cwd"] == str(s.work)


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
    assert second[-1].text == "PreToolUse:Read hook additional context: Read b instead."
    assert second[-1].origin == "system"


async def test_a_rewrite_is_recorded_so_another_worker_does_not_ask_again(
        tmp_path: Path) -> None:
    """The hook's verdict on its own rewrite is in the log: the worker that
    picks the call up after its approval does not ask the hook about it."""
    config = {"PreToolUse": hook("pre")}
    rewrite = specific("PreToolUse", permissionDecision="ask",
                       updatedInput={"file_path": "b.txt"})
    s = await Setup(tmp_path, config, [reply("", READ_A), reply("ok")],
                    answers={"pre": rewrite}).start()
    last = (await s.events())[-1]
    assert isinstance(last, Waiting) and last.on == "approval"
    assert len(s.runner.payloads("pre")) == 1
    s.agent = Agent(name="a", model="m",
                    extensions=[Hooks(hooks=config, runner=s.runner), FileTools()])
    await s.client.answer(s.tid, last.ids[0], "allow")
    await s.runtime().run_until_idle()
    [(text, is_error)] = await s.results()
    assert not is_error and "other" in text
    # Asked once more about the model's own call, never about its rewrite.
    assert [p["tool_input"] for p in s.runner.payloads("pre")] == [{"file_path": "a.txt"}] * 2


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
        "PostToolUse:Read hook additional context: Treat as secret."


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
        'PostToolUseFailure:Read hook blocking error from command: "failed": Check the path first.'


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
    config = {"SessionStart": hook("start", "startup|resume")}
    s = await Setup(tmp_path, config, [reply("a"), reply("b"), reply("c"), reply("d")],
                    answers={"start": lambda p: ok(f"source was {p['source']}")}).start()
    assert s.system_texts(0) == ["SessionStart hook success: source was startup"]
    await s.send("again")  # the same process: no new SessionStart
    assert [p["source"] for p in s.runner.payloads("start")] == ["startup"]
    # Another worker (or a restart: a fresh plugin) picking the task up is
    # no resume; the request is what the log says.
    s.agent = Agent(name="a", model="m",
                    extensions=[Hooks(hooks=config, runner=s.runner), FileTools()])
    await s.send("on another worker")
    assert [p["source"] for p in s.runner.payloads("start")] == ["startup"]
    # A front end reopening the task is.
    await s.client.signal(s.tid, RESUMED_SIGNAL)
    await s.runtime().run_until_idle()
    await s.send("after a resume")
    assert [p["source"] for p in s.runner.payloads("start")] == ["startup", "resume"]
    assert s.provider.requests[3].messages[-2].text == \
        "SessionStart hook success: source was resume"


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
    assert "SessionStart hook additional context: Re-read NOTES.md." in s.system_texts(2)


async def test_compaction_context_survives_a_blocked_prompt(tmp_path: Path) -> None:
    hooks = {"SessionStart": hook("start", "compact"), "UserPromptSubmit": hook("prompt")}
    answers: dict[str, Answer] = {
        "start": specific("SessionStart", additionalContext="Re-read NOTES.md."),
        "prompt": lambda p: HookResult(2, stderr="no") if "SECRET" in p["prompt"]
        else HookResult(0)}
    s = await Setup(tmp_path, hooks, [reply("first"), reply("summary text"), reply("next")],
                    answers=answers, extensions=(Compaction(auto=False),)).start()
    await s.send(command_text(""))
    await s.send("my SECRET")
    await s.send("go on")
    assert [p["source"] for p in s.runner.payloads("start")] == ["compact"]
    assert "SessionStart hook additional context: Re-read NOTES.md." in s.system_texts(2)


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


async def test_stop_runs_when_a_turn_ends_on_an_empty_reply(tmp_path: Path) -> None:
    # Claude Code 2.1.312 runs Stop once the asked-again reply is empty too,
    # with no last_assistant_message.
    s = await Setup(tmp_path, {"Stop": hook("stop")}, [reply(""), reply("")],
                    answers={"stop": HookResult(0)}).start()
    assert len(s.provider.requests) == 2
    [payload] = s.runner.payloads("stop")
    assert payload["stop_hook_active"] is False
    assert "last_assistant_message" not in payload


async def test_http_hook_end_to_end(tmp_path: Path) -> None:
    seen: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"hookSpecificOutput": {
            "hookEventName": "PreToolUse", "permissionDecision": "deny",
            "permissionDecisionReason": "the policy server says no"}})

    hooks = {"PreToolUse": [{"hooks": [{"type": "http", "url": "https://policy.example/h"}]}],
             "SessionStart": [{"hooks": [{"type": "http", "url": "https://policy.example/s"}]}]}
    s = await Setup(tmp_path, hooks, [reply("", READ_A), reply("ok")],
                    http_transport=httpx.MockTransport(handle)).start()
    assert await s.results() == [("the policy server says no", True)]
    assert [p["hook_event_name"] for p in seen] == ["PreToolUse"]  # none on SessionStart
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
    assert c.requests[0].messages[0].text == "SubagentStart hook additional context: Be brief."
    first, second = runner.payloads("stop")
    assert (first["last_assistant_message"], first["stop_hook_active"]) == ("draft", False)
    assert second["stop_hook_active"] is True and second["agent_type"] == "scout"
    [denied] = c.requests[1].messages[-1].content
    assert isinstance(denied, ToolResult) and denied.is_error
    assert denied.content == [Text(
        text='SubagentStop hook blocking error from command: "stop": Run the tests first.')]
    end = [r.event for r in await client.events(child.task_id)][-1]
    assert isinstance(end, TaskEnded) and end.answer == "1 2 3"


async def test_sub_agent_stop_runs_when_it_ends_on_an_empty_reply(tmp_path: Path) -> None:
    runner = FakeRunner({"stop": HookResult(0)})
    hooks = Hooks(hooks={"SubagentStop": hook("stop")}, runner=runner)
    roster = [SubagentSpec(name="scout", description="Looks around.", agent="worker")]
    parent = Agent(name="main", model="p", extensions=[Subtasks(roster), hooks])
    worker = Agent(name="worker", model="c", extensions=[Subtasks(()), hooks])
    p = ScriptedProvider([reply("", ("Agent", {"description": "look", "prompt": "count",
                                               "subagent_type": "scout"})),
                          reply("it said nothing")])
    c = ScriptedProvider([reply(""), reply("")])
    store = SqliteStore(str(tmp_path / "t.db"))
    providers = {"p": p, "c": c}
    rt = Runtime(store=store, agents={"main": parent, "worker": worker},
                 provider_for=lambda m: providers[m], env_for=lambda _s: LocalExecEnv(str(tmp_path)),
                 sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("main", "go")
    await rt.run_until_idle()
    [child] = await client.tasks(parent_id=tid)
    [payload] = runner.payloads("stop")
    assert payload["agent_id"] == child.task_id and "last_assistant_message" not in payload
    end = [r.event for r in await client.events(child.task_id)][-1]
    assert isinstance(end, TaskEnded) and end.status == "done" and end.answer == ""
    await store.close()


async def test_a_sub_agent_stop_prompt_hook_sees_no_unanswered_call(tmp_path: Path) -> None:
    hooks = Hooks(hooks={"SubagentStop": [{"hooks": [{"type": "prompt",
                                                      "prompt": "Were tests run?"}]}]})
    roster = [SubagentSpec(name="scout", description="Looks around.", agent="worker")]
    parent = Agent(name="main", model="p", extensions=[Subtasks(roster), hooks])
    worker = Agent(name="worker", model="c", extensions=[Subtasks(()), hooks])
    p = ScriptedProvider([reply("", ("Agent", {"description": "look", "prompt": "count",
                                               "subagent_type": "scout"})),
                          reply("launched"), reply("thanks")])
    c = ScriptedProvider([reply("", ("SubagentHandback", {"message": "draft"})),
                          reply('{"ok": false, "reason": "Run the tests."}'),
                          reply("", ("SubagentHandback", {"message": "1 2 3"})),
                          reply('{"ok": true}')])
    store = SqliteStore(str(tmp_path / "t.db"))
    providers = {"p": p, "c": c}
    rt = Runtime(store=store, agents={"main": parent, "worker": worker},
                 provider_for=lambda m: providers[m],
                 env_for=lambda _s: LocalExecEnv(str(tmp_path)), sleep=_no_sleep)
    client = Client(store)
    await client.create("main", "go")
    await rt.run_until_idle()
    judged = [r for r in c.requests if not r.tools]
    assert len(judged) == 2
    for request in judged:
        answered = {b.call_id for m in request.messages for b in m.content
                    if isinstance(b, ToolResult)}
        assert all(u.id in answered for m in request.messages for u in m.tool_uses)
    assert c.requests[2].messages[-1].content[0].text == (  # type: ignore[union-attr]
        'SubagentStop hook blocking error from command: "Were tests run?": Run the tests.')


async def test_agent_hook_looks_with_read_only_tools_then_answers(tmp_path: Path) -> None:
    hooks = {"Stop": [{"hooks": [{"type": "agent", "prompt": "Was b.txt read?"}]}]}
    verdicts = [
        reply("done"),
        reply("", ("Read", {"file_path": "b.txt"})),
        reply("", ("StructuredOutput", {"ok": False, "reason": "b.txt says other"})),
        reply("fixed"),
        reply("", ("StructuredOutput", {"ok": True, "reason": "fine"})),
    ]
    s = await Setup(tmp_path, hooks, verdicts).start()
    look = s.provider.requests[1]
    assert {t.name for t in look.tools} == {"Read", "StructuredOutput"}  # no Write / Edit
    assert "Was b.txt read?" in look.messages[-1].text
    seen = s.provider.requests[2].messages[-1].content
    assert isinstance(seen[0], ToolResult) and "other" in seen[0].text
    assert s.provider.requests[3].messages[-1].text == (
        'Stop hook blocking error from command: "Was b.txt read?": b.txt says other')
    assert len(s.provider.requests) == 5


class _Guard(Extension):
    """Asks before reading ``b.txt``, refuses ``a.txt``."""

    name = "guard"

    async def before_tool(self, view: TaskView, call: ToolUse) -> Gate | None:
        path = str(call.input.get("file_path", ""))
        if path.endswith("b.txt"):
            return Ask(prompt="read b.txt?")
        if path.endswith("a.txt"):
            return Deny(reason="a.txt is off limits")
        return None


async def test_agent_hook_tools_go_through_the_permission_checks(tmp_path: Path) -> None:
    hooks = {"Stop": [{"hooks": [{"type": "agent", "prompt": "Was b.txt read?"}]}]}
    verdicts = [
        reply("done"),
        reply("", ("Read", {"file_path": "b.txt"}), ("Read", {"file_path": "a.txt"})),
        reply("", ("StructuredOutput", {"ok": True, "reason": "fine"})),
    ]
    s = await Setup(tmp_path, hooks, verdicts, extensions=(_Guard(),)).start()
    asked, denied = s.provider.requests[2].messages[-1].content
    assert isinstance(asked, ToolResult) and asked.is_error and "other" not in asked.text
    assert isinstance(denied, ToolResult) and denied.is_error and "hello" not in denied.text
    assert "a.txt is off limits" in denied.text


async def test_agent_hook_that_never_answers_fails_open(tmp_path: Path) -> None:
    hooks = {"Stop": [{"hooks": [{"type": "agent", "prompt": "check"}]}]}
    s = await Setup(tmp_path, hooks, [reply("done"), reply("I think it is fine")]).start()
    assert len(s.provider.requests) == 2
    assert any("Stop hook failed" in n for n in await s.notices())


class McpLike(Extension):
    name = "fake-mcp"

    def __init__(self, answer: str, is_error: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        outer = self

        class Verify:
            name = "mcp__chk__verify"
            description = "verify"
            input_schema: dict[str, Any] = {"type": "object"}
            read_only = False

            async def run(self, input: dict[str, Any], ctx: Any) -> ToolOutput:
                outer.calls.append(input)
                return ToolOutput(content=[Text(text=answer)], is_error=is_error)

        self._tool = Verify()

    def tools(self, view: TaskView) -> list[Any]:
        return [self._tool]


async def test_mcp_tool_hook_calls_the_tool_with_filled_input(tmp_path: Path) -> None:
    answer = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                                "permissionDecision": "deny",
                                                "permissionDecisionReason": "not that file"}})
    mcp = McpLike(answer)
    hooks = {"PreToolUse": [{"matcher": "Read", "hooks": [{
        "type": "mcp_tool", "server": "chk", "tool": "verify",
        "input": {"path": "${tool_input.file_path}", "missing": "${nope.x}", "n": 3}}]}]}
    s = await Setup(tmp_path, hooks, [reply("", READ_A), reply("ok")],
                    extensions=(mcp,)).start()
    assert mcp.calls == [{"path": "a.txt", "missing": "", "n": 3}]
    assert await s.results() == [("not that file", True)]


async def test_mcp_tool_hook_error_or_missing_tool_is_a_failure(tmp_path: Path) -> None:
    hooks: dict[str, Any] = {"PreToolUse": [{"hooks": [{"type": "mcp_tool", "server": "chk",
                                                        "tool": "verify"}]}]}
    s = await Setup(tmp_path, hooks, [reply("", READ_A), reply("ok")],
                    extensions=(McpLike("boom", is_error=True),)).start()
    assert (await s.results())[0][1] is False
    assert any("returned an error: boom" in n for n in await s.notices())
    hooks["PreToolUse"][0]["hooks"][0]["server"] = "other"
    s = await Setup(tmp_path / "2", hooks, [reply("", READ_A), reply("ok")]).start()
    assert any("offers no tool" in n for n in await s.notices())


async def test_post_tool_batch_once_per_reply_with_every_result(tmp_path: Path) -> None:
    answers = {"batch": specific("PostToolBatch", additionalContext="Both read.")}
    s = Setup(tmp_path, {"PostToolBatch": hook("batch")},
              [reply("", READ_A, ("Read", {"file_path": "b.txt"})), reply("ok")],
              answers=answers)
    await s.start()
    [payload] = s.runner.payloads("batch")
    calls = payload["tool_calls"]
    assert [c["tool_input"]["file_path"] for c in calls] == ["a.txt", "b.txt"]
    assert "hello" in calls[0]["tool_response"] and "other" in calls[1]["tool_response"]
    assert "PostToolBatch hook additional context: Both read." in s.system_texts(1)


async def test_stop_failure_on_a_failed_model_call_only(tmp_path: Path) -> None:
    from noeta.core.errors import Fatal

    s = await Setup(tmp_path, {"StopFailure": hook("failed", "rate_limit|authentication_failed")},
                    [Fatal("bad key", status=401, body="invalid x-api-key")]).start()
    [payload] = s.runner.payloads("failed")
    assert payload["error"] == "authentication_failed" and "bad key" in payload["error_details"]
    s = await Setup(tmp_path / "2", {"StopFailure": hook("failed")},
                    [reply("fine")]).start()
    assert s.runner.payloads("failed") == []


class MovesShell(Extension):
    name = "mover"

    def __init__(self) -> None:
        class Cd:
            name = "Cd"
            description = "cd"
            input_schema: dict[str, Any] = {"type": "object"}
            read_only = False

            async def run(self, input: dict[str, Any], ctx: Any) -> ToolOutput:
                ctx.record("shell", "cwd", {"cwd": input["to"]})
                return ToolOutput(content=[Text(text="moved")])

        self._tool = Cd()

    def tools(self, view: TaskView) -> list[Any]:
        return [self._tool]


async def test_cwd_changed_when_the_shell_moves(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"CwdChanged": hook("cwd")},
                    [reply("", ("Cd", {"to": "/elsewhere"})), reply("", ("Cd", {"to": ""})),
                     reply("ok")], extensions=(MovesShell(),)).start()
    first, second = s.runner.payloads("cwd")
    assert (first["old_cwd"], first["new_cwd"]) == (str(s.work), "/elsewhere")
    assert (second["old_cwd"], second["new_cwd"]) == ("/elsewhere", str(s.work))


async def test_post_model_switch_adds_context_for_the_new_model(tmp_path: Path) -> None:
    from noeta.core.loop.state import CONFIG_SIGNAL

    answers = {"switch": specific("PostModelSwitch", additionalContext="Now on m2.")}
    s = await Setup(tmp_path, {"PostModelSwitch": hook("switch", "m2")},
                    [reply("one"), reply("two")], answers=answers).start()
    await s.client.signal(s.tid, CONFIG_SIGNAL, {"model": "m2"})
    await s.send("again")
    [payload] = s.runner.payloads("switch")
    assert (payload["from_model"], payload["to_model"]) == ("m", "m2")
    assert any("Now on m2." in t for t in s.system_texts(1))


# ---------------------------------------------------------------- the model told


async def test_continue_false_is_told_to_the_model(tmp_path: Path) -> None:
    """Claude Code's ``<hook name> hook stopped continuation: <reason>``:
    PreToolUse only with a reason, PostToolUse with a default one."""
    s = await Setup(tmp_path, {"PreToolUse": hook("pre")}, [reply("", READ_A), reply("next")],
                    answers={"pre": ok({"continue": False, "stopReason": "Freeze is on."})}
                    ).start()
    await s.send("go on")
    assert "PreToolUse:Read hook stopped continuation: Freeze is on." in s.system_texts(1)
    s = await Setup(tmp_path / "post", {"PostToolUse": hook("post")},
                    [reply("", READ_A), reply("next")],
                    answers={"post": ok({"continue": False})}).start()
    assert len(s.provider.requests) == 1
    await s.send("go on")
    assert "PostToolUse:Read hook stopped continuation: Execution stopped by PostToolUse hook" \
        in s.system_texts(1)


async def test_a_stop_hook_that_stops_continuation_is_told(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"Stop": hook("stop")}, [reply("done"), reply("next")],
                    answers={"stop": ok({"continue": False})}).start()
    await s.send("more")
    assert "Stop hook stopped continuation: Stop hook prevented continuation" \
        in s.system_texts(1)


async def test_a_post_tool_batch_block_ends_the_turn_and_says_why(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"PostToolBatch": hook("batch", decision="block")},
                    [reply("", READ_A), reply("next")],
                    answers={"batch": ok({"decision": "block", "reason": "Lint failed."})}
                    ).start()
    assert len(s.provider.requests) == 1
    await s.send("go on")
    assert "PostToolBatch hook stopped continuation: Lint failed." in s.system_texts(1)


async def test_plain_output_of_a_prompt_hook_is_its_success(tmp_path: Path) -> None:
    s = await Setup(tmp_path, {"UserPromptSubmit": hook("ups")}, [reply("ok")],
                    answers={"ups": ok("Branch is main.")}).start()
    assert s.system_texts(0) == ["UserPromptSubmit hook success: Branch is main."]


async def test_hook_output_over_the_cap_is_saved_with_a_preview(tmp_path: Path) -> None:
    long = "x" * 12_000
    s = Setup(tmp_path, {"UserPromptSubmit": hook("ups")}, [reply("ok")],
              answers={"ups": specific("UserPromptSubmit", additionalContext=long)})
    spill = tmp_path / "spill"

    def runtime() -> Runtime:
        return Runtime(store=s.store, agents={"a": s.agent}, provider_for=lambda _m: s.provider,
                       env_for=lambda _s: LocalExecEnv(str(s.work)), sleep=_no_sleep,
                       spill_root=str(spill))

    s.runtime = runtime  # type: ignore[method-assign]
    await s.start()
    [text] = s.system_texts(0)
    assert text.startswith("UserPromptSubmit hook additional context: <persisted-output>")
    assert len(text) < 3_000
    [saved] = [p for p in spill.rglob("*.txt")]
    assert saved.read_text() == long and str(saved) in text


async def test_an_async_hooks_answer_reaches_the_next_model_call(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def later(hook: HookCommand, payload: dict[str, Any], cwd: str | None) -> HookResult:
        await release.wait()
        return specific("PostToolUse", additionalContext="Tests passed.")

    config = {"PostToolUse": [{"hooks": [{"type": "command", "command": "bg",
                                          "async": True}]}]}
    s = Setup(tmp_path, config, [reply("", READ_A), reply("done"), reply("again")])
    s.hooks.runner = later
    await s.start()
    assert not any("Tests passed." in t for r in s.provider.requests for m in r.messages
                   for t in [m.text])
    release.set()
    await asyncio.sleep(0.05)
    # Delivered through the task's inbox: whichever worker runs it next.
    s.agent = Agent(name="a", model="m",
                    extensions=[Hooks(hooks=config, runner=later), FileTools()])
    await s.send("more")
    assert "Tests passed." in s.system_texts(2)
    await s.send("and more")
    assert s.system_texts(3).count("Tests passed.") == 1


async def test_an_async_rewake_hook_wakes_the_model_on_exit_2(tmp_path: Path) -> None:
    release = asyncio.Event()

    async def later(hook: HookCommand, payload: dict[str, Any], cwd: str | None) -> HookResult:
        await release.wait()
        return HookResult(2, stderr="2 tests failed")

    config = {"PostToolUse": [{"matcher": "Read", "hooks": [
        {"type": "command", "command": "bg", "asyncRewake": True}]}]}
    s = Setup(tmp_path, config, [reply("", READ_A), reply("done"), reply("fixing")])
    s.hooks.runner = later
    await s.start()
    assert len(s.provider.requests) == 2
    release.set()
    await asyncio.sleep(0.05)
    await s.runtime().run_until_idle()
    assert len(s.provider.requests) == 3
    assert s.provider.requests[2].messages[-1].text == \
        'Stop hook blocking error from command "PostToolUse:Read": 2 tests failed'


async def test_a_background_session_start_answer_already_given_is_left_out(
        tmp_path: Path) -> None:
    # Claude Code leaves out a background SessionStart answer whose texts
    # the newest earlier SessionStart output already holds; judged from the
    # task's log, so a worker that did not run the first hook judges alike.
    release = asyncio.Event()
    notes = {"bg": "Read NOTES.md.", "bg2": "Tests are slow here."}

    async def runner(hook: HookCommand, payload: dict[str, Any], cwd: str | None) -> HookResult:
        if hook.command == "start":
            return specific("SessionStart", additionalContext="Read NOTES.md.")
        await release.wait()
        return specific("SessionStart", additionalContext=notes[hook.command])

    config = {"SessionStart": [{"hooks": [
        {"type": "command", "command": "start"},
        {"type": "command", "command": "bg", "async": True},
        {"type": "command", "command": "bg2", "async": True}]}]}
    s = Setup(tmp_path, config, [reply("a"), reply("b"), reply("c")])
    s.hooks.runner = runner
    await s.start()
    assert s.system_texts(0) == ["SessionStart hook additional context: Read NOTES.md."]
    release.set()
    await asyncio.sleep(0.05)
    s.agent = Agent(name="a", model="m",
                    extensions=[Hooks(hooks=config, runner=runner), FileTools()])
    await s.send("more")
    told = s.system_texts(1)
    assert told.count("Read NOTES.md.") == 0
    assert "Tests are slow here." in told


async def test_a_prompt_hooks_continue_false_keeps_the_prompt_and_tells_the_model(
        tmp_path: Path) -> None:
    # Claude Code: no model call, the prompt stays, and the model later reads
    # that the hook stopped it.
    stop = {"n": 0}

    def answer(payload: dict[str, Any]) -> HookResult:
        stop["n"] += 1
        return ok({"continue": False, "stopReason": "Freeze is on."}) if stop["n"] == 1 \
            else ok({"continue": False}) if stop["n"] == 2 else ok()

    s = await Setup(tmp_path, {"UserPromptSubmit": hook("gate")}, [reply("ok")],
                    answers={"gate": answer}).start("deploy it")
    assert s.provider.requests == []
    assert "Operation stopped by hook: Freeze is on." in await s.notices()
    assert not [e for e in await s.events()
                if isinstance(e, ExtensionRecorded) and e.kind == "turn_failed"]
    await s.send("try again")
    assert s.provider.requests == []
    await s.send("now")
    texts = [m.text for m in s.provider.requests[0].messages if m.role == "user"]
    assert texts[:2] == ["deploy it", "Operation stopped by hook: Freeze is on."]
    assert texts[2:4] == ["try again", "Operation stopped by hook"]
    assert texts[-1] == "now"
