"""Track A (D7) — recorded reminder-provider seams.

Covers the four load-bearing properties of the ``turn_intake`` / ``task_seed``
seam mechanism (``noeta.execution.reminders``):

* **ledger origin** — a provider's ``Reminder`` lands in the ledger as a real
  message carrying its ``origin``, recorded through the Engine's sole
  origin-writer verb, ordered *incoming turn, then reminder(s)*;
* **ordering** — multiple providers on one seam run in ``(plugin, name)`` order
  (:class:`ReminderProviderRegistry`);
* **loud failure** — a provider raise propagates and fails the turn (no silent
  skip);
* **no re-invoke on resume** — the reminder is recorded once; folding the ledger
  (resume/replay) reproduces it **without** calling the provider again
  (call-count test).

The engine setup mirrors ``tests/test_message_origin.py`` — a real
:class:`Engine` over the in-memory storage triple — so the assertions read the
same ledger the production path writes.
"""

from __future__ import annotations

import pytest

from noeta.core.engine import Engine
from noeta.core.fold import fold, messages_from_appended
from noeta.execution.reminders import (
    TASK_SEED,
    TURN_INTAKE,
    RecallView,
    Reminder,
    ReminderProviderRegistry,
    SeamProvider,
    build_recall_view,
    record_intake_reminders,
    run_reminder_providers,
)
from noeta.policies.stub import StubFinishPolicy
from noeta.protocols.messages import Message, TextBlock
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)
from noeta.testing.composer import trivial_three_segment


def _engine_setup() -> tuple[Engine, InMemoryEventLog, InMemoryContentStore, str, str]:
    content_store = InMemoryContentStore()
    dispatcher = InMemoryDispatcher()
    event_log = InMemoryEventLog(lease_validator=dispatcher)
    engine = Engine(
        event_log=event_log,
        content_store=content_store,
        composer=trivial_three_segment(content_store),
        policy=StubFinishPolicy(answer="ok"),
    )
    task = engine.create_task(goal="reminder seam", policy_name="p")
    dispatcher.enqueue(task.task_id)
    lease = dispatcher.lease(worker_id="w-reminders")
    assert lease is not None
    return engine, event_log, content_store, task.task_id, lease.lease_id


def _ledger_user_turns(
    log: InMemoryEventLog, cs: InMemoryContentStore, task_id: str
) -> list[Message]:
    out: list[Message] = []
    for env in log.read(task_id):
        if env.type == "MessagesAppended":
            out.extend(messages_from_appended(env, cs))
    return [m for m in out if m.role == "user"]


# ---------------------------------------------------------------------------
# Reminder value / RecallView
# ---------------------------------------------------------------------------


def test_reminder_rejects_illegal_origin() -> None:
    """``origin`` must be ``system`` or ``memory`` — anything else raises."""
    Reminder(text="ok", origin="system")
    Reminder(text="ok", origin="memory")
    with pytest.raises(ValueError):
        Reminder(text="nope", origin="human")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        Reminder(text="nope", origin=None)  # type: ignore[arg-type]


def test_recall_view_text_is_the_textblock_concatenation() -> None:
    """The recall key is the newline-joined ``TextBlock`` text; images are ignored."""
    view = build_recall_view(
        object(),
        [TextBlock(text="first"), TextBlock(text="second")],
    )
    assert view.text == "first\nsecond"
    assert view.task_id is None  # opaque task -> defensive None


# ---------------------------------------------------------------------------
# ledger origin
# ---------------------------------------------------------------------------


def test_reminder_lands_in_ledger_with_origin() -> None:
    """A provider's reminder is recorded as a user turn carrying its origin,
    after the incoming turn."""
    engine, log, cs, task_id, lease_id = _engine_setup()
    task = fold(log, cs, task_id)

    def provider(view: RecallView) -> tuple[Reminder, ...]:
        return (Reminder(text=f"recalled: {view.text}", origin="memory"),)

    record_intake_reminders(
        engine,
        task,
        content=[TextBlock(text="the goal")],
        lease_id=lease_id,
        providers=(provider,),
        origin="system",
    )

    turns = _ledger_user_turns(log, cs, task_id)
    assert len(turns) == 2
    # incoming turn first, with the caller's origin
    assert turns[0].content[0].text == "the goal"
    assert turns[0].origin == "system"
    # reminder second, with the reminder's own origin
    assert turns[1].content[0].text == "recalled: the goal"
    assert turns[1].origin == "memory"
    # fold reproduces the same order + tags
    rebuilt = fold(log, cs, task_id)
    user = [m for m in rebuilt.runtime.messages if m.role == "user"]
    assert [m.origin for m in user] == ["system", "memory"]


def test_no_reminders_is_a_plain_append() -> None:
    """A provider that yields nothing ⇒ exactly the plain single-turn append."""
    engine, log, cs, task_id, lease_id = _engine_setup()
    task = fold(log, cs, task_id)

    def provider(view: RecallView) -> tuple[Reminder, ...]:
        return ()

    record_intake_reminders(
        engine,
        task,
        content=[TextBlock(text="just the goal")],
        lease_id=lease_id,
        providers=(provider,),
        origin=None,
    )
    turns = _ledger_user_turns(log, cs, task_id)
    assert len(turns) == 1
    assert turns[0].origin is None


# ---------------------------------------------------------------------------
# ordering — (plugin, name)
# ---------------------------------------------------------------------------


def test_providers_run_in_plugin_name_order() -> None:
    """The registry orders providers by ``(plugin, name)`` regardless of
    registration order, and the recorded reminders follow that order."""
    calls: list[str] = []

    def make(tag: str):
        def provider(view: RecallView) -> tuple[Reminder, ...]:
            calls.append(tag)
            return (Reminder(text=tag, origin="system"),)

        return provider

    # Deliberately out of order; sort key is (plugin, name).
    registry = ReminderProviderRegistry(
        {
            TURN_INTAKE: [
                SeamProvider("zeta", "a", make("zeta.a")),
                SeamProvider("alpha", "b", make("alpha.b")),
                SeamProvider("alpha", "a", make("alpha.a")),
            ]
        }
    )
    providers = registry.providers(TURN_INTAKE)

    engine, log, cs, task_id, lease_id = _engine_setup()
    task = fold(log, cs, task_id)
    record_intake_reminders(
        engine,
        task,
        content=[TextBlock(text="g")],
        lease_id=lease_id,
        providers=providers,
    )

    assert calls == ["alpha.a", "alpha.b", "zeta.a"]
    turns = _ledger_user_turns(log, cs, task_id)
    # incoming turn + three reminders, reminders in sorted order
    assert [t.content[0].text for t in turns[1:]] == ["alpha.a", "alpha.b", "zeta.a"]
    # unknown seam -> no providers, no crash
    assert registry.providers(TASK_SEED) == ()


# ---------------------------------------------------------------------------
# loud failure
# ---------------------------------------------------------------------------


def test_provider_raise_fails_the_turn_loudly() -> None:
    """A provider raise propagates — no silent skip."""

    def boom(view: RecallView) -> tuple[Reminder, ...]:
        raise RuntimeError("provider exploded")

    with pytest.raises(RuntimeError, match="provider exploded"):
        run_reminder_providers(build_recall_view(object(), [TextBlock(text="x")]), (boom,))

    engine, log, cs, task_id, lease_id = _engine_setup()
    task = fold(log, cs, task_id)
    with pytest.raises(RuntimeError, match="provider exploded"):
        record_intake_reminders(
            engine,
            task,
            content=[TextBlock(text="x")],
            lease_id=lease_id,
            providers=(boom,),
        )


# ---------------------------------------------------------------------------
# no re-invoke on resume (call-count test)
# ---------------------------------------------------------------------------


def test_resume_does_not_reinvoke_providers() -> None:
    """The provider runs once at the live turn; folding the ledger (resume)
    reproduces the recorded reminder WITHOUT calling the provider again."""
    engine, log, cs, task_id, lease_id = _engine_setup()
    task = fold(log, cs, task_id)

    count = {"n": 0}

    def provider(view: RecallView) -> tuple[Reminder, ...]:
        count["n"] += 1
        return (Reminder(text="recorded once", origin="memory"),)

    record_intake_reminders(
        engine,
        task,
        content=[TextBlock(text="the goal")],
        lease_id=lease_id,
        providers=(provider,),
    )
    assert count["n"] == 1

    # Resume: fold the ledger back several times (what a resume/replay does).
    for _ in range(3):
        rebuilt = fold(log, cs, task_id)
        recalled = [
            m
            for m in rebuilt.runtime.messages
            if m.role == "user" and m.origin == "memory"
        ]
        assert len(recalled) == 1
        assert recalled[0].content[0].text == "recorded once"

    # The provider was never invoked again — the reminder rides the ledger.
    assert count["n"] == 1
