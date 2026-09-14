"""The "new skills" note: naming, on a task's next turn, the skills that
joined its roster since the roster it last saw.

The Engine is rebuilt every turn, so a skill installed while the process
runs is in the ``skill`` tool's enum on the next turn by construction. The
enum alone is easy to miss, so — like Claude Code's "New skills discovered
in …, now available via the Skill tool" — the turn also carries a recorded
note naming them.

Two halves, both process-local, both living in the task's local state
(``noeta.runtime.task_local``):

* :class:`SkillRoster` — one per task, kept in the task-local slot
  :data:`ROSTER_SLOT`: the model-invocable roster the pack composed on its
  latest build (``note_built``, called by the session pack through the
  ``task_slot`` the host binds into ``plugin_config["skills"]``) and the
  roster last announced to the task (``take_new``). ``take_new`` returns the
  names present in the latest roster but not in the announced one, and
  moves the announced roster forward. A task's first sighting announces
  nothing (the opening turn's roster is the baseline, as in Claude Code); a
  removed skill is silent (it simply leaves the enum); a restart forgets
  the slot, so the first turn after it announces nothing either.
* :func:`new_skills_reminder_provider` — the ``turn_intake`` provider the
  host binds for a ``skill_invocation`` agent, over the host's
  ``peek(task_id, name)`` into the task-local slots. It reads
  ``view.task_id``'s roster and yields one ``Reminder(origin="system")``
  when there is something new. Its output is recorded, so resume folds the
  note back and never re-derives it.

The intake seam runs inside the woken window, after the turn's Engine was
built (``run_leased_task`` resolves the Engine, then runs the prelude), so
the slot already holds this turn's roster when the provider reads it.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from noeta.execution.reminders import RecallView, Reminder


__all__ = [
    "ROSTER_SLOT",
    "SkillRoster",
    "new_skills_note",
    "new_skills_reminder_provider",
]

#: The task-local slot name the pack and the provider meet at.
ROSTER_SLOT = "skills.roster"


class SkillRoster:
    """One task's ``(latest roster, announced roster)``; see the module
    docstring. Touched by the task's one driving thread at a time (the pack
    on the build, the provider on the intake that follows), so unlocked."""

    def __init__(self) -> None:
        self._latest: Optional[frozenset[str]] = None
        self._announced: Optional[frozenset[str]] = None

    def note_built(self, names: Iterable[str]) -> None:
        """Record the roster the pack just composed."""
        self._latest = frozenset(names)

    def take_new(self) -> tuple[str, ...]:
        """The names in the latest roster that the announced roster lacks,
        sorted; the announced roster then becomes the latest. ``()`` on a
        first sighting, on no roster, or when nothing was added."""
        latest = self._latest
        if latest is None:
            return ()
        announced = self._announced
        self._announced = latest
        if announced is None:
            return ()
        return tuple(sorted(latest - announced))

    @property
    def latest(self) -> Optional[frozenset[str]]:
        return self._latest


def new_skills_note(names: tuple[str, ...]) -> str:
    """The recorded note text for ``names`` (non-empty)."""
    listed = ", ".join(names)
    return (
        "New skills are available via the `skill` tool this turn: "
        f"{listed}. They are in its roster now; call `skill` with a name "
        "to load one."
    )


def new_skills_reminder_provider(
    peek: Callable[[str, str], Optional[Any]],
) -> Any:
    """The ``turn_intake`` provider over the host's task-local ``peek``; see
    the module docstring."""

    def provider(view: RecallView) -> tuple[Reminder, ...]:
        task_id = view.task_id
        if not task_id:
            return ()
        roster = peek(str(task_id), ROSTER_SLOT)
        if roster is None:
            return ()
        added = roster.take_new()
        if not added:
            return ()
        return (Reminder(text=new_skills_note(added), origin="system"),)

    return provider
