"""What a task of a tenant other than the default one may read or write
in-process, whatever its rules, mode or approvals: an allow list. It may
reach its tenant's workspace; its own corners of noeta's places (its spilled
tool outputs and its tree's transcripts, its tenant's plan files, in its
tenant's project directory of its working directory the memory and, to read,
its tree's compaction transcripts, and read access to the operator's skills
and saved workflows); its tenant's own temporary directory
(:meth:`TenantFence.temp_dir`: its commands' ``TMPDIR`` and its job logs);
and, to read, the machine's programs, libraries and settings
(:data:`SYSTEM_READABLE`, but never ``/etc/noeta``, a home directory or the
server's own directories). Everything else is refused: noeta's home (the
tokens, settings, other tenants' directories, the store), the store's files,
``/proc``, noeta's places in the temp directories, other tenants'
workspaces, every home directory (the operator's credentials) and the
server's own directories (the default tenant's workspace).

Tenants share the worker's OS user, so this is the only boundary between a
tenant's file tools and those files; a tenant answers its own approval
prompts, so nothing it can approve may lift it.

:meth:`TenantFence.refusal` is the check for in-process file access;
:meth:`TenantFence.confine` makes the same places unreadable to the task's
sandboxed commands.

Paths given to :meth:`TenantFence.refusal` must already be resolved
(symlinks and ``..``); the fence resolves its own places the same way."""

from __future__ import annotations

import dataclasses
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
from noeta.toolkit.sandbox import SandboxSettings
from noeta.toolkit.tools import spill_path

__all__ = ["SYSTEM_PATHS", "TenantFence", "store_files"]

#: Outside noeta's home: the machine's noeta settings and every process's
#: environment, command line and open files.
SYSTEM_PATHS = ("/etc/noeta", "/proc")
#: What a tenant's task may read (never write) outside its workspace and
#: corners: the machine's programs, libraries and settings, but for
#: :data:`SYSTEM_PATHS`, home directories and the server's own directories.
#: Not ``/opt``, ``/srv``, ``/var`` or ``/mnt``: other software's data and
#: other disks are kept there (``/opt`` may be a link to a data disk).
SYSTEM_READABLE = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc",
                   "/private/etc", "/nix/store")
#: Where people's home directories are kept; every one of them, the worker's
#: own home and each login account's are hidden from a tenant's task.
HOME_ROOTS = ("/home", "/root", "/Users", "/var/root")
#: Places a directory to hide may not hold: hiding one would hide the
#: machine's programs or the command's own temporary files.
_NEVER_HIDDEN = (*SYSTEM_READABLE, "/opt", "/tmp", "/var/tmp", "/dev", "/proc", "/sys", "/run")
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
    """The machine's home directories: :data:`HOME_ROOTS`, the worker's
    ``~`` and every login account's (root's and those from uid 500 on), but
    none that holds the machine's programs (an account whose home is
    ``/``)."""
    out = [*HOME_ROOTS, os.path.expanduser("~")]
    try:
        accounts = pwd.getpwall()
    except OSError:  # pragma: no cover - no account database
        accounts = []
    out += [a.pw_dir for a in accounts if (a.pw_uid == 0 or 500 <= a.pw_uid < 65534)
            and posixpath.isabs(a.pw_dir)]
    return [d for d in dict.fromkeys(posixpath.normpath(d) for d in out) if _hideable(d)]


def _hideable(path: str) -> bool:
    """Whether hiding ``path`` leaves the machine's programs, temporary
    directories and kernel file systems alone."""
    keep = [*_NEVER_HIDDEN, *map(_real, _NEVER_HIDDEN)]
    return not any(_within(k, path) for k in keep) \
        and not any(_within(path, k) for k in ("/dev", "/proc", "/sys"))


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
    one."""

    home: str | None = None
    spill_root: str | None = None
    plans_dir: str | None = None
    store_files: tuple[str, ...] = ()
    workspaces: Mapping[str, str] = field(default_factory=dict)
    temp_root: str | None = None
    server_dirs: tuple[str, ...] = ()

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
        fenced = [p for p in (self.home or str(noeta_home()), self.spill_root, self.plans_dir)
                  if p]
        if any(_within(path, _real(p)) for p in fenced):
            if any(_within(path, own)
                   for own in self._own(tenant, task_id, root_id, cwd, write)):
                return None
            return head + "it is in noeta's own directories (tokens, settings, other tasks' " \
                "files)"
        # What is left is an allow list: the workspace, the own temporary
        # directory and, to read, the machine's programs and settings.
        real = _real(path)
        if self._workspace_owner(real) == tenant or _within(real, own_temp):
            return None
        if not write and self._system_readable(real):
            return None
        return head + "it is outside the tenant's workspace and its own places in noeta"

    def _system_readable(self, real: str) -> bool:
        if not any(_within(real, p) or _within(real, _real(p)) for p in SYSTEM_READABLE):
            return False
        hidden = [*self.servers(), *home_dirs()]
        return not any(_within(real, h) or _within(real, _real(h)) for h in hidden)

    def servers(self) -> list[str]:
        """The server's own directories: the worker's working directory and
        :attr:`server_dirs`."""
        return list(dict.fromkeys([os.getcwd(), *self.server_dirs]))

    def confine(self, settings: SandboxSettings, *, tenant: str, task_id: str,
                root_id: str = "", cwd: str = "") -> SandboxSettings:
        """``settings`` for the commands of the task ``task_id`` (as
        :meth:`refusal` names it): the fenced places, every home directory
        and the server's own directories unreadable but for the tenant's
        workspace, the task's own corners, its tenant's temporary directory
        and the Python that runs the sandbox's network bridge; no
        ``allowWrite`` / ``allowRead`` / ``additionalDirectories`` of the
        operator's; every command inside the sandbox (no excluded commands,
        no way out, filesystem confined) and a fresh ``/proc`` of its own
        PID namespace. ``settings`` themselves for the default tenant."""
        if tenant == DEFAULT_TENANT:
            return settings
        home = self.home or str(noeta_home())
        workspace = self.workspaces.get(tenant)
        others = [w for name, w in self.workspaces.items() if name != tenant]
        temps = self.temp_places()
        people = [d for d in (*home_dirs(), *self.servers()) if _hideable(d)
                  and not (workspace and _within(_real(d), _real(workspace)))]
        denied = [home, *(p for p in (self.spill_root, self.plans_dir) if p),
                  *(s for s in SYSTEM_PATHS if s != "/proc"), *self.store_files, *others,
                  *temps, *people]
        own = [*self._own(tenant, task_id, root_id or task_id, cwd, write=False),
               self.temp_dir(tenant), *([workspace] if workspace else []),
               os.path.realpath(sys.executable), sys.base_prefix]
        return dataclasses.replace(
            settings, tenant_deny_read=tuple(dict.fromkeys(denied)),
            tenant_allow_read=tuple(own), allow_write=(), allow_read=(),
            tenant_temp_places=tuple(temps), tenant_temp_dir=self.temp_dir(tenant),
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
