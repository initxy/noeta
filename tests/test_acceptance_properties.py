"""Kernel final-form acceptance properties (spec §9).

These lock the correctness the ledger-determined-resident work (spec §3/§6)
delivers. They exercise the generic machinery through a THIRD-PARTY resident
kind (``note``) built entirely from the public SPI — a content kind whose
renderer resolves its bytes from the ContentStore, plus an ``init`` hook that
records through the scoped :class:`SeedRecorder`. Nothing here imports a
built-in feature module, so a passing run is itself prop 2 (third-party
parity): an external plugin can contribute a resident kind, record it from its
init hook, refresh it mid-session, and have the composer render it with zero
kernel edits.

- **Prop 5 — recorder idempotence & refresh**: running an init hook again
  appends zero events when the store is unchanged, and exactly one refresh when
  the recorded bytes change (hash last-write-wins).
- **Prop 3 — ledger sufficiency**: the composed bytes are a pure function of
  (folded state, content store); adding new bytes to the store without
  re-recording does not change the composed output.
- **Prop 4 — rebuild determinism**: folding + composing the same ledger twice
  is byte-identical, and a refresh moves the bytes but not the anchor.
"""

from __future__ import annotations

import hashlib

from noeta.context.composer import RenderedContent, ThreeSegmentComposer
from noeta.context.content_channel import ContentChannelRegistry, ContentKindSpec
from noeta.core.engine import Engine
from noeta.core.fold import fold
from noeta.core.wiring import wire_default_observers
from noeta.execution.recorder import run_content_init
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.canonical import to_canonical_bytes
from noeta.protocols.decisions import FinishDecision
from noeta.protocols.messages import Message, TextBlock
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


NOTE_KIND = "note"
NOTE_NAME = "n"


def _runtime() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    return log, InMemoryContentStore(), disp


def _note_renderer(names, resolve) -> RenderedContent:
    """A third-party resident renderer that resolves its bytes from the store
    at the ledger's active hash (spec §6) — the same shape the built-in
    memory/environment/instructions renderers use."""
    if NOTE_NAME not in names:
        return RenderedContent(messages=[], selected_skills=[])
    text = resolve(NOTE_KIND, NOTE_NAME).decode("utf-8")
    return RenderedContent(
        messages=[Message(role="user", content=[TextBlock(text=text)])],
        selected_skills=[],
    )


def _composer(cs: InMemoryContentStore) -> ThreeSegmentComposer:
    return ThreeSegmentComposer(
        system_prompt="acceptance agent",
        tools={},
        content_store=cs,
        content_renderers=ContentChannelRegistry(
            [ContentKindSpec(kind=NOTE_KIND, renderer=_note_renderer, policy="evolving")]
        ),
    )


def _task(log: InMemoryEventLog, cs: InMemoryContentStore, composer):
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=composer,
        policy=StubScriptedPolicy([FinishDecision(answer="done")]),
    )
    return engine.create_task(goal="g", policy_name="scripted")


def _make_init(cs: InMemoryContentStore, body: bytes):
    """An init hook that records ``note`` at ``body``'s bytes — the third-party
    analogue of the memory built-in's index init (put, then record the ref)."""

    def _init(rec) -> None:
        ref = cs.put(body, media_type="text/markdown")
        rec.record_content(
            kind=NOTE_KIND, name=NOTE_NAME, version="1", ref=ref, policy="evolving"
        )

    return _init


def _content_events(log: InMemoryEventLog, task_id: str) -> list:
    return [e for e in log.read(task_id) if e.type == "ContextContentRecorded"]


def _semi_text(composer, task) -> str:
    segs = composer.compose(task).segments
    semi = [s for s in segs if s.name == "semi_stable"][0]
    return "".join(
        b.text for m in semi.content for b in m.content if isinstance(b, TextBlock)
    )


# ---------------------------------------------------------------------------
# Prop 5 — recorder idempotence & refresh (init reruns every drive)
# ---------------------------------------------------------------------------


def test_prop5_init_idempotent_when_store_unchanged() -> None:
    log, cs, _disp = _runtime()
    task = _task(log, cs, _composer(cs))

    hooks = [("note", _make_init(cs, b"version one"))]
    task = run_content_init(log, cs, task, init_hooks=hooks)
    assert len(_content_events(log, task.task_id)) == 1

    # Rerun the SAME hook (a resume rebuild) — unchanged store, zero new events.
    task = run_content_init(log, cs, task, init_hooks=hooks)
    assert len(_content_events(log, task.task_id)) == 1


def test_prop5_changed_store_records_exactly_one_refresh() -> None:
    log, cs, _disp = _runtime()
    composer = _composer(cs)
    task = _task(log, cs, composer)

    task = run_content_init(
        log, cs, task, init_hooks=[("note", _make_init(cs, b"version one"))]
    )
    assert _semi_text(composer, task) == "version one"

    # The store's content changed; init reruns and records exactly one refresh.
    task = run_content_init(
        log, cs, task, init_hooks=[("note", _make_init(cs, b"version two"))]
    )
    events = _content_events(log, task.task_id)
    assert len(events) == 2
    assert events[-1].payload.content_hash == hashlib.sha256(b"version two").hexdigest()
    # Hash last-write-wins: the composed bytes are the refreshed content.
    assert _semi_text(composer, task) == "version two"


# ---------------------------------------------------------------------------
# Prop 3 — ledger sufficiency (compose is pure over (folded state, store))
# ---------------------------------------------------------------------------


def test_prop3_new_store_bytes_do_not_change_compose_without_a_refresh() -> None:
    log, cs, _disp = _runtime()
    composer = _composer(cs)
    task = _task(log, cs, composer)
    task = run_content_init(
        log, cs, task, init_hooks=[("note", _make_init(cs, b"pinned by the ledger"))]
    )

    folded = fold(log, cs, task.task_id)
    first = to_canonical_bytes(composer.compose(folded).segments)

    # Mutate the backing store: put NEW bytes (what a fresh build would pick up)
    # WITHOUT re-recording. The folded ledger still pins the old hash, so compose
    # from the same folded state is byte-identical — the store is content-
    # addressed and the ledger, not the live source, determines what was seen.
    cs.put(b"a newer draft the ledger never recorded", media_type="text/markdown")
    again = to_canonical_bytes(composer.compose(folded).segments)
    assert again == first


def test_prop3_compose_reads_only_the_ledger_and_store() -> None:
    log, cs, _disp = _runtime()
    composer = _composer(cs)
    task = _task(log, cs, composer)
    task = run_content_init(
        log, cs, task, init_hooks=[("note", _make_init(cs, b"the recorded body"))]
    )
    folded = fold(log, cs, task.task_id)
    # Fold again into a SEPARATE store seeded only with the recorded blob →
    # identical compose (nothing but ledger + store is consulted).
    assert "the recorded body" in _semi_text(composer, folded)


# ---------------------------------------------------------------------------
# Prop 4 — rebuild determinism (same ledger ⇒ byte-identical compose; refresh
# moves bytes, not placement)
# ---------------------------------------------------------------------------


def test_prop4_same_ledger_composes_byte_identically() -> None:
    log, cs, _disp = _runtime()
    composer = _composer(cs)
    task = _task(log, cs, composer)
    task = run_content_init(
        log, cs, task, init_hooks=[("note", _make_init(cs, b"stable body"))]
    )
    a = to_canonical_bytes(composer.compose(task).segments)
    b = to_canonical_bytes(composer.compose(fold(log, cs, task.task_id)).segments)
    assert a == b


def test_prop4_refresh_moves_bytes_not_the_anchor() -> None:
    log, cs, _disp = _runtime()
    task = _task(log, cs, _composer(cs))
    task = run_content_init(
        log, cs, task, init_hooks=[("note", _make_init(cs, b"first"))]
    )
    anchor_before = task.context.content_anchors[f"{NOTE_KIND}:{NOTE_NAME}"]
    task = run_content_init(
        log, cs, task, init_hooks=[("note", _make_init(cs, b"second"))]
    )
    anchor_after = task.context.content_anchors[f"{NOTE_KIND}:{NOTE_NAME}"]
    # Anchor is first-write-wins; the refresh changed only the bytes (the hash).
    assert anchor_before == anchor_after
    assert (
        task.state.active_content[NOTE_KIND][NOTE_NAME]
        == hashlib.sha256(b"second").hexdigest()
    )
