"""The commands tools run besides ``Bash``'s — a skill's script, an inline
skill command, ``git`` (and the hooks it runs) for a worktree, a rewind's
delete — run inside the task's sandbox when its tenant is not the default
one, so they cannot read what that tenant's ``Bash`` cannot."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from noeta.backends.execenvs import sandbox as sb
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.cli.config import local_env_for
from noeta.core.loop.state import TaskState
from noeta.core.tool import ToolContext
from noeta.plugins.skills.script import RunSkillScriptTool
from noeta.toolkit.sandbox import SandboxSettings, run_in_tenant_sandbox
from noeta.toolkit.tenant_fence import TenantFence
from noeta.toolkit.worktrees import git


def _bwrap_works() -> bool:
    bwrap = shutil.which("bwrap")
    return bwrap is not None and sys.platform.startswith("linux") \
        and sb._probe_bwrap(bwrap) is None


pytestmark = pytest.mark.skipif(not _bwrap_works(),
                                reason="bubblewrap cannot create namespaces here")


def _envs(tmp_path: Path) -> tuple[LocalExecEnv, LocalExecEnv, Path, Path]:
    home, ws = tmp_path / "home", tmp_path / "ws"
    home.mkdir()
    ws.mkdir()
    (home / "tokens.json").write_text("TOKENS")
    fence = TenantFence(home=str(home), workspaces={"acme": str(ws)})
    env_for = local_env_for(SandboxSettings(enabled=True), fence)
    envs = []
    for tenant in ("default", "acme"):
        state = TaskState(task_id="t1")
        state.tenant = tenant
        state.config = {"cwd": str(ws)}
        env = env_for(state)
        assert isinstance(env, LocalExecEnv)
        envs.append(env)
    return envs[0], envs[1], home, ws


async def test_a_command_runs_in_the_tenants_sandbox(tmp_path: Path) -> None:
    default, tenant, home, _ = _envs(tmp_path)
    argv = ["cat", str(home / "tokens.json")]
    assert b"TOKENS" in (await run_in_tenant_sandbox(default, argv)).stdout
    assert b"TOKENS" not in (await run_in_tenant_sandbox(tenant, argv)).stdout


async def test_a_git_hook_runs_in_the_tenants_sandbox(tmp_path: Path) -> None:
    default, tenant, home, ws = _envs(tmp_path)
    run = {"cwd": ws, "check": True, "capture_output": True}
    subprocess.run(["git", "init", "-q"], **run)  # type: ignore[call-overload]
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                    "--allow-empty", "-m", "x"], **run)  # type: ignore[call-overload]
    hook = ws / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ncat {home / 'tokens.json'} > {ws / 'seen.txt'} 2>&1\n")
    hook.chmod(0o755)
    await git(tenant, ["checkout", "-q", "-b", "one"], str(ws))
    assert "TOKENS" not in (ws / "seen.txt").read_text()
    await git(default, ["checkout", "-q", "-b", "two"], str(ws))
    assert "TOKENS" in (ws / "seen.txt").read_text()


async def test_a_skill_script_runs_in_the_tenants_sandbox(tmp_path: Path) -> None:
    default, tenant, home, ws = _envs(tmp_path)
    skill = ws / ".noeta" / "skills" / "tools"
    skill.mkdir(parents=True)
    (skill / "show.sh").write_text(f"cat {home / 'tokens.json'}\n")
    state: dict[str, Any] = {"skills": {"active": {"tools": {
        "dir": str(skill), "scripts": ["show.sh"]}}}}
    tool = RunSkillScriptTool()

    async def output(env: LocalExecEnv) -> str:
        ctx = ToolContext(task_id="t1", call_id="c1", env=env, state=state,
                          record=lambda *_a: None)
        out = await tool.run({"skill": "tools", "relpath": "show.sh"}, ctx)
        return "".join(getattr(c, "text", "") for c in out.content)

    assert "TOKENS" in await output(default)
    assert "TOKENS" not in await output(tenant)
    assert os.path.exists(skill / "show.sh")
