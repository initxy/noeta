"""The three built-in compose-time reminder renders.

Each render is a pure function of the narrow
:class:`~noeta.context.reminders.ReminderView` projection — no clock, no
randomness, no external fetch — so recomposing a turn yields the same bytes.
The registry *mechanism* stays in ``noeta.context.reminders``; the parent
manifest declares these renders by ``ref``, and the kernel never imports one.
"""

from __future__ import annotations

from typing import Optional

from noeta.context.reminders import ReminderSpec, ReminderView


__all__ = [
    "BUILTIN_REMINDER_PRIORITIES",
    "default_reminder_specs",
    "todo_reminder",
    "delegation_reminder",
    "read_suggestion_reminder",
]


def todo_reminder(view: ReminderView) -> Optional[str]:
    """List the *unfinished* todos so the model does not go blind on its plan.

    ``TaskState.todos`` is folded state the model writes through ``todo_write``
    but otherwise never sees again. An empty or all-completed list renders
    nothing — a finished checklist must not nag.
    """
    unfinished = [
        t
        for t in view.todos
        if isinstance(t, dict) and t.get("status") != "completed"
    ]
    if not unfinished:
        return None
    lines = [
        f"- [{t.get('status', 'pending')}] {t.get('content', '')}"
        for t in unfinished
    ]
    return (
        "Your current todo list (unfinished items only). Keep it updated as "
        "you make progress — mark items in_progress / completed via "
        "todo_write so it stays accurate:\n"
        + "\n".join(lines)
    )


def delegation_reminder(view: ReminderView) -> Optional[str]:
    """Just-in-time fan-out nudge while delegation is offered and unused.

    Live only while ``delegation_enabled`` and no ``spawn_subagent`` has landed
    yet, so it stops nudging the moment the first sub-agent is spawned.
    """
    if not view.delegation_enabled or view.already_spawned:
        return None
    return (
        "When you delegate independent work to sub-agents, batch ALL the "
        "goals into ONE spawn_subagent call's spawns array so they run "
        "concurrently and the results return together. Spawning one per "
        "turn is sequential, not parallel."
    )


def read_suggestion_reminder(view: ReminderView) -> Optional[str]:
    """Suggest a different read strategy while compaction is thrashing.

    Live only while ``ContextState.compaction_thrashing`` is latched.
    """
    if not view.compaction_thrashing:
        return None
    return (
        "The context window keeps getting refilled to the limit by what "
        "looks like a single large file or large tool output, so compaction "
        "is spinning without freeing real headroom. Consider a different "
        "reading strategy: read in chunks, read only the relevant section, "
        "or extract the key points once and re-read on demand instead of "
        "pulling the whole large content back into context each time."
    )


#: Name -> priority, chosen so the composed order is todo -> delegation ->
#: read and spread by 100 so third-party reminders can interleave. The manifest
#: declarations must carry the same numbers.
BUILTIN_REMINDER_PRIORITIES: dict[str, int] = {
    "unfinished-todos": 100,
    "delegation-nudge": 200,
    "read-suggestion": 300,
}


def default_reminder_specs() -> tuple[ReminderSpec, ...]:
    """The three built-in reminders as specs — the impl-side equivalent of
    resolving the manifest, for a caller that already holds this module.
    """
    return (
        ReminderSpec(
            "unfinished-todos",
            BUILTIN_REMINDER_PRIORITIES["unfinished-todos"],
            todo_reminder,
        ),
        ReminderSpec(
            "delegation-nudge",
            BUILTIN_REMINDER_PRIORITIES["delegation-nudge"],
            delegation_reminder,
        ),
        ReminderSpec(
            "read-suggestion",
            BUILTIN_REMINDER_PRIORITIES["read-suggestion"],
            read_suggestion_reminder,
        ),
    )
