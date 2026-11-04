"""Signals front ends post (``client.signal(task_id, name)``) for plugins to
act on: facts about the person's use of a task that only a front end
knows, recorded in the log like any other input."""

from __future__ import annotations

__all__ = ["ARCHIVE_SIGNAL", "RENAME_SIGNAL", "RESUMED_SIGNAL"]

#: A front end reopened an existing task for the person (``--resume``,
#: ``--continue``, ``/resume``, an editor loading a session): what
#: Claude Code's start hooks see as a resume. A worker picking a task up is not one.
RESUMED_SIGNAL = "resumed"

#: The person named the task (``{"title": ...}``): the title from then on,
#: whatever the task was named before.
RENAME_SIGNAL = "rename"

#: The person archived the task (``{"archived": true}``) or brought it back
#: (``false``): front ends list it apart. No plugin acts on it; the session
#: list reads it from the inbox (``noeta.host.summaries``), where it lands
#: even on an ended task.
ARCHIVE_SIGNAL = "archive"
