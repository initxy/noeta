"""``noeta web``: the web app against a local server in the background.

    noeta web [--port N] [--no-open]   start ``noeta serve`` on 127.0.0.1 (or reuse the
                                       one running) and open the web app in the browser
    noeta web status                   whether it runs, where, its log
    noeta web stop                     stop it

The server is ``noeta serve --host 127.0.0.1 --allow-root <home>`` with the web app
noeta carries, detached into its own session, its output in ``~/.noeta/logs/web.log``.
Its own bearer token reaches it through ``NOETA_SERVER_TOKEN`` (never the command
line); the token, pid and port are kept in ``~/.noeta/web.json`` (owner only), and the
browser gets the token in the address's fragment, which the page takes off."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from noeta.cli import home_dir

#: The port ``noeta web`` takes when it is free (``noeta serve``'s default).
DEFAULT_PORT = 8642
HOST = "127.0.0.1"
#: How long a new server has to answer before ``noeta web`` gives up on it.
START_SECONDS = 60.0
#: How long a stopped server has to exit before it is killed.
STOP_SECONDS = 10.0
#: The global flags a new server is started with when ``noeta web`` is given them.
PASSED_FLAGS = ("model", "provider", "base_url", "store", "effort")

NO_WEB_APP = ("noeta web: this noeta has no web app built in (a source checkout without "
              "the built web/); run make web-bundle in the checkout, or install noeta from PyPI")


def add_parser(sub: Any) -> None:
    w = sub.add_parser("web", help="open the web app on a local server in the background")
    w.add_argument("action", nargs="?", choices=("status", "stop"),
                   help="status: whether the server runs; stop: stop it")
    w.add_argument("--port", type=int, default=None,
                   help=f"the port of a new server (default {DEFAULT_PORT}, else a free one)")
    w.add_argument("--no-open", action="store_true", help="print the address, open no browser")


@dataclass
class Server:
    """A ``noeta web`` server, as ``~/.noeta/web.json`` keeps it."""

    pid: int
    port: int
    token: str
    log: str
    cwd: str = ""

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/"

    @property
    def app_url(self) -> str:
        return f"{self.url}#token={self.token}"


def _state_file() -> Path:
    return home_dir.HOME / "web.json"


def _log_file() -> Path:
    return home_dir.HOME / "logs" / "web.log"


def _alive(pid: int) -> bool:
    # A child of this process that exited stays a zombie until reaped.
    with contextlib.suppress(ChildProcessError, OSError):
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def answers(server: Server, timeout: float = 2.0) -> bool:
    """Whether the server's API answers its token."""
    import httpx

    from noeta.toolkit.http import DIRECT_LOOPBACK

    try:
        with httpx.Client(timeout=timeout, mounts=DIRECT_LOOPBACK) as client:
            resp = client.get(f"{server.url}agents",
                              headers={"authorization": f"Bearer {server.token}"})
    except httpx.HTTPError:
        return False
    return resp.status_code == 200


def _forget() -> None:
    with contextlib.suppress(FileNotFoundError):
        _state_file().unlink()


def running() -> Server | None:
    """The ``noeta web`` server whose process is alive, if any; a record of
    one that died is removed."""
    try:
        data = json.loads(_state_file().read_text("utf-8"))
        server = Server(**{k: data[k] for k in ("pid", "port", "token", "log")},
                        cwd=str(data.get("cwd", "")))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, KeyError, TypeError):
        _forget()
        return None
    if not _alive(int(server.pid)):
        _forget()
        return None
    return server


def _keep(server: Server) -> None:
    home_dir.ensure_home()
    path = _state_file()
    fresh = path.with_name(f".web-{os.getpid()}.json")
    with contextlib.suppress(FileNotFoundError):
        fresh.unlink()
    fd = os.open(fresh, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(asdict(server), fh)
    os.replace(fresh, path)


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((HOST, port))
        except OSError:
            return False
    return True


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        port: int = s.getsockname()[1]
    return port


def _ours(server: Server) -> bool:
    """Whether ``server.pid`` is still the server ``noeta web`` started (a
    pid can be reused): its API answers the token, or its command line is
    ``noeta serve``'s."""
    if answers(server):
        return True
    try:
        out = subprocess.run(["ps", "-o", "args=", "-p", str(server.pid)], capture_output=True,
                             text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "noeta.cli.main serve" in out


def _show(server: Server, open_browser: bool) -> None:
    if open_browser:
        with contextlib.suppress(webbrowser.Error):
            webbrowser.open(server.app_url)


def _log_tail(path: str, lines: int = 15) -> str:
    try:
        text = Path(path).read_text("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.rstrip().splitlines()[-lines:])


def _serve_command(port: int, web_dir: str, flags: dict[str, str]) -> list[str]:
    cmd = [sys.executable, "-m", "noeta.cli.main"]
    for name, value in flags.items():
        cmd += [f"--{name.replace('_', '-')}", value]
    return [*cmd, "serve", "--host", HOST, "--port", str(port),
            "--allow-root", str(Path.home()), "--web", web_dir]


def start(*, port: int | None, open_browser: bool, cwd: str,
          flags: dict[str, str] | None = None) -> int:
    """Start the server (or reuse the running one), wait until it answers,
    print its address and open it. The exit code."""
    from noeta.host.http import bundled_web_dir

    server = running()
    if server is not None:
        if not _wait(server):
            print(f"noeta web: the server (pid {server.pid}) does not answer; see "
                  f"{server.log}, or noeta web stop and start again", file=sys.stderr)
            return 1
        print(f"noeta web is already running at {server.app_url}")
        _show(server, open_browser)
        return 0
    web_dir = bundled_web_dir()
    if web_dir is None:
        print(NO_WEB_APP, file=sys.stderr)
        return 1
    import uvicorn  # noqa: F401 - the server extra, named by main() when missing

    if port is None:
        port = DEFAULT_PORT if _port_free(DEFAULT_PORT) else _free_port()
    elif not _port_free(port):
        print(f"noeta web: port {port} is in use; pick another with --port", file=sys.stderr)
        return 1
    token = secrets.token_urlsafe(32)
    log = _log_file()
    home_dir.ensure_home()
    log.parent.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        child = subprocess.Popen(
            _serve_command(port, web_dir, flags or {}), cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=out, stderr=subprocess.STDOUT, start_new_session=True,
            env={**os.environ, "NOETA_SERVER_TOKEN": token})
    server = Server(pid=child.pid, port=port, token=token, log=str(log), cwd=cwd)
    _keep(server)
    if not _wait(server, child):
        if child.poll() is None:
            _terminate(child.pid)
        _forget()
        tail = _log_tail(str(log))
        print(f"noeta web: the server did not start; the end of its log ({log}):"
              + (f"\n{tail}" if tail else " (empty)"), file=sys.stderr)
        return 1
    print(f"noeta web is running at {server.app_url}")
    print("Stop it with: noeta web stop")
    _show(server, open_browser)
    return 0


def _wait(server: Server, child: subprocess.Popen[bytes] | None = None) -> bool:
    """Whether the server answers within :data:`START_SECONDS` (``child``:
    the process just started, given up on when it exits)."""
    deadline = time.monotonic() + START_SECONDS
    while not answers(server, timeout=1.0):
        exited = child.poll() is not None if child is not None else not _alive(server.pid)
        if exited or time.monotonic() > deadline:
            return False
        time.sleep(0.2)
    return True


def _terminate(pid: int) -> None:
    """SIGTERM ``pid``'s process group (the server and what it started),
    then SIGKILL when it has not exited in :data:`STOP_SECONDS`."""
    def send(sig: int) -> None:
        try:
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)

    send(signal.SIGTERM)
    deadline = time.monotonic() + STOP_SECONDS
    while _alive(pid):
        if time.monotonic() > deadline:
            send(signal.SIGKILL)
            deadline = time.monotonic() + STOP_SECONDS
        time.sleep(0.1)


def stop(quiet: bool = False) -> Server | None:
    """Stop the running server; the one stopped, if any."""
    server = running()
    if server is None:
        if not quiet:
            print("noeta web is not running")
        return None
    if _ours(server):
        _terminate(server.pid)
    _forget()
    if not quiet:
        print("noeta web stopped")
    return server


def status() -> int:
    server = running()
    if server is None:
        print("noeta web is not running")
        return 1
    state = "running" if answers(server) else "starting or not answering"
    print(f"noeta web is {state} at {server.url} (pid {server.pid}, log {server.log})")
    return 0


def run(args: argparse.Namespace, cwd: str) -> int:
    if args.action == "stop":
        stop()
        return 0
    if args.action == "status":
        return status()
    flags = {k: str(v) for k in PASSED_FLAGS if (v := getattr(args, k, None))}
    return start(port=args.port, open_browser=not args.no_open, cwd=cwd, flags=flags)
