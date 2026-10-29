"""A fenced ``LocalExecEnv`` checks the file it actually opened (and the
directory a write lands in), so a symlink swapped after the permission gate
looked cannot carry a tenant's read or write into a fenced place."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from noeta.backends.execenvs.local import LocalExecEnv
from noeta.core.events import DEFAULT_TENANT
from noeta.core.loop.state import TaskState
from noeta.toolkit.tenant_fence import TenantFence


def _env(tmp_path: Path) -> tuple[LocalExecEnv, Path, Path]:
    ws, secret = tmp_path / "ws", tmp_path / "secret"
    ws.mkdir()
    secret.mkdir()
    (secret / "tokens.json").write_text("token")
    (ws / "ok.txt").write_text("fine")
    real_secret = os.path.realpath(secret)

    def fence(path: str, write: bool) -> str | None:
        inside = path == real_secret or path.startswith(real_secret + "/")
        return f"fenced: {path}" if inside else None

    return LocalExecEnv(str(ws), fence=fence), ws, secret


async def test_a_read_through_a_link_into_the_fence_is_refused(tmp_path: Path) -> None:
    env, ws, secret = _env(tmp_path)
    os.symlink(secret / "tokens.json", ws / "link")
    assert await env.read_bytes("ok.txt") == b"fine"
    with pytest.raises(PermissionError, match="fenced"):
        await env.read_bytes("link")
    with pytest.raises(PermissionError, match="fenced"):
        await env.read_range(str(ws / "link"), 0, 3)


async def test_a_write_into_a_fenced_directory_is_refused(tmp_path: Path) -> None:
    env, ws, secret = _env(tmp_path)
    os.symlink(secret, ws / "dir")
    await env.write_bytes("new/a.txt", b"x")
    assert (ws / "new" / "a.txt").read_bytes() == b"x"
    with pytest.raises(PermissionError, match="fenced"):
        await env.write_bytes("dir/planted.txt", b"x")
    with pytest.raises(PermissionError, match="fenced"):
        await env.write_bytes(str(secret / "tokens.json"), b"x")
    assert sorted(p.name for p in secret.iterdir()) == ["tokens.json"]
    assert (secret / "tokens.json").read_text() == "token"


async def test_a_fenced_write_keeps_the_files_mode_and_writes_through_a_link(
        tmp_path: Path) -> None:
    env, ws, _ = _env(tmp_path)
    (ws / "run.sh").write_text("old")
    os.chmod(ws / "run.sh", 0o750)
    os.symlink(ws / "run.sh", ws / "alias")
    await env.write_bytes("alias", b"new")
    assert (ws / "run.sh").read_bytes() == b"new" and (ws / "alias").is_symlink()
    assert (ws / "run.sh").stat().st_mode & 0o777 == 0o750


def test_the_cli_fences_only_other_tenants_tasks(tmp_path: Path) -> None:
    from noeta.cli.config import local_env_for

    env_for = local_env_for(None, TenantFence(home=str(tmp_path / "home")))
    mine, theirs = TaskState(task_id="t"), TaskState(task_id="u")
    theirs.tenant = "acme"
    assert mine.tenant == DEFAULT_TENANT
    assert getattr(env_for(mine), "fence", None) is None
    fence = getattr(env_for(theirs), "fence", None)
    assert fence is not None and fence(str(tmp_path / "home" / "tokens.json"), False)
