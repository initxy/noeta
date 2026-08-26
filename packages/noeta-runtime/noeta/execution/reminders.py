"""Recorded reminder-provider seams: at a named seam a provider reads a narrow
:class:`RecallView` and returns :class:`Reminder` s that the seam records through
the Engine's sole origin-writer verb — and/or :class:`ResidentActivation` s that
it records as content-channel residents through ``Engine.record_content``.

Because the output is **recorded**, a provider may be impure — query a vector DB,
an external system, the memory store — and resume/replay folds the reminder back
from the ledger instead of re-invoking the provider. The load-bearing contract:
providers run *before* the incoming message enters the ledger (so they may read
live state) while their reminders are recorded *after* it, several providers on
one seam run in ``(plugin, name)`` order, and a provider that raises fails the
turn loudly — one that prefers degradation catches internally.

The seam is a small set of plain functions rather than an "injector" interface;
the abstraction earns its keep once a second and third seam grow real providers.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    ClassVar,
    Iterable,
    Mapping,
    Optional,
    Sequence,
    Union,
)

from noeta.protocols.decisions import TaskStatePatch
from noeta.protocols.events import CONTENT_DRIFT_POLICIES
from noeta.protocols.messages import Block, MessageOrigin, TextBlock


__all__ = [
    "TURN_INTAKE",
    "TASK_SEED",
    "REMINDER_SEAMS",
    "IntakeGoalPrelude",
    "IntakeItem",
    "Reminder",
    "ResidentActivation",
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
#: The seam catalogue. Widening it costs a constant and a recording call site,
#: not a mechanism change.
REMINDER_SEAMS: tuple[str, ...] = (TURN_INTAKE, TASK_SEED)

#: The origins a recorded reminder may carry. ``system`` = host-injected
#: context; ``memory`` = cross-task recall. Anything else is a forged tag.
_REMINDER_ORIGINS: frozenset[str] = frozenset({"system", "memory"})


@dataclass(frozen=True, slots=True)
class Reminder:
    """One reminder to record through the origin-writer seam.

    ``origin`` must be ``system`` or ``memory``: the recording path is the single
    writer of ``Message.origin`` (a forged marker in model or tool output is just
    text), and a reminder that is neither host-system nor recalled-memory has no
    legitimate author tag.
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
class ResidentActivation:
    """One content-channel resident to activate at the seam.

    The track-C sibling of :class:`Reminder`: where a reminder is recorded as a
    follow-up *turn*, an activation is recorded as a ``(kind, name)`` resident
    through the Engine's ``record_content`` verb — the seam ``put()`` s ``body``
    and emits ``ContextContentRecorded``, so a provider never touches the
    ledger or the content store. What enters context is then whatever the
    kind's renderer composes from the recorded bytes, placed by the activation
    anchor (semi_stable pre-loop, else inside the dynamic suffix at the point
    of activation) and re-hung after a compaction summary — the right shape
    for content that must enter a task once and stay.

    ``refresh=False`` (the default) is activate-once: a ``(kind, name)``
    already active in this task, at ANY hash, appends nothing. ``refresh=True``
    records a refresh whenever the hash moved (the seed recorder's default).
    """

    kind: str
    name: str
    body: bytes
    version: str = "1"
    policy: str = "evolving"
    media_type: str = "text/markdown"
    refresh: bool = False

    def __post_init__(self) -> None:
        if not self.kind or not self.name:
            raise ValueError(
                "ResidentActivation.kind and ResidentActivation.name must be "
                "non-empty"
            )
        if self.policy not in CONTENT_DRIFT_POLICIES:
            raise ValueError(
                f"ResidentActivation.policy {self.policy!r} illegal; expected "
                f"one of {CONTENT_DRIFT_POLICIES}"
            )


#: What a provider may return at a seam: a follow-up turn or a resident
#: activation. The seam records activations before reminder turns, so a
#: resident activated on a goal anchors right after that goal. A reminder is
#: read by duck typing (``text`` + ``origin``); an activation must be the typed
#: :class:`ResidentActivation`.
IntakeItem = Union[Reminder, ResidentActivation]


@dataclass(frozen=True, slots=True)
class RecallView:
    """Narrow, read-only projection a reminder provider sees at a seam.

    Deliberately small — enough for recall and retrieval, nothing that lets a
    provider reach into the Engine. A provider is impure over *external* systems
    (that is the point) but reads only these fields of the task.
    """

    #: ``None`` when the seam fires before a task id exists.
    task_id: Optional[str]
    message: tuple[Block, ...]
    #: The folded narrow-sense task state, or ``None`` when this seam cannot
    #: supply it.
    task_state: Optional[Any]
    workspace_path: Optional[Path]

    @property
    def text(self) -> str:
        """The incoming message's concatenated ``TextBlock`` text — the recall key.

        Newline-joined in block order; images ride the message but never drive
        retrieval.
        """
        return "\n".join(b.text for b in self.message if isinstance(b, TextBlock))


#: A reminder provider: the narrow view -> zero or more intake items (reminder
#: turns and/or resident activations). May be impure (its output is recorded);
#: a raise propagates and fails the turn loudly.
ReminderProvider = Callable[[RecallView], Iterable[IntakeItem]]


@dataclass(frozen=True, slots=True)
class SeamProvider:
    """One provider bound to a seam, carrying its ``(plugin, name)`` sort key."""

    plugin: str
    name: str
    provider: ReminderProvider


class ReminderProviderRegistry:
    """Seam name -> the providers to run, ordered ``(plugin, name)``.

    Built once at session construction from the activated plugins' resolved
    ``reminder_provider`` contributions. An unknown seam resolves to an empty
    tuple — no providers, no crash.
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

    Reads the task defensively so a caller holding only an opaque task handle —
    before the full task is materialised, say — still gets a legal view; a
    provider that needs a field the seam could not supply sees ``None``.
    """
    return RecallView(
        task_id=getattr(task, "task_id", None),
        message=tuple(content),
        task_state=getattr(task, "state", None),
        workspace_path=workspace_path,
    )


def run_reminder_providers(
    view: RecallView, providers: Sequence[ReminderProvider]
) -> list[IntakeItem]:
    """Run every provider in the given order, concatenating their items.

    A raise **propagates** and fails the turn — no silent skip. The order is the
    caller's, already ``(plugin, name)``-sorted when it comes from a
    :class:`ReminderProviderRegistry`.
    """
    out: list[IntakeItem] = []
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
    """Record an incoming user turn plus every provider's items, in order.

    The sequence is load-bearing and pinned by goldens: run the providers first
    (impure, before the ledger is touched), record the incoming turn, then
    record each :class:`ResidentActivation` through ``Engine.record_content``
    (its anchor is the rolling-history length at fold time, so the resident
    renders right after the goal), then each :class:`Reminder` as its own
    follow-up turn tagged with the reminder's ``origin``. Providers that all
    return nothing leave exactly the plain ``append_user_message`` bytes, so a
    session without reminder providers is byte-identical to a bare append.
    Resume folds the recorded turns and activations back from the ledger and
    never re-enters this function.
    """
    view = build_recall_view(task, content, workspace_path=workspace_path)
    items = run_reminder_providers(view, providers)
    task = engine.append_user_message(
        task, content=content, lease_id=lease_id, trace_id=trace_id, origin=origin
    )
    for item in items:
        if isinstance(item, ResidentActivation):
            task = engine.record_content(
                task,
                kind=item.kind,
                name=item.name,
                version=item.version,
                body=item.body,
                media_type=item.media_type,
                policy=item.policy,
                refresh=item.refresh,
                lease_id=lease_id,
                trace_id=trace_id,
            )
    # Reminders are read by duck typing (``text`` + ``origin``) — the contract
    # third-party providers have always relied on; only an activation has to
    # be the typed ``ResidentActivation``.
    for item in items:
        if not isinstance(item, ResidentActivation):
            task = engine.append_user_message(
                task,
                content=[TextBlock(text=item.text)],
                lease_id=lease_id,
                trace_id=trace_id,
                origin=item.origin,
            )
    return task


@dataclass(frozen=True, slots=True)
class IntakeGoalPrelude:
    """``send_goal`` prelude routing the goal through the ``turn_intake`` seam.

    Drop-in sibling of :class:`noeta.runtime.worker.AppendMessagePrelude` for a
    session with ``turn_intake`` providers, so a resumed turn gets the same
    intake the opening turn got; a goal whose providers all return nothing
    ledgers exactly the plain-prelude bytes.

    The field order is load-bearing: attachments seed BEFORE the goal as their
    own ``origin="system"`` messages and never feed the recall key, and the
    skill-activation patch lands AFTER it. Only the goal append itself is routed
    through the recording seam (goal, then the providers' resident activations,
    then their reminder turns).

    ``providers`` is the FULL composed tuple for this turn — the host owns the
    composition rule; the kernel never distinguishes one provider from another.
    """

    content: list[Block]
    providers: tuple[ReminderProvider, ...] = ()
    origin: Optional[MessageOrigin] = None
    attachment_texts: tuple[str, ...] = ()
    activate_skills: tuple[str, ...] = ()

    #: Providers read live state then append, which is safe at seed time.
    durable_at_seed: ClassVar[bool] = True

    def __call__(self, engine: Any, task: Any, *, lease_id: str) -> Any:
        for text in self.attachment_texts:
            engine.append_user_message(
                task, content=[TextBlock(text=text)], lease_id=lease_id,
                origin="system",
            )
        task = record_intake_reminders(
            engine, task, content=self.content, lease_id=lease_id,
            providers=self.providers,
            origin=self.origin,
        )
        if self.activate_skills:
            task = engine.apply_state_patch(
                task,
                patch=TaskStatePatch(activate_skills=list(self.activate_skills)),
                lease_id=lease_id,
            )
        return task
