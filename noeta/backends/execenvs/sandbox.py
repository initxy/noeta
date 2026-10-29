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
  or the paths whose contents run or configure code later (:data:`PROTECTED`,
  submodule git directories, the configured hooks directory).
* **Connect** only through the filtering proxy
  (:mod:`.sandbox_proxy`), to ``network.allowedDomains``; the hosts it
  refused are reported after the command.

On Linux the command runs in ``bwrap`` with its own network, PID and mount
view, behind :mod:`.sandbox_bridge` (loopback relays to the proxy and a
seccomp filter against Unix sockets and user namespaces). On macOS a
generated Seatbelt profile allows outbound connections only to the proxy's
loopback ports. Elsewhere — or with ``bwrap`` missing or unable to create
namespaces — :meth:`LocalSandbox.unavailable` says why. What a command may
touch, and the ``bwrap`` arguments and Seatbelt profile for it, are
:mod:`.sandbox_plan`.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from noeta.backends.execenvs.sandbox_bridge import filter_unsupported
from noeta.backends.execenvs.sandbox_plan import (
    PLACEHOLDERS,
    PROTECTED,
    Plan,
    bwrap_args,
    exclude_overlays,
    plan_paths,
    seatbelt_profile,
)
from noeta.backends.execenvs.sandbox_proxy import FilterProxy, Listeners, default_proxy
from noeta.toolkit.paths import make_tenant_temp_dir, private_dir, temp_root
from noeta.toolkit.sandbox import DEFAULT_DENY_READ, SandboxSettings

__all__ = [
    "DEFAULT_DENY_READ",
    "PLACEHOLDERS",
    "PROTECTED",
    "LocalSandbox",
    "Prepared",
    "bwrap_args",
    "seatbelt_profile",
]

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
    placeholders: list[tuple[str, bool]] = field(default_factory=list)
    remove_after: list[str] = field(default_factory=list)
    _closed: bool = field(default=False, repr=False)

    def violations(self) -> list[str]:
        return list(self.listeners.refused) if self.listeners is not None else []

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _release(self.placeholders)
        _remove_planted(self.remove_after)
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
    rooted at ``root``. With ``settings.tenant_temp_dir`` (a tenant's task)
    the commands keep their ``TMPDIR`` and per-command files there instead
    of in the directory this process's commands share, and noeta's temp
    places (``settings.tenant_temp_places``) are made before each command,
    so one made later cannot appear unhidden while it runs."""

    def __init__(self, settings: SandboxSettings, root: str, *,
                 proxy: FilterProxy | None = None, platform: str | None = None) -> None:
        self.settings = settings
        # Resolved again for each command: a directory on the way may have
        # become a link since.
        self.root = os.path.abspath(root)
        self.proxy = proxy or default_proxy()
        self.platform = platform or sys.platform
        self.temp_dir = settings.tenant_temp_dir
        self._base: str | None = None

    # -------------------------------------------------------------- checks

    def _bwrap(self) -> str | None:
        return self.settings.bwrap_path or shutil.which("bwrap")

    def unavailable(self) -> str | None:
        if self.platform.startswith("linux"):
            unfiltered = filter_unsupported(self.settings.allow_all_unix_sockets,
                                            self.settings.nested_user_namespaces)
            if unfiltered is not None:
                return unfiltered
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
        if self.temp_dir is not None:
            for place in self.settings.tenant_temp_places:
                with contextlib.suppress(OSError):
                    private_dir(place)
            return make_tenant_temp_dir(self.temp_dir)
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
            prepared.remove_after = plan.remove_after
            if self.platform.startswith("linux"):
                prepared.placeholders = _hold(plan)
                await self._linux(prepared, plan, list(argv), cwd, work, writable)
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
                     work: str, writable: Sequence[str]) -> None:
        listeners = await self._listeners(prepared, unix_dir=work)
        relays = [(listeners.http, INNER_HTTP_PORT), (listeners.socks, INNER_SOCKS_PORT)]
        bwrap = self._bwrap()
        assert bwrap is not None
        empty = os.path.join(work, "empty")
        with open(empty, "w"):
            pass
        os.chmod(empty, 0o444)
        overlays: list[tuple[str, str]] = []
        projects = [os.path.realpath(p) for p in (self.root, *writable)]
        for i, (path, text) in enumerate(exclude_overlays(projects, prepared.placeholders)):
            source = os.path.join(work, f"exclude-{i}")
            with open(source, "w", encoding="utf-8", errors="surrogateescape") as f:
                f.write(text)
            overlays.append((source, path))
        prepared.argv = [*bwrap_args(self.settings, plan, bwrap=bwrap, cwd=cwd,
                                     relays=relays, bridge=str(_BRIDGE), empty=empty,
                                     overlays=overlays), *argv]
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



#: Placeholders in use by this process's running commands. One is removed
#: from the host when the last command using it is done — never while
#: another still relies on it: deleting a mount point on the host unmounts
#: it in every sandbox.
_HELD: dict[str, tuple[int, bool]] = {}


def _hold(plan: Plan) -> list[tuple[str, bool]]:
    """The placeholders this command holds: its own, and those another
    running command made that it now sees as existing paths."""
    mine = dict(plan.placeholders)
    for path in (*plan.pinned, *plan.deny_write):
        if path in _HELD:
            mine.setdefault(path, _HELD[path][1])
    for path, is_file in mine.items():
        _HELD[path] = (_HELD.get(path, (0, is_file))[0] + 1, is_file)
    return list(mine.items())


def _release(placeholders: list[tuple[str, bool]]) -> None:
    # Deepest first: a placeholder may sit inside another one.
    for path, is_file in sorted(placeholders, key=lambda p: -len(p[0])):
        count = _HELD.get(path, (1, is_file))[0] - 1
        if count > 0:
            _HELD[path] = (count, is_file)
            continue
        _HELD.pop(path, None)
        # Only what bwrap left: an empty directory or an empty file.
        try:
            if is_file:
                if os.path.isfile(path) and not os.path.islink(path) \
                        and os.path.getsize(path) == 0:
                    os.unlink(path)
            else:
                os.rmdir(path)
        except OSError:
            pass


def _remove_planted(paths: list[str]) -> None:
    """Remove what a command made at a path that was missing and that it
    must not leave behind (a ``.git/commondir``)."""
    for path in paths:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path)
            elif os.path.lexists(path):
                os.unlink(path)
        except OSError:
            pass


_BASE: str | None = None


def _process_base() -> str:
    """This process's sandbox directory (the ``$TMPDIR`` its commands share
    and the per-command proxy sockets), in ``<temp root>/sandbox/``, removed
    when the process exits."""
    global _BASE
    if _BASE is None or not os.path.isdir(_BASE):
        parent = private_dir(os.path.join(temp_root(), "sandbox"),
                             top=os.path.dirname(temp_root()))
        _BASE = tempfile.mkdtemp(prefix=f"w{os.getpid()}-", dir=parent)
        os.makedirs(os.path.join(_BASE, "tmp"), mode=0o700, exist_ok=True)
        atexit.register(shutil.rmtree, _BASE, True)
    return _BASE
