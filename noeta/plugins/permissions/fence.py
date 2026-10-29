"""The tenant fence at the gate: a call of a non-default tenant's task that
names a fenced path (:mod:`noeta.toolkit.tenant_fence`) is denied before any
rule, mode or approval is looked at, so nothing the tenant answers lifts it.
Commands are the sandbox's to confine; every other tool's path arguments are
checked as written, normalized, and resolved through symlinks."""

from __future__ import annotations

import os
import posixpath
from typing import Any

from noeta.core.events import DEFAULT_TENANT
from noeta.core.extension import TaskView
from noeta.core.types import ToolUse
from noeta.plugins.permissions.shell_check import real_path
from noeta.toolkit import permission_rules as r
from noeta.toolkit.tenant_fence import TenantFence

#: Input keys that name a file or directory a tool reads or writes.
PATH_KEYS = ("file_path", "notebook_path", "path", "scriptPath")


def _paths(call: ToolUse) -> list[str]:
    return [v for k in PATH_KEYS if isinstance(v := call.input.get(k), str) and v]


async def refusal(fence: TenantFence, view: TaskView, call: ToolUse) -> str | None:
    """Why ``call`` may not run in this task, or ``None``."""
    tenant = view.state.tenant
    if tenant == DEFAULT_TENANT or call.name in r.SHELL_TOOLS:
        return None
    cwd = view.cwd or os.getcwd()
    home = os.path.expanduser("~")
    write = call.name in r.EDIT_TOOLS
    for raw in _paths(call):
        if raw == "~" or raw.startswith("~/"):
            raw = home + raw[1:]
        joined = raw if posixpath.isabs(raw) else posixpath.join(cwd, raw)
        # ``link/..`` is where the link points, then up: resolve the path as
        # written as well as its tidied form.
        candidates: dict[str, Any] = dict.fromkeys(
            [posixpath.normpath(joined), await real_path(view, joined),
             await real_path(view, posixpath.normpath(joined))])
        for path in candidates:
            why = fence.refusal(path, tenant=tenant, task_id=view.task_id,
                                root_id=view.state.root_id or view.task_id, cwd=cwd,
                                write=write)
            if why is not None:
                return why
    return None
