"""``reminders`` built-in — the three compose-time reminder renders (impl).

Microkernel M2: the three built-in renderers moved here from
``noeta.context.reminders``, which keeps only the registry *mechanism*
(:class:`~noeta.context.reminders.ReminderSpec` /
:class:`~noeta.context.reminders.ReminderRegistry`) the locked composer
consults. The manifest in the parent package declares these renders by ``ref``;
the SDK client build resolves them through the ordinary loader path
(:func:`noeta.client.parts.default_reminder_specs`) and injects them into the
kernel builder (``build_session_inputs(base_reminders=…)``) — the kernel never
imports a renderer.

Each render is a pure function of the narrow
:class:`~noeta.context.reminders.ReminderView` projection (no clock, no
randomness, no external fetch — the module red line over there). The text bytes
are copied word-for-word from the pre-migration composer methods so the
composed dynamic suffix stays byte-identical (D8 / acceptance 2); the
priorities keep the composed order todo -> delegation -> read.
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
    but otherwise never sees again. Surface only the items not ``completed``; an
    empty or all-completed list renders nothing (a finished checklist must not
    nag). Migrated verbatim from ``ThreeSegmentComposer._append_todo_reminder``.
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

    Live only while ``delegation_enabled`` AND no ``spawn_subagent`` has landed
    yet — self-limiting once the first sub-agent is spawned. Migrated verbatim
    from ``ThreeSegmentComposer._append_concurrency_reminder``.
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

    Live only while ``ContextState.compaction_thrashing`` is latched. Migrated
    verbatim from ``ThreeSegmentComposer._append_compaction_thrashing_reminder``.
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


#: The built-in reminders' names -> priority. Chosen so the composed order is
#: todo -> delegation -> read, byte-identical to the pre-migration append order.
#: Spread by 100 to leave room for third-party reminders to interleave. The
#: manifest declarations carry the same numbers (the listing surface).
BUILTIN_REMINDER_PRIORITIES: dict[str, int] = {
    "unfinished-todos": 100,
    "delegation-nudge": 200,
    "read-suggestion": 300,
}


def default_reminder_specs() -> tuple[ReminderSpec, ...]:
    """The three built-in reminders as specs, in declaration order.

    The injection value for the kernel builder's ``base_reminders`` parameter.
    The SDK path resolves the same three through the manifest
    (:func:`noeta.client.parts.default_reminder_specs`); this direct constructor
    is the impl-side equivalent for callers already holding the impl.
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
