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
from noeta.toolkit.protected_paths import PROTECTED_DIRS, PROTECTED_FILES, PROTECTED_NESTED
from noeta.toolkit.sandbox import SandboxSettings

__all__ = [
    "DEFAULT_DENY_READ",
    "PLACEHOLDERS",
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
#: Paths under a writable directory a confined command may not change: the
#: permissions plugin's protected directories and files (noeta's project
#: directory, editor tasks, hook runners, shell and package-manager
#: settings) and git's hooks and config — code that would later run outside
#: the sandbox. ``.git`` itself stays writable (git works in the sandbox) but
#: pinned. Only a missing :data:`PLACEHOLDERS` path is covered by an empty
#: read-only placeholder, so it cannot be created either.
PROTECTED = (
    *(d for d in PROTECTED_DIRS if d != ".git"),
    *("/".join(n) for n in PROTECTED_NESTED),
    *sorted(PROTECTED_FILES),
    ".git/hooks", ".git/config", ".git/config.worktree",
)
#: The protected names a command must not create when missing: what is
#: executed on the next ordinary command without a confirmation step —
#: noeta's own directory, git's hooks and config, and the settings a
#: package manager or build tool runs code from (``yarnPath``, npm's
#: ``node-options``, a pnpmfile, bun's preload, cargo's and maven's
#: extensions, bazel's flags and the bazelisk download). The other protected
#: names are read-only when present but get no placeholder, since each runs
#: only after a separate step of the person's (``direnv allow``, an editor
#: trusting the workspace, installing a hook runner): a placeholder is an
#: empty entry the person sees appear in the project while a command runs.
#: The configured hooks directory is always placeheld, and a hook runner's
#: other configs (:data:`HOOK_RUNNER_CONFIGS`) once it is in use.
PLACEHOLDERS = frozenset({
    ".noeta", ".git/hooks", ".git/config", ".git/config.worktree",
    ".npmrc", ".yarnrc", ".yarnrc.yml", ".yarn", ".pnpmfile.cjs", ".pnp.cjs",
    ".pnp.loader.mjs", "bunfig.toml", ".bunfig.toml", ".cargo", ".mvn", ".bazelrc",
    ".bazelversion", ".bazeliskrc"})
#: The :data:`PLACEHOLDERS` that are directories; the rest are files.
_PLACEHELD_DIRS = frozenset({".noeta", ".git/hooks", ".yarn", ".cargo", ".mvn"})
#: A hook runner's configs: when one is present the hooks run it, and a
#: missing one (a ``-local`` override) is read on the next commit.
HOOK_RUNNER_CONFIGS = ("lefthook.yml", ".lefthook.yml", "lefthook.yaml", ".lefthook.yaml",
                       "lefthook-local.yml", ".lefthook-local.yml", "lefthook-local.yaml",
                       ".lefthook-local.yaml")
#: Directories that stay writable but are mounted onto themselves: a mount
#: point cannot be renamed, so what is protected inside cannot be moved
#: away and recreated. Every directory between a working directory and a
#: protected path is pinned the same way.
PINNED = (".git",)
#: Git config files: a missing one is covered by an empty file, which git
#: reads as no settings (a directory there would break git), as is a
#: missing config file of a tool (an empty file is no settings; a directory
#: breaks pnpm). Any other missing path gets an empty directory, which git
#: does not list; a placeholder file at a repository's top is listed in the
#: command's view of ``.git/info/exclude``, so git does not list it either.
_GIT_CONFIGS = ("config", "config.worktree")
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
    _closed: bool = field(default=False, repr=False)

    def violations(self) -> list[str]:
        return list(self.listeners.refused) if self.listeners is not None else []

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _release(self.placeholders)
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
    """What one command may touch, as absolute paths.

    ``pinned`` directories are mounted onto themselves (still writable).
    ``placeholders`` are missing denied paths, each covered by an empty
    read-only directory (or file, when ``True``) that ``bwrap`` creates on
    the host as its mount point."""

    writable: list[str]
    deny_write: list[str]
    deny_read: list[str]
    allow_read: list[str]
    pinned: list[str] = field(default_factory=list)
    placeholders: list[tuple[str, bool]] = field(default_factory=list)


def _first_missing(path: str) -> str | None:
    """The outermost missing component of ``path``, when its parent is a
    real directory; ``None`` when ``path`` exists or there is none."""
    if os.path.lexists(path):
        return None
    current = path
    while True:
        parent = os.path.dirname(current)
        if parent == current:
            return None
        if os.path.lexists(parent):
            real_dir = os.path.isdir(parent) and not os.path.islink(parent)
            return current if real_dir else None
        current = parent


def _module_gitdirs(gitdir: str, limit: int = 256) -> list[str]:
    """The submodules' git directories under ``gitdir/modules`` (a name may
    hold slashes; a submodule may have its own)."""
    found: list[str] = []
    stack = [os.path.join(gitdir, "modules")]
    while stack and len(found) < limit:
        try:
            entries = list(os.scandir(stack.pop()))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_dir(follow_symlinks=False):
                continue
            if os.path.isfile(os.path.join(entry.path, "HEAD")):
                found.append(entry.path)
                stack.append(os.path.join(entry.path, "modules"))
            else:
                stack.append(entry.path)
    return found


def _hooks_dir(project: str) -> str | None:
    """The hooks directory git runs from for ``project`` (``core.hooksPath``
    when set), or ``None`` when it is not a git work tree."""
    if not os.path.lexists(os.path.join(project, ".git")):
        return None
    try:
        done = subprocess.run(["git", "rev-parse", "--git-path", "hooks"], cwd=project,
                              capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    out = done.stdout.decode("utf-8", "replace").strip()
    if done.returncode != 0 or not out:
        return None
    return os.path.realpath(os.path.join(project, out))


def _git_protected(project: str) -> list[str]:
    """What git runs code from in ``project`` beyond :data:`PROTECTED`: each
    submodule's config and hooks, and the configured hooks directory."""
    out: list[str] = []
    for module in _module_gitdirs(os.path.join(project, ".git")):
        out += [os.path.join(module, "hooks"),
                *(os.path.join(module, name) for name in _GIT_CONFIGS)]
    hooks = _hooks_dir(project)
    if hooks is not None:
        out.append(hooks)
    return out


def _hook_runner_configs(project: str) -> tuple[str, ...]:
    """:data:`HOOK_RUNNER_CONFIGS` when one of them is in ``project``."""
    present = any(os.path.lexists(os.path.join(project, n)) for n in HOOK_RUNNER_CONFIGS)
    return HOOK_RUNNER_CONFIGS if present else ()


def exclude_overlays(projects: Sequence[str],
                     placeholders: Sequence[tuple[str, bool]]) -> list[tuple[str, str]]:
    """``(path, content)``: each project's ``.git/info/exclude`` as a
    command sees it, listing the placeholder files at the project's top so
    git neither lists nor adds them."""
    out: list[tuple[str, str]] = []
    for project in projects:
        names = sorted(os.path.basename(path) for path, is_file in placeholders
                       if is_file and os.path.dirname(path) == project)
        exclude = os.path.join(project, ".git", "info", "exclude")
        if not names or not os.path.isfile(exclude) or os.path.islink(exclude):
            continue
        try:
            with open(exclude, encoding="utf-8", errors="surrogateescape") as f:
                text = f.read()
        except OSError:
            continue
        if text and not text.endswith("\n"):
            text += "\n"
        out.append((exclude, text + "".join(f"/{n}\n" for n in names)))
    return out


def _between(path: str, writes: Sequence[str]) -> list[str]:
    """The real directories strictly between a writable directory and
    ``path``, outermost first."""
    for w in writes:
        if path != w and _within(path, w):
            parts = os.path.relpath(path, w).split(os.sep)[:-1]
            dirs = [os.path.join(w, *parts[:i]) for i in range(1, len(parts) + 1)]
            return [d for d in dirs if os.path.isdir(d) and not os.path.islink(d)]
    return []


def plan_paths(settings: SandboxSettings, root: str, tmpdir: str,
               writable: Sequence[str] = ()) -> Plan:
    """The paths of :class:`Plan` for a command whose working directory is
    ``root``."""
    base = root
    writes = [root, *_expand(settings, settings.allow_write, base), tmpdir, *writable]
    writes = [w for w in dict.fromkeys(os.path.realpath(w) for w in writes) if os.path.exists(w)]
    user_deny = [os.path.realpath(p) for p in _expand(settings, settings.deny_write, base)]
    # Placeholders go where a missing path matters: the denyWrite settings
    # and the protected paths of the working directories (not of TMPDIR or
    # allowWrite, which no one trusts as a project).
    projects = [os.path.realpath(p) for p in (root, *writable)]
    git_protected = [path for p in projects for path in _git_protected(p)
                     if any(_within(path, w) for w in writes)]
    protected = [*(os.path.join(p, n) for p in projects for n in PROTECTED), *git_protected]
    deny_write = [*user_deny, *protected]
    pinned: list[str] = []
    for w in writes:
        for name in PINNED:
            path = os.path.join(w, name)
            if os.path.isdir(path) and not os.path.islink(path):
                pinned.append(path)
            elif os.path.lexists(path):
                deny_write.append(path)  # a worktree's ``.git`` file
        deny_write += [os.path.join(w, p) for p in PROTECTED]
    home = str(noeta_home())
    deny_read = _expand(settings, [*DEFAULT_DENY_READ, home, *settings.deny_read], base)
    allow_read = _expand(settings, [*(os.path.join(home, p) for p in HOME_ALLOW_READ),
                                    *settings.allow_read], base)
    # A denied directory holding a writable one would hide it.
    deny_read = [d for d in deny_read if not any(_within(w, d) for w in writes)]
    hidden = [d for d in deny_read if os.path.isdir(d)]
    # Inside a hidden directory a path is unreadable and read-only already;
    # binding it would show it again.
    deny_write = [p for p in dict.fromkeys(deny_write)
                  if not any(_within(p, d) for d in hidden)]
    for path in deny_write:
        pinned += _between(path, writes)
    pinned = sorted(dict.fromkeys(p for p in pinned if p not in deny_write),
                    key=lambda p: p.count("/"))
    placeholders: dict[str, bool] = {}
    names = {os.path.join(p, n): n not in _PLACEHELD_DIRS
             for p in projects for n in (*PLACEHOLDERS, *_hook_runner_configs(p))}
    guarded = [*names, *git_protected]
    for path in [*user_deny, *guarded]:
        missing = _first_missing(path)
        if missing is not None and path in deny_write \
                and any(_within(missing, w) and missing != w for w in writes):
            placeholders.setdefault(missing, missing == path and (
                names.get(path, False) or ("/.git/" in path
                                           and os.path.basename(path) in _GIT_CONFIGS)))
    return Plan(writable=writes, deny_write=deny_write, deny_read=deny_read,
                allow_read=allow_read, pinned=pinned, placeholders=list(placeholders.items()))


def bwrap_args(settings: SandboxSettings, plan: Plan, *, bwrap: str, cwd: str,
               relays: Sequence[tuple[str, int]], bridge: str, empty: str = "/dev/null",
               overlays: Sequence[tuple[str, str]] = ()) -> list[str]:
    """The ``bwrap`` command line for ``plan``; the command follows it.
    ``empty`` is the empty file a placeholder file shows; ``overlays`` are
    ``(source, path)`` files shown read-only over ``path``."""
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
        for path in plan.pinned:
            args += ["--bind", path, path]
        for path in plan.deny_write:
            if os.path.exists(path):
                args += ["--ro-bind", path, path]
        for path, is_file in plan.placeholders:
            if is_file:
                args += ["--ro-bind", empty, path]
            else:
                args += ["--tmpfs", path, "--remount-ro", path]
        for source, path in overlays:
            args += ["--ro-bind", source, path]
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
        # A subpath rule does not stop renaming a directory above it.
        lines += [f"(deny file-write* (literal {_sb_quote(os.path.realpath(p))}))"
                  for p in plan.pinned]
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
