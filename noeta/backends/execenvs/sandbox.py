"""``LocalSandbox``: confine a command on this machine, as Claude Code's
sandbox does — bubblewrap on Linux, Seatbelt (``sandbox-exec``) on macOS.

What a confined command may do:

* **Read** everything except ``filesystem.denyRead`` (plus ``Read(...)``
  deny rules and ``credentials`` entries) and the credential stores of
  :data:`DEFAULT_DENY_READ`; ``filesystem.allowRead`` re-opens a path
  inside one of them.
* **Write** only in the working directory, ``filesystem.allowWrite`` (plus
  ``Edit(...)`` allow rules and the additional working directories), and a
  private temporary directory (``TMPDIR``); never in ``filesystem.denyWrite``
  or the files that change what the agent may do (:data:`PROTECTED`).
* **Connect** only through the filtering proxy
  (:mod:`.sandbox_proxy`), to ``network.allowedDomains``; the hosts it
  refused are reported after the command.

On Linux the command runs in ``bwrap`` with its own network, PID and mount
view, behind :mod:`.sandbox_bridge` (loopback relays to the proxy and a
seccomp filter against Unix sockets and user namespaces). On macOS a
generated Seatbelt profile allows outbound connections only to the proxy's
loopback ports. Elsewhere — or with ``bwrap`` missing or unable to create
namespaces — :meth:`LocalSandbox.unavailable` says why.
"""

from __future__ import annotations

import atexit
import glob
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from noeta.backends.execenvs.sandbox_proxy import FilterProxy, Listeners, default_proxy
from noeta.toolkit.context import noeta_home
from noeta.toolkit.sandbox import SandboxSettings

__all__ = [
    "DEFAULT_DENY_READ",
    "PROTECTED",
    "LocalSandbox",
    "Prepared",
    "bwrap_args",
    "seatbelt_profile",
]

#: Credential stores in the home directory a confined command never reads
#: unless ``allowRead`` names them.
DEFAULT_DENY_READ = (
    "~/.ssh", "~/.gnupg", "~/.aws", "~/.azure", "~/.config/gcloud", "~/.kube",
    "~/.docker/config.json", "~/.config/gh", "~/.netrc", "~/.git-credentials",
    "~/.pypirc", "~/.npmrc", "~/.cargo/credentials", "~/.cargo/credentials.toml",
    "~/.pgpass", "~/.password-store",
)
#: Inside noeta's home (otherwise unreadable: tokens, the task store), what
#: a command may still read: spilled tool output and project memory.
HOME_ALLOW_READ = ("tool-results", "projects")
#: Paths under a writable directory a confined command may not change:
#: noeta's project settings and extensions, and git's hooks and config
#: (code that would later run outside the sandbox).
PROTECTED = (
    ".noeta/settings.json", ".noeta/settings.local.json", ".noeta/mcp.json",
    ".noeta/plugins", ".noeta/agents", ".noeta/skills", ".noeta/prompts",
    ".noeta/rules", ".noeta/workflows", ".git/hooks", ".git/config",
)
#: Loopback ports the proxies listen on inside a Linux sandbox.
INNER_HTTP_PORT, INNER_SOCKS_PORT = 3128, 1080
_BRIDGE = Path(__file__).with_name("sandbox_bridge.py")


@dataclass
class Prepared:
    argv: list[str]
    env: dict[str, str]
    proxy: FilterProxy | None = None
    listeners: Listeners | None = None
    workdir: str | None = None
    _closed: bool = field(default=False, repr=False)

    def violations(self) -> list[str]:
        return list(self.listeners.refused) if self.listeners is not None else []

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.proxy is not None and self.listeners is not None:
            await self.proxy.close(self.listeners)
        if self.workdir is not None:
            shutil.rmtree(self.workdir, ignore_errors=True)


def _proxy_env(http: str, socks: str) -> dict[str, str]:
    env = {"NO_PROXY": "localhost,127.0.0.1,::1", "no_proxy": "localhost,127.0.0.1,::1"}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env[name] = http
    for name in ("ALL_PROXY", "all_proxy"):
        env[name] = socks
    return env


def _expand(settings: SandboxSettings, paths: Sequence[str], base: str) -> list[str]:
    out: list[str] = []
    for raw in paths:
        path = settings.resolve(raw, base)
        if any(c in path for c in "*?["):
            out += sorted(glob.glob(path, recursive=True))
        else:
            out.append(path)
    # bwrap mounts on the real path; a symlinked home would not resolve.
    return list(dict.fromkeys(os.path.realpath(p) for p in out))


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


@dataclass(frozen=True)
class Plan:
    """What one command may touch, as absolute paths."""

    writable: list[str]
    deny_write: list[str]
    deny_read: list[str]
    allow_read: list[str]


def plan_paths(settings: SandboxSettings, root: str, tmpdir: str,
               writable: Sequence[str] = ()) -> Plan:
    """The paths of :class:`Plan` for a command whose working directory is
    ``root``."""
    base = root
    writes = [root, *_expand(settings, settings.allow_write, base), tmpdir, *writable]
    writes = [w for w in dict.fromkeys(os.path.realpath(w) for w in writes) if os.path.exists(w)]
    deny_write = [os.path.realpath(p) for p in _expand(settings, settings.deny_write, base)]
    for w in writes:
        deny_write += [os.path.join(w, p) for p in PROTECTED]
    home = str(noeta_home())
    deny_read = _expand(settings, [*DEFAULT_DENY_READ, home, *settings.deny_read], base)
    allow_read = _expand(settings, [*(os.path.join(home, p) for p in HOME_ALLOW_READ),
                                    *settings.allow_read], base)
    # A denied directory holding a writable one would hide it.
    deny_read = [d for d in deny_read if not any(_within(w, d) for w in writes)]
    return Plan(writable=writes, deny_write=list(dict.fromkeys(deny_write)),
                deny_read=deny_read, allow_read=allow_read)


def bwrap_args(settings: SandboxSettings, plan: Plan, *, bwrap: str, cwd: str,
               relays: Sequence[tuple[str, int]], bridge: str) -> list[str]:
    """The ``bwrap`` command line for ``plan``; the command follows it."""
    args = [bwrap, "--die-with-parent", "--new-session", "--unshare-pid", "--unshare-net",
            "--unshare-ipc", "--unshare-uts"]
    if settings.filesystem_disabled:
        args += ["--bind", "/", "/"]
    else:
        args += ["--ro-bind", "/", "/"]
    args += ["--dev", "/dev"]
    args += ["--ro-bind", "/proc", "/proc"] if settings.weaker_nested else ["--proc", "/proc"]
    if not settings.filesystem_disabled:
        for path in plan.writable:
            args += ["--bind", path, path]
        hidden = [p for p in plan.deny_read if os.path.isdir(p)]
        for path in plan.deny_read:
            if path in hidden:
                args += ["--tmpfs", path]
            elif os.path.exists(path):
                args += ["--ro-bind", "/dev/null", path]
        # Re-opened paths are mounted while the empty directories over the
        # denied ones are still writable (their mount points), then those
        # turn read-only.
        for path in plan.allow_read:
            if os.path.exists(path) and any(_within(path, d) for d in hidden):
                args += ["--ro-bind", path, path]
        for path in hidden:
            args += ["--remount-ro", path]
        for path in plan.deny_write:
            if os.path.exists(path):
                args += ["--ro-bind", path, path]
            elif path.endswith("/.git/hooks") and os.path.isdir(os.path.dirname(path)):
                # An empty read-only hooks directory: nothing can plant one.
                args += ["--tmpfs", path, "--remount-ro", path]
    for path, _port in relays:
        args += ["--ro-bind", path, path]
    args += ["--ro-bind", bridge, bridge]
    for name in settings.deny_env:
        args += ["--unsetenv", name]
    args += ["--chdir", cwd, "--"]
    inner = [sys.executable, "-I", "-S", bridge]
    for kind, (path, port) in zip(("--http", "--socks"), relays, strict=False):
        inner += [kind, path, str(port)]
    if settings.allow_all_unix_sockets:
        inner.append("--allow-unix")
    if settings.nested_user_namespaces:
        inner.append("--allow-userns")
    return [*args, *inner, "--"]


def _sb_quote(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(settings: SandboxSettings, plan: Plan,
                     proxy_ports: Sequence[int]) -> str:
    """The macOS Seatbelt profile for ``plan``: later rules win."""
    lines = ["(version 1)", "(allow default)"]
    if not settings.filesystem_disabled:
        lines.append("(deny file-write*)")
        lines += [f"(allow file-write* (subpath {_sb_quote(os.path.realpath(p))}))"
                  for p in plan.writable]
        lines.append('(allow file-write* (literal "/dev/null") (literal "/dev/zero") '
                     '(regex #"^/dev/tty") (regex #"^/dev/fd/"))')
        lines += [f"(deny file-read* (subpath {_sb_quote(os.path.realpath(p))}))"
                  for p in plan.deny_read]
        lines += [f"(allow file-read* (subpath {_sb_quote(os.path.realpath(p))}))"
                  for p in plan.allow_read if any(_within(p, d) for d in plan.deny_read)]
        lines += [f"(deny file-write* (subpath {_sb_quote(os.path.realpath(p))}))"
                  for p in plan.deny_write]
    lines.append("(deny network*)")
    lines += [f'(allow network-outbound (remote ip "localhost:{port}"))'
              for port in proxy_ports]
    if settings.allow_local_binding:
        lines.append('(allow network-bind (local ip "localhost:*"))')
        lines.append('(allow network-inbound (local ip "localhost:*"))')
        lines.append('(allow network-outbound (remote ip "localhost:*"))')
    if settings.allow_all_unix_sockets:
        lines.append("(allow network* (remote unix-socket))")
    else:
        lines += [f"(allow network* (remote unix-socket (path-literal {_sb_quote(p)})))"
                  for p in settings.allow_unix_sockets]
    return "\n".join(lines) + "\n"


_PROBES: dict[str, str | None] = {}


def _probe_bwrap(bwrap: str) -> str | None:
    """Why ``bwrap`` cannot create the sandbox's namespaces here, or
    ``None`` when it can. Checked once per binary."""
    if bwrap in _PROBES:
        return _PROBES[bwrap]
    try:
        done = subprocess.run(
            [bwrap, "--die-with-parent", "--unshare-pid", "--unshare-net", "--ro-bind", "/", "/",
             "--dev", "/dev", "--proc", "/proc", "true"],
            capture_output=True, timeout=15, check=False)
        reason = None if done.returncode == 0 else (
            "bwrap cannot create the sandbox here: "
            + (done.stderr.decode("utf-8", "replace").strip() or f"exit {done.returncode}"))
    except (OSError, subprocess.TimeoutExpired) as exc:
        reason = f"bwrap cannot run: {exc}"
    _PROBES[bwrap] = reason
    return reason


class LocalSandbox:
    """The sandbox of a :class:`~noeta.backends.execenvs.local.LocalExecEnv`
    rooted at ``root``."""

    def __init__(self, settings: SandboxSettings, root: str, *,
                 proxy: FilterProxy | None = None, platform: str | None = None) -> None:
        self.settings = settings
        self.root = os.path.realpath(root)
        self.proxy = proxy or default_proxy()
        self.platform = platform or sys.platform
        self._base: str | None = None

    # -------------------------------------------------------------- checks

    def _bwrap(self) -> str | None:
        return self.settings.bwrap_path or shutil.which("bwrap")

    def unavailable(self) -> str | None:
        if self.platform.startswith("linux"):
            bwrap = self._bwrap()
            if bwrap is None:
                return ("bubblewrap (bwrap) is not installed; install it (apt install "
                        "bubblewrap, dnf install bubblewrap) to run commands in the sandbox")
            return _probe_bwrap(bwrap)
        if self.platform == "darwin":
            if not os.path.exists("/usr/bin/sandbox-exec"):
                return "sandbox-exec is missing on this Mac"
            return None
        return f"the sandbox is not supported on {self.platform}"

    # -------------------------------------------------------------- setup

    def _base_dir(self) -> str:
        if self._base is None or not os.path.isdir(self._base):
            self._base = _process_base()
        return self._base

    def tmpdir(self) -> str:
        """The confined commands' private temporary directory."""
        return os.path.join(self._base_dir(), "tmp")

    async def prepare(self, argv: Sequence[str], *, cwd: str,
                      writable: Sequence[str] = ()) -> Prepared:
        reason = self.unavailable()
        if reason is not None:
            raise RuntimeError(reason)
        base = self._base_dir()
        work = os.path.join(base, uuid.uuid4().hex[:12])
        os.makedirs(work, mode=0o700)
        prepared = Prepared(argv=[], env={"TMPDIR": self.tmpdir()}, workdir=work)
        try:
            plan = plan_paths(self.settings, self.root, self.tmpdir(), writable)
            if self.platform.startswith("linux"):
                await self._linux(prepared, plan, list(argv), cwd, work)
            else:
                await self._darwin(prepared, plan, list(argv))
        except BaseException:
            await prepared.close()
            raise
        return prepared

    async def _listeners(self, prepared: Prepared, **where: object) -> Listeners:
        s = self.settings
        listeners = await self.proxy.open(
            allowed=s.allowed_domains, denied=s.denied_domains,
            upstream_http=s.http_proxy_port, upstream_socks=s.socks_proxy_port,
            **where)  # type: ignore[arg-type]
        prepared.proxy, prepared.listeners = self.proxy, listeners
        return listeners

    async def _linux(self, prepared: Prepared, plan: Plan, argv: list[str], cwd: str,
                     work: str) -> None:
        listeners = await self._listeners(prepared, unix_dir=work)
        relays = [(listeners.http, INNER_HTTP_PORT), (listeners.socks, INNER_SOCKS_PORT)]
        bwrap = self._bwrap()
        assert bwrap is not None
        prepared.argv = [*bwrap_args(self.settings, plan, bwrap=bwrap, cwd=cwd,
                                     relays=relays, bridge=str(_BRIDGE)), *argv]
        prepared.env.update(_proxy_env(f"http://127.0.0.1:{INNER_HTTP_PORT}",
                                       f"socks5h://127.0.0.1:{INNER_SOCKS_PORT}"))

    async def _darwin(self, prepared: Prepared, plan: Plan, argv: list[str]) -> None:
        listeners = await self._listeners(prepared, tcp_host="127.0.0.1")
        http_port, socks_port = listeners.http[1], listeners.socks[1]
        profile = seatbelt_profile(self.settings, plan, (http_port, socks_port))
        unset: list[str] = []
        for name in self.settings.deny_env:
            unset += ["-u", name]
        prepared.argv = ["/usr/bin/sandbox-exec", "-p", profile,
                         *(["/usr/bin/env", *unset] if unset else []), *argv]
        prepared.env.update(_proxy_env(f"http://127.0.0.1:{http_port}",
                                       f"socks5h://127.0.0.1:{socks_port}"))



_BASE: str | None = None


def _process_base() -> str:
    """This process's sandbox directory (the shared ``$TMPDIR`` and the
    per-command proxy sockets), removed when the process exits."""
    global _BASE
    if _BASE is None or not os.path.isdir(_BASE):
        _BASE = tempfile.mkdtemp(prefix="noeta-sandbox-")
        os.makedirs(os.path.join(_BASE, "tmp"), mode=0o700, exist_ok=True)
        atexit.register(shutil.rmtree, _BASE, True)
    return _BASE
