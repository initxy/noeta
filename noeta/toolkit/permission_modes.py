"""The permission-mode vocabulary the permissions plugin acts on and front
ends offer: the modes, their names, the order a mode key steps through, the
signal that switches the mode, and the mode a task's log last recorded."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal

from noeta.core.events import ExtensionRecorded, Recorded

PermissionMode = Literal["default", "acceptEdits", "plan", "auto", "dontAsk",
                         "bypassPermissions"]

#: Claude Code's permission modes (the permissions plugin's), how front ends
#: name them, the order a front end's mode key steps through (``dontAsk``
#: is set only by configuration), and the signal that switches the mode
#: (``client.signal(task_id, MODE_SIGNAL, {"mode": ...})``).
PERMISSION_MODES: tuple[str, ...] = ("default", "acceptEdits", "plan", "auto", "dontAsk",
                                     "bypassPermissions")
MODE_CYCLE: tuple[str, ...] = ("default", "acceptEdits", "plan", "auto", "bypassPermissions")
MODE_NAMES: dict[str, str] = {
    "default": "Default (ask before changes)",
    "acceptEdits": "Accept edits",
    "plan": "Plan (explore, change nothing)",
    "auto": "Auto (a classifier screens what would ask)",
    "dontAsk": "Don't ask (refuse what would ask)",
    "bypassPermissions": "Bypass permissions (ask for nothing)",
}
MODE_SIGNAL = "permission_mode"


def current_mode(events: Sequence[Recorded]) -> str | None:
    """The task's permission mode, as its permissions plugin last recorded
    it (``None`` before it has)."""
    mode = None
    for rec in events:
        ev = rec.event
        if isinstance(ev, ExtensionRecorded) and ev.extension == "permissions" \
                and ev.kind in ("mode", "plan_exit") and isinstance(ev.data, dict):
            mode = str(ev.data.get("mode") or "") or mode
    return mode


def next_mode(mode: str | None) -> str:
    """The mode after ``mode`` in :data:`MODE_CYCLE`."""
    if mode not in MODE_CYCLE:
        return MODE_CYCLE[0]
    return MODE_CYCLE[(MODE_CYCLE.index(mode) + 1) % len(MODE_CYCLE)]
