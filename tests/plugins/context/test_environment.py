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
from noeta.core.events import ExtensionRecorded, Recorded, Waiting
from noeta.core.execenv import RunResult
from noeta.core.extension import Extension, TaskView
from noeta.core.loop.state import TaskState
from noeta.core.tool import ToolFlags
from noeta.plugins.environment import Environment, extension, facts, notes, render_git
from noeta.plugins.environment import git as git_capture


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
async def test_a_partial_clone_never_fetches_through_the_repositorys_programs(
        tmp_path: Path) -> None:
    from tests.plugins.test_project_status import lazy_fetching_repo

    pwned = tmp_path / "PWNED"
    repo = tmp_path / "repo"
    lazy_fetching_repo(repo, pwned, ext=True)
    text = await _block(Environment(), _view(str(repo), LocalExecEnv(str(repo))))
    assert not pwned.exists()
    assert " - Is a git repository: true" in text


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


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_parent_repository_clean_filter_never_runs_from_a_subdirectory(
        tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sub = repo / "src"
    sub.mkdir(parents=True)
    pwned = tmp_path / "PWNED"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    (sub / "a.txt").write_text("a\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "filter.x.clean",
                    f"touch {pwned}; cat"], check=True)
    (repo / ".gitattributes").write_text("*.txt filter=x\n")
    (sub / "a.txt").touch()
    text = await _block(Environment(), _view(str(sub), LocalExecEnv(str(sub))))
    assert not pwned.exists()
    assert "Current branch: trunk" in text and "Status:\n(not captured)" in text


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_repository_signature_program_never_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    pwned = tmp_path / "PWNED"
    program = tmp_path / "verify.sh"
    program.write_text(f"#!/bin/sh\ntouch {pwned}\n")
    program.chmod(0o755)

    def git(*argv: str, stdin: bytes | None = None) -> str:
        return subprocess.run(["git", "-C", str(repo), *argv], input=stdin, check=True,
                              capture_output=True).stdout.decode().strip()

    git("init", "-q", "-b", "trunk")
    body = (f"tree {git('write-tree')}\nauthor a <a@b> 1 +0000\n"
            "committer a <a@b> 1 +0000\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n x\n"
            " -----END PGP SIGNATURE-----\n\nsigned\n")
    git("update-ref", "HEAD", git("hash-object", "-t", "commit", "-w", "--stdin",
                                  stdin=body.encode()))
    git("config", "log.showSignature", "true")
    git("config", "gpg.program", str(program))
    text = await _block(Environment(), _view(str(repo), LocalExecEnv(str(repo))))
    assert not pwned.exists()
    assert "signed" in text


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_submodule_clean_filter_never_runs(tmp_path: Path) -> None:
    sub, repo = tmp_path / "sub", tmp_path / "repo"
    pwned = tmp_path / "PWNED"
    who = ["-c", "user.name=t", "-c", "user.email=t@t"]
    for path in (sub, repo):
        subprocess.run(["git", "init", "-q", "-b", "trunk", str(path)], check=True)
    (sub / "a.txt").write_text("a\n")
    subprocess.run(["git", "-C", str(sub), "add", "a.txt"], check=True)
    subprocess.run(["git", "-C", str(sub), *who, "commit", "-q", "-m", "init"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "protocol.file.allow=always", "submodule",
                    "add", "-q", str(sub), "sub"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(repo), *who, "commit", "-q", "-m", "sub"], check=True)
    inner = repo / "sub"
    subprocess.run(["git", "-C", str(inner), "config", "filter.x.clean",
                    f"touch {pwned}; cat"], check=True)
    (inner / ".gitattributes").write_text("*.txt filter=x\n")
    (inner / "a.txt").write_text("b\n")
    text = await _block(Environment(), _view(str(repo), LocalExecEnv(str(repo))))
    assert not pwned.exists()
    assert "Current branch: trunk" in text


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_working_directory_below_the_repository_top_is_in_the_repository(
        tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    (sub / "a.txt").write_text("a\n")
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "init"], check=True)
    (sub / "b.txt").write_text("b\n")
    text = await _block(Environment(), _view(str(sub), LocalExecEnv(str(sub))))
    assert " - Is a git repository: true" in text
    assert "Current branch: trunk" in text and "init" in text and "b.txt" in text
    # Outside any repository: false, and no snapshot.
    outside = tmp_path / "plain" / "deeper"
    outside.mkdir(parents=True)
    text = await _block(Environment(), _view(str(outside), LocalExecEnv(str(outside))))
    assert " - Is a git repository: false" in text and "# Git status" not in text


async def test_the_shell_line_names_the_shell_commands_run_in(
        monkeypatch: pytest.MonkeyPatch) -> None:
    # Claude Code's format: bare names, ``bash (login shell: zsh)``.
    bash = shutil.which("bash") or "bash"
    monkeypatch.setenv("SHELL", "/opt/zsh-test/zsh")
    assert facts._shell() == "bash (login shell: zsh)"
    monkeypatch.setenv("SHELL", bash)
    assert facts._shell() == "bash"
    monkeypatch.setenv("SHELL", "/opt/other/bash")
    assert facts._shell() == "bash"
    monkeypatch.delenv("SHELL")
    assert facts._shell() == "bash"


class FakeContainer:
    """An ExecEnv that is not the local one; records what it runs."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.argv: list[list[str]] = []

    async def run(self, argv: Sequence[str], *, cwd: str | None = None,
                  env: Mapping[str, str] | None = None, stdin: bytes | None = None,
                  timeout: float | None = None, output_cap: int | None = None) -> RunResult:
        self.argv.append(list(argv))
        if argv[0] == "uname":
            return RunResult(exit_code=0, stdout=b"Linux\n")
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
    # The platform is the container's, whatever this machine is.
    assert " - Platform: linux" in text
    assert "Model: `agent-model`." in text
    for argv in env.argv:
        assert argv[:3] == ["git", "-c", "core.fsmonitor="] or argv[0] == "uname"
    assert any("status" in a for a in env.argv)


async def test_a_container_subdirectory_finds_the_repository_above_it() -> None:
    env = FakeContainer("/workspace")
    asked: list[str] = []
    stat = env.stat

    async def recording(path: str) -> object | None:
        asked.append(path)
        return await stat(path)

    env.stat = recording  # type: ignore[method-assign]
    text = await _block(Environment(model="m"), _view("/workspace/src/app", env))
    assert " - Is a git repository: true" in text and "Current branch: main" in text
    assert asked[:3] == ["/workspace/src/app/.git", "/workspace/src/.git", "/workspace/.git"]


async def test_a_long_status_is_cut_at_2000_characters_with_a_note() -> None:
    lines = "".join(f"?? dir/file-{i:04d}-é.txt\n" for i in range(200))

    class Busy(FakeContainer):
        async def run(self, argv: Sequence[str], **kw: Any) -> RunResult:
            if "status" in argv:
                return RunResult(exit_code=0, stdout=lines.encode())
            return await super().run(argv, **kw)

    text = await _block(Environment(model="m"), _view("/workspace", Busy("/workspace")))
    shown = text.split("Status:\n", 1)[1].split("\n\nRecent commits:", 1)[0]
    assert shown == lines[:2000] + git_capture.GIT_STATUS_CUT_NOTE
    assert "run `git status` with Bash" in shown
    short = "?? a.txt\n?? b.txt\n"

    class Quiet(FakeContainer):
        async def run(self, argv: Sequence[str], **kw: Any) -> RunResult:
            if "status" in argv:
                return RunResult(exit_code=0, stdout=short.encode())
            return await super().run(argv, **kw)

    text = await _block(Environment(model="m"), _view("/workspace", Quiet("/workspace")))
    assert "Status:\n?? a.txt\n?? b.txt\n\nRecent commits:" in text


async def test_a_status_that_fails_or_times_out_is_not_called_clean() -> None:
    # A home directory kept as a dotfiles repository: git status runs out
    # of time, and the model must not be told the checkout is clean.
    for result in (RunResult(exit_code=-1, stdout=b"", timed_out=True),
                   RunResult(exit_code=128, stdout=b"")):
        class Slow(FakeContainer):
            async def run(self, argv: Sequence[str], _r: RunResult = result,
                          **kw: Any) -> RunResult:
                if "status" in argv:
                    return _r
                return await super().run(argv, **kw)

        text = await _block(Environment(model="m"), _view("/workspace", Slow("/workspace")))
        assert "Status:\n(not captured)\n" in text and "(clean)" not in text


async def test_a_container_whose_platform_cannot_be_told_gets_no_platform_line() -> None:
    class Bare(FakeContainer):
        async def run(self, argv: Sequence[str], **kw: Any) -> RunResult:
            if argv[0] == "uname":
                return RunResult(exit_code=127, stdout=b"")
            return await super().run(argv, **kw)

    text = await _block(Environment(model="m"), _view("/workspace", Bare("/workspace")))
    assert " - Platform:" not in text and " - Is a git repository: true" in text


def test_an_unset_git_user_and_no_commits_leave_no_placeholder() -> None:
    text = render_git(branch="main", main="main", user="", status="", commits="")
    assert "Git user" not in text and "(unset)" not in text and "(none)" not in text
    assert "Main branch (where pull requests usually go): main\n\nStatus:\n(clean)" in text
    assert "Recent commits:\n\n" in text
    assert "Git user: ada\n\nStatus:" in render_git(branch="b", main="m", user="ada",
                                                     status="", commits="abc x")


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
    # The short display name in the model line, the full one in the trailer.
    assert "Model: Opus 5.5, exact id `claude-opus-5-5`; knowledge cutoff June 2026." \
        in text
    assert "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" in text
    text = await _block(Environment(attribution=False),
                        _view(str(tmp_path), env, model="claude-opus-5-5"))
    assert "Co-Authored-By" not in text


def test_the_co_author_address_comes_from_the_catalog() -> None:
    from noeta.core import models
    from noeta.plugins.environment import render_attribution

    models.register_models({
        "g1-test-other": models.ModelSpec(real_model_id="g1-test-other", context_window=1000,
                                          max_output_tokens=100, display_name="Other 1"),
        "g1-test-mailed": models.ModelSpec(real_model_id="g1-test-mailed",
                                           context_window=1000, max_output_tokens=100,
                                           display_name="Mailed 2",
                                           coauthor_email="bot@example.com"),
    })
    assert render_attribution("g1-test-other") is None
    note = render_attribution("g1-test-mailed")
    assert note is not None and "Co-Authored-By: Mailed 2 <bot@example.com>" in note
    claude = render_attribution("claude-opus-5-5")
    assert claude is not None and "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>" in claude


async def _no_sleep(_: float) -> None:
    return None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_first_request_layout_and_budget_notes(tmp_path: Path) -> None:
    """The git snapshot and attribution ride in the first user turn before
    the prompt; the environment, the deferred tools, budget and date follow
    it as one system
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
    provider = ScriptedProvider([spent, reply("done"), reply("again"), reply("plan on")])

    class Probe:
        name = "Probe"
        description = "probe"
        input_schema: dict[str, Any] = {"type": "object"}
        read_only = True
        clearable = False

        async def run(self, input: dict[str, Any], ctx: Any) -> Any:
            from noeta.core.tool import text_output
            return text_output("ok")

    class Later(Probe):
        name = "Later"
        flags = ToolFlags(deferred=True)

    agent = Agent(name="a", model="claude-opus-5-5", tools=[Probe(), Later()],
                  extensions=[Environment(today=lambda: "2026-10-28")])
    rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                 env_for=lambda _s: LocalExecEnv(str(repo)), sleep=_no_sleep)
    client = Client(store)
    tid = await client.create("a", "hi", config={"cwd": str(repo)})
    await rt.run_until_idle()
    await client.send(tid, "more")
    await rt.run_until_idle()
    # A typed slash command gets no budget note (Claude Code sends none).
    await client.send(tid, "<command-name>/plan</command-name>\n<command-args></command-args>")
    await rt.run_until_idle()
    await store.close()

    first = provider.requests[0].messages
    assert [m.role for m in first] == ["user", "user", "user", *["system"] * 4]
    assert first[0].text.startswith("Here is context from the workspace")
    assert first[1].text.startswith("From here on, end the git commit messages")
    assert first[2].text == "hi"
    assert first[3].text.startswith("# Environment\n")
    assert first[4].text.startswith("The tools below are deferred")
    assert first[4].text.endswith("before calling them:\nLater")
    assert first[5].text == "<total_tokens>15000000 tokens left</total_tokens>"
    assert first[6].text == "The date today is 2026-10-28."
    second = provider.requests[1].messages
    assert second[-1].role == "system"
    assert second[-1].text == "<total_tokens>14999000 tokens left</total_tokens>"
    third = provider.requests[2].messages
    assert [m.text for m in third[-2:]] == [
        "more", "<total_tokens>15000000 tokens left</total_tokens>"]
    assert sum(m.text.startswith("# Environment") for m in third) == 1
    assert provider.requests[3].messages[-1].text.startswith("<command-name>/plan")


def test_the_deferred_delta_names_what_joined_and_what_left() -> None:
    from noeta.plugins.environment import deferred_delta
    assert deferred_delta(["A"], ["A"], ["A"]) == ""
    text = deferred_delta(["A", "mcp__s__x"], ["A", "mcp__s__x", "mcp__s__y"],
                          ["A", "B", "mcp__s__y"])
    parts = text.split("\n\n")
    assert parts[0].startswith("The tools below are deferred") and parts[0].endswith("\nB")
    assert parts[1] == ("1 deferred tool is available again (their MCP server reconnected; "
                        "the names were given earlier in this conversation): mcp__s__y. Load "
                        "them with ToolSearch as before.")
    assert parts[2].startswith("These deferred tools are no longer available (their MCP server "
                               "disconnected)") and parts[2].endswith(":\nmcp__s__x")
    assert parts[3].startswith("This is background information")


async def test_deferred_tools_that_come_and_go_mid_task_are_told(tmp_path: Path) -> None:
    from noeta.backends.stores.sqlite import SqliteStore
    from noeta.core.agent import Agent
    from noeta.core.client import Client
    from noeta.core.extension import Extension
    from noeta.core.runner import Runtime
    from noeta.core.tool import text_output
    from noeta.testing import ScriptedProvider, reply

    class Remote:
        description = "remote"
        input_schema: dict[str, Any] = {"type": "object"}
        read_only = True
        clearable = False
        flags = ToolFlags(deferred=True)

        def __init__(self, name: str) -> None:
            self.name = name

        async def run(self, input: dict[str, Any], ctx: Any) -> Any:
            return text_output("ok")

    class Server(Extension):
        name = "server"
        up: tuple[str, ...] = ("mcp__s__a",)

        def tools(self, view: Any) -> Any:
            return [Remote(n) for n in self.up]

    server = Server()
    store = SqliteStore(str(tmp_path / "t.db"))
    provider = ScriptedProvider([reply("one"), reply("two"), reply("three"), reply("four")])
    agent = Agent(name="a", model="m", extensions=[server, Environment(today=lambda: "2026-10-28")])

    def runtime() -> Runtime:  # a new one per message: another worker, say
        return Runtime(store=store, agents={"a": agent}, provider_for=lambda _m: provider,
                       env_for=lambda _s: LocalExecEnv(str(tmp_path)), sleep=_no_sleep)

    client = Client(store)
    tid = await client.create("a", "hi", config={"cwd": str(tmp_path)})
    await runtime().run_until_idle()
    server.up = ("mcp__s__a", "mcp__s__b")
    await client.send(tid, "more")
    await runtime().run_until_idle()
    server.up = ("mcp__s__b",)
    await client.send(tid, "again")
    await runtime().run_until_idle()
    await client.send(tid, "quiet")
    await runtime().run_until_idle()
    await store.close()

    def notes(i: int) -> list[str]:
        return [m.text for m in provider.requests[i].messages
                if m.role == "system" and "deferred" in m.text][-1:]

    assert notes(1)[0].startswith("The tools below are deferred") and \
        notes(1)[0].endswith("\nmcp__s__b")
    assert "no longer available (their MCP server disconnected)" in notes(2)[0]
    assert notes(2)[0].split("\n\n")[0].endswith(":\nmcp__s__a")
    # Nothing changed since: no new note.
    assert len([m for m in provider.requests[3].messages if "no longer available" in m.text]) \
        == 1


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_changed_git_snapshot_is_given_again_at_a_later_turn(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", "trunk", str(repo)], check=True)
    (repo / "a.txt").write_text("a\n")
    subprocess.run([*git, "add", "a.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "first"], check=True)
    ext = Environment(git_refresh=True)
    view = _view(str(repo), LocalExecEnv(str(repo)))
    await ext.on_start(view)
    st: Any = None
    for rec in view.pending:
        assert isinstance(rec, ExtensionRecorded)
        st = ext.reduce(st, rec.kind, rec.data)
    st = ext.reduce(st, "sent", {})

    def later() -> TaskView:
        state = TaskState(task_id="t", config={"cwd": str(repo)}, ext={"environment": st})
        return TaskView(state=state, env=LocalExecEnv(str(repo)))

    assert [r for r in await ext.reminders(later()) if r.kind == "git"] == []
    # An edit (the model's own, say) changes the status, not the branch or commit.
    (repo / "a.txt").write_text("edited\n")
    assert [r for r in await ext.reminders(later()) if r.kind == "git"] == []
    subprocess.run([*git, "checkout", "-q", "-b", "fix/other"], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "second"], check=True)
    again = [r for r in await ext.reminders(later()) if r.kind == "git"]
    assert len(again) == 1 and "Current branch: fix/other" in again[0].text
    assert "second" in again[0].text and "replaces the earlier snapshot" in again[0].text
    st = ext.reduce(st, "git", again[0].data)
    assert [r for r in await ext.reminders(later()) if r.kind == "git"] == []
    # The model's own commit, inside a turn: the turn's end notes where HEAD
    # is, so the next turn has nothing new to tell.
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "by the model"], check=True)
    ended = later()
    await ext.on_event(ended, Recorded(seq=9, at=0.0, event=Waiting(on="input")))
    [mark] = ended.pending
    assert isinstance(mark, ExtensionRecorded) and mark.kind == "git_turn_end"
    st = ext.reduce(st, mark.kind, mark.data)
    assert [r for r in await ext.reminders(later()) if r.kind == "git"] == []
    # Switched off: the start snapshot only.
    subprocess.run([*git, "checkout", "-q", "trunk"], check=True)
    assert [r for r in await Environment(git_refresh=False).reminders(later())
            if r.kind == "git"] == []
    [back] = [r for r in await ext.reminders(later()) if r.kind == "git"]
    st = ext.reduce(st, "git", back.data)
    # Someone commits on the same branch between turns: that counts.
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "between"], check=True)
    [between] = [r for r in await ext.reminders(later()) if r.kind == "git"]
    assert "between" in between.text


async def test_a_model_claude_code_tells_no_budget_gets_no_budget(tmp_path: Path) -> None:
    """Only a model Claude Code knows as a Claude model is told the
    ``<total_tokens>`` budget: any other model gets no budget notes and the
    prompt's budget line is taken out wherever it sits (an appended prompt
    and a plugin's part follow it); a Claude model keeps both."""
    from noeta.backends.stores.sqlite import SqliteStore
    from noeta.core.agent import Agent
    from noeta.core.client import Client
    from noeta.core.extension import ContextPart, Extension
    from noeta.core.runner import Runtime
    from noeta.testing import ScriptedProvider, reply

    class Part(Extension):
        name = "part"

        def context(self, view: TaskView) -> Sequence[ContextPart]:
            return [ContextPart(text="A plugin's part.")]

    prompt = ("Be brief.\n\n<total_tokens>15000000 tokens left</total_tokens>\n\n"
              "Appended by the host.")
    sent: dict[str, Any] = {}
    for model in ("claude-opus-5-5", "opus", "g3-test-gateway-model"):
        store = SqliteStore(str(tmp_path / f"{model}.db"))
        provider = ScriptedProvider([reply("", ("Read", {})), reply("done")])
        agent = Agent(name="a", model=model, system_prompt=prompt,
                      extensions=[Environment(git_status=False, today=lambda: "2026-10-30"),
                                  Part()])
        rt = Runtime(store=store, agents={"a": agent}, provider_for=lambda _m, p=provider: p,
                     env_for=lambda _s: LocalExecEnv(str(tmp_path)), sleep=_no_sleep)
        await Client(store).create("a", "hi", config={"cwd": str(tmp_path)})
        await rt.run_until_idle()
        await store.close()
        sent[model] = provider.requests
    for model in ("claude-opus-5-5", "opus"):
        first, second = sent[model]
        assert first.system[-1].text == f"{prompt}\nA plugin's part."
        assert any(m.text == "<total_tokens>15000000 tokens left</total_tokens>"
                   for m in first.messages)
        assert second.messages[-1].text.startswith("<total_tokens>")
    for request in sent["g3-test-gateway-model"]:
        assert request.system[-1].text == "Be brief.\n\nAppended by the host.\nA plugin's part."
        assert not any("<total_tokens>" in m.text for m in request.messages)


@pytest.mark.parametrize(("model", "told"), [
    ("claude-opus-5-5", True), ("opus", True), ("claude-opus-5-5[1m]", True),
    ("us.anthropic.claude-opus-5-5-v1:0", True), ("anthropic/claude-opus-5-5", True),
    ("claude-opus-5-5-20260101", True), ("CLAUDE-OPUS-5-5", True),
    ("claude-opus-4-1", True), ("claude-3-5-haiku", True), ("claude-sonnet-4-20250514", True),
    ("gateway/other-model", False), ("gateway/deepseek-v4.1-flash", False),
    ("claude-foo-9", False), ("claude-opus-9", False), ("claude-3-opus", False),
])
def test_the_budget_goes_to_the_models_claude_code_knows(model: str, told: bool) -> None:
    from noeta.plugins.environment import gets_budget
    assert gets_budget(model) is told


async def test_a_compaction_gives_the_stored_snapshot_when_git_fails_or_is_slow(
        monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    ext = Environment(model="m")
    view = _view("/workspace", FakeContainer("/workspace"))
    fresh = await ext._git_for_compaction(view, "stored")
    assert fresh.startswith("Here is context") and " M src/x.py" in fresh
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and rec.kind == "git" and rec.data["text"] == fresh

    class Gone(FakeContainer):
        async def stat(self, path: str) -> object | None:
            raise OSError("container restarting")

    view = _view("/workspace", Gone("/workspace"))
    assert await ext._git_for_compaction(view, "stored") == "stored" and not view.pending

    class Slow(FakeContainer):
        async def run(self, argv: Sequence[str], **kw: Any) -> RunResult:
            await asyncio.sleep(5)
            return await super().run(argv, **kw)

    monkeypatch.setattr(extension, "GIT_RECAPTURE_TIMEOUT_S", 0.05)
    view = _view("/workspace", Slow("/workspace"))
    assert await ext._git_for_compaction(view, "stored") == "stored" and not view.pending

    class Plain(FakeContainer):
        async def stat(self, path: str) -> object | None:
            return None

    # No longer a repository: no snapshot at all.
    view = _view("/workspace", Plain("/workspace"))
    assert await ext._git_for_compaction(view, "stored") == ""


def _git_repo(path: Path, branch: str = "main") -> list[str]:
    git = ["git", "-C", str(path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "first"], check=True)
    return git


async def _started(ext: Environment, view: TaskView) -> Any:
    """The plugin's state after ``on_start`` and the first request."""
    await ext.on_start(view)
    st: Any = None
    for rec in view.pending:
        assert isinstance(rec, ExtensionRecorded)
        st = ext.reduce(st, rec.kind, rec.data)
    return ext.reduce(st, "sent", {})


def _later(cwd: str, st: Any, *, extensions: Sequence[Any] = (), **config: Any) -> TaskView:
    state = TaskState(task_id="t", config={"cwd": cwd, **config}, ext={"environment": st})
    return TaskView(state=state, env=LocalExecEnv(cwd), extensions=extensions)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_the_main_branch_falls_back_as_claude_code_does(tmp_path: Path) -> None:
    """``origin/HEAD``, else ``origin/main`` / ``origin/master``, else
    ``main``; a local branch alone does not count."""
    repo = tmp_path / "repo"
    git = _git_repo(repo, "trunk")
    env = LocalExecEnv(str(repo))
    assert await git_capture._main_branch(env, str(repo)) == "main"
    subprocess.run([*git, "update-ref", "refs/remotes/origin/master", "HEAD"], check=True)
    assert await git_capture._main_branch(env, str(repo)) == "master"
    subprocess.run([*git, "update-ref", "refs/remotes/origin/dev", "HEAD"], check=True)
    subprocess.run([*git, "symbolic-ref", "refs/remotes/origin/HEAD",
                    "refs/remotes/origin/dev"], check=True)
    assert await git_capture._main_branch(env, str(repo)) == "dev"


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_the_status_is_trimmed_as_claude_code_trims_it(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    git = _git_repo(repo)
    (repo / "a.txt").write_text("a\n")
    subprocess.run([*git, "add", "a.txt"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "a"], check=True)
    (repo / "a.txt").write_text("b\n")
    text = await _block(Environment(), _view(str(repo), LocalExecEnv(str(repo))))
    assert "Status:\nM a.txt\n" in text


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_linked_worktree_gets_the_worktree_and_stash_lines(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    git = _git_repo(repo)
    wt = tmp_path / "wt"
    subprocess.run([*git, "worktree", "add", "-q", str(wt)], check=True)
    text = await _block(Environment(), _view(str(wt), LocalExecEnv(str(wt))))
    assert f" - {facts.WORKTREE_LINE}\n - {facts.STASH_LINE}\n" \
        " - Is a git repository: true" in text
    text = await _block(Environment(), _view(str(repo), LocalExecEnv(str(repo))))
    assert facts.WORKTREE_LINE not in text and facts.STASH_LINE not in text


class _Dirs(Extension):
    """A plugin that keeps the working directories (as permissions does)."""

    name = "permissions"

    def __init__(self, *dirs: str) -> None:
        self.dirs = list(dirs)

    def working_directories(self, view: TaskView) -> tuple[str, ...]:
        return (view.cwd or "", *self.dirs)


async def test_additional_directories_are_listed_and_their_changes_told(
        tmp_path: Path) -> None:
    dirs = _Dirs("/srv/shared", "/srv/docs")
    view = TaskView(state=TaskState(task_id="t", config={"cwd": str(tmp_path)}),
                    env=LocalExecEnv(str(tmp_path)), extensions=[dirs])
    ext = Environment()
    st = await _started(ext, view)
    assert " - Is a git repository: false\n - Additional working directories:\n" \
        "  - /srv/shared\n  - /srv/docs\n - Platform:" in st["text"]
    assert [r for r in await ext.reminders(_later(str(tmp_path), st, extensions=[dirs]))
            if r.kind == "env_update"] == []
    dirs.dirs = ["/srv/shared", "/srv/new"]
    [update] = [r for r in await ext.reminders(_later(str(tmp_path), st, extensions=[dirs]))
                if r.kind == "env_update"]
    assert update.text == ("# Environment update\n"
                           " - Additional working directories added:\n  - /srv/new\n"
                           " - Additional working directories removed:\n  - /srv/docs")
    assert update.role == "system"
    st = ext.reduce(st, update.kind, update.data)
    assert "  - /srv/new\n" in st["text"] and "/srv/docs" not in st["text"]
    assert [r for r in await ext.reminders(_later(str(tmp_path), st, extensions=[dirs]))
            if r.kind == "env_update"] == []


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
async def test_a_directory_that_became_a_repository_is_told(tmp_path: Path) -> None:
    ext = Environment(git_status=False)
    st = await _started(ext, _view(str(tmp_path), LocalExecEnv(str(tmp_path))))
    _git_repo(tmp_path)
    [update] = [r for r in await ext.reminders(_later(str(tmp_path), st))
                if r.kind == "env_update"]
    assert update.text == "# Environment update\n - Is a git repository: true (was false)"


async def test_a_model_switch_is_told_before_the_next_call(tmp_path: Path) -> None:
    ext = Environment()
    st = await _started(ext, _view(str(tmp_path), LocalExecEnv(str(tmp_path)),
                                   model="claude-opus-5-5"))
    assert st["model"] == "claude-opus-5-5"
    same = await ext.reminders(_later(str(tmp_path), st, model="claude-opus-5-5"))
    assert [r for r in same if r.kind == "model"] == []
    [note] = [r for r in await ext.reminders(_later(str(tmp_path), st, model="claude-fable-5-1"))
              if r.kind == "model"]
    assert note.text == ("The model changed during this conversation. Model: Fable 5.1, exact id "
                         "`claude-fable-5-1`; knowledge cutoff June 2026.")
    st = ext.reduce(st, note.kind, note.data)
    assert "Model: Fable 5.1" in st["text"] and "Opus 5.5" not in st["text"]
    assert [r for r in await ext.reminders(_later(str(tmp_path), st, model="claude-fable-5-1"))
            if r.kind == "model"] == []
    # After a compaction the environment goes out again naming the model now.
    st = ext.reduce(st, "rehold", {"date": True})
    sent = await ext.reminders(_later(str(tmp_path), st, model="claude-sonnet-5-5"))
    [env] = [r for r in sent if r.kind == "sent"]
    assert "Model: Sonnet 5.5, exact id `claude-sonnet-5-5`" in env.text
    assert [r for r in sent if r.kind == "model"] == []


async def test_the_language_setting_is_told_and_its_change(tmp_path: Path) -> None:
    ext = Environment(language="Deutsch")
    st = await _started(ext, _view(str(tmp_path), LocalExecEnv(str(tmp_path))))
    st = ext.reduce(st, "sent", {})
    held = TaskState(task_id="t", config={"cwd": str(tmp_path)},
                     ext={"environment": {**st, "held": True}})
    [note] = [r for r in await ext.reminders(TaskView(state=held, env=LocalExecEnv(str(
        tmp_path)))) if r.kind == "language"]
    assert note.text.startswith("# Language\nAnswer in Deutsch:") and note.role == "system"
    st = ext.reduce(st, note.kind, note.data)
    assert [r for r in await ext.reminders(_later(str(tmp_path), st))
            if r.kind == "language"] == []
    [cleared] = [r for r in await Environment().reminders(_later(str(tmp_path), st))
                 if r.kind == "language"]
    assert cleared.text == notes.LANGUAGE_CLEARED
    assert [r for r in await Environment().reminders(_later(str(tmp_path), {**st,
            "language": None})) if r.kind == "language"] == []


def test_the_attribution_settings_replace_or_remove_the_lines() -> None:
    from noeta.plugins.environment import NO_ATTRIBUTION, render_attribution

    custom = render_attribution("claude-opus-5-5", commit="Assisted-by: AI", pr="")
    assert custom is not None and "- Commit messages end with:\nAssisted-by: AI" in custom
    assert "Pull request" not in custom.split("\n", 1)[1] and "Co-Authored-By" not in custom
    only_pr = render_attribution("claude-opus-5-5", pr="made by a bot")
    assert only_pr is not None and "Co-Authored-By: Claude Opus 5.5" in only_pr
    assert only_pr.endswith("- Pull request descriptions end with:\nmade by a bot")
    assert render_attribution("claude-opus-5-5", commit="", pr="") == NO_ATTRIBUTION
    assert render_attribution("unknown-model", commit="", pr="") == NO_ATTRIBUTION
    assert render_attribution("unknown-model") is None


async def test_fable_and_haiku_get_claude_codes_paragraphs_in_the_main_prompt() -> None:
    from noeta.core.types import ModelRequest, SystemPart
    from noeta.plugins.environment import EARLY_STOPPING, FABLE_IDENTITY

    prompt = ("Intro.\n\nCaution.\n\n# Session-specific guidance\n - Skills.\n\n# Environment\n"
              " - Facts.\n\nAct.\n\n<total_tokens>15000000 tokens left</total_tokens>")
    ext = Environment()
    view = TaskView(state=TaskState(task_id="t", config={}))

    async def system(model: str, text: str = prompt) -> str:
        request = ModelRequest(model=model, system=[SystemPart(text="Id."), SystemPart(text=text)])
        out = await ext.before_model(view, request)
        assert out.system[0].text == "Id."
        return out.system[1].text

    assert await system("claude-opus-5-5") == prompt
    assert await system("fable") == prompt.replace(
        "Caution.\n\n", f"Caution.\n\n{FABLE_IDENTITY}\n\n")
    assert await system("claude-haiku-5-5") == prompt.replace(
        "Act.\n\n", f"Act.\n\n{EARLY_STOPPING}\n\n")
    # A prompt that is not the main one (a sub-agent's, a replaced one) gets none.
    assert await system("claude-fable-5-1", "You are a helper.") == "You are a helper."
