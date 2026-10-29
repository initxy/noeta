"""What a task of a tenant other than the default one may never read or
write in-process, whatever its rules, mode or approvals.

It may read what the OS user can, but for a protected set: noeta's home (the
tokens, settings, other tenants' directories, the store) but for its own
corners, the store's files, ``/etc/noeta``, ``/proc``, noeta's places in the
temp directories but for its tenant's own, other tenants' workspaces, the
server's own directories (the default tenant's workspace), the credential
stores of every home directory (:data:`~noeta.toolkit.sandbox.DEFAULT_DENY_READ`
under each of :func:`home_dirs`) and the operator's ``denyRead``. It may write
only its tenant's workspace, its own corners and its commands' ``TMPDIR`` in
its tenant's own temporary directory (:meth:`TenantFence.temp_dir`, which
also holds its job logs, readable only). Its own corners are its spilled tool outputs and its tree's
transcripts, its tenant's plan files, in its tenant's project directory of
its working directory the memory and, to read, its tree's compaction
transcripts, and read access to the operator's skills and saved workflows.

Tenants share the worker's OS user, so this is the only boundary between a
tenant's file tools and those files; a tenant answers its own approval
prompts, so nothing it can approve may lift it.

:meth:`TenantFence.refusal` is the check for in-process file access;
:meth:`TenantFence.confine` makes the same places unreadable to the task's
sandboxed commands, which write only where file tools may.

Paths given to :meth:`TenantFence.refusal` must already be resolved
(symlinks and ``..``); the fence resolves its own places the same way."""

from __future__ import annotations

import dataclasses
import fnmatch
import os
import posixpath
import pwd
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from noeta.core.events import DEFAULT_TENANT
from noeta.toolkit.paths import (
    job_log_roots,
    noeta_home,
    project_dir,
    temp_root,
    tenant_temp_dir,
)
from noeta.toolkit.sandbox import DEFAULT_DENY_READ, SandboxSettings
from noeta.toolkit.tools import spill_path

__all__ = ["SYSTEM_PATHS", "TenantFence", "credential_paths", "home_dirs", "store_files"]

#: Outside noeta's home: the machine's noeta settings and every process's
#: environment, command line and open files.
SYSTEM_PATHS = ("/etc/noeta", "/proc")
#: Where people's home directories are kept: each directory in one is a home.
HOME_ROOTS = ("/home", "/Users")
#: Places a server directory to hide may not hold: hiding one would hide the
#: machine's programs or the command's own temporary files.
_NEVER_HIDDEN = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc",
                 "/private/etc", "/nix/store", "/opt", "/tmp", "/var/tmp", "/dev", "/proc",
                 "/sys", "/run")
#: Inside noeta's home, what a tenant's task may read but never write.
_HOME_READABLE = ("skills", "workflows")
#: The files sqlite keeps beside a database.
_SQLITE_SIDE_FILES = ("", "-wal", "-shm", "-journal")


def store_files(url: str) -> tuple[str, ...]:
    """The files a ``sqlite:///path`` store keeps; none for another store."""
    if not url.startswith("sqlite:///"):
        return ()
    path = url.removeprefix("sqlite:///")
    if not path or path == ":memory:":
        return ()
    path = os.path.abspath(os.path.expanduser(path))
    return tuple(path + side for side in _SQLITE_SIDE_FILES)


def _real(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _within(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


def home_dirs() -> list[str]:
    """The machine's home directories: the worker's ``~``, every account's
    and each directory in :data:`HOME_ROOTS`."""
    out = [os.path.expanduser("~")]
    try:
        out += [a.pw_dir for a in pwd.getpwall() if posixpath.isabs(a.pw_dir)]
    except OSError:  # pragma: no cover - no account database
        pass
    for root in HOME_ROOTS:
        try:
            out += [e.path for e in os.scandir(root) if e.is_dir()]
        except OSError:
            continue
    return list(dict.fromkeys(posixpath.normpath(d) for d in out))


def credential_paths() -> list[str]:
    """The sandbox's credential stores (``~/.ssh``, ``~/.aws``, ...) in every
    home directory."""
    return [posixpath.join(home, c.removeprefix("~/")) for home in home_dirs()
            for c in DEFAULT_DENY_READ]


def _hideable(path: str) -> bool:
    """Whether hiding ``path`` leaves the machine's programs, temporary
    directories and kernel file systems alone."""
    keep = [*_NEVER_HIDDEN, *map(_real, _NEVER_HIDDEN)]
    return not any(_within(k, path) for k in keep) \
        and not any(_within(path, k) for k in ("/dev", "/proc", "/sys"))


def _matches(path: str, place: str) -> bool:
    """Whether ``path`` is ``place`` or inside it; a ``place`` with ``*``,
    ``?`` or ``[`` is a pattern, matched by ``path`` or a directory above
    it."""
    if not any(c in place for c in "*?["):
        return _within(path, place) or _within(path, _real(place))
    here = path
    while True:
        if fnmatch.fnmatchcase(here, place):
            return True
        parent = posixpath.dirname(here)
        if parent == here:
            return False
        here = parent


@dataclass(frozen=True)
class TenantFence:
    """The fenced places. ``home``: noeta's home (default ``NOETA_HOME`` /
    ``~/.noeta`` when checked); ``spill_root`` and ``plans_dir`` are fenced
    wherever they are (a task keeps its own spill directories and its
    tenant's plan directory); ``store_files``: the store's files;
    ``workspaces``: each tenant's workspace, by tenant name; ``temp_root``:
    noeta's directory in the temp directory (default
    :func:`~noeta.toolkit.paths.temp_root`), where each tenant has its own;
    ``server_dirs``: the server's own directories (the default tenant's
    workspace), beside the worker's working directory, which always is
    one; ``deny_read``: the operator's ``denyRead`` (absolute paths or
    patterns)."""

    home: str | None = None
    spill_root: str | None = None
    plans_dir: str | None = None
    store_files: tuple[str, ...] = ()
    workspaces: Mapping[str, str] = field(default_factory=dict)
    temp_root: str | None = None
    server_dirs: tuple[str, ...] = ()
    deny_read: tuple[str, ...] = ()

    def temp_dir(self, tenant: str) -> str:
        """The tenant's own temporary directory (its tasks' ``TMPDIR`` and
        background job logs)."""
        return tenant_temp_dir(tenant, self.temp_root)

    def temp_places(self) -> list[str]:
        """noeta's places in the temp directories, shared by this machine's
        tasks: its temp root and the background job log directories."""
        return [self.temp_root or temp_root(), *job_log_roots()]

    def refusal(self, path: str, *, tenant: str, task_id: str, root_id: str = "",
                cwd: str = "", write: bool = False) -> str | None:
        """Why the task ``task_id`` (of ``tenant``, in tree ``root_id``,
        working in ``cwd``) may not reach the resolved ``path``, or ``None``.
        A default-tenant task is never refused."""
        if tenant == DEFAULT_TENANT:
            return None
        path = posixpath.normpath(path)
        head = f"tenant {tenant!r} may not {'write' if write else 'read'} {path}: "
        for system in SYSTEM_PATHS:
            if _within(path, system) or _within(path, _real(system)):
                return head + "it holds this machine's settings or other processes' data"
        if any(path == f or path == _real(f) for f in self.store_files):
            return head + "it is the task store"
        owner = self._workspace_owner(path)
        if owner is not None and owner != tenant:
            return head + "it is in another tenant's workspace"
        own_temp = _real(self.temp_dir(tenant))
        if not _within(path, own_temp) and any(
                _within(path, p) or _within(path, _real(p)) for p in self.temp_places()):
            return head + "it holds other tasks' command output and temporary files"
        # Of its own, file tools write only its commands' TMPDIR: not the job
        # logs and their state files, nor a command's sandbox files.
        own_tmp = posixpath.join(own_temp, "tmp")
        if write and _within(path, own_temp) and not _within(path, own_tmp):
            return head + "it holds the tenant's job logs and sandbox files"
        fenced = [p for p in (self.home or str(noeta_home()), self.spill_root, self.plans_dir)
                  if p]
        if any(_within(path, _real(p)) for p in fenced):
            if any(_within(path, own)
                   for own in self._own(tenant, task_id, root_id, cwd, write)):
                return None
            return head + "it is in noeta's own directories (tokens, settings, other tasks' " \
                "files)"
        real = _real(path)
        why = self._protected(real, tenant)
        if why is not None:
            return head + why
        if not write or self._workspace_owner(real) == tenant or _within(real, own_tmp):
            return None
        return head + "it is outside the tenant's workspace and its own places in noeta"

    def cwd_refusal(self, real: str, *, tenant: str, task_id: str, root_id: str = "",
                    cwd: str = "") -> str | None:
        """Why the task may not work in ``cwd``, which resolves to ``real``
        now: outside its tenant's workspace, or in a place it may not write
        (a directory on the way swapped for a link since the task was made).
        ``None`` for the default tenant, or one whose workspace is not
        known here."""
        workspace = self.workspaces.get(tenant)
        if tenant == DEFAULT_TENANT or workspace is None:
            return None
        real = posixpath.normpath(real)
        head = f"the working directory {cwd or real} resolves to {real}"
        if self._workspace_owner(real) != tenant:
            return f"{head}, outside tenant {tenant!r}'s workspace {workspace}"
        why = self.refusal(real, tenant=tenant, task_id=task_id, root_id=root_id, cwd=cwd,
                           write=True)
        return None if why is None else f"{head}; {why}"

    def _protected(self, real: str, tenant: str) -> str | None:
        """Why ``real`` is in the protected set beyond noeta's own places: the
        server's own directories, a home directory's credential stores or the
        operator's ``denyRead``. Inside the tenant's workspace only a place
        inside it counts (a server directory may hold the workspaces)."""
        workspace = self.workspaces.get(tenant)
        mine = _real(workspace) if workspace else None
        places = [*((d, "it is in the server's own directories") for d in self.servers()),
                  *((c, "it holds credentials") for c in credential_paths()),
                  *((d, "the operator's denyRead names it") for d in self.deny_read)]
        for place, why in places:
            if _matches(real, place) and not (
                    mine and _within(real, mine) and _matches(mine, place)):
                return why
        return None

    def servers(self) -> list[str]:
        """The server's own directories: the worker's working directory and
        :attr:`server_dirs`, but none that holds the machine's programs or
        temporary directories."""
        return [d for d in dict.fromkeys([os.getcwd(), *self.server_dirs]) if _hideable(d)]

    def confine(self, settings: SandboxSettings, *, tenant: str, task_id: str,
                root_id: str = "", cwd: str = "") -> SandboxSettings:
        """``settings`` for the commands of the task ``task_id`` (as
        :meth:`refusal` names it): what :meth:`refusal` protects unreadable
        but for the tenant's workspace, the task's own corners, its tenant's
        temporary directory and the Python that runs the sandbox's network
        bridge; no ``allowWrite`` / ``allowRead`` / ``additionalDirectories``
        of the operator's; every command inside the sandbox (no excluded
        commands, no way out, filesystem confined) and a fresh ``/proc`` of
        its own PID namespace. ``settings`` themselves for the default
        tenant."""
        if tenant == DEFAULT_TENANT:
            return settings
        home = self.home or str(noeta_home())
        workspace = self.workspaces.get(tenant)
        others = [w for name, w in self.workspaces.items() if name != tenant]
        temps = self.temp_places()
        # A server directory inside the workspace stays hidden (the
        # workspace is mounted first); one holding the workspace is hidden
        # with the workspace mounted again inside it.
        servers = self.servers()
        # Missing ones too: the plan is made again for each command, so one
        # created after the task started is hidden from the next command.
        creds = credential_paths()
        denied = [home, *(p for p in (self.spill_root, self.plans_dir) if p),
                  *(s for s in SYSTEM_PATHS if s != "/proc"), *self.store_files, *others,
                  *temps, *servers, *creds, *settings.deny_read]
        own = [*self._own(tenant, task_id, root_id or task_id, cwd, write=False),
               self.temp_dir(tenant), *([workspace] if workspace else []),
               os.path.realpath(sys.executable), sys.base_prefix]
        return dataclasses.replace(
            settings, tenant_deny_read=tuple(dict.fromkeys(denied)),
            tenant_allow_read=tuple(own), allow_write=(), allow_read=(),
            tenant_temp_places=tuple(temps), tenant_temp_dir=self.temp_dir(tenant),
            tenant_write_roots=(workspace,) if workspace else (),
            noeta_home_readable=(),
            excluded_commands=(), allow_unsandboxed=False, filesystem_disabled=False,
            weaker_nested=False)

    def _workspace_owner(self, path: str) -> str | None:
        """The tenant whose workspace most closely holds ``path``."""
        best: tuple[int, str] | None = None
        for name, workspace in self.workspaces.items():
            real = _real(workspace)
            if _within(path, real) and (best is None or len(real) > best[0]):
                best = (len(real), name)
        return best[1] if best else None

    def _own(self, tenant: str, task_id: str, root_id: str, cwd: str,
             write: bool) -> list[str]:
        """The task's own corners of the fenced places."""
        home = _real(self.home or str(noeta_home()))
        out: list[str] = []
        if self.spill_root:
            out += [posixpath.dirname(spill_path(_real(self.spill_root), t, "x"))
                    for t in dict.fromkeys(x for x in (task_id, root_id) if x)]
        if self.plans_dir:
            out.append(posixpath.join(_real(self.plans_dir), tenant))
        tree = list(dict.fromkeys(x for x in (task_id, root_id) if x))
        for d in self._project_roots(tenant, cwd):
            project = str(project_dir(d, Path(home), tenant))
            # Its memory, and to read, its tree's transcripts: not other
            # tasks' of the same directory.
            out.append(posixpath.join(project, "memory"))
            if not write:
                out += [posixpath.join(project, f"{t}.jsonl") for t in tree]
        if not write:
            out += [posixpath.join(home, p) for p in _HOME_READABLE]
        return out

    def _project_roots(self, tenant: str, cwd: str) -> list[str]:
        """The working directory and, within the tenant's workspace, the
        directories above it (memory may be kept for a repository's root)."""
        if not cwd:
            return []
        here = posixpath.normpath(cwd)
        workspace = self.workspaces.get(tenant)
        if workspace is None:
            return [here]
        top = _real(workspace)
        out = []
        while _within(here, top):
            out.append(here)
            if here == top or here == "/":
                break
            here = posixpath.dirname(here)
        return out or [posixpath.normpath(cwd)]
