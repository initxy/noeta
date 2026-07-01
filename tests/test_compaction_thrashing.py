"""⑥ compaction thrashing detection (D6.1/D6.2/D6.3).

Detects "several compactions land within a few turns of each other" — a single
large file / large tool output repeatedly refilling the window so compaction
spins without freeing headroom — and injects an ``origin="system"`` reminder
suggesting a different read strategy.

The detection state lives on ``ContextState`` and is folded ONLY from the
``Compacted`` event stream (single writer ``fold._on_compacted``), measuring the
turn-gap between consecutive compactions in ``GovernanceState.iterations`` (the
per-compose turn counter folded from ``ContextPlanComposed``). It is therefore
deterministic and reconstructs identically on resume — no in-memory counter.

Complementary to the anti-spiral guard (compaction with NO boundary progress →
FailDecision): thrashing is the opposite case — compaction makes progress but
the freed window is immediately refilled.
"""

from __future__ import annotations

from typing import Any

from noeta.context.composer import ThreeSegmentComposer
from noeta.core.fold import _HANDLERS, _THRASH_CLOSE_TURNS, _THRASH_RUN_LIMIT
from noeta.protocols.events import (
    CompactedPayload,
    ContextPlanComposedPayload,
    EventEnvelope,
)
from noeta.protocols.messages import Message, TextBlock
from noeta.protocols.task import Task, TaskState
from noeta.protocols.tool import ToolContext, ToolResult
from noeta.protocols.values import ContentRef
from noeta.storage.memory import InMemoryContentStore

_STORE = InMemoryContentStore()


def _ref(h: str) -> ContentRef:
    return ContentRef(hash=h, size=1, media_type="application/json")


def _compose(task: Task) -> None:
    """Advance the fold-maintained turn counter by one (one Engine step)."""
    env = EventEnvelope.build(
        task_id=task.task_id,
        type="ContextPlanComposed",
        payload=ContextPlanComposedPayload(plan_ref=_ref("sha256:plan")),
    )
    _HANDLERS["ContextPlanComposed"](task, env, _STORE)


def _compact(task: Task, *, boundary: int) -> None:
    env = EventEnvelope.build(
        task_id=task.task_id,
        type="Compacted",
        payload=CompactedPayload(
            summary_ref=_ref("sha256:summary"),
            boundary_count=boundary,
            replaced_count=boundary,
            composer_version="three_segment.v5",
        ),
    )
    _HANDLERS["Compacted"](task, env, _STORE)


def _advance(task: Task, turns: int) -> None:
    for _ in range(turns):
        _compose(task)


# ---------------------------------------------------------------------------
# D6.1/D6.2 — fold latches / clears the flag from the Compacted stream
# ---------------------------------------------------------------------------


def test_single_compaction_is_not_thrashing() -> None:
    """One compaction (no prior marker) never trips the flag."""
    task = Task(task_id="t-1")
    _compose(task)
    _compact(task, boundary=5)

    assert task.context.last_compaction_marker == 1
    assert task.context.close_compaction_run == 0
    assert task.context.compaction_thrashing is False


def test_M_consecutive_close_compactions_latch_thrashing() -> None:
    """``_THRASH_RUN_LIMIT`` consecutive close (gap ``<= _THRASH_CLOSE_TURNS``)
    refills in a row latch ``compaction_thrashing``."""
    task = Task(task_id="t-1")

    # Baseline compaction at turn 1 — establishes the marker, run still 0.
    _compose(task)
    _compact(task, boundary=5)
    assert task.context.compaction_thrashing is False

    # ``_THRASH_RUN_LIMIT`` close refills, each within ``_THRASH_CLOSE_TURNS``
    # turns of the previous compaction. The flag latches on the last one.
    boundary = 5
    for i in range(_THRASH_RUN_LIMIT):
        _advance(task, _THRASH_CLOSE_TURNS)  # gap == K (still "close")
        boundary += 1
        _compact(task, boundary=boundary)
        expected_run = i + 1
        assert task.context.close_compaction_run == expected_run
        assert task.context.compaction_thrashing is (
            expected_run >= _THRASH_RUN_LIMIT
        )

    assert task.context.compaction_thrashing is True


def test_distant_compaction_clears_run_and_flag() -> None:
    """Once latched, a single distant compaction (gap ``> _THRASH_CLOSE_TURNS``)
    resets the run to 0 and clears the flag — the composer reminder disappears on
    its own (D6.3)."""
    task = Task(task_id="t-1")
    _compose(task)
    _compact(task, boundary=5)
    boundary = 5
    for _ in range(_THRASH_RUN_LIMIT):
        _advance(task, _THRASH_CLOSE_TURNS)
        boundary += 1
        _compact(task, boundary=boundary)
    assert task.context.compaction_thrashing is True

    # A compaction further than K turns away breaks the streak.
    _advance(task, _THRASH_CLOSE_TURNS + 1)
    _compact(task, boundary=boundary + 1)

    assert task.context.close_compaction_run == 0
    assert task.context.compaction_thrashing is False


def test_non_close_compaction_never_latches() -> None:
    """Compactions spaced further than K turns apart never accumulate a run."""
    task = Task(task_id="t-1")
    boundary = 5
    for _ in range(_THRASH_RUN_LIMIT + 2):
        _advance(task, _THRASH_CLOSE_TURNS + 1)  # always distant
        boundary += 1
        _compact(task, boundary=boundary)
        assert task.context.close_compaction_run == 0
        assert task.context.compaction_thrashing is False


def test_fold_resume_reproduces_thrashing_state() -> None:
    """The flag is folded purely from the event stream, so re-folding the same
    events (the resume path: rebuild Task from the EventLog) yields identical
    state — no in-memory counter leaks across the boundary."""

    def _replay() -> Task:
        task = Task(task_id="t-1")
        _compose(task)
        _compact(task, boundary=5)
        boundary = 5
        for _ in range(_THRASH_RUN_LIMIT):
            _advance(task, _THRASH_CLOSE_TURNS)
            boundary += 1
            _compact(task, boundary=boundary)
        return task

    a = _replay()
    b = _replay()
    assert a.context.compaction_thrashing is True
    assert b.context.compaction_thrashing == a.context.compaction_thrashing
    assert b.context.close_compaction_run == a.context.close_compaction_run
    assert b.context.last_compaction_marker == a.context.last_compaction_marker


# ---------------------------------------------------------------------------
# D6.3 — composer injects an origin="system" reminder when the flag is set
# ---------------------------------------------------------------------------


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.risk_level = "low"
        self.input_schema = {"type": "object", "additionalProperties": True}

    def invoke(
        self, arguments: dict[str, Any], ctx: ToolContext
    ) -> ToolResult:  # pragma: no cover - never invoked by composer
        raise NotImplementedError


def _composer() -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="you are a helpful agent",
        tools={"echo": _FakeTool("echo")},
        content_store=InMemoryContentStore(),
    )


def _task() -> Task:
    task = Task(task_id="t-1", state=TaskState())
    task.runtime.messages.append(
        Message(role="user", content=[TextBlock(text="hi")])
    )
    return task


def _system_reminders(view: Any) -> list[str]:
    return [
        m.content[0].text
        for m in view.segments[2].content
        if m.origin == "system"
        and m.content
        and isinstance(m.content[0], TextBlock)
    ]


def test_reminder_appended_when_thrashing_flag_set() -> None:
    """With ``compaction_thrashing`` set, a trailing ``origin="system"`` reminder
    suggests a different read strategy. Plain text only — the
    ``<system-reminder>`` tag is the adapter's job (provider-neutral)."""
    task = _task()
    task.context.compaction_thrashing = True

    view = _composer().compose(task)

    reminders = _system_reminders(view)
    assert len(reminders) == 1
    text = reminders[0]
    # Advisory tone, not an error: it points at the read strategy.
    assert "read" in text.lower()
    assert "<system-reminder>" not in text
    last = view.segments[2].content[-1]
    assert last.role == "user"
    assert last.origin == "system"


def test_no_reminder_when_flag_unset() -> None:
    """Default (flag False) yields the verbatim history — no reminder."""
    task = _task()
    history = list(task.runtime.messages)

    view = _composer().compose(task)

    assert view.segments[2].content == history
    assert _system_reminders(view) == []


def test_reminder_not_written_to_runtime_messages() -> None:
    """The reminder is a compose-time View product: it must NOT enter
    ``runtime.messages`` and must not survive across composes."""
    task = _task()
    task.context.compaction_thrashing = True
    before = len(task.runtime.messages)

    composer = _composer()
    composer.compose(task)
    composer.compose(task)

    assert len(task.runtime.messages) == before
    assert all(m.origin != "system" for m in task.runtime.messages)


def test_reminder_lands_in_dynamic_suffix_only() -> None:
    """The reminder rides the volatile dynamic_suffix segment only: the
    stable_prefix / semi_stable hashes (the prompt-cache prefix) are byte-
    identical with and without the flag; only the dynamic_suffix hash rotates."""
    composer = _composer()
    off = _task()
    on = _task()
    on.context.compaction_thrashing = True

    v_off = composer.compose(off)
    v_on = composer.compose(on)

    assert v_off.segments[0].segment_hash == v_on.segments[0].segment_hash
    assert v_off.segments[1].segment_hash == v_on.segments[1].segment_hash
    assert v_off.segments[2].segment_hash != v_on.segments[2].segment_hash
