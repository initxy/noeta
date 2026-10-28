"""``ContainerExecEnv``: a running Docker container as an ``ExecEnv``.

Every operation is one ``docker exec`` against a container the host started
(``start_container`` is a small helper for that; provisioning policy —
naming, mounts, reuse, idle reclamation — stays the host's). Tools run inside
the container and see only what is mounted into it; the host's environment
never reaches a command (``docker exec`` starts from the container's own).

What the container needs: ``sh``, GNU coreutils (``cat``, ``head``,
``tail``, ``stat``, ``realpath``, ``mktemp``) and util-linux ``setsid`` —
every Debian/Ubuntu-based image has them, busybox/alpine does not — plus
``bash`` for the ``Bash`` tool.

Mechanics, and the pitfalls they avoid:

* **Killing the command, not just the client.** Signalling the local
  ``docker exec`` client does not stop the process in the container. Each
  command therefore runs as ``setsid -w sh -c '<record pid>; "$@"'``, its own
  process group with its pid in a per-run file; on timeout or cancellation
  the env signals that group inside the container (SIGTERM, a grace period,
  then SIGKILL), which also reaps backgrounded grandchildren. The local client
  then exits by itself, with whatever the command printed.
* **Bytes, not text.** Without a TTY, ``docker exec``'s stdout is a raw byte
  stream, so files are read with ``cat`` / ``tail | head`` and written by
  piping into ``cat`` with no encoding step. (0.6's HTTP sandbox had to
  base64 through a text endpoint and wrap its guards in a subshell so a bare
  ``exit`` could not kill its persistent shell; a fresh ``docker exec`` per
  operation has no persistent shell to wedge.)
* **The same errors as the local env.** A shell guard runs before each
  read and maps "missing", "unreadable" and "a directory" to distinct exit
  codes, raised as ``FileNotFoundError`` / ``PermissionError`` /
  ``IsADirectoryError``. A write goes through a temp file in the target's
  directory and a rename, keeps an existing file's mode and owner, and
  creates parents.
* **Bounded output.** Captured streams are bounded while they are read
  (``output_cap`` keeps the tail), as on the host.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import posixpath
import tempfile
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import msgspec

from noeta.backends.execenvs.local import KILL_GRACE_S, LocalExecEnv
from noeta.backends.execenvs.sandbox_proxy import Listeners, default_proxy
from noeta.core.execenv import FileStat, RunResult
from noeta.toolkit.local_env import ENV_ALLOWLIST
from noeta.toolkit.sandbox import SandboxSettings

__all__ = [
    "DEFAULT_CAPS",
    "DEFAULT_PIDS_LIMIT",
    "DOCKER_ENV",
    "ContainerError",
    "ContainerExecEnv",
    "container_state",
    "ensure_container_proxy",
    "remove_container",
    "start_container",
    "stop_container",
]

#: Host variables the ``docker`` CLI itself may need (which daemon, which
#: config). They reach the CLI only, never a command in the container.
DOCKER_ENV = ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_CERT_PATH",
              "DOCKER_TLS_VERIFY")
#: Variables the ``docker`` client's own environment carries; a command's
#: value for one of these is passed as ``-e KEY=VALUE`` instead.
_CLIENT_ENV = frozenset({*ENV_ALLOWLIST, *DOCKER_ENV})

#: Budget for one file operation (a ``docker exec`` of ``cat``, ``stat``…).
FILE_OP_TIMEOUT_S = 120.0
#: How long to wait for the client to exit after the group was killed.
_DRAIN_S = 5.0

_NOT_FOUND, _NO_PERM, _IS_DIR = 3, 4, 5

# Record the group leader's pid, run the command, clean up. ``$1`` is the
# pid file; the command is the rest. Not ``exec``: the shell stays to remove the
# file, and a missing program exits 127 as through any shell.
_WRAP = 'f=$1; shift; echo $$ > "$f"; "$@"; s=$?; rm -f -- "$f"; exit $s'

# Signal the group named in the pid file. The file may not exist yet when a
# cancel lands right after the start, so look for it a little while.
_KILL = ('i=0; while [ ! -s "$1" ] && [ $i -lt 20 ]; do sleep 0.05; i=$((i+1)); done; '
         '[ -s "$1" ] || exit 0; kill -s "$2" -- "-$(cat -- "$1")" 2>/dev/null; '
         '[ "$2" = KILL ] && rm -f -- "$1"; exit 0')

_GUARD = (f'[ -e "$1" ] || exit {_NOT_FOUND}; [ -d "$1" ] && exit {_IS_DIR}; '
          f'[ -r "$1" ] || exit {_NO_PERM}; ')

_READ = _GUARD + 'exec cat -- "$1"'
_READ_RANGE = _GUARD + 'tail -c "+$(($2 + 1))" -- "$1" | head -c "$3"'

_WRITE = (
    't=$(realpath -m -- "$1") || exit 1; '
    f'[ -d "$t" ] && exit {_IS_DIR}; '
    'd=$(dirname -- "$t"); mkdir -p -- "$d" || exit 1; '
    'tmp=$(mktemp "$d/.noeta-XXXXXX") || exit 1; '
    'if ! cat > "$tmp"; then rm -f -- "$tmp"; exit 1; fi; '
    'if [ -e "$t" ]; then chmod --reference="$t" -- "$tmp"; '
    'chown --reference="$t" -- "$tmp" 2>/dev/null; '
    'else chmod "$(printf %o $((0666 & ~0$(umask))))" -- "$tmp"; fi; '
    'mv -f -- "$tmp" "$t" || { rm -f -- "$tmp"; exit 1; }'
)

_STAT = (
    'if [ -L "$1" ]; then l=1; else l=0; fi; '
    'if [ -e "$1" ]; then stat -L -c "%s %.9Y %F" -- "$1" || exit 1; '
    'elif [ $l = 1 ]; then stat -c "%s %.9Y %F" -- "$1" || exit 1; '
    f'else exit {_NOT_FOUND}; fi; echo $l'
)


class ContainerError(OSError):
    """``docker`` itself failed (no daemon, no such container)."""


class ContainerExecEnv:
    """An ``ExecEnv`` over ``docker exec`` into a running container.

    ``container``: its name or id. ``cwd``: the workspace directory inside
    it, which relative paths resolve against (default ``/workspace``).
    ``user``: run as this container user (``docker exec -u``); default the
    image's. ``env``: variables set on every command (laid under a call's
    own ``env``). ``docker``: the CLI to run. ``kill_grace``: the SIGTERM ->
    SIGKILL window.
    """

    def __init__(
        self,
        container: str,
        *,
        cwd: str = "/workspace",
        user: str | None = None,
        env: Mapping[str, str] | None = None,
        docker: str = "docker",
        kill_grace: float = KILL_GRACE_S,
    ) -> None:
        self.local = False
        if not posixpath.isabs(cwd):
            raise ValueError(f"cwd must be an absolute path inside the container: {cwd!r}")
        self.container = container
        self.cwd = posixpath.normpath(cwd)
        self.user = user
        self.env = dict(env or {})
        self.docker = docker
        self.kill_grace = kill_grace
        self._host = LocalExecEnv(tempfile.gettempdir(), inherit_env=DOCKER_ENV,
                                  kill_grace=kill_grace)

    def _abs(self, path: str) -> str:
        return path if posixpath.isabs(path) else posixpath.join(self.cwd, path)

    def _exec(self, argv: Sequence[str], *, cwd: str | None = None,
              env: Mapping[str, str] | None = None, stdin: bool = False
              ) -> tuple[list[str], dict[str, str]]:
        """The ``docker exec`` argv and the variables the ``docker`` client
        must carry: each variable is named with ``-e KEY`` and its value
        travels in the client's environment, never on its command line
        (where ``ps`` shows it to every user)."""
        cmd = [self.docker, "exec"]
        if stdin:
            cmd.append("-i")
        if self.user:
            cmd += ["-u", self.user]
        cmd += ["-w", self._abs(cwd) if cwd is not None else self.cwd]
        values: dict[str, str] = {}
        for key, value in {**self.env, **(env or {})}.items():
            if key in _CLIENT_ENV:
                # The client itself reads these (PATH finds ``docker``,
                # DOCKER_HOST picks the daemon): set them in the container
                # only. Not secrets.
                cmd += ["-e", f"{key}={value}"]
            else:
                cmd += ["-e", key]
                values[key] = value
        return [*cmd, self.container, *argv], values

    # -- processes ---------------------------------------------------------

    async def run(
        self,
        argv: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
        output_cap: int | None = None,
    ) -> RunResult:
        if not argv:
            raise ValueError("argv must not be empty")
        pidfile = f"/tmp/.noeta-run-{uuid.uuid4().hex}.pid"
        cmd, values = self._exec(["setsid", "-w", "sh", "-c", _WRAP, "sh", pidfile, *argv],
                                 cwd=cwd, env=env, stdin=stdin is not None)
        task = asyncio.ensure_future(self._host.run(cmd, env=values, stdin=stdin,
                                                    output_cap=output_cap))
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout)
        except TimeoutError:
            await self._stop(pidfile, task)
            result = await self._settle(task)
            return msgspec.structs.replace(result, exit_code=-1, timed_out=True)
        except BaseException:
            # Cancelled: the command must not outlive the call, even if this
            # coroutine is cancelled again while the kill is under way.
            stopper = asyncio.ensure_future(self._stop(pidfile, task))
            with contextlib.suppress(BaseException):
                await asyncio.shield(stopper)
            task.cancel()
            raise
        self._raise_docker_failure(result)
        return result

    async def _stop(self, pidfile: str, task: asyncio.Future[RunResult]) -> None:
        """SIGTERM the command's group in the container, give it the grace
        period, then SIGKILL it."""
        await self._signal(pidfile, "TERM")
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), self.kill_grace)
        await self._signal(pidfile, "KILL")

    async def _signal(self, pidfile: str, sig: str) -> None:
        with contextlib.suppress(OSError):
            cmd, values = self._exec(["sh", "-c", _KILL, "sh", pidfile, sig])
            await self._host.run(cmd, env=values, timeout=FILE_OP_TIMEOUT_S)

    async def _settle(self, task: asyncio.Future[RunResult]) -> RunResult:
        try:
            return await asyncio.wait_for(asyncio.shield(task), _DRAIN_S)
        except TimeoutError:
            # The client is stuck (a process escaped the group and holds the
            # pipe): give up on its output.
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            return RunResult(exit_code=-1, stdout=b"", timed_out=True)

    def _raise_docker_failure(self, result: RunResult) -> None:
        """``docker exec`` itself failed: no daemon or no such container
        (exit 1 or 125), or the process could not start, e.g. a missing
        working directory (126/127 with an OCI message)."""
        if result.exit_code not in (1, 125, 126, 127):
            return
        # The OCI message arrives on stdout, the daemon's on stderr.
        err = (result.stderr or result.stdout[:4096]).decode("utf-8", errors="replace").strip()
        if result.exit_code in (1, 125, 126, 127) and err.startswith(
                ("Error response from daemon", "OCI runtime", "Cannot connect to the Docker")):
            if "chdir" in err or "no such file or directory" in err.lower():
                raise FileNotFoundError(errno.ENOENT, err)
            raise ContainerError(err or f"docker exec failed ({result.exit_code})")

    # -- files -------------------------------------------------------------

    async def _sh(self, script: str, *args: str, stdin: bytes | None = None) -> RunResult:
        cmd, values = self._exec(["sh", "-c", script, "sh", *args], stdin=stdin is not None)
        result = await self._host.run(cmd, env=values, stdin=stdin, timeout=FILE_OP_TIMEOUT_S)
        if result.timed_out:
            raise ContainerError(f"docker exec timed out after {FILE_OP_TIMEOUT_S:.0f}s")
        self._raise_docker_failure(result)
        return result

    @staticmethod
    def _fail(result: RunResult, path: str) -> OSError:
        err = result.stderr.decode("utf-8", errors="replace").strip()
        code = result.exit_code
        if code == _NOT_FOUND:
            return FileNotFoundError(errno.ENOENT, "No such file or directory", path)
        if code == _NO_PERM or "Permission denied" in err:
            return PermissionError(errno.EACCES, "Permission denied", path)
        if code == _IS_DIR:
            return IsADirectoryError(errno.EISDIR, "Is a directory", path)
        return OSError(errno.EIO, err or f"container command failed ({code})", path)

    async def read_bytes(self, path: str) -> bytes:
        target = self._abs(path)
        result = await self._sh(_READ, target)
        if result.exit_code != 0:
            raise self._fail(result, target)
        return result.stdout

    async def read_range(self, path: str, offset: int, length: int) -> bytes:
        target = self._abs(path)
        if length <= 0:
            if await self.stat(target) is None:
                raise FileNotFoundError(errno.ENOENT, "No such file or directory", target)
            return b""
        result = await self._sh(_READ_RANGE, target, str(max(0, offset)), str(length))
        if result.exit_code != 0:
            raise self._fail(result, target)
        return result.stdout

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = self._abs(path)
        result = await self._sh(_WRITE, target, stdin=data)
        if result.exit_code != 0:
            raise self._fail(result, target)

    async def stat(self, path: str) -> FileStat | None:
        target = self._abs(path)
        result = await self._sh(_STAT, target)
        if result.exit_code == _NOT_FOUND:
            return None
        if result.exit_code != 0:
            err = result.stderr.decode("utf-8", errors="replace")
            if "Not a directory" in err or "No such file" in err:
                return None
            raise self._fail(result, target)
        first, _, link = result.stdout.decode("utf-8", errors="replace").strip() \
            .rpartition("\n")
        size, mtime, kind = first.split(" ", 2)
        return FileStat(size=int(size), mtime=float(mtime), is_dir=kind == "directory",
                        is_symlink=link.strip() == "1")

    async def realpath(self, path: str) -> str:
        target = self._abs(path)
        result = await self._sh('realpath -m -- "$1"', target)
        if result.exit_code != 0:
            raise self._fail(result, target)
        return result.stdout.decode("utf-8", errors="surrogateescape").rstrip("\n")


# ---------------------------------------------------------------------------
# Provisioning helpers (the host decides when to call them)
# ---------------------------------------------------------------------------


async def _docker(docker: str, *args: str, timeout: float = FILE_OP_TIMEOUT_S,
                  env: Mapping[str, str] | None = None) -> RunResult:
    host = LocalExecEnv(tempfile.gettempdir(), inherit_env=DOCKER_ENV)
    try:
        return await host.run([docker, *args], timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise ContainerError(f"{docker!r} not found: {exc}") from exc


#: Capabilities a container keeps by default: Docker's default set without
#: ``MKNOD``, ``NET_RAW``, ``SETFCAP``, ``SETPCAP``, ``SYS_CHROOT`` and
#: ``AUDIT_WRITE`` — enough for root to own and write the mounted workspace.
DEFAULT_CAPS = ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "KILL", "SETGID", "SETUID",
                "NET_BIND_SERVICE")
#: Default ``--pids-limit``: a fork bomb stops here.
DEFAULT_PIDS_LIMIT = 1024
#: Writable scratch on a read-only root (``--tmpfs``), executable.
_TMPFS = ("/tmp", "/var/tmp", "/run", "/root")
#: Docker labels a sandboxed container carries, so a later process can find
#: its network and serve its proxy again (:func:`ensure_container_proxy`).
_NET_LABEL, _PROXY_LABEL = "noeta.network", "noeta.proxy"
#: The filtering proxies this process serves, by container name.
_PROXIES: dict[str, Listeners] = {}


def _sandbox(value: SandboxSettings | Mapping[str, object] | None) -> SandboxSettings | None:
    if value is None or isinstance(value, SandboxSettings):
        return value
    return SandboxSettings.from_json(value)


async def _gateway(docker: str, network: str) -> str:
    result = await _docker(docker, "network", "inspect", "-f",
                           "{{range .IPAM.Config}}{{.Gateway}} {{end}}", network)
    gateway = result.stdout.decode().split()
    if result.exit_code != 0 or not gateway:
        raise ContainerError(f"cannot read the gateway of docker network {network!r}")
    return gateway[0]


async def _open_proxy(name: str, settings: SandboxSettings, host: str,
                      ports: tuple[int, int] = (0, 0)) -> Listeners:
    proxy = default_proxy()
    old = _PROXIES.pop(name, None)
    if old is not None:
        await proxy.close(old)
    listeners = await proxy.open(
        allowed=settings.allowed_domains, denied=settings.denied_domains, tcp_host=host,
        tcp_ports=ports, upstream_http=settings.http_proxy_port,
        upstream_socks=settings.socks_proxy_port)
    _PROXIES[name] = listeners
    return listeners


async def start_container(
    image: str,
    *,
    name: str | None = None,
    workdir: str = "/workspace",
    mounts: Sequence[tuple[str, str] | tuple[str, str, bool]] = (),
    env: Mapping[str, str] | None = None,
    args: Sequence[str] = (),
    command: Sequence[str] = ("sleep", "infinity"),
    docker: str = "docker",
    network: str | None = "none",
    read_only: bool = True,
    caps: Sequence[str] | None = DEFAULT_CAPS,
    pids_limit: int | None = DEFAULT_PIDS_LIMIT,
    memory: str | None = None,
    cpus: str | None = None,
    sandbox: SandboxSettings | Mapping[str, object] | None = None,
) -> str:
    """``docker run -d`` a long-lived container to exec into and return its
    name. ``mounts`` are ``(host_path, container_path[, read_only])``;
    ``args`` go to ``docker run`` as given. The container runs ``command``
    (default: sleep forever) under ``--init``, so stopping it is prompt and
    zombies are reaped. A failed start removes what it created.

    Secure by default: no network (``network="none"``; ``None`` is Docker's
    default bridge, or name one), a read-only root with writable scratch
    (``/tmp``, ``/var/tmp``, ``/run``, ``/root``, and ``workdir`` when nothing
    is mounted there), :data:`DEFAULT_CAPS` only (``caps=None`` keeps
    Docker's), no privilege gain through setuid programs, and at most
    ``pids_limit`` processes; ``memory`` / ``cpus`` cap the rest.

    ``sandbox`` — Claude Code's ``sandbox`` settings, mapped onto the
    container: with ``network.allowedDomains`` (or the user's own proxy
    port) the container joins an internal network of its own, which reaches
    only this process's filtering proxy on the network's gateway (the proxy
    variables point there), so only those hosts answer. The filesystem
    settings have no counterpart: the container sees only its mounts."""
    name = name or f"noeta-{uuid.uuid4().hex[:12]}"
    settings = _sandbox(sandbox)
    argv = ["run", "-d", "--init", "--name", name, "-w", workdir]
    mounted = False
    for mount in mounts:
        src, dst = os.path.abspath(mount[0]), mount[1]
        mount_ro = len(mount) > 2 and bool(mount[2])
        argv += ["-v", f"{src}:{dst}{':ro' if mount_ro else ''}"]
        mounted = mounted or posixpath.normpath(dst) == posixpath.normpath(workdir)
    if read_only:
        argv.append("--read-only")
        scratch = [*_TMPFS, *(() if mounted else (workdir,))]
        for path in dict.fromkeys(scratch):
            argv += ["--tmpfs", f"{path}:rw,exec,nosuid"]
    if caps is not None:
        argv += ["--cap-drop", "ALL", *(x for cap in caps for x in ("--cap-add", cap))]
    argv += ["--security-opt", "no-new-privileges"]
    if pids_limit:
        argv += ["--pids-limit", str(pids_limit)]
    if memory:
        argv += ["--memory", memory]
    if cpus:
        argv += ["--cpus", cpus]
    values: dict[str, str] = {}
    variables = dict(env or {})
    own_network: str | None = None
    if settings is not None and settings.enabled and (
            settings.allowed_domains or settings.http_proxy_port or settings.socks_proxy_port):
        own_network = f"{name}-net"
        made = await _docker(docker, "network", "create", "--internal", own_network)
        if made.exit_code != 0:
            raise ContainerError(f"docker network create failed for {own_network!r}: "
                                 f"{made.stderr.decode('utf-8', errors='replace').strip()}")
        try:
            listeners = await _open_proxy(name, settings, await _gateway(docker, own_network))
        except BaseException:
            await _docker(docker, "network", "rm", own_network)
            raise
        (host, http_port), (_, socks_port) = listeners.http, listeners.socks
        argv += ["--network", own_network, "--label", f"{_NET_LABEL}={own_network}",
                 "--label", f"{_PROXY_LABEL}={host}:{http_port}:{socks_port}"]
        http, socks = f"http://{host}:{http_port}", f"socks5h://{host}:{socks_port}"
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            variables.setdefault(key, http)
        for key in ("ALL_PROXY", "all_proxy"):
            variables.setdefault(key, socks)
    elif network is not None:
        argv += ["--network", network]
    for key, value in variables.items():
        if key in _CLIENT_ENV:
            argv += ["-e", f"{key}={value}"]
        else:  # the value travels in the client's environment, not argv
            argv += ["-e", key]
            values[key] = value
    argv += [*args, image, *command]
    result = await _docker(docker, *argv, env=values)
    if result.exit_code != 0:
        await remove_container(name, docker=docker)
        raise ContainerError(f"docker run failed for {name!r}: "
                             f"{result.stderr.decode('utf-8', errors='replace').strip()}")
    # The workdir may be missing from the image when nothing is mounted there.
    await _docker(docker, "exec", name, "mkdir", "-p", workdir)
    return name


async def ensure_container_proxy(name: str, sandbox: SandboxSettings | Mapping[str, object],
                                 *, docker: str = "docker") -> bool:
    """Serve the filtering proxy of a container :func:`start_container`
    sandboxed, from this process (after a restart): on the address its
    labels record. ``False`` when the container has none."""
    if name in _PROXIES:
        return True
    settings = _sandbox(sandbox)
    result = await _docker(docker, "inspect", "-f",
                           f'{{{{index .Config.Labels "{_PROXY_LABEL}"}}}}', name)
    label = result.stdout.decode().strip()
    if result.exit_code != 0 or settings is None or label.count(":") != 2:
        return False
    host, http_port, socks_port = label.split(":")
    await _open_proxy(name, settings, host, (int(http_port), int(socks_port)))
    return True


async def stop_container(name: str, *, docker: str = "docker", timeout: int = 10) -> bool:
    """``docker stop``: processes end, the container and its files stay
    (``docker start`` brings it back). ``False`` when it failed."""
    result = await _docker(docker, "stop", "-t", str(timeout), name, timeout=timeout + 30)
    return result.exit_code == 0


async def remove_container(name: str, *, docker: str = "docker") -> None:
    """``docker rm -f``, best effort, with the network and proxy
    :func:`start_container` made for its sandbox."""
    listeners = _PROXIES.pop(name, None)
    if listeners is not None:
        await default_proxy().close(listeners)
    with contextlib.suppress(ContainerError):
        await _docker(docker, "rm", "-f", name)
        await _docker(docker, "network", "rm", f"{name}-net")


async def container_state(name: str, *, docker: str = "docker") -> str:
    """``"running"``, ``"stopped"`` or ``"absent"``."""
    result = await _docker(docker, "inspect", "-f", "{{.State.Running}}", name)
    if result.exit_code != 0:
        return "absent"
    return "running" if result.stdout.strip() == b"true" else "stopped"


def open_env(cwd: str, *, container: str, **kw: Any) -> ContainerExecEnv:
    """The ``container`` entry point of ``noeta.execenvs``. A ``sandbox``
    argument is accepted and left to provisioning (:func:`start_container`,
    :func:`ensure_container_proxy`): a container is confined as a whole."""
    kw.pop("sandbox", None)
    return ContainerExecEnv(container, cwd=cwd, **kw)
