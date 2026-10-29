"""The sandbox profile of a task whose tenant is not the default one: on top
of the operator's settings, its commands may not read noeta's home (but its
own corners), the spill directory (but its own), the store's files,
``/etc/noeta``, another tenant's workspace, the server's own directories or
any home directory's credential stores, write only where its file tools may,
and never leave the sandbox."""

from __future__ import annotations

import dataclasses
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from noeta.backends.execenvs import sandbox as sb
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.cli.config import local_env_for
from noeta.core.loop.state import TaskState
from noeta.toolkit.paths import project_slug
from noeta.toolkit.sandbox import SandboxSettings
from noeta.toolkit.tenant_fence import TenantFence


def _bwrap_works() -> bool:
    bwrap = shutil.which("bwrap")
    return bwrap is not None and sys.platform.startswith("linux") \
        and sb._probe_bwrap(bwrap) is None


needs_bwrap = pytest.mark.skipif(not _bwrap_works(),
                                 reason="bubblewrap cannot create namespaces here")


class Places:
    def __init__(self, tmp: Path) -> None:
        self.home = tmp / "home"
        self.spill = tmp / "spill"
        self.store = tmp / "db" / "tasks.sqlite"
        self.mine = tmp / "ws" / "acme"
        self.theirs = tmp / "ws" / "globex"
        self.project = self.home / "projects" / "acme" / project_slug(str(self.mine))
        for d in (self.home / "skills", self.home / "plans" / "acme", self.home / "plans" / "globex",
                  self.spill / "t1", self.spill / "other", self.store.parent, self.mine,
                  self.theirs, self.project / "memory",
                  self.home / "projects" / project_slug(str(self.mine))):
            d.mkdir(parents=True, exist_ok=True)
        files = {
            self.home / "tokens.json": "TOKENS", self.home / "skills" / "s.md": "SKILL",
            self.home / "plans" / "acme" / "p.md": "MYPLAN",
            self.home / "plans" / "globex" / "p.md": "THEIRPLAN",
            self.project / "memory" / "m.md": "MYMEMORY",
            self.project / "t1.jsonl": "MYTRANSCRIPT",
            self.project / "t9.jsonl": "THEIRTRANSCRIPT",
            self.home / "projects" / project_slug(str(self.mine)) / "m.md": "THEIRMEMORY",
            self.spill / "t1" / "a.txt": "MYSPILL", self.spill / "other" / "a.txt": "THEIRSPILL",
            self.store: "STORE", self.mine / "f.txt": "MYFILE", self.theirs / "f.txt": "THEIRFILE",
        }
        for path, text in files.items():
            path.write_text(text)
        self.fence = TenantFence(home=str(self.home), spill_root=str(self.spill),
                                 plans_dir=str(self.home / "plans"),
                                 store_files=(str(self.store),),
                                 workspaces={"acme": str(self.mine), "globex": str(self.theirs)})


def _state(tenant: str, cwd: Path) -> TaskState:
    state = TaskState(task_id="t1")
    state.tenant = tenant
    state.config = {"cwd": str(cwd)}
    return state


def test_the_profile_of_a_tenants_task(tmp_path: Path) -> None:
    p = Places(tmp_path)
    loose = SandboxSettings(enabled=True, excluded_commands=("docker *",),
                            filesystem_disabled=True, weaker_nested=True,
                            deny_read=("~/.ssh",))
    box = p.fence.confine(loose, tenant="acme", task_id="t1", cwd=str(p.mine))
    assert box.excluded_commands == () and not box.allow_unsandboxed
    # Another task's own corners differ; the tool description it gets does not.
    assert p.fence.confine(loose, tenant="acme", task_id="t2", cwd=str(p.mine)) == box
    assert not box.filesystem_disabled and not box.weaker_nested
    assert box.deny_read == ("~/.ssh",)  # what the model is told stays the operator's
    plan = sb.plan_paths(box, str(p.mine), str(tmp_path / "tmp"))
    real = os.path.realpath
    for denied in (p.home, p.spill, p.store, p.theirs, "/etc/noeta"):
        assert real(str(denied)) in plan.deny_read
    assert real(str(p.mine)) not in plan.deny_read
    for own in (p.spill / "t1", p.home / "skills", p.home / "plans" / "acme",
                p.project / "memory", p.project / "t1.jsonl"):
        assert real(str(own)) in plan.allow_read
    assert real(str(p.project)) not in plan.allow_read
    assert real(str(p.home / "tool-results")) not in plan.allow_read
    assert real(str(p.home / "projects")) not in plan.allow_read
    args = " ".join(sb.bwrap_args(box, plan, bwrap="bwrap", cwd=str(p.mine), relays=[],
                                  bridge="/b.py"))
    assert f"--tmpfs {real(str(p.theirs))}" in args
    assert f"--ro-bind /dev/null {real(str(p.store))}" in args
    assert "--proc /proc" in args  # its own PID namespace: no other process is visible
    profile = sb.seatbelt_profile(box, plan, (4000, 4001))
    assert f'(deny file-read* (subpath "{real(str(p.theirs))}"))' in profile
    assert f'(allow file-read* (subpath "{real(str(p.spill / "t1"))}"))' in profile


def test_the_default_tenants_profile_is_the_operators(tmp_path: Path) -> None:
    p = Places(tmp_path)
    settings = SandboxSettings(enabled=True, excluded_commands=("docker *",))
    assert p.fence.confine(settings, tenant="default", task_id="t1") is settings
    env_for = local_env_for(settings, p.fence)
    env = env_for(_state("default", p.mine))
    assert isinstance(env, LocalExecEnv) and env.sandbox is not None
    assert env.sandbox.settings is settings
    tenant_env = env_for(_state("acme", p.mine))
    assert isinstance(tenant_env, LocalExecEnv) and tenant_env.sandbox is not None
    assert tenant_env.sandbox.settings.excluded_commands == ()


async def _out(env: LocalExecEnv, command: str) -> str:
    assert env.sandbox is not None
    prepared = await env.sandbox.prepare(["bash", "-c", command], cwd=env.cwd)
    try:
        result = await env.run(prepared.argv, env=prepared.env, timeout=60)
    finally:
        await prepared.close()
    return (result.stdout + result.stderr).decode("utf-8", "replace")


@needs_bwrap
async def test_a_tenants_command_cannot_read_the_fenced_places(tmp_path: Path) -> None:
    p = Places(tmp_path)
    env_for = local_env_for(SandboxSettings(enabled=True), p.fence)
    show = " ".join(f"cat {f};" for f in (
        p.home / "tokens.json", p.home / "skills" / "s.md", p.home / "plans" / "acme" / "p.md",
        p.home / "plans" / "globex" / "p.md",
        p.project / "memory" / "m.md", p.project / "t1.jsonl", p.project / "t9.jsonl",
        p.home / "projects" / project_slug(str(p.mine)) / "m.md", p.spill / "t1" / "a.txt",
        p.spill / "other" / "a.txt", p.store, p.mine / "f.txt", p.theirs / "f.txt"))
    show += f" cat /proc/{os.getpid()}/environ; echo PIDS=$(ls /proc | grep -c '^[0-9]')"
    tenant = env_for(_state("acme", p.mine))
    assert isinstance(tenant, LocalExecEnv)
    out = await _out(tenant, show)
    for seen in ("SKILL", "MYPLAN", "MYMEMORY", "MYTRANSCRIPT", "MYSPILL", "MYFILE"):
        assert seen in out
    for hidden in ("TOKENS", "THEIRPLAN", "THEIRMEMORY", "THEIRTRANSCRIPT", "THEIRSPILL",
                   "STORE", "THEIRFILE"):
        assert hidden not in out
    assert "PATH=" not in out
    pids = int(out.rsplit("PIDS=", 1)[1].split()[0])
    assert pids < 10
    # The default tenant's commands see what they always did.
    default = env_for(_state("default", p.mine))
    assert isinstance(default, LocalExecEnv)
    out = await _out(default, f"cat {p.theirs / 'f.txt'} {p.spill / 'other' / 'a.txt'}")
    assert "THEIRFILE" in out and "THEIRSPILL" in out


class Shared:
    """What a machine's tasks leave in the temp directories: a background
    job's log, the commands' temporary files of a worker process, and
    another tenant's own temporary directory."""

    def __init__(self, temp: Path, monkeypatch: pytest.MonkeyPatch, p: Places) -> None:
        monkeypatch.setenv("TMPDIR", str(temp))
        self.root = temp / "noeta-root"
        self.fence = dataclasses.replace(p.fence, temp_root=str(self.root))
        self.log = temp / f"noeta-background-{os.getuid()}" / "w999" / "abc.output"
        self.sandbox_tmp = self.root / "sandbox" / "w1-x" / "tmp" / "f"
        self.theirs = self.root / "tenants" / "globex" / "tmp" / "f"
        self.their_log = self.root / "tenants" / "globex" / "jobs" / "w1" / "j.output"
        for path, text in ((self.log, "SECRET-OF-B"), (self.sandbox_tmp, "SHAREDTMP"),
                           (self.theirs, "GLOBEXTMP"), (self.their_log, "GLOBEXLOG")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        self.mine = Path(self.fence.temp_dir("acme"))

    def files(self) -> list[Path]:
        return [self.log, self.sandbox_tmp, self.theirs, self.their_log]


@pytest.fixture
def temp() -> Iterator[Path]:
    """A temp directory with a short path: a command's proxy sockets are
    made in its temporary directory."""
    made = Path(tempfile.mkdtemp(prefix="nt-", dir="/tmp"))
    yield made
    shutil.rmtree(made, ignore_errors=True)


def test_the_fence_keeps_tenants_out_of_shared_temp_places(
        tmp_path: Path, temp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = Places(tmp_path)
    s = Shared(temp, monkeypatch, p)
    for path in s.files():
        why = s.fence.refusal(str(path), tenant="acme", task_id="t1", cwd=str(p.mine))
        assert why is not None and "other tasks' command output" in why
        assert s.fence.refusal(str(path), tenant="default", task_id="t1") is None
    own = s.mine / "jobs" / "w1" / "x.output"
    assert s.fence.refusal(str(own), tenant="acme", task_id="t1", cwd=str(p.mine)) is None
    assert s.fence.refusal(str(own), tenant="acme", task_id="t1", cwd=str(p.mine),
                           write=True) is None


async def test_a_tenants_env_keeps_its_temp_files_and_job_logs_apart(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.plugins.shell.jobs import Registry

    p = Places(tmp_path)
    s = Shared(tmp_path, monkeypatch, p)
    env_for = local_env_for(None, s.fence)
    tenant = env_for(_state("acme", p.mine))
    assert isinstance(tenant, LocalExecEnv) and tenant.temp_dir == str(s.mine)
    assert os.stat(s.mine).st_mode & 0o777 == 0o700
    logs = await Registry()._log_dir(tenant)
    assert logs.startswith(str(s.mine / "jobs") + "/")
    for path in s.files():
        with pytest.raises(PermissionError):
            await tenant.read_bytes(str(path))
    # The default tenant's tasks keep the machine's shared places.
    default = env_for(_state("default", p.mine))
    assert isinstance(default, LocalExecEnv) and default.temp_dir is None
    assert (await Registry()._log_dir(default)).startswith(str(s.log.parent.parent) + "/")


@needs_bwrap
async def test_a_tenants_command_cannot_read_other_tasks_temp_files(
        tmp_path: Path, temp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = Places(tmp_path)
    s = Shared(temp, monkeypatch, p)
    env_for = local_env_for(SandboxSettings(enabled=True), s.fence)
    tenant = env_for(_state("acme", p.mine))
    assert isinstance(tenant, LocalExecEnv)
    show = " ".join(f"cat {f};" for f in s.files())
    show += ' echo "TMP=$TMPDIR"; echo MINE > "$TMPDIR/m" && cat "$TMPDIR/m"'
    out = await _out(tenant, show)
    for hidden in ("SECRET-OF-B", "SHAREDTMP", "GLOBEXTMP", "GLOBEXLOG"):
        assert hidden not in out
    assert f"TMP={s.mine / 'tmp'}" in out and "MINE" in out
    # A place made while the command runs is hidden too: it was made first.
    assert (s.root / "sandbox").is_dir()
    default = env_for(_state("default", p.mine))
    assert isinstance(default, LocalExecEnv)
    out = await _out(default, f"cat {s.log}")
    assert "SECRET-OF-B" in out


def test_a_writable_directory_inside_a_tenants_denied_one_is_shown_again(
        tmp_path: Path) -> None:
    outer = tmp_path / "outer"
    work = outer / "work"
    (work / ".git").mkdir(parents=True)
    (outer / "secret").write_text("x")
    real = os.path.realpath
    tenant = SandboxSettings(enabled=True, tenant_deny_read=(str(outer),))
    plan = sb.plan_paths(tenant, str(work), str(tmp_path / "tmp"))
    assert real(str(outer)) in plan.deny_read and plan.rebound == [real(str(work))]
    assert real(str(work / ".git" / "hooks")) in plan.deny_write
    args = sb.bwrap_args(tenant, plan, bwrap="bwrap", cwd=str(work), relays=[], bridge="/b")
    line = " ".join(args)
    assert line.index(f"--tmpfs {real(str(outer))}") < line.rindex(
        f"--bind {real(str(work))} {real(str(work))} --") < line.index(
        f"--remount-ro {real(str(outer))}")
    # The operator's denyRead of a directory holding the working one is
    # dropped, as before.
    operator = SandboxSettings(enabled=True, deny_read=(str(outer),))
    plan = sb.plan_paths(operator, str(work), str(tmp_path / "tmp"))
    assert real(str(outer)) not in plan.deny_read and plan.rebound == []


async def test_a_tenants_opened_file_is_checked_against_the_protected_set(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = Places(tmp_path)
    home = tmp_path / "userhome"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id_test").write_text("SSHKEY")
    (tmp_path / "shared.txt").write_text("SHARED")
    monkeypatch.setenv("HOME", str(home))
    os.symlink(home / ".ssh" / "id_test", p.mine / "link")
    os.symlink(tmp_path / "shared.txt", p.mine / "shared")
    tenant = local_env_for(None, p.fence)(_state("acme", p.mine))
    with pytest.raises(PermissionError, match="it holds credentials"):
        await tenant.read_bytes("link")
    with pytest.raises(PermissionError, match="outside the tenant's workspace"):
        await tenant.write_bytes(str(tmp_path / "elsewhere.txt"), b"x")
    assert await tenant.read_bytes("f.txt") == b"MYFILE"
    assert await tenant.read_bytes("shared") == b"SHARED"
    assert await tenant.read_bytes("/etc/hostname") is not None


class People:
    """The operator's home (credentials and notes), another account's in a
    directory of homes, the server's directory and an ``--allow-root`` path,
    an additional directory of the operator's, a place its ``denyRead``
    names and shared data elsewhere."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from noeta.toolkit import tenant_fence

        self.home, self.homes = tmp / "userhome", tmp / "homes"
        self.server, self.root = tmp / "server", tmp / "allowed-root"
        self.extra, self.denied, self.data = tmp / "extra", tmp / "denied", tmp / "data"
        for path, text in ((self.home / ".ssh" / "id_test", "SSHKEY"),
                           (self.home / "notes", "HOMENOTE"),
                           (self.homes / "bob" / ".aws" / "credentials", "BOBKEY"),
                           (self.homes / "bob" / "notes", "BOBNOTE"),
                           (self.server / ".env", "SERVERENV"), (self.root / "data", "ROOTDATA"),
                           (self.extra / "f", "EXTRA"), (self.denied / "f", "DENIED"),
                           (self.data / "opt" / "f", "OPTDATA")):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setattr(tenant_fence, "HOME_ROOTS", (str(self.homes),))
        monkeypatch.chdir(self.server)


@needs_bwrap
async def test_a_tenants_command_reads_all_but_the_protected_set(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = Places(tmp_path)
    o = People(tmp_path, monkeypatch)
    fence = dataclasses.replace(p.fence, server_dirs=(str(o.root),))
    # The operator's additional directory is writable for the default
    # tenant's commands, not for another tenant's.
    settings = SandboxSettings(enabled=True, allow_write=(str(o.extra),),
                               deny_read=(str(o.denied),))
    env_for = local_env_for(settings, fence)
    show = (f"cat ~/.ssh/id_test {o.home}/.ssh/id_test {o.homes}/bob/.aws/credentials "
            f"{o.server}/.env {o.root}/data {o.denied}/f; "
            f"ls /var/lib >/dev/null && echo VARSEEN; "
            f"cat ~/notes {o.homes}/bob/notes {o.extra}/f {o.data}/opt/f; "
            f"echo W > {o.extra}/g; echo W > {o.data}/g; "
            f"echo MINE > {p.mine}/new && cat {p.mine}/new")
    tenant = env_for(_state("acme", p.mine))
    assert isinstance(tenant, LocalExecEnv)
    out = await _out(tenant, show)
    for hidden in ("SSHKEY", "BOBKEY", "SERVERENV", "ROOTDATA", "DENIED"):
        assert hidden not in out, out
    for seen in ("HOMENOTE", "BOBNOTE", "EXTRA", "OPTDATA", "VARSEEN", "MINE"):
        assert seen in out, out
    assert not (o.extra / "g").exists() and not (o.data / "g").exists()
    default = env_for(_state("default", p.mine))
    assert isinstance(default, LocalExecEnv)
    out = await _out(default, show)
    for seen in ("BOBKEY", "SERVERENV", "ROOTDATA", "HOMENOTE"):
        assert seen in out, out
    # The sandbox's own credential stores and the operator's denyRead.
    assert "SSHKEY" not in out and "DENIED" not in out
    assert (o.extra / "g").exists()


@needs_bwrap
async def test_a_workspace_inside_the_server_directory_stays_the_tenants(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = tmp_path / "server"
    mine = server / "tenants" / "acme"
    (mine / "src").mkdir(parents=True)
    (mine / "src" / "f").write_text("MYFILE")
    (server / ".env").write_text("SERVERENV")
    monkeypatch.chdir(server)
    fence = TenantFence(home=str(tmp_path / "home"), workspaces={"acme": str(mine)})
    tenant = local_env_for(SandboxSettings(enabled=True), fence)(_state("acme", mine))
    assert isinstance(tenant, LocalExecEnv)
    out = await _out(tenant, f"cat {server}/.env; cat src/f; echo NEW > src/g && cat src/g")
    assert "SERVERENV" not in out and "MYFILE" in out and "NEW" in out, out
    assert await tenant.read_bytes("src/f") == b"MYFILE"
    with pytest.raises(PermissionError, match="server's own directories"):
        await tenant.read_bytes(str(server / ".env"))
