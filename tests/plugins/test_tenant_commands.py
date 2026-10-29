"""The commands tools run besides ``Bash``'s — a skill's script, an inline
skill command, ``git`` (and the filters it runs) for a worktree, a rewind's
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
from noeta.plugins.environment import capture_git
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


async def test_a_git_filter_runs_in_the_tenants_sandbox(tmp_path: Path) -> None:
    default, tenant, home, ws = _envs(tmp_path)
    run = {"cwd": ws, "check": True, "capture_output": True}
    (ws / "a.txt").write_text("a\n")
    (ws / ".gitattributes").write_text("a.txt filter=probe\n")
    subprocess.run(["git", "init", "-q"], **run)  # type: ignore[call-overload]
    subprocess.run(["git", "add", "-A"], **run)  # type: ignore[call-overload]
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=a", "commit", "-q",
                    "-m", "x"], **run)  # type: ignore[call-overload]
    subprocess.run(["git", "config", "filter.probe.smudge",  # type: ignore[call-overload]
                    f"cat {home / 'tokens.json'} > {ws / 'seen.txt'} 2>&1; cat"], **run)
    hook = ws / ".git" / "hooks" / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {ws / 'hook-ran'}\n")
    hook.chmod(0o755)
    (ws / "a.txt").unlink()
    await git(tenant, ["checkout", "-q", "--", "a.txt"], str(ws))
    assert "TOKENS" not in (ws / "seen.txt").read_text()
    (ws / "a.txt").unlink()
    await git(default, ["checkout", "-q", "--", "a.txt"], str(ws))
    assert "TOKENS" in (ws / "seen.txt").read_text()
    # noeta's own git runs no hook at all.
    await git(default, ["checkout", "-q", "-b", "two"], str(ws))
    assert not (ws / "hook-ran").exists()


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


def _signed_head(ws: Path) -> None:
    """A repository at ``ws`` whose HEAD carries a signature, so a
    ``git log`` with ``log.showSignature`` runs ``gpg.program``."""
    def run(*argv: str, stdin: bytes | None = None) -> str:
        done = subprocess.run(["git", *argv], cwd=ws, input=stdin, check=True,
                              capture_output=True)
        return done.stdout.decode().strip()

    run("init", "-q")
    body = (f"tree {run('write-tree')}\nauthor a <a@b> 1 +0000\n"
            "committer a <a@b> 1 +0000\ngpgsig -----BEGIN PGP SIGNATURE-----\n \n x\n"
            " -----END PGP SIGNATURE-----\n\nsigned\n")
    run("update-ref", "HEAD", run("hash-object", "-t", "commit", "-w", "--stdin",
                                  stdin=body.encode()))


async def test_the_environment_git_snapshot_runs_in_the_tenants_sandbox(
        tmp_path: Path) -> None:
    _, tenant, home, ws = _envs(tmp_path)
    _signed_head(ws)
    script = ws / "verify.sh"
    script.write_text(f"#!/bin/sh\ncat {home / 'tokens.json'} > {ws / 'seen.txt'} 2>&1\n")
    script.chmod(0o755)
    subprocess.run(["git", "config", "log.showSignature", "true"], cwd=ws, check=True)
    subprocess.run(["git", "config", "gpg.program", str(script)], cwd=ws, check=True)
    assert "Recent commits" in await capture_git(tenant, str(ws))
    seen = ws / "seen.txt"
    assert not seen.exists() or "TOKENS" not in seen.read_text()
