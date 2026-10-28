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
    plan = sb.plan_paths(settings, str(tmp_path), str(tmp_path), ())
    profile = sb.seatbelt_profile(settings, plan, (4000, 4001))
    assert profile.startswith("(version 1)\n(allow default)\n(deny file-write*)")
    assert f'(allow file-write* (subpath "{tmp_path}"))' in profile
    assert '(allow network-outbound (remote ip "localhost:4000"))' in profile
    assert '(path-literal "/var/run/x.sock")' in profile
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
    assert (ws / ".git" / "hooks").is_dir()  # made empty and read-only


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
    env = LocalExecEnv(str(tmp_path), inherit_env=["API_TOKEN"], sandbox={
        "enabled": True,
        "credentials": {"envVars": [{"name": "API_TOKEN", "mode": "deny"}]}})
    _, out, _ = await _run(env, f"cat {home}/token; cat {home}/tool-results/out.txt; "
                                   "echo token=${API_TOKEN:-unset}")
    assert "secret" not in out and "spilled" in out and "token=unset" in out


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
