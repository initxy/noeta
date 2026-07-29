"""Recorded reminder-provider seams — track A of the redesign (D7).

A **reminder_provider** is an *impure* contribution at a named recording seam:
given a narrow read-only :class:`RecallView`, it returns zero or more
:class:`Reminder` s that the seam records through the Engine's sole origin-writer
verb (``append_user_message``). Because the output is **recorded**, a provider
may be impure — query a vector DB, an external system, the memory store — and
resume/replay folds the reminder back **from the ledger, never re-invoking the
provider**. This is the seam that both re-expresses noeta's built-in memory
auto-recall and opens RAG-backed memory plugins; one design closes both (D7).

Two seams in v1 (``task_wake`` / ``subtask_result`` are deferred until a real
tenant demands them):

* :data:`TURN_INTAKE` — a user message being recorded (the goal / follow-up turn).
* :data:`TASK_SEED` — task creation (the opening seed of a new task).

Contract, load-bearing and pinned by the characterization goldens
(``tests/test_recall_intake_order_characterization.py``):

* Providers run **before** the incoming message enters the ledger (they are
  allowed to read live state), but their reminders are recorded **after** the
  incoming turn, so the transcript reads *message then reminder(s)*.
* Multiple providers on one seam run in ``(plugin, name)`` order.
* A provider **raise fails the turn loudly** — no silent skip. A provider that
  prefers degradation catches internally.
* ``origin ∈ {system, memory}``; the reminder rides the user channel and each
  adapter renders it (Anthropic wraps host injections in ``<system-reminder>``).

The seam is deliberately a small set of plain functions rather than an
"injector" interface — the same rule-of-two restraint the earlier memory seam
kept: the abstraction hardens when the second/third seam grows real providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from noeta.protocols.messages import Block, MessageOrigin, TextBlock


__all__ = [
    "TURN_INTAKE",
    "TASK_SEED",
    "REMINDER_SEAMS",
    "Reminder",
    "RecallView",
    "ReminderProvider",
    "SeamProvider",
    "ReminderProviderRegistry",
    "build_recall_view",
    "run_reminder_providers",
    "record_intake_reminders",
]


#: The user-message intake seam (a goal / follow-up turn being recorded).
TURN_INTAKE = "turn_intake"
#: The task-creation seam (the opening seed of a new task).
TASK_SEED = "task_seed"
#: The v1 seam catalogue. Widening it (``task_wake`` / ``subtask_result``) is a
#: matter of adding a constant and a recording call site, not a mechanism change.
REMINDER_SEAMS: tuple[str, ...] = (TURN_INTAKE, TASK_SEED)

#: The origins a recorded reminder may carry (D7). ``system`` = host-injected
#: context; ``memory`` = cross-task recall. Anything else is a forged tag.
_REMINDER_ORIGINS: frozenset[str] = frozenset({"system", "memory"})


@dataclass(frozen=True, slots=True)
class Reminder:
    """One recorded reminder: ``text`` recorded through the origin-writer seam.

    ``origin`` must be ``system`` or ``memory`` — the recording path is the
    single writer of ``Message.origin`` (a forged marker in model/tool output is
    just text), and a reminder that is neither host-system nor recalled-memory
    has no legitimate author tag.
    """

    text: str
    origin: MessageOrigin

    def __post_init__(self) -> None:
        if self.origin not in _REMINDER_ORIGINS:
            raise ValueError(
                f"Reminder.origin {self.origin!r} illegal; expected one of "
                f"{sorted(_REMINDER_ORIGINS)}"
            )


@dataclass(frozen=True, slots=True)
class RecallView:
    """Narrow, read-only projection a reminder provider sees at a seam.

    Deliberately small: the task id, the incoming message blocks, a
    ``TaskState`` projection, and the workspace path — enough for recall /
    retrieval, nothing that lets a provider reach into the Engine. A provider is
    impure over *external* systems (that is the point) but reads only these
    fields of the task.
    """

    #: The task's id (``None`` when the seam fires before a task id exists).
    task_id: Optional[str]
    #: The incoming user message's content blocks (goal / follow-up turn).
    message: tuple[Block, ...]
    #: A ``TaskState`` projection (the folded narrow-sense task state), or
    #: ``None`` when unavailable at this seam.
    task_state: Optional[Any]
    #: The session workspace path, or ``None``.
    workspace_path: Optional[Path]

    @property
    def text(self) -> str:
        """The incoming message's concatenated ``TextBlock`` text — the recall key.

        Newline-joined in block order; images ride the message but never drive
        retrieval (the same rule the memory recall key has always used).
        """
        return "\n".join(b.text for b in self.message if isinstance(b, TextBlock))


#: A reminder provider: the narrow view -> zero or more reminders. May be impure
#: (its output is recorded); a raise propagates and fails the turn loudly.
ReminderProvider = Callable[[RecallView], Iterable[Reminder]]


@dataclass(frozen=True, slots=True)
class SeamProvider:
    """One provider bound to a seam, carrying its ``(plugin, name)`` sort key."""

    plugin: str
    name: str
    provider: ReminderProvider


class ReminderProviderRegistry:
    """Seam name -> the providers to run, ordered ``(plugin, name)``.

    Built once at session construction from the activated plugins' resolved
    ``reminder_provider`` contributions. The composer-side reminder registry
    (track B) is its compose-time sibling; this one is the recording-time seam
    table. Unknown seams resolve to an empty tuple (no providers, no crash).
    """

    def __init__(self, providers_by_seam: Mapping[str, Sequence[SeamProvider]]) -> None:
        table: dict[str, tuple[ReminderProvider, ...]] = {}
        for seam, entries in providers_by_seam.items():
            ordered = sorted(entries, key=lambda e: (e.plugin, e.name))
            table[seam] = tuple(e.provider for e in ordered)
        self._by_seam = table

    def providers(self, seam: str) -> tuple[ReminderProvider, ...]:
        """The ordered providers for ``seam`` (empty when none are registered)."""
        return self._by_seam.get(seam, ())

    def seams(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_seam))


def build_recall_view(
    task: Any,
    content: Sequence[Block],
    *,
    workspace_path: Optional[Path] = None,
) -> RecallView:
    """Project ``task`` + the incoming ``content`` into a :class:`RecallView`.

    Reads the task defensively (``getattr``) so a caller that has only an opaque
    task handle — e.g. before the full task is materialised — still gets a legal
    view; a provider that needs a field the seam could not supply sees ``None``.
    """
    return RecallView(
        task_id=getattr(task, "task_id", None),
        message=tuple(content),
        task_state=getattr(task, "state", None),
        workspace_path=workspace_path,
    )


def run_reminder_providers(
    view: RecallView, providers: Sequence[ReminderProvider]
) -> list[Reminder]:
    """Run every provider in the given order, concatenating their reminders.

    Providers are impure by contract; a raise **propagates** (fails the turn
    loudly — no silent skip). The order is the caller's (already sorted
    ``(plugin, name)`` when it comes from a :class:`ReminderProviderRegistry`).
    """
    out: list[Reminder] = []
    for provider in providers:
        out.extend(provider(view))
    return out


def record_intake_reminders(
    engine: Any,
    task: Any,
    *,
    content: list[Block],
    lease_id: str,
    providers: Sequence[ReminderProvider],
    trace_id: Optional[str] = None,
    origin: Optional[MessageOrigin] = None,
    workspace_path: Optional[Path] = None,
) -> Any:
    """Record an incoming user turn plus every provider's reminders, in order.

    The load-bearing sequence (pinned by the recall-intake characterization
    goldens): build the view, run the providers **first** (impure, before the
    ledger is touched), then record the incoming turn with the caller's
    ``origin``, then record each reminder as its own follow-up turn tagged with
    the reminder's ``origin`` — through ``engine.append_user_message``, the
    Engine's sole origin-writer seam.

    No providers (or providers that all return nothing) ⇒ exactly the plain
    ``append_user_message`` ledger bytes, so a session with no reminder providers
    is byte-identical to a bare append. Resume folds the recorded reminders back
    from the ledger and **never** re-enters this function — the providers run
    once, at the live turn boundary, and never again.
    """
    view = build_recall_view(task, content, workspace_path=workspace_path)
    reminders = run_reminder_providers(view, providers)
    task = engine.append_user_message(
        task, content=content, lease_id=lease_id, trace_id=trace_id, origin=origin
    )
    for reminder in reminders:
        task = engine.append_user_message(
            task,
            content=[TextBlock(text=reminder.text)],
            lease_id=lease_id,
            trace_id=trace_id,
            origin=reminder.origin,
        )
    return task
