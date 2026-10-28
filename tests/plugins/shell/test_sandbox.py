"""``Bash`` with a sandboxed env: which commands are confined, the refusal
when the sandbox is required and cannot start, the violations note, the
description's sandbox section, background jobs; and real confinement where
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
from noeta.core.events import Say
from noeta.core.tool import ToolContext
from noeta.plugins.shell import Registry, Shell
from noeta.plugins.shell.bash import BashTool
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


@dataclass
class View:
    env: Any


def test_the_description_gains_a_sandbox_section(ws: Path) -> None:
    shell = Shell(background=False)
    plain = shell.tools(View(LocalExecEnv(str(ws))))  # type: ignore[arg-type]
    assert "Sandbox:" not in plain[0].description
    env, _ = fake_env(ws, allowed_domains=("github.com",), allow_write=("out",),
                      deny_read=("~/private",))
    tools = shell.tools(View(env))  # type: ignore[arg-type]
    text = tools[0].description
    assert text.startswith(plain[0].description)
    assert "Sandbox:" in text and f"`{ws}`" in text and f"`{ws}/out`" in text
    assert "`github.com`" in text and "`~/private`" in text
    assert "dangerouslyDisableSandbox: true" in text and "<sandbox_violations>" in text
    assert shell.tools(View(env))[0] is tools[0]  # cached  # type: ignore[arg-type]
    held, _ = fake_env(ws, allow_unsandboxed=False, http_proxy_port=9000,
                       filesystem_disabled=True)
    text = shell.tools(View(held))[0].description  # type: ignore[arg-type]
    assert "switched off" in text and "user's proxy" in text and "not confined" in text
    none, _ = fake_env(ws)
    assert "no host can be reached" in shell.tools(View(none))[0].description  # type: ignore[arg-type]
    broken, fake = fake_env(ws)
    fake.reason = "no"
    assert shell.tools(View(broken))[0].description == plain[0].description  # type: ignore[arg-type]


async def test_background_jobs_run_sandboxed(tmp_path: Path) -> None:
    work = tmp_path / "w"
    work.mkdir()
    registry = Registry(log_dir=str(tmp_path / "logs"))
    shell = Shell(registry=registry)
    env, fake = fake_env(work)
    posts: list[str] = []

    async def post(body: Any, task_id: str | None = None) -> None:
        assert isinstance(body, Say)
        posts.append(body.message.text)

    ctx = ToolContext(task_id="t", call_id="c1", env=env, state={},
                      record=lambda *_: None, post=post, root_id="t")
    out = await shell._tools[0].run({"command": PROBE, "run_in_background": True}, ctx)
    job_id = out.meta["job_id"]
    job = registry.get(job_id)
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
