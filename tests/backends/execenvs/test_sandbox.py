"""The local sandbox: the filtering proxy (any platform), the bwrap and
Seatbelt command lines, and — where bubblewrap can create namespaces — real
confined commands: writes outside the allowed paths fail, a host not allowed
is refused, an allowed one is reached through the proxy."""

from __future__ import annotations

import asyncio
import http.server
import os
import shutil
import struct
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from noeta.backends.execenvs import sandbox as sb
from noeta.backends.execenvs.local import LocalExecEnv
from noeta.backends.execenvs.sandbox_proxy import FilterProxy
from noeta.toolkit.sandbox import SandboxSettings


def _bwrap_works() -> bool:
    bwrap = shutil.which("bwrap")
    return bwrap is not None and sys.platform.startswith("linux") \
        and sb._probe_bwrap(bwrap) is None


needs_bwrap = pytest.mark.skipif(not _bwrap_works(),
                                 reason="bubblewrap cannot create namespaces here")


@pytest.fixture
def server() -> Iterator[int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"reached-" + self.path.encode())

        def log_message(self, *args: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "noeta-home"
    path.mkdir()
    monkeypatch.setenv("NOETA_HOME", str(path))
    return path


# ---------------------------------------------------------------- the proxy


async def _http_get(port: int, url: str) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {url} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    data = await reader.read()
    writer.close()
    return data


async def test_proxy_forwards_allowed_and_refuses_the_rest(server: int) -> None:
    proxy = FilterProxy()
    ls = await proxy.open(allowed=[f"127.0.0.1:{server}"], tcp_host="127.0.0.1")
    try:
        ok = await _http_get(ls.http[1], f"http://127.0.0.1:{server}/x")
        assert b"200" in ok.split(b"\r\n")[0] and ok.endswith(b"reached-/x")
        refused = await _http_get(ls.http[1], "http://blocked.example/")
        assert refused.startswith(b"HTTP/1.1 403")
        assert ls.refused == ["blocked.example:80"]
        # CONNECT tunnels the same way.
        reader, writer = await asyncio.open_connection(*ls.http)
        writer.write(f"CONNECT 127.0.0.1:{server} HTTP/1.1\r\n\r\n".encode())
        assert (await reader.readline()).startswith(b"HTTP/1.1 200")
        await reader.readline()
        writer.write(b"GET /tunnel HTTP/1.0\r\n\r\n")
        assert (await reader.read()).endswith(b"reached-/tunnel")
        writer.close()
    finally:
        await proxy.close(ls)


async def test_socks_proxy_checks_the_host(server: int) -> None:
    proxy = FilterProxy()
    ls = await proxy.open(allowed=[f"127.0.0.1:{server}"], tcp_host="127.0.0.1")
    try:
        async def ask(host: bytes, port: int) -> tuple[asyncio.StreamReader,
                                                       asyncio.StreamWriter, int]:
            reader, writer = await asyncio.open_connection(*ls.socks)
            writer.write(b"\x05\x01\x00")
            assert await reader.readexactly(2) == b"\x05\x00"
            writer.write(b"\x05\x01\x00\x03" + bytes([len(host)]) + host
                         + struct.pack("!H", port))
            reply = await reader.readexactly(10)
            return reader, writer, reply[1]

        reader, writer, code = await ask(b"127.0.0.1", server)
        assert code == 0
        writer.write(b"GET /s HTTP/1.0\r\n\r\n")
        assert (await reader.read()).endswith(b"reached-/s")
        writer.close()
        _, writer, code = await ask(b"evil.example", 443)
        assert code == 2
        writer.close()
        assert ls.refused == ["evil.example:443"]
    finally:
        await proxy.close(ls)


async def test_proxy_checks_the_address_a_name_resolves_to(
        server: int, monkeypatch: pytest.MonkeyPatch) -> None:
    proxy = FilterProxy()
    ls = await proxy.open(allowed=["*"], tcp_host="127.0.0.1")
    try:
        # An allowed name (here every name) resolving to this machine or a
        # metadata address is refused like a host not allowed.
        for host in ("0x7f000001", "169.254.169.254"):
            out = await _http_get(ls.http[1], f"http://{host}:{server}/x")
            assert out.startswith(b"HTTP/1.1 403"), host
        reader, writer = await asyncio.open_connection(*ls.http)
        writer.write(f"CONNECT 0x7f000001:{server} HTTP/1.1\r\n\r\n".encode())
        assert (await reader.readline()).startswith(b"HTTP/1.1 403")
        writer.close()
        assert ls.refused == [f"0x7f000001:{server}", f"169.254.169.254:{server}"]
        # So is a loopback address spelled any other way: ``*`` never opens
        # this machine's ports.
        for host in ("::ffff:127.0.0.1", "0:0:0:0:0:0:0:1", "0:0::1", "::127.0.0.1"):
            out = await _http_get(ls.http[1], f"http://[{host}]:{server}/x")
            assert out.startswith(b"HTTP/1.1 403"), host
    finally:
        await proxy.close(ls)
    # The connection goes to the address that was checked, not to a second
    # lookup of the name.
    from noeta.backends.execenvs import sandbox_proxy

    async def pinned(host: str, port: int, *_a: object) -> str:
        assert host == "pinned.example"
        return "127.0.0.1"

    monkeypatch.setattr(sandbox_proxy, "resolve_allowed", pinned)
    ls = await proxy.open(allowed=["pinned.example"], tcp_host="127.0.0.1")
    try:
        out = await _http_get(ls.http[1], f"http://pinned.example:{server}/p")
        assert out.endswith(b"reached-/p")
    finally:
        await proxy.close(ls)


async def test_proxy_hands_off_to_the_users_own(server: int) -> None:
    proxy = FilterProxy()
    # The "user's proxy" here is the test server itself: the request
    # reaches it unread and unfiltered.
    ls = await proxy.open(allowed=[], tcp_host="127.0.0.1", upstream_http=server)
    try:
        out = await _http_get(ls.http[1], "/direct")
        assert out.endswith(b"reached-/direct")
        assert ls.refused == []
    finally:
        await proxy.close(ls)


# ---------------------------------------------------------------- command lines


def test_plan_and_bwrap_args(tmp_path: Path, home: Path) -> None:
    root = tmp_path / "ws"
    (root / ".git" / "hooks").mkdir(parents=True)
    (root / ".git" / "config").write_text("")
    (root / "secret").mkdir()
    extra = tmp_path / "extra"
    extra.mkdir()
    settings = SandboxSettings.from_json({
        "enabled": True,
        "filesystem": {"allowWrite": [str(extra)], "denyRead": ["secret"],
                       "allowRead": ["secret/ok"]},
        "credentials": {"envVars": [{"name": "API_TOKEN", "mode": "mask"}]}})
    plan = sb.plan_paths(settings, str(root), str(tmp_path / "t"), ())
    assert str(root) in plan.writable and str(extra) in plan.writable
    assert str(tmp_path / "t") not in plan.writable  # missing paths are dropped
    assert str(root / ".git" / "hooks") in plan.deny_write
    assert str(root / "secret") in plan.deny_read
    assert str(home) in plan.deny_read
    args = sb.bwrap_args(settings, plan, bwrap="bwrap", cwd=str(root),
                         relays=[("/r/http.sock", 3128)], bridge="/b.py")
    joined = " ".join(args)
    assert args[:2] == ["bwrap", "--die-with-parent"] and "--unshare-net" in args
    assert f"--bind {root} {root}" in joined
    assert f"--tmpfs {root}/secret" in joined
    assert f"--ro-bind {root}/.git/hooks {root}/.git/hooks" in joined
    assert "--unsetenv API_TOKEN" in joined
    assert args[-1] == "--" and "--http" in args and "--allow-unix" not in args
    loose = SandboxSettings(enabled=True, allow_all_unix_sockets=True,
                            nested_user_namespaces=True, weaker_nested=True)
    loose_args = sb.bwrap_args(loose, plan, bwrap="bwrap", cwd=str(root), relays=[],
                               bridge="/b.py")
    assert "--allow-unix" in loose_args and "--allow-userns" in loose_args
    assert "--proc" not in loose_args


def test_seatbelt_profile_shape(tmp_path: Path, home: Path) -> None:
    # Generated only: macOS is not available to run it here.
    settings = SandboxSettings(enabled=True, allow_local_binding=True,
                               allow_unix_sockets=("/var/run/x.sock",))
    (tmp_path / ".git").mkdir()
    plan = sb.plan_paths(settings, str(tmp_path), str(tmp_path), ())
    profile = sb.seatbelt_profile(settings, plan, (4000, 4001))
    assert profile.startswith("(version 1)\n(allow default)\n(deny file-write*)")
    assert f'(allow file-write* (subpath "{tmp_path}"))' in profile
    assert '(allow network-outbound (remote ip "localhost:4000"))' in profile
    assert '(path-literal "/var/run/x.sock")' in profile
    assert f'(deny file-write* (subpath "{tmp_path}/.noeta"))' in profile
    assert f'(deny file-write* (literal "{tmp_path}/.git"))' in profile  # no rename
    assert profile.index("(deny network*)") < profile.index("localhost:4001")


def test_missing_bwrap_is_explained(tmp_path: Path) -> None:
    settings = SandboxSettings(enabled=True, bwrap_path="/nonexistent/bwrap")
    linux = sb.LocalSandbox(settings, str(tmp_path), platform="linux")
    reason = linux.unavailable()
    assert reason is not None and "bwrap" in reason
    other = sb.LocalSandbox(settings, str(tmp_path), platform="win32")
    assert "not supported" in (other.unavailable() or "")
    env = LocalExecEnv(str(tmp_path), sandbox={"enabled": False})
    assert env.sandbox is None


def test_bwrap_missing_from_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    linux = sb.LocalSandbox(SandboxSettings(enabled=True), str(tmp_path), platform="linux")
    assert "not installed" in (linux.unavailable() or "")


# ---------------------------------------------------------------- real commands


async def _run(env: LocalExecEnv, command: str) -> tuple[int, str, list[str]]:
    assert env.sandbox is not None
    prepared = await env.sandbox.prepare(["bash", "-c", command], cwd=env.cwd)
    try:
        result = await env.run(prepared.argv, env=prepared.env, timeout=60)
    finally:
        refused = prepared.violations()
        await prepared.close()
    return result.exit_code, (result.stdout + result.stderr).decode(), refused


_FETCH = ("import os,sys,urllib.request as u;os.environ.pop('no_proxy',0);"
          "os.environ.pop('NO_PROXY',0);"
          "o=u.build_opener(u.ProxyHandler({'http': os.environ['http_proxy']}));"
          "r=o.open(sys.argv[1], timeout=10);print(r.status, r.read().decode())")


@needs_bwrap
async def test_writes_outside_the_allowed_paths_fail(tmp_path: Path, home: Path) -> None:
    ws, outside = tmp_path / "ws", tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    env = LocalExecEnv(str(ws), sandbox={"enabled": True})
    code, out, _ = await _run(env, f"echo in > {ws}/a && cat {ws}/a")
    assert code == 0 and out.strip() == "in"
    code, out, _ = await _run(env, f"echo out > {outside}/b")
    assert code != 0 and "Read-only file system" in out
    assert not (outside / "b").exists()
    code, out, _ = await _run(env, 'touch "$TMPDIR/t" && echo tmp-ok')
    assert code == 0 and "tmp-ok" in out
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]\n")
    code, out, _ = await _run(env, f"echo x >> {ws}/.git/config")
    assert code != 0
    assert (ws / ".git" / "config").read_text() == "[core]\n"
    # The empty read-only placeholder over the missing hooks directory is
    # gone with the command.
    assert not (ws / ".git" / "hooks").exists() and not (ws / ".noeta").exists()


@needs_bwrap
async def test_protected_paths_cannot_be_created_or_moved_away(tmp_path: Path,
                                                               home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    env = LocalExecEnv(str(ws), sandbox={"enabled": True})
    # Missing: nothing under .noeta or .git can be created.
    for command in ("mkdir -p .noeta/plugins && echo x > .noeta/plugins/x.py",
                    "mkdir -p .noeta && echo {} > .noeta/settings.local.json",
                    "echo {} > .noeta/mcp.json",
                    "git init -q . && git config core.fsmonitor 'touch pwned'",
                    "mkdir -p .git && echo '[core]' > .git/config"):
        code, _, _ = await _run(env, command)
        assert code != 0, command
    assert sorted(os.listdir(ws)) == []
    # Existing: neither directory can be renamed away and recreated.
    (ws / ".noeta").mkdir()
    (ws / ".noeta" / "settings.json").write_text("{}")
    (ws / ".git" / "hooks").mkdir(parents=True)
    (ws / ".git" / "config").write_text("[core]\n")
    for command in ("mv .noeta old && mkdir .noeta && echo x > .noeta/settings.local.json",
                    "mkdir .noeta/plugins",
                    "echo x > .noeta/settings.local.json",
                    "mv .git old && mkdir -p .git && echo x > .git/config",
                    "rm -rf .git/hooks && mkdir .git/hooks && echo x > .git/hooks/pre-commit"):
        code, _, _ = await _run(env, command)
        assert code != 0, command
    assert sorted(os.listdir(ws)) == [".git", ".noeta"]
    assert os.listdir(ws / ".noeta") == ["settings.json"]
    assert (ws / ".git" / "config").read_text() == "[core]\n"
    assert os.listdir(ws / ".git" / "hooks") == []
    # The rest of .git stays writable: git still commits.
    code, out, _ = await _run(env, "echo ref > .git/HEAD.tmp && rm .git/HEAD.tmp && echo ok")
    assert code == 0 and "ok" in out


def test_a_worktree_git_file_is_read_only(tmp_path: Path, home: Path) -> None:
    (tmp_path / ".git").write_text("gitdir: /elsewhere\n")
    plan = sb.plan_paths(SandboxSettings(enabled=True), str(tmp_path), str(tmp_path / "t"))
    assert str(tmp_path / ".git") in plan.deny_write and plan.pinned == []
    held = dict(plan.placeholders)
    assert held[str(tmp_path / ".noeta")] is False
    assert not any(p.startswith(str(tmp_path / ".git") + "/") for p in held)


def test_placeholders_outlive_no_command_but_the_last(tmp_path: Path, home: Path) -> None:
    plan = sb.plan_paths(SandboxSettings(enabled=True), str(tmp_path), str(tmp_path / "t"))
    first = sb._hold(plan)
    (tmp_path / ".noeta").mkdir()  # what bwrap leaves on the host
    (tmp_path / ".git").mkdir()
    second = sb._hold(sb.plan_paths(SandboxSettings(enabled=True), str(tmp_path),
                                    str(tmp_path / "t")))
    sb._release(first)
    assert (tmp_path / ".noeta").is_dir() and (tmp_path / ".git").is_dir()
    sb._release(second)
    assert not (tmp_path / ".noeta").exists() and not (tmp_path / ".git").exists()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=cwd,
                   check=True, capture_output=True)


def _repo_with_hooks_elsewhere(ws: Path) -> None:
    """A repository whose code-running paths go beyond ``.git/hooks`` and
    ``.git/config``: a submodule's git directory, hooks in ``githooks``
    (``core.hooksPath``), editor tasks and direnv."""
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "core.hooksPath", "githooks")
    module = ws / ".git" / "modules" / "lib"
    (module / "hooks").mkdir(parents=True)
    (module / "HEAD").write_text("ref: refs/heads/main\n")
    (module / "config").write_text("[core]\n")
    (ws / "githooks").mkdir()
    (ws / ".husky").mkdir()
    (ws / ".vscode").mkdir()
    (ws / ".vscode" / "tasks.json").write_text("{}")
    (ws / ".envrc").write_text("")
    (ws / "a.txt").write_text("a\n")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "init")


def test_the_protected_list_follows_the_permissions_and_git(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    _repo_with_hooks_elsewhere(ws)
    plan = sb.plan_paths(SandboxSettings(enabled=True), str(ws), str(tmp_path / "t"))
    for path in (ws / ".envrc", ws / ".vscode", ws / ".husky", ws / "githooks",
                 ws / ".git" / "modules", ws / ".git" / "commondir",
                 ws / ".git" / "config.worktree"):
        assert str(path) in plan.deny_write, path
    held = dict(plan.placeholders)
    # Missing git config files are empty files; anything else an empty
    # directory, which git does not list.
    assert held[str(ws / ".git" / "config.worktree")] is True
    assert held[str(ws / ".noeta")] is False
    # Other protected names are read-only when present, never placeheld: a
    # placeholder is a directory the person sees appear in the project.
    assert str(ws / ".pre-commit-config.yaml") not in held
    assert set(held) <= {str(ws / n) for n in sb.PLACEHOLDERS}, held
    # Git dies on an empty commondir: a missing one is removed afterwards.
    assert plan.remove_after == [str(ws / ".git" / "commondir")]
    # Every directory above a protected path is pinned: none can be renamed.
    assert str(ws / ".git") in plan.pinned


def _linked_worktree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(main repository, its git directory, a linked worktree)``, the
    worktree where an isolated agent's goes."""
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q")
    (main / "a.txt").write_text("a\n")
    _git(main, "add", "-A")
    _git(main, "commit", "-qm", "init")
    tree = main / ".noeta" / "worktrees" / "agent-x"
    _git(main, "worktree", "add", "-q", "-b", "worktree-agent-x", str(tree))
    return main, main / ".git", tree


def test_a_linked_worktree_may_write_the_git_data_it_commits_to(tmp_path: Path,
                                                                home: Path) -> None:
    main, common, tree = _linked_worktree(tmp_path)
    plan = sb.plan_paths(SandboxSettings(enabled=True), str(tree), str(tmp_path / "t"))
    gitdir = common / "worktrees" / "agent-x"
    for path in (gitdir, common / "objects", common / "refs", common / "logs"):
        assert str(path) in plan.writable, path
    for path in (common, common / "hooks", common / "config", main):
        assert str(path) not in plan.writable, path
    for path in (gitdir / "commondir", gitdir / "gitdir", gitdir / "config.worktree"):
        assert str(path) in plan.deny_write, path
    assert dict(plan.placeholders)[str(gitdir / "config.worktree")] is True
    profile = sb.seatbelt_profile(SandboxSettings(enabled=True), plan, ())
    assert f'(allow file-write* (subpath "{common}/objects"))' in profile
    assert f'(deny file-write* (subpath "{gitdir}/commondir"))' in profile
    assert f'(allow file-write* (subpath "{common}"))' not in profile


def test_a_git_file_not_pointing_back_opens_nothing(tmp_path: Path, home: Path) -> None:
    _, common, _ = _linked_worktree(tmp_path)
    elsewhere = tmp_path / "ws"
    elsewhere.mkdir()
    (elsewhere / ".git").write_text(f"gitdir: {common / 'worktrees' / 'agent-x'}\n")
    plan = sb.plan_paths(SandboxSettings(enabled=True), str(elsewhere), str(tmp_path / "t"))
    assert plan.writable == [str(elsewhere)]
    (elsewhere / ".git").write_text(f"gitdir: {tmp_path}\n")  # not a linked worktree
    plan = sb.plan_paths(SandboxSettings(enabled=True), str(elsewhere), str(tmp_path / "t"))
    assert plan.writable == [str(elsewhere)]


@needs_bwrap
async def test_an_agent_commits_in_its_worktree_but_cannot_touch_git_code(
        tmp_path: Path, home: Path) -> None:
    main, common, tree = _linked_worktree(tmp_path)
    env = LocalExecEnv(str(tree), sandbox={"enabled": True})
    git = "git -c user.name=t -c user.email=t@t"
    code, out, _ = await _run(env, f"echo hi > f && {git} add f && {git} commit -qm x && "
                                   f"{git} log --oneline | wc -l")
    assert code == 0, out
    assert out.split() == ["2"]
    gitdir = common / "worktrees" / "agent-x"
    for command in (f"echo x >> {common}/config", f"echo x > {common}/hooks/pre-commit",
                    f"echo x > {gitdir}/config.worktree", f"echo {tmp_path} > {gitdir}/commondir",
                    f"mv {gitdir} {common}/worktrees/old", f"echo x > {common}/HEAD",
                    f"echo x > {main}/b.txt"):
        code, _, _ = await _run(env, command)
        assert code != 0, command
    assert not (gitdir / "config.worktree").exists()
    assert not (common / "hooks" / "pre-commit").exists()
    assert (gitdir / "commondir").read_text().strip() == "../.."


@needs_bwrap
async def test_git_cannot_be_pointed_at_other_config_or_hooks(tmp_path: Path,
                                                              home: Path) -> None:
    main, common, _ = _linked_worktree(tmp_path)
    module = common / "modules" / "lib"
    module.mkdir(parents=True)
    (module / "HEAD").write_text("ref: refs/heads/main\n")
    env = LocalExecEnv(str(main), sandbox={"enabled": True})
    # commondir redirects config and hooks; a missing one written by the
    # command is gone when it ends.
    code, out, _ = await _run(env, "echo ../evil > .git/commondir && echo planted")
    assert "planted" in out or code != 0
    assert not (common / "commondir").exists()
    gitdir = common / "worktrees" / "agent-x"
    for command in ("mkdir -p .git/modules/new/hooks", "echo x > .git/modules/lib/config",
                    "mv .git/modules .git/old-modules",
                    f"echo {tmp_path} > {gitdir}/commondir", f"echo x > {gitdir}/gitdir"):
        code, _, _ = await _run(env, command)
        assert code != 0, command
    assert sorted(os.listdir(common / "modules")) == ["lib"]
    assert not (module / "config").exists()
    assert (gitdir / "commondir").read_text().strip() == "../.."
    # Git still works in the sandbox, and the placeholders are gone.
    git = "git -c user.name=t -c user.email=t@t"
    code, out, _ = await _run(env, f"echo hi > f && {git} add f && {git} commit -qm x && "
                                   f"{git} status --short && echo ok")
    assert code == 0 and "ok" in out, out
    shutil.rmtree(common / "modules")
    code, out, _ = await _run(env, f"{git} status --short && echo ok")
    assert code == 0 and "ok" in out, out
    assert not (common / "modules").exists() and not (common / "commondir").exists()


def _workspace_of_repos(tmp_path: Path) -> Path:
    """A workspace that is no repository itself, holding one two levels down
    (with editor tasks and a missing hooks directory) and one in a
    dependency tree."""
    ws = tmp_path / "ws"
    repo = ws / "group" / "proj"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    shutil.rmtree(repo / ".git" / "hooks")
    (repo / ".vscode").mkdir()
    deep = ws / "node_modules" / "dep"
    deep.mkdir(parents=True)
    _git(deep, "init", "-q")
    return ws


def test_repositories_below_the_working_directory_are_protected(tmp_path: Path,
                                                                home: Path) -> None:
    ws = _workspace_of_repos(tmp_path)
    repo = ws / "group" / "proj"
    plan = sb.plan_paths(SandboxSettings(enabled=True), str(ws), str(tmp_path / "t"))
    for path in (repo / ".git" / "config", repo / ".git" / "hooks", repo / ".vscode"):
        assert str(path) in plan.deny_write, path
    assert dict(plan.placeholders)[str(repo / ".git" / "hooks")] is False
    assert str(repo / ".git") in plan.pinned and str(repo) in plan.pinned
    assert plan.remove_after == [str(repo / ".git" / "commondir")]
    # Dependency trees are not searched.
    assert not any("node_modules" in p for p in plan.deny_write)


@needs_bwrap
async def test_a_nested_repository_hook_cannot_be_planted(tmp_path: Path, home: Path) -> None:
    ws = _workspace_of_repos(tmp_path)
    repo = ws / "group" / "proj"
    env = LocalExecEnv(str(ws), sandbox={"enabled": True})
    for command in ("mkdir -p group/proj/.git/hooks && echo x > group/proj/.git/hooks/pre-commit",
                    "echo x >> group/proj/.git/config", "echo x > group/proj/.vscode/tasks.json",
                    "mv group/proj group/old"):
        code, _, _ = await _run(env, command)
        assert code != 0, command
    assert not (repo / ".git" / "hooks").exists()
    code, out, _ = await _run(env, "echo hi > group/proj/f && git -C group/proj status "
                                   "--porcelain && echo ok")
    assert code == 0 and "ok" in out and "f" in out, out


#: What a package manager or build tool runs on its next ordinary command.
_RUN_ON_NEXT_COMMAND = (".npmrc", ".yarnrc", ".yarnrc.yml", ".pnpmfile.cjs", ".pnp.cjs",
                        ".pnp.loader.mjs", "bunfig.toml", ".bunfig.toml", ".bazelrc",
                        ".bazelversion", ".bazeliskrc", ".cargo", ".mvn", ".yarn")


def test_an_empty_project_gets_placeholders_for_what_runs_next(tmp_path: Path,
                                                               home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    held = dict(sb.plan_paths(SandboxSettings(enabled=True), str(ws),
                              str(tmp_path / "t")).placeholders)
    assert held == {str(ws / ".noeta"): False, str(ws / ".git"): False,
                    **{str(ws / n): n not in (".cargo", ".mvn", ".yarn")
                       for n in _RUN_ON_NEXT_COMMAND}}
    # What needs a separate step of the person's gets none.
    for name in (".envrc", ".vscode", ".devcontainer", ".idea", ".pre-commit-config.yaml",
                 "lefthook-local.yml"):
        assert str(ws / name) not in held


def test_a_missing_hooks_directory_is_placeheld_whatever_its_name(tmp_path: Path,
                                                                  home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "core.hooksPath", ".husky")
    (ws / "lefthook.yml").write_text("")
    held = dict(sb.plan_paths(SandboxSettings(enabled=True), str(ws),
                              str(tmp_path / "t")).placeholders)
    assert held[str(ws / ".husky")] is False
    # With lefthook in use, its local config runs on the next commit too.
    assert held[str(ws / "lefthook-local.yml")] is True


@needs_bwrap
async def test_what_a_tool_runs_next_cannot_be_created(tmp_path: Path, home: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    _git(ws, "init", "-q")
    _git(ws, "config", "core.hooksPath", ".husky")
    (ws / "a.txt").write_text("a\n")
    before = sorted(os.listdir(ws))
    exclude = (ws / ".git" / "info" / "exclude").read_text()
    env = LocalExecEnv(str(ws), sandbox={"enabled": True})
    for command in ("mkdir -p .husky && echo x > .husky/pre-commit",
                    "echo x > .husky/pre-commit",
                    "echo 'node-options=--require ./x.js' > .npmrc",
                    "echo 'yarnPath: x.js' > .yarnrc.yml", "echo x > .pnpmfile.cjs",
                    "echo 'preload = [\"x.ts\"]' > bunfig.toml",
                    "mkdir -p .cargo && echo x > .cargo/config.toml",
                    "mkdir -p .mvn && echo x > .mvn/extensions.xml",
                    "echo 'build --x' > .bazelrc", "mkdir -p .yarn/plugins"):
        code, _, _ = await _run(env, command)
        assert code != 0, command
    assert sorted(os.listdir(ws)) == before
    # The placeholders read as empty, and git neither lists nor adds them.
    git = "git -c user.name=t -c user.email=t@t"
    code, out, _ = await _run(env, f"cat .npmrc bunfig.toml && {git} status --porcelain && "
                                   f"{git} add -A && {git} commit -qm a && "
                                   f"{git} show --stat --format= HEAD")
    assert code == 0, out
    assert out.split()[:2] == ["??", "a.txt"] and ".npmrc" not in out, out
    assert sorted(os.listdir(ws)) == before
    assert (ws / ".git" / "info" / "exclude").read_text() == exclude


@needs_bwrap
async def test_what_runs_outside_the_sandbox_cannot_be_written(tmp_path: Path,
                                                               home: Path) -> None:
    ws = tmp_path / "ws"
    _repo_with_hooks_elsewhere(ws)
    (ws / ".pre-commit-config.yaml").write_text("")
    (ws / ".config" / "git").mkdir(parents=True)
    (ws / ".config" / "git" / "config").write_text("")
    with (ws / ".git" / "info" / "exclude").open("a") as f:
        f.write(".pre-commit-config.yaml\n.config/\n")
    before = sorted(os.listdir(ws))
    env = LocalExecEnv(str(ws), sandbox={"enabled": True})
    for command in ("echo x >> .git/modules/lib/config",
                    "echo x > .git/modules/lib/hooks/post-checkout",
                    "mv .git/modules/lib .git/modules/old",
                    "mv .git/modules .git/old",
                    "echo '[core]' > .git/config.worktree",
                    "echo x > githooks/pre-commit",
                    "mv githooks old && mkdir githooks && echo x > githooks/pre-commit",
                    "echo x > .husky/pre-commit",
                    "echo 'repos: []' > .pre-commit-config.yaml",
                    "echo x > .vscode/tasks.json",
                    "echo x >> .envrc",
                    "echo x > .config/git/config"):
        code, _, _ = await _run(env, command)
        assert code != 0, command
    assert sorted(os.listdir(ws)) == before
    assert (ws / ".git" / "modules" / "lib" / "config").read_text() == "[core]\n"
    assert os.listdir(ws / "githooks") == [] and os.listdir(ws / ".git/modules/lib/hooks") == []
    assert (ws / ".envrc").read_text() == "" and not (ws / ".git" / "config.worktree").exists()
    # Git still works in the sandbox, placeholders and all.
    git = "git -c user.name=t -c user.email=t@t"
    code, out, _ = await _run(env, f"echo b > b.txt && {git} status --porcelain && "
                                   f"{git} add -A && {git} commit -qm b && "
                                   f"{git} checkout -qb other && {git} checkout -q - && "
                                   f"{git} submodule status && {git} log --oneline | wc -l")
    assert code == 0, out
    assert out.split() == ["??", "b.txt", "2"]


@needs_bwrap
async def test_network_goes_only_through_the_filter(tmp_path: Path, home: Path,
                                                    server: int) -> None:
    env = LocalExecEnv(str(tmp_path), sandbox={
        "enabled": True, "network": {"allowedDomains": [f"127.0.0.1:{server}"]}})
    code, out, refused = await _run(
        env, f"python3 -c \"{_FETCH}\" http://127.0.0.1:{server}/allowed")
    assert code == 0 and "200 reached-/allowed" in out and refused == []
    code, out, refused = await _run(
        env, f"python3 -c \"{_FETCH}\" http://not-allowed.example/")
    assert code != 0 and "403" in out
    assert refused == ["not-allowed.example:80"]
    # Straight to the host's port, around the proxy: another network.
    code, out, _ = await _run(
        env, f"python3 -c \"import socket;socket.create_connection(('127.0.0.1',{server}),3)\"")
    assert code != 0 and "Connection refused" in out


@needs_bwrap
async def test_unix_sockets_and_user_namespaces_are_refused(tmp_path: Path,
                                                            home: Path) -> None:
    env = LocalExecEnv(str(tmp_path), sandbox={"enabled": True})
    code, out, _ = await _run(
        env, "python3 -c 'import socket; socket.socket(socket.AF_UNIX)'")
    assert code != 0 and "Operation not permitted" in out
    if shutil.which("unshare"):
        code, _, _ = await _run(env, "unshare -U true")
        assert code != 0
    loose = LocalExecEnv(str(tmp_path), sandbox={
        "enabled": True, "network": {"allowAllUnixSockets": True}})
    code, _, _ = await _run(
        loose, "python3 -c 'import socket; socket.socket(socket.AF_UNIX)'")
    assert code == 0


@needs_bwrap
async def test_credentials_are_hidden(tmp_path: Path, home: Path,
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    (home / "token").write_text("secret")
    (home / "tool-results").mkdir()
    (home / "tool-results" / "out.txt").write_text("spilled")
    monkeypatch.setenv("API_TOKEN", "hunter2")
    env = LocalExecEnv(str(tmp_path), sandbox={
        "enabled": True,
        "credentials": {"envVars": [{"name": "API_TOKEN", "mode": "deny"}]}})
    _, out, _ = await _run(env, f"cat {home}/token; cat {home}/tool-results/out.txt; "
                                   "echo token=${API_TOKEN:-unset}")
    assert "secret" not in out and "spilled" in out and "token=unset" in out


@needs_bwrap
async def test_a_pasted_image_in_noetas_home_can_be_copied(tmp_path: Path, home: Path) -> None:
    (home / "image-cache" / "t").mkdir(parents=True)
    (home / "image-cache" / "t" / "1-1.png").write_bytes(b"png bytes")
    env = LocalExecEnv(str(tmp_path), sandbox={"enabled": True})
    code, _, _ = await _run(env, f"cp {home}/image-cache/t/1-1.png logo.png")
    assert code == 0 and (tmp_path / "logo.png").read_bytes() == b"png bytes"


@needs_bwrap
async def test_a_timed_out_command_dies_with_its_sandbox(tmp_path: Path, home: Path) -> None:
    env = LocalExecEnv(str(tmp_path), sandbox={"enabled": True}, kill_grace=0.5)
    assert env.sandbox is not None
    prepared = await env.sandbox.prepare(
        ["bash", "-c", f"sleep 30 & echo $! > {tmp_path}/pid; wait"], cwd=env.cwd)
    result = await env.run(prepared.argv, env=prepared.env, timeout=2)
    await prepared.close()
    assert result.timed_out
    await asyncio.sleep(0.5)
    pid = int((tmp_path / "pid").read_text())  # the namespace's pid; gone with it
    alive = [p for p in os.listdir("/proc") if p.isdigit()
             and _cmdline(p) == b"sleep\x0030\x00" and _ppid_chain_has(p, os.getpid())]
    assert pid and not alive


def _cmdline(pid: str) -> bytes:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


def _ppid_chain_has(pid: str, ancestor: int) -> bool:
    current = int(pid)
    while current > 1:
        if current == ancestor:
            return True
        try:
            stat = Path(f"/proc/{current}/stat").read_text()
        except OSError:
            return False
        current = int(stat.rpartition(")")[2].split()[1])
    return False


# ---------------------------------------------------------------- seccomp


def _run_bpf(program: bytes, nr: int, arch: int, arg0: int = 0) -> int:
    """What the classic BPF ``program`` returns for one system call."""
    data = struct.pack("<iI", nr, arch) + struct.pack("<Q", 0) + struct.pack("<Q", arg0)
    ins = [struct.unpack("HBBI", program[i:i + 8]) for i in range(0, len(program), 8)]
    pc, acc = 0, 0
    while True:
        code, jt, jf, k = ins[pc]
        if code == 0x20:
            acc = struct.unpack_from("<I", data, k)[0]
        elif code == 0x06:
            return int(k)
        elif code in (0x15, 0x45):
            hit = acc == k if code == 0x15 else bool(acc & k)
            pc += jt if hit else jf
        else:
            raise AssertionError(f"unexpected opcode {code:#x}")
        pc += 1


def test_seccomp_filter_refuses_other_abis(monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.backends.execenvs import sandbox_bridge as bridge
    monkeypatch.setattr(bridge.platform, "machine", lambda: "x86_64")
    program = bridge._filter(allow_unix=False, allow_userns=False)
    assert program is not None
    x86_64, i386 = 0xC000003E, 0x40000003
    allow, kill = 0x7FFF0000, 0x80000000
    assert _run_bpf(program, 41, x86_64, 1) == 0x00050000 | 1  # socket(AF_UNIX)
    assert _run_bpf(program, 41, x86_64, 2) == allow  # socket(AF_INET)
    assert _run_bpf(program, 0, x86_64) == allow
    # i386 calls (socket is 359, socketcall 102 there) never reach the
    # x86_64 checks: any of them kills the process.
    for nr in (359, 102, 20):
        assert _run_bpf(program, nr, i386, 1) == kill
    assert _run_bpf(program, 41 | 0x40000000, x86_64, 1) == 0x00050000 | 38  # x32


_I386_SOCKET = (
    "import ctypes,mmap;"
    # push rbx; mov eax,359 (i386 socket); mov ebx,1 (AF_UNIX); mov ecx,1;
    # xor edx,edx; int 0x80; pop rbx; ret
    "c=bytes.fromhex('53b867010000bb01000000b90100000031d2cd805bc3');"
    "m=mmap.mmap(-1,4096,prot=7);m.write(c);"
    "a=ctypes.addressof(ctypes.c_char.from_buffer(m));"
    "print('fd',ctypes.CFUNCTYPE(ctypes.c_int)(a)())")


@needs_bwrap
@pytest.mark.skipif(os.uname().machine != "x86_64", reason="an i386 call needs x86_64")
async def test_an_i386_system_call_cannot_open_a_unix_socket(tmp_path: Path,
                                                             home: Path) -> None:
    env = LocalExecEnv(str(tmp_path), sandbox={"enabled": True})
    code, out, _ = await _run(env, f'python3 -c "{_I386_SOCKET}"')
    assert "fd " not in out
    assert code != 0


def test_seccomp_filter_refuses_reaching_into_other_processes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.backends.execenvs import sandbox_bridge as bridge
    monkeypatch.setattr(bridge.platform, "machine", lambda: "x86_64")
    x86_64, eperm = 0xC000003E, 0x00050000 | 1
    # ptrace, process_vm_readv, process_vm_writev, pidfd_getfd: refused even
    # when Unix sockets and user namespaces are allowed.
    for allow in (False, True):
        program = bridge._filter(allow_unix=allow, allow_userns=allow)
        assert program is not None
        for nr in (101, 310, 311, 438):
            assert _run_bpf(program, nr, x86_64) == eperm


# Finds the relay the command was forked from by its command line, whatever
# shells stand between them, then tries to read it and to be traced.
_PROBE_RELAY = """\
import ctypes, os
p = os.getppid()
while p > 1 and b"sandbox_bridge" not in open(f"/proc/{p}/cmdline", "rb").read():
    p = int(open(f"/proc/{p}/stat").read().rpartition(")")[2].split()[1])
assert p > 1
try:
    open(f"/proc/{p}/environ", "rb").read()
    print("read relay")
except OSError as exc:
    print("relay closed", exc.errno)
libc = ctypes.CDLL(None, use_errno=True)
print("traceme", libc.ptrace(0, 0, 0, 0), ctypes.get_errno())
"""


@needs_bwrap
async def test_a_command_cannot_reach_into_the_relay(tmp_path: Path, home: Path) -> None:
    script = tmp_path / "probe.py"
    script.write_text(_PROBE_RELAY)
    for settings in ({"enabled": True},
                     {"enabled": True, "network": {"allowAllUnixSockets": True}}):
        env = LocalExecEnv(str(tmp_path), sandbox=settings)
        code, out, _ = await _run(env, f"python3 {script}")
        assert code == 0, out
        assert "relay closed" in out and "read relay" not in out, out
        assert "traceme -1 1" in out, out  # EPERM


def test_seccomp_filter_refuses_io_uring_with_unix_sockets(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from noeta.backends.execenvs import sandbox_bridge as bridge
    monkeypatch.setattr(bridge.platform, "machine", lambda: "aarch64")
    aarch64, eperm, allow = 0xC00000B7, 0x00050000 | 1, 0x7FFF0000
    strict = bridge._filter(allow_unix=False, allow_userns=True)
    loose = bridge._filter(allow_unix=True, allow_userns=True)
    assert strict is not None and loose is not None
    for nr in (425, 426, 427):  # io_uring_setup, _enter, _register
        assert _run_bpf(strict, nr, aarch64) == eperm
        assert _run_bpf(loose, nr, aarch64) == allow


@pytest.mark.parametrize(("machine", "audit", "nr"),
                         [("x86_64", 0xC000003E, 53), ("aarch64", 0xC00000B7, 199)])
def test_seccomp_filter_refuses_a_unix_socketpair(monkeypatch: pytest.MonkeyPatch,
                                                  machine: str, audit: int, nr: int) -> None:
    from noeta.backends.execenvs import sandbox_bridge as bridge
    monkeypatch.setattr(bridge.platform, "machine", lambda: machine)
    eperm, allow = 0x00050000 | 1, 0x7FFF0000
    strict = bridge._filter(allow_unix=False, allow_userns=False)
    loose = bridge._filter(allow_unix=True, allow_userns=False)
    assert strict is not None and loose is not None
    assert _run_bpf(strict, nr, audit, 1) == eperm  # socketpair(AF_UNIX, ...)
    assert _run_bpf(strict, nr, audit, 2) == allow
    assert _run_bpf(loose, nr, audit, 1) == allow


@needs_bwrap
async def test_a_unix_socketpair_cannot_be_made(tmp_path: Path, home: Path) -> None:
    env = LocalExecEnv(str(tmp_path), sandbox={"enabled": True})
    code, out, _ = await _run(
        env, "python3 -c 'import socket; socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)'")
    assert code != 0 and "Operation not permitted" in out


@needs_bwrap
async def test_io_uring_cannot_be_set_up(tmp_path: Path, home: Path) -> None:
    probe = ("import ctypes;l=ctypes.CDLL(None,use_errno=True);"
             "p=ctypes.create_string_buffer(120);"
             "print('setup',l.syscall(425,1,p),ctypes.get_errno())")
    env = LocalExecEnv(str(tmp_path), sandbox={"enabled": True})
    code, out, _ = await _run(env, f'python3 -c "{probe}"')
    assert code == 0 and out.split()[:3] in (["setup", "-1", "1"], ["setup", "-1", "38"]), out
