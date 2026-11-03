"""``Bash`` with a sandboxed env: which commands are confined, the refusal
when the sandbox is required and cannot start, the violations note, the
sandbox reminder, background jobs; and real confinement where
bubblewrap works."""

from __future__ import annotations

import asyncio
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs import sandbox as sb
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.events import ExtensionRecorded, Say
from noeta.core.extension import TaskView
from noeta.core.loop.state import TaskState
from noeta.core.tool import ToolContext
from noeta.plugins.shell import Registry, Shell
from noeta.plugins.shell.bash import BashTool
from noeta.plugins.shell.sandbox import SANDBOX_OFF
from noeta.plugins.shell.state import SANDBOX_KIND, ShellState, reduce_shell
from noeta.toolkit.sandbox import SandboxSettings
from tests.plugins.conftest import Harness, text_of


@dataclass
class Prepared:
    argv: list[str]
    env: dict[str, str]
    refused: list[str]
    closed: list[bool]

    def violations(self) -> list[str]:
        return self.refused

    async def close(self) -> None:
        self.closed.append(True)


@dataclass
class FakeSandbox:
    """Marks a command as confined by prefixing ``env SANDBOXED=1``."""

    settings: SandboxSettings
    reason: str | None = None
    refused: list[str] = field(default_factory=list)
    calls: list[list[str]] = field(default_factory=list)
    closed: list[bool] = field(default_factory=list)

    def unavailable(self) -> str | None:
        return self.reason

    async def prepare(self, argv: Sequence[str], *, cwd: str,
                      writable: Sequence[str] = ()) -> Prepared:
        self.calls.append(list(argv))
        return Prepared(["env", "SANDBOXED=1", *argv], {"FROM_SANDBOX": "yes"},
                        list(self.refused), self.closed)


def fake_env(ws: Path, **settings: Any) -> tuple[LocalExecEnv, FakeSandbox]:
    env = LocalExecEnv(str(ws))
    fake = FakeSandbox(SandboxSettings(enabled=True, **settings))
    env.sandbox = fake  # type: ignore[assignment]
    return env, fake


PROBE = 'echo "${SANDBOXED:-no}/${FROM_SANDBOX:-no}"'


async def test_commands_run_through_the_sandbox(ws: Path) -> None:
    env, fake = fake_env(ws, excluded_commands=("docker *",))
    h = Harness(env=env)
    assert text_of(await h.call(BashTool(), command=PROBE)) == "1/yes"
    assert fake.closed == [True]
    out = await h.call(BashTool(), command=PROBE, dangerouslyDisableSandbox=True)
    assert text_of(out) == "no/no"
    assert len(fake.calls) == 1
    fake.refused = ["evil.example:443"]
    out = await h.call(BashTool(), command="true")
    assert text_of(out).startswith("<sandbox_violations>\n- network: evil.example:443")


async def test_unsandboxed_runs_can_be_switched_off(ws: Path) -> None:
    env, _ = fake_env(ws, allow_unsandboxed=False)
    out = await Harness(env=env).call(BashTool(), command=PROBE,
                                      dangerouslyDisableSandbox=True)
    assert text_of(out) == "1/yes"


async def test_an_unavailable_sandbox(ws: Path) -> None:
    env, fake = fake_env(ws)
    fake.reason = "bwrap is missing"
    assert text_of(await Harness(env=env).call(BashTool(), command=PROBE)) == "no/no"
    env, fake = fake_env(ws, fail_if_unavailable=True, excluded_commands=("git *",))
    fake.reason = "bwrap is missing"
    h = Harness(env=env)
    out = await h.call(BashTool(), command=PROBE, dangerouslyDisableSandbox=True)
    assert out.is_error and "bwrap is missing" in text_of(out)
    assert "not run" in text_of(out)
    assert not (await h.call(BashTool(), command="git --version")).is_error


def _view(shell: Shell, env: Any, state: ShellState | None = None,
          denied: tuple[str, ...] = ()) -> TaskView:
    task = TaskState(task_id="t", ext={"shell": state} if state is not None else {})
    return TaskView(state=task, env=env, extensions=[shell], denied_tools=denied)


async def _told(shell: Shell, env: Any, state: ShellState) -> tuple[str, ShellState] | None:
    """The sandbox reminder for ``env`` after ``state``, and the state it
    leaves; ``None`` when nothing is sent."""
    view = _view(shell, env, state)
    found = [r for r in await shell.reminders(view) if r.kind == SANDBOX_KIND]
    if not found:
        return None
    [reminder] = found
    assert reminder.role == "system"
    return reminder.text, reduce_shell(state, SANDBOX_KIND, reminder.data)


async def test_the_sandbox_is_told_in_a_reminder_again_when_it_changes(ws: Path) -> None:
    # Claude Code's sandbox_instructions: not part of Bash's description, a
    # harness note sent when it differs from the last one, a note when it is off.
    shell = Shell(background=False)
    plain = LocalExecEnv(str(ws))
    assert "sandbox" not in shell.tools(_view(shell, plain))[0].description.lower()
    st = ShellState()
    assert await _told(shell, plain, st) is None
    env, _ = fake_env(ws, allowed_domains=("github.com",), allow_write=("out",),
                      deny_read=("~/private",))
    assert "sandbox" not in shell.tools(_view(shell, env))[0].description.lower()
    told = await _told(shell, env, st)
    assert told is not None
    text, st = told
    assert text.startswith("Bash sandbox:") and f"`{ws}`" in text and f"`{ws}/out`" in text
    assert "`github.com`" in text and "`~/private`" in text
    assert "dangerouslyDisableSandbox: true" in text and "<sandbox_violations>" in text
    assert await _told(shell, env, st) is None  # unchanged: nothing
    held, _ = fake_env(ws, allow_unsandboxed=False, http_proxy_port=9000,
                       filesystem_disabled=True)
    told = await _told(shell, held, st)
    assert told is not None
    text, st = told
    assert "switched off" in text and "user's proxy" in text and "not confined" in text
    none, _ = fake_env(ws)
    told = await _told(shell, none, st)
    assert told is not None and "no host can be reached" in told[0]
    broken, fake = fake_env(ws)
    fake.reason = "no"
    told = await _told(shell, broken, told[1])
    assert told is not None and told[0] == SANDBOX_OFF
    assert await _told(shell, broken, told[1]) is None
    # Nothing for a task without Bash.
    view = _view(shell, env, ShellState(), denied=("Bash",))
    assert [r for r in await shell.reminders(view) if r.kind == SANDBOX_KIND] == []


async def test_a_compaction_tells_the_sandbox_again(ws: Path) -> None:
    shell = Shell(background=False)
    env, _ = fake_env(ws)
    told = await _told(shell, env, ShellState())
    assert told is not None
    view = _view(shell, env, told[1])
    view.keeps_tail = True
    await shell.resident(view)
    [rec] = view.pending
    assert isinstance(rec, ExtensionRecorded) and (rec.kind, rec.data) == (SANDBOX_KIND,
                                                                           {"text": ""})
    assert await _told(shell, env, reduce_shell(told[1], SANDBOX_KIND, rec.data)) is not None


async def test_background_jobs_run_sandboxed(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    registry = Registry(log_dir=str(tmp_path / "logs"))
    shell = Shell(registry=registry)
    env, fake = fake_env(work)
    posts: list[str] = []

    async def post(body: Any, task_id: str | None = None, **_kw: Any) -> None:
        assert isinstance(body, Say)
        posts.append(body.message.text)

    ctx = ToolContext(task_id="t", call_id="c1", env=env, state={},
                      record=lambda *_: None, post=post, root_id="t")
    out = await shell._tools[0].run({"command": PROBE, "run_in_background": True}, ctx)
    job_id = out.meta["job_id"]
    job = registry.find("t", job_id)
    assert job is not None
    await asyncio.wait_for(job.done.wait(), 10)
    assert job.sender is not None
    await asyncio.wait_for(job.sender, 10)
    assert (await env.read_bytes(job.out_path)).startswith(b"1/yes")
    assert fake.calls and fake.closed == [True]


# ---------------------------------------------------------------- real bwrap


def _bwrap_works() -> bool:
    bwrap = shutil.which("bwrap")
    return bwrap is not None and sys.platform.startswith("linux") \
        and sb._probe_bwrap(bwrap) is None


@pytest.mark.skipif(not _bwrap_works(), reason="bubblewrap cannot create namespaces here")
async def test_bash_in_a_real_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOETA_HOME", str(tmp_path / "home"))
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    h = Harness(env=LocalExecEnv(str(ws), sandbox={"enabled": True}))
    assert text_of(await h.call(BashTool(), command="echo ok > inside && cat inside")) == "ok"
    out = await h.call(BashTool(), command=f"echo no > {outside}/f")
    assert out.is_error and "Read-only file system" in text_of(out)
    out = await h.call(BashTool(), command=f"echo yes > {outside}/f",
                       dangerouslyDisableSandbox=True)
    assert not out.is_error and (outside / "f").read_text() == "yes\n"
    out = await h.call(BashTool(), command=(
        "python3 -c \"import os,urllib.request as u;"
        "u.build_opener(u.ProxyHandler({'http': os.environ['http_proxy']}))"
        ".open('http://blocked.example/', timeout=10)\""))
    assert out.is_error and "blocked.example:80" in text_of(out)
    assert "<sandbox_violations>" in text_of(out)
