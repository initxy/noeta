"""Signals front ends post (``client.signal(task_id, name)``) for plugins to
act on: facts about the person's use of a task that only a front end
knows, recorded in the log like any other input."""

from __future__ import annotations

__all__ = ["RESUMED_SIGNAL"]

#: A front end reopened an existing task for the person (``--resume``,
#: ``--continue``, ``/resume``, an editor loading a session): what
#: Claude Code's start hooks see as a resume. A worker picking a task up is not one.
RESUMED_SIGNAL = "resumed"
