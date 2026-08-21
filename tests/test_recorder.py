"""The scoped session recorder — the one write verb a plugin's ``init`` hook
gets (spec §4.4/§4.5).

Covers :class:`SeedRecorder` (the kernel's concrete
:class:`~noeta.execution.session_pack.SessionRecorder`) and
:func:`run_content_init` directly, now that the three feature-named
``record_*`` seams are gone: the generic recorder is where the emit / gate /
fold recipe and the ``actor="plugin:<name>"`` provenance stamp live, and the
init loop is what both the driver's ``seed_start`` and the subtask drain run
over an engine's ``content_init_hooks``.
"""

from __future__ import annotations

from noeta.context.composer import ThreeSegmentComposer
from noeta.core.engine import Engine
from noeta.core.wiring import wire_default_observers
from noeta.execution.recorder import SeedRecorder, run_content_init
from noeta.policies.stub import StubScriptedPolicy
from noeta.protocols.decisions import FinishDecision
from noeta.storage.memory import (
    InMemoryContentStore,
    InMemoryDispatcher,
    InMemoryEventLog,
)


def _runtime() -> tuple[InMemoryEventLog, InMemoryContentStore, InMemoryDispatcher]:
    disp = InMemoryDispatcher()
    log = InMemoryEventLog(lease_validator=disp)
    wire_default_observers(log, disp)
    return log, InMemoryContentStore(), disp


def _task(log: InMemoryEventLog, cs: InMemoryContentStore):
    engine = Engine(
        event_log=log,
        content_store=cs,
        composer=ThreeSegmentComposer(
            system_prompt="recorder test agent", tools={}, content_store=cs
        ),
        policy=StubScriptedPolicy([FinishDecision(answer="done")]),
    )
    return engine.create_task(goal="g", policy_name="scripted")


def _content_events(log: InMemoryEventLog, task_id: str) -> list:
    return [e for e in log.read(task_id) if e.type == "ContextContentRecorded"]


def test_record_content_emits_and_activates_with_plugin_actor() -> None:
    log, cs, _disp = _runtime()
    task = _task(log, cs)
    ref = cs.put(b"index bytes", media_type="text/markdown")

    rec = SeedRecorder(log, cs, task, actor="plugin:memory")
    rec.record_content(
        kind="memory", name="index", version="1", ref=ref, policy="evolving"
    )

    events = _content_events(log, task.task_id)
    assert len(events) == 1
    payload = events[0].payload
    assert (payload.kind, payload.name, payload.version) == ("memory", "index", "1")
    assert payload.content_hash == ref.hash
    assert payload.policy == "evolving"
    # The kernel stamps the plugin provenance label; the single-writer invariant
    # is about the physical writer (this recorder), not the actor string.
    assert events[0].actor == "plugin:memory"
    assert rec.task.state.active_content["memory"] == {"index": ref.hash}


def test_record_content_noops_on_empty_triple() -> None:
    log, cs, _disp = _runtime()
    task = _task(log, cs)
    ref = cs.put(b"body", media_type="text/markdown")
    rec = SeedRecorder(log, cs, task, actor="plugin:x")

    rec.record_content(kind="", name="index", version="1", ref=ref, policy="evolving")
    rec.record_content(kind="k", name="", version="1", ref=ref, policy="evolving")

    assert _content_events(log, task.task_id) == []
    assert rec.task.state.active_content == {}


def test_record_content_is_first_only_for_an_active_name() -> None:
    log, cs, _disp = _runtime()
    task = _task(log, cs)
    ref = cs.put(b"body", media_type="text/markdown")
    rec = SeedRecorder(log, cs, task, actor="plugin:x")

    rec.record_content(kind="k", name="n", version="1", ref=ref, policy="pinned")
    rec.record_content(kind="k", name="n", version="1", ref=ref, policy="pinned")

    assert len(_content_events(log, task.task_id)) == 1


def test_record_content_refreshes_an_active_name_at_a_new_hash() -> None:
    # Default mode: a changed source records exactly one refresh (hash
    # last-write-wins) — the memory-index behavior.
    log, cs, _disp = _runtime()
    task = _task(log, cs)
    rec = SeedRecorder(log, cs, task, actor="plugin:x")

    first = cs.put(b"v1 bytes", media_type="text/markdown")
    second = cs.put(b"v2 bytes", media_type="text/markdown")
    rec.record_content(kind="k", name="n", version="1", ref=first, policy="evolving")
    rec.record_content(kind="k", name="n", version="1", ref=second, policy="evolving")

    assert len(_content_events(log, task.task_id)) == 2
    assert rec.task.state.active_content["k"]["n"] == second.hash


def test_record_content_refresh_false_is_first_write_wins() -> None:
    # Activate-once mode: once active at ANY hash, a later record with
    # DIFFERENT bytes appends nothing — the environment resident's guard
    # against its re-captured clock/git snapshot busting the prompt cache
    # on every resume.
    log, cs, _disp = _runtime()
    task = _task(log, cs)
    rec = SeedRecorder(log, cs, task, actor="plugin:environment")

    first = cs.put(b"snapshot at task start", media_type="text/markdown")
    second = cs.put(b"snapshot at resume", media_type="text/markdown")
    rec.record_content(
        kind="environment", name="workspace", version="3",
        ref=first, policy="evolving", refresh=False,
    )
    rec.record_content(
        kind="environment", name="workspace", version="3",
        ref=second, policy="evolving", refresh=False,
    )

    assert len(_content_events(log, task.task_id)) == 1
    assert rec.task.state.active_content["environment"]["workspace"] == first.hash


def test_record_content_refresh_false_still_makes_the_first_write() -> None:
    # First-write-wins still WRITES first: a not-yet-active resident records
    # normally under refresh=False.
    log, cs, _disp = _runtime()
    task = _task(log, cs)
    rec = SeedRecorder(log, cs, task, actor="plugin:environment")
    ref = cs.put(b"snapshot", media_type="text/markdown")

    rec.record_content(
        kind="environment", name="workspace", version="3",
        ref=ref, policy="evolving", refresh=False,
    )

    assert len(_content_events(log, task.task_id)) == 1
    assert rec.task.state.active_content["environment"]["workspace"] == ref.hash


def test_run_content_init_runs_hooks_in_order_and_stamps_each_plugin() -> None:
    log, cs, _disp = _runtime()
    task = _task(log, cs)

    def alpha(rec) -> None:
        ref = cs.put(b"alpha", media_type="text/markdown")
        rec.record_content(
            kind="alpha", name="a", version="1", ref=ref, policy="evolving"
        )

    def beta(rec) -> None:
        ref = cs.put(b"beta", media_type="text/markdown")
        rec.record_content(
            kind="beta", name="b", version="1", ref=ref, policy="evolving"
        )

    task = run_content_init(
        log, cs, task, init_hooks=[("alpha", alpha), ("beta", beta)]
    )

    events = _content_events(log, task.task_id)
    assert [(e.payload.kind, e.actor) for e in events] == [
        ("alpha", "plugin:alpha"),
        ("beta", "plugin:beta"),
    ]
    assert {k: set(v) for k, v in task.state.active_content.items()} == {
        "alpha": {"a"},
        "beta": {"b"},
    }


def test_run_content_init_is_idempotent_when_nothing_changed() -> None:
    log, cs, _disp = _runtime()
    task = _task(log, cs)

    def hook(rec) -> None:
        ref = cs.put(b"same bytes", media_type="text/markdown")
        rec.record_content(
            kind="k", name="n", version="1", ref=ref, policy="evolving"
        )

    task = run_content_init(log, cs, task, init_hooks=[("p", hook)])
    assert len(_content_events(log, task.task_id)) == 1

    # Resume re-runs every init hook; an unchanged resident appends nothing.
    task = run_content_init(log, cs, task, init_hooks=[("p", hook)])
    assert len(_content_events(log, task.task_id)) == 1


def test_run_content_init_empty_hooks_leaves_ledger_untouched() -> None:
    log, cs, _disp = _runtime()
    task = _task(log, cs)
    task = run_content_init(log, cs, task, init_hooks=())
    assert _content_events(log, task.task_id) == []
