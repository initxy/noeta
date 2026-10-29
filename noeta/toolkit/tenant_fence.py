"""What a task of a tenant other than the default one may never read or
write in-process, whatever its rules, mode or approvals: noeta's home (the
tokens, settings, other tenants' directories, the store), the store's files,
the machine's ``/etc/noeta``, ``/proc`` (other processes' environments) and
other tenants' workspaces.

Tenants share the worker's OS user, so this is the only boundary between a
tenant's file tools and those files; a tenant answers its own approval
prompts, so nothing it can approve may lift it. Inside the fenced places a
task keeps its own corners: its spilled tool outputs and its tree's
transcripts, its tenant's plan files, the project directory (memory) of its
working directory, and read access to the operator's skills.

Paths given to :meth:`TenantFence.refusal` must already be resolved
(symlinks and ``..``); the fence resolves its own places the same way."""

from __future__ import annotations

import os
import posixpath
from collections.abc import Mapping
from dataclasses import dataclass, field

from noeta.core.events import DEFAULT_TENANT
from noeta.toolkit.paths import noeta_home, project_slug
from noeta.toolkit.tools import spill_path

__all__ = ["SYSTEM_PATHS", "TenantFence", "store_files"]

#: Outside noeta's home: the machine's noeta settings and every process's
#: environment, command line and open files.
SYSTEM_PATHS = ("/etc/noeta", "/proc")
#: Inside noeta's home, what a tenant's task may read but never write.
_HOME_READABLE = ("skills",)
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


@dataclass(frozen=True)
class TenantFence:
    """The fenced places. ``home``: noeta's home (default ``NOETA_HOME`` /
    ``~/.noeta`` when checked); ``spill_root`` and ``plans_dir`` are fenced
    wherever they are (a task keeps its own spill directories and its
    tenant's plan directory); ``store_files``: the store's files;
    ``workspaces``: each tenant's workspace, by tenant name."""

    home: str | None = None
    spill_root: str | None = None
    plans_dir: str | None = None
    store_files: tuple[str, ...] = ()
    workspaces: Mapping[str, str] = field(default_factory=dict)

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
        fenced = [p for p in (self.home or str(noeta_home()), self.spill_root, self.plans_dir)
                  if p]
        if not any(_within(path, _real(p)) for p in fenced):
            return None
        if any(_within(path, own) for own in self._own(tenant, task_id, root_id, cwd, write)):
            return None
        return head + "it is in noeta's own directories (tokens, settings, other tasks' files)"

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
        out += [posixpath.join(home, "projects", project_slug(d))
                for d in self._project_roots(tenant, cwd)]
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
