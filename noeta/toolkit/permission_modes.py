"""The permission-mode vocabulary the permissions plugin acts on and front
ends offer: the modes, their names, the order a mode key steps through, the
signal that switches the mode, and the mode a task's log last recorded.

The permissions plugin publishes the vocabulary a task offers as its
``modes`` record (:func:`modes_record`): front ends read that
(:func:`published_modes`) rather than the constants here, so a plugin that
refuses ``bypassPermissions`` is never stepped into it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from noeta.core.events import ExtensionRecorded, Recorded
from noeta.toolkit.plugin_records import PERMISSIONS

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
#: Claude Code's mode names noeta takes as another name for its own.
MODE_ALIASES: dict[str, str] = {"manual": "default"}


def current_mode(events: Sequence[Recorded]) -> str | None:
    """The task's permission mode, as its permissions plugin last recorded
    it (``None`` before it has)."""
    mode = None
    for rec in events:
        ev = rec.event
        if isinstance(ev, ExtensionRecorded) and ev.extension == PERMISSIONS \
                and ev.kind in ("mode", "plan_exit") and isinstance(ev.data, dict):
            mode = str(ev.data.get("mode") or "") or mode
    return mode


def next_mode(mode: str | None, cycle: Sequence[str] = MODE_CYCLE) -> str:
    """The mode after ``mode`` in ``cycle``."""
    if mode is None or mode not in cycle:
        return cycle[0]
    return cycle[(list(cycle).index(mode) + 1) % len(cycle)]


#: The permissions plugin's record of the vocabulary a task offers.
MODES_KIND = "modes"


@dataclass(frozen=True)
class Modes:
    """The modes a task offers, the order a mode key steps through them, and
    how front ends name them."""

    modes: tuple[str, ...] = PERMISSION_MODES
    cycle: tuple[str, ...] = MODE_CYCLE
    names: Mapping[str, str] = field(default_factory=lambda: dict(MODE_NAMES))

    def without(self, mode: str) -> Modes:
        return Modes(modes=tuple(m for m in self.modes if m != mode),
                     cycle=tuple(m for m in self.cycle if m != mode),
                     names={k: v for k, v in self.names.items() if k != mode})


def modes_record(modes: Modes) -> dict[str, Any]:
    """The ``modes`` record's data for ``modes``."""
    return {"modes": list(modes.modes), "cycle": list(modes.cycle), "names": dict(modes.names)}


def published_modes(events: Sequence[Recorded]) -> Modes | None:
    """The vocabulary the task's permissions plugin last published
    (``None`` before it has)."""
    found = None
    for rec in events:
        ev = rec.event
        if isinstance(ev, ExtensionRecorded) and ev.extension == PERMISSIONS \
                and ev.kind == MODES_KIND and isinstance(ev.data, dict):
            d = ev.data
            found = Modes(modes=tuple(str(m) for m in d.get("modes") or ()),
                          cycle=tuple(str(m) for m in d.get("cycle") or ()),
                          names={str(k): str(v) for k, v in (d.get("names") or {}).items()})
    return found
