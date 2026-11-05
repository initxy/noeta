"""The main prompt's ``!`` line (Claude Code gives it only in its
interactive terminal): a task whose front end runs ``!`` commands
(``bash_mode`` in its config, the terminal UI's tasks) gets it; every other
task, and a sub-agent of a terminal UI task, does not; a fork keeps its
parent's prompt. The ``# Session-specific guidance`` heading goes only when
the skill line has gone too."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noeta.agents import default_agents
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.stores.sqlite import SqliteStore
from noeta.core.client import Client
from noeta.core.runner import Runtime
from noeta.core.types import Message, ModelRequest, ModelResponse, Text, ToolUse, Usage
from noeta.toolkit.interactive import BASH_MODE_KEY

BANG = "suggest they type `! <command>` in the prompt"
HEADING = "# Session-specific guidance"
SKILL = "`/<skill-name>`"


def _reply(text: str = "", *calls: tuple[str, str, dict[str, Any]]) -> ModelResponse:
    content: list[Any] = [Text(text=text)] if text else []
    content += [ToolUse(id=i, name=n, input=a) for i, n, a in calls]
    return ModelResponse(message=Message(role="assistant", content=content),
                         stop_reason="tool_use" if calls else "end_turn",
                         usage=Usage(uncached=10, output=5))


class Script:
    def __init__(self, replies: list[ModelResponse]) -> None:
        self.replies = replies
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest, on_delta: Any = None) -> ModelResponse:
        self.requests.append(request)
        return self.replies.pop(0) if self.replies else _reply("ok")


async def _no_sleep(_: float) -> None:
    return None


async def _systems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, config: dict[str, Any],
                   replies: list[ModelResponse], *, skill: bool = False
                   ) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """Each request's system text, and each task's recorded config."""
    home, work = tmp_path / "home", tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    if skill:
        d = work / ".agents" / "skills" / "deploy"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: deploy\ndescription: Ship it.\n---\nShip.\n")
    script = Script(replies)
    store = SqliteStore(str(tmp_path / "t.db"))
    rt = Runtime(store=store, agents={a.name: a for a in default_agents("claude-opus-5-5")},
                 provider_for=lambda _m: script,  # type: ignore[arg-type,return-value]
                 env_for=lambda s: LocalExecEnv(str(s.config.get("cwd") or work)),
                 sleep=_no_sleep)
    client = Client(store)
    await client.create("main", "Hi.", config={"cwd": str(work),
                                               "permission_mode": "bypassPermissions",
                                               **config})
    await rt.run_until_idle()
    configs = {r.task_id: (await client.events(r.task_id))[0].event.config  # type: ignore[attr-defined]
               for r in await store.tasks(limit=100)}
    await store.close()
    return ["".join(p.text for p in r.system) for r in script.requests], configs


@pytest.mark.parametrize("skill", [False, True])
async def test_a_terminal_ui_task_gets_the_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                skill: bool) -> None:
    [system], _ = await _systems(tmp_path, monkeypatch, {BASH_MODE_KEY: True}, [_reply("ok")],
                                 skill=skill)
    assert f"{HEADING}\n - If you need the user to run a shell command" in system
    assert BANG in system
    assert (SKILL in system) is skill


@pytest.mark.parametrize("skill", [False, True])
async def test_other_tasks_do_not(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                  skill: bool) -> None:
    # What -p / stream-json, ACP and HTTP create: no key (or ``interactive``
    # alone, which is about who answers, not about ``!`` commands).
    [system], _ = await _systems(tmp_path, monkeypatch, {"interactive": False}, [_reply("ok")],
                                 skill=skill)
    assert "`! <command>`" not in system
    assert (HEADING in system) is skill
    if skill:
        assert f"{HEADING}\n - When the user types {SKILL}" in system


async def test_a_sub_agent_does_not_inherit_it_and_a_fork_keeps_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replies = [
        _reply("", ("a1", "Agent", {"description": "look", "prompt": "Look.",
                                    "subagent_type": "general-purpose"}),
               ("a2", "Agent", {"description": "fork", "prompt": "Go on.",
                                "subagent_type": "fork"})),
    ]
    systems, configs = await _systems(tmp_path, monkeypatch, {BASH_MODE_KEY: True}, replies)
    by_type = {c.get("subagent_type"): c for c in configs.values()}
    assert BASH_MODE_KEY not in by_type["general-purpose"]
    assert by_type["fork"][BASH_MODE_KEY] is True
    main = systems[0]
    assert BANG in main
    # The parent's requests and the fork's.
    assert sum(s == main for s in systems) >= 3
    assert any(BANG not in s for s in systems)  # general-purpose's own prompt
