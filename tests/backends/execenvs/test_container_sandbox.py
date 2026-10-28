"""``start_container``'s secure defaults and the sandbox settings mapped onto
a container: the ``docker run`` line (a fake ``docker``, no daemon), and —
with Docker and a suitable image — no network by default, and only the
allowed hosts through the proxy with ``network.allowedDomains``."""

from __future__ import annotations

import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from noeta.backends.execenvs.container import (
    _PROXIES,
    ContainerExecEnv,
    ensure_container_proxy,
    remove_container,
    start_container,
)
from tests.backends.execenvs.test_container import IMAGE


def _fake_docker(tmp_path: Path, gateway: str = "10.9.0.1") -> tuple[str, Path]:
    log = tmp_path / "calls"
    fake = tmp_path / "docker"
    fake.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {log}\n'
        f'case "$*" in "network inspect"*) echo "{gateway} ";; '
        'inspect*) echo "127.0.0.1:41000:41001";; esac\n')
    fake.chmod(0o755)
    return str(fake), log


async def test_secure_defaults(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path)
    await start_container("img", name="box", docker=docker,
                          mounts=[(str(tmp_path), "/workspace")], memory="1g", cpus="2")
    run = log.read_text().splitlines()[0]
    assert "--network none" in run and "--read-only" in run
    assert "--tmpfs /tmp:rw,exec,nosuid" in run and "--tmpfs /workspace" not in run
    assert "--cap-drop ALL" in run and "--cap-add DAC_OVERRIDE" in run
    assert "--cap-add NET_RAW" not in run
    assert "--security-opt no-new-privileges" in run and "--pids-limit 1024" in run
    assert "--memory 1g" in run and "--cpus 2" in run
    log.unlink()
    await start_container("img", name="open", docker=docker, network=None, read_only=False,
                          caps=None, pids_limit=None)
    run = log.read_text().splitlines()[0]
    assert "--network" not in run and "--read-only" not in run and "--cap-drop" not in run
    assert "--tmpfs /workspace:rw,exec,nosuid" not in run


async def test_allowed_domains_map_to_an_internal_network(tmp_path: Path) -> None:
    docker, log = _fake_docker(tmp_path, gateway="127.0.0.1")
    try:
        await start_container("img", name="sbx", docker=docker, sandbox={
            "enabled": True, "network": {"allowedDomains": ["pypi.org"]}})
        calls = log.read_text().splitlines()
        assert calls[0] == "network create --internal sbx-net"
        run = next(c for c in calls if c.startswith("run "))
        assert "--network sbx-net" in run and "--network none" not in run
        assert "--label noeta.network=sbx-net" in run and "-e HTTPS_PROXY" in run
        assert "sbx" in _PROXIES
        # Without allowed hosts a sandbox means no network at all.
        log.unlink()
        await start_container("img", name="closed", docker=docker, sandbox={"enabled": True})
        assert "--network none" in log.read_text()
    finally:
        await remove_container("sbx", docker=docker)
    assert "sbx" not in _PROXIES
    assert "network rm sbx-net" in log.read_text()
    assert await ensure_container_proxy("sbx", {"enabled": True}, docker=docker)
    await remove_container("sbx", docker=docker)


@pytest.fixture
def server() -> Iterator[int]:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"reached-from-container")

        def log_message(self, *args: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1]
    srv.shutdown()


# Speaks HTTP to the proxy with bash alone (the test image may have no curl).
_GET = ('h=${http_proxy#http://}; exec 3<>"/dev/tcp/${h%:*}/${h##*:}" || exit 9; '
        'printf "GET %s HTTP/1.0\\r\\n\\r\\n" "$1" >&3; cat <&3')


@pytest.mark.skipif(IMAGE is None, reason="docker or a suitable image is missing")
async def test_a_sandboxed_container_reaches_only_allowed_hosts(server: int) -> None:
    assert IMAGE is not None
    closed = await start_container(IMAGE)
    try:
        env = ContainerExecEnv(closed)
        out = await env.run(["bash", "-c", "exec 3<>/dev/tcp/1.1.1.1/80 && echo open"],
                            timeout=20)
        assert out.exit_code != 0 and b"open" not in out.stdout
        wrote = await env.run(["sh", "-c", "echo x > /etc/noeta-x"], timeout=20)
        assert wrote.exit_code != 0  # read-only root
        assert (await env.run(["sh", "-c", "echo x > /tmp/x && cat /tmp/x"])).stdout == b"x\n"
    finally:
        await remove_container(closed)
    name = await start_container(IMAGE, sandbox={
        "enabled": True, "network": {"allowedDomains": [f"127.0.0.1:{server}"]}})
    try:
        env = ContainerExecEnv(name)
        ok = await env.run(["bash", "-c", _GET, "bash", f"http://127.0.0.1:{server}/"],
                           timeout=30)
        assert b"reached-from-container" in ok.stdout
        refused = await env.run(["bash", "-c", _GET, "bash", "http://blocked.example/"],
                                timeout=30)
        assert b"403" in refused.stdout
        assert "blocked.example:80" in _PROXIES[name].refused
        direct = await env.run(["bash", "-c", "exec 3<>/dev/tcp/1.1.1.1/80 && echo open"],
                               timeout=20)
        assert b"open" not in direct.stdout
    finally:
        await remove_container(name)
