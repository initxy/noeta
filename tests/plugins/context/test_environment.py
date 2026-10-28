"""The environment message and git snapshot: captured through the task's
ExecEnv with hardened git (a repository's config cannot run a program),
shell / OS lines only on the local env, the model line falling back to the
agent's model; where each goes in the requests; the token budget notes."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.events import ExtensionRecorded
from noeta.core.execenv import RunResult
from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.plugins.environment import Environment


def _view(cwd: str, env: Any, **config: Any) -> TaskView:
    return TaskView(state=TaskState(task_id="t", config={"cwd": cwd, **config}), env=env)


async def _block(ext: Environment, view: TaskView) -> str:
    """Everything ``on_start`` captured: the held environment text, then
    the texts of the messages it recorded."""
    await ext.on_start(view)
    env, *rest = view.pending
    assert isinstance(env, ExtensionRecorded) and env.kind == "environment"
    assert env.message is None and env.data["held"]
    texts = [env.data["text"]]
    for rec in rest:
        assert isinstance(rec, ExtensionRecorded) and rec.message is not None
        texts.append(rec.message.text)
    return "\n".join(texts)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_repository_fsmonitor_hook_never_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pwned = tmp_path / "PWNED"
    hook = tmp_path / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {pwned}\nexit 1\n")
    hook.chmod(0o755)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    (repo / "kept.txt").write_text("k\n")
    subprocess.run([*git, "add", "kept.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(repo), "config", "core.fsmonitor", str(hook)], check=True)
    text = await _block(Environment(), _view(str(repo), LocalExecEnv(str(repo))))
    assert not pwned.exists()
    assert " - Is a git repository: true" in text
    assert "Current branch: trunk" in text and "Recent commits:\n" in text
    assert "?? a.txt" in text
    assert " - Shell: " in text and " - OS Version: " in text


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_repository_clean_filter_never_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pwned = tmp_path / "PWNED"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run([*git, "add", "a.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "filter.x.clean",
                    f"touch {pwned}; cat"], check=True)
    (repo / ".gitattributes").write_text("*.txt filter=x\n")
    (repo / "a.txt").touch()
    text = await _block(Environment(), _view(str(repo), LocalExecEnv(str(repo))))
    assert not pwned.exists()
    assert "Current branch: trunk" in text
    assert "Status:\n(not captured)" in text


class FakeContainer:
    """An ExecEnv that is not the local one; records what it runs."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.argv: list[list[str]] = []

    async def run(self, argv: Sequence[str], *, cwd: str | None = None,
                  env: Mapping[str, str] | None = None, stdin: bytes | None = None,
                  timeout: float | None = None, output_cap: int | None = None) -> RunResult:
        self.argv.append(list(argv))
        if "config" in argv:
            return RunResult(exit_code=1, stdout=b"")  # no filter driver configured
        out = b"main\n" if "rev-parse" in argv else b" M src/x.py\n"
        return RunResult(exit_code=0, stdout=out)

    async def stat(self, path: str) -> object | None:
        return object() if path == "/workspace/.git" else None


async def test_a_container_env_is_asked_and_host_facts_left_out() -> None:
    env = FakeContainer("/workspace")
    text = await _block(Environment(model="agent-model"), _view("/workspace", env))
    assert " - Primary working directory: /workspace" in text
    assert "Current branch: main" in text and " M src/x.py" in text
    assert " - Shell: " not in text and " - OS Version: " not in text
    assert "Model: `agent-model`." in text
    for argv in env.argv:
        assert argv[:3] == ["git", "-c", "core.fsmonitor="]
    assert any("status" in a for a in env.argv)


async def test_the_model_line_prefers_the_task_config(tmp_path: Path) -> None:
    env = LocalExecEnv(str(tmp_path))
    text = await _block(Environment(model="agent-model"), _view(str(tmp_path), env, model="cfg"))
    assert "Model: `cfg`." in text
    text = await _block(Environment(model="agent-model"), _view(str(tmp_path), env))
    assert "Model: `agent-model`." in text


async def test_a_catalogued_model_gets_its_name_cutoff_and_the_attribution_note(
        tmp_path: Path) -> None:
    env = LocalExecEnv(str(tmp_path))
    text = await _block(Environment(), _view(str(tmp_path), env, model="claude-opus-5-5"))
    assert "Model: Claude Opus 5.5, exact id `claude-opus-5-5`; knowledge cutoff June 2026." \
        in text
    assert "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" in text
    text = await _block(Environment(attribution=False),
                        _view(str(tmp_path), env, model="claude-opus-5-5"))
    assert "Co-Authored-By" not in text


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_first_request_layout_and_budget_notes(tmp_path: Path) -> None:
    """The git snapshot and attribution ride in the first user turn before
    the prompt; the environment, budget and date follow it as one system
    turn; every later prompt and tool result gets a budget note, which
    counts down the output spent in the turn."""
    from noeta.backends.stores.sqlite import SqliteStore
    from noeta.core.agent import Agent
    from noeta.core.client import Client
    from noeta.core.runner import Runtime
    from noeta.core.types import ModelResponse, Usage
    from noeta.testing import ScriptedProvider, reply

    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.txt").write_text("a\n")
    store = SqliteStore(str(tmp_path / "t.db"))
    call = reply("", ("Probe", {}))
    spent = ModelResponse(message=call.message, stop_reason="tool_use",
                          usage=Usage(output=1000))
    provider = ScriptedProvider([spent, reply("done"), reply("again")])

    class Probe:
        name = "Probe"
        description = "probe"
        input_schema: dict[str, Any] = {"type": "object"}
        read_only = True
        clearable = False

        async def run(self, input: dict[str, Any], ctx: Any) -> Any:
            from noeta.core.tool import text_output
            return text_output("ok")

    agent = Agent(name="a", model="claude-opus-5-5", tools=[Probe()],
                  extensions=[Environment(today=lambda: "2026-10-28")])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(repo)), sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "hi", config={"cwd": str(repo)})
    await rt.run_until_idle()
    await client.send(tid, "more")
    await rt.run_until_idle()
    await store.close()

    first = provider.requests[0].messages
    assert [m.role for m in first] == ["user", "user", "user", "system", "system", "system"]
    assert first[0].text.startswith("Here is context from the workspace")
    assert first[1].text.startswith("From here on, end the git commit messages")
    assert first[2].text == "hi"
    assert first[3].text.startswith("# Environment\n")
    assert first[4].text == "<total_tokens>15000000 tokens left</total_tokens>"
    assert first[5].text == "The date today is 2026-10-28."
    second = provider.requests[1].messages
    assert second[-1].role == "system"
    assert second[-1].text == "<total_tokens>14999000 tokens left</total_tokens>"
    third = provider.requests[2].messages
    assert [m.text for m in third[-2:]] == [
        "more", "<total_tokens>15000000 tokens left</total_tokens>"]
    assert sum(m.text.startswith("# Environment") for m in third) == 1
