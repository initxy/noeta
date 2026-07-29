"""Tests for the three new-surface example manifest plugins (M5).

One exemplar per new extension surface opened by the SDK-extensibility redesign
(``docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md``):

* ``redaction``          — a ``tool_result_transform`` (D9): pure ``ToolResult
  -> ToolResult``, applied inside the ToolRuntime before recording, so the
  secret never lands in the ledger or content store (acceptance 10).
* ``checklist-reminder`` — a compose-time ``reminder`` (track B / D8): a pure
  function of the composer's ``ReminderView`` projection, rendered at the
  dynamic-suffix tail.
* ``memory-recall``      — a RAG-style ``reminder_provider`` (track A / D7): an
  impure (recorded) provider at the ``turn_intake`` seam, with a stub retriever.

Each is checked at the manifest boundary (loads / lists / resolves through
``load_plugin_set``) and at the runtime-contract boundary (the resolved callable
behaves as its surface's runtime registry expects). Tests may reach runtime
internals for the registries; the *examples* stay on the public surface (verified
by ``tests/test_public_surface.py``).
"""

from __future__ import annotations

import dataclasses
import importlib.util
from pathlib import Path

from noeta.client.plugin_set import load_plugins
from noeta.context.reminders import ReminderSpec, default_reminder_registry, ReminderView
from noeta.execution.reminders import (
    RecallView,
    ReminderProviderRegistry,
    SeamProvider,
    TURN_INTAKE,
    run_reminder_providers,
)
from noeta.protocols.decisions import ToolCall
from noeta.protocols.messages import TextBlock
from noeta.protocols.tool import ToolResult
from noeta.runtime.tool import ToolRuntime
from noeta.storage.memory import InMemoryContentStore, InMemoryEventLog


_PLUGINS = Path(__file__).resolve().parents[1] / "examples" / "plugins"


def _load(name: str):
    path = _PLUGINS / name / "plugin.py"
    spec = importlib.util.spec_from_file_location(f"_ex_{name.replace('-', '_')}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pset(name: str):
    return load_plugins(builtins=False, modules=[str(_PLUGINS / name / "plugin.py")])


# ===========================================================================
# redaction — tool_result_transform (D9)
# ===========================================================================


_SECRET = "sk-TOP-SECRET-abcdefgh12345"


def test_redaction_lists_the_transform_and_priority():
    pset = _pset("redaction")
    assert pset.names() == ("redaction",)
    listed = pset.contributions("tool_result_transform")
    assert [(p, c.name, c.params.get("priority")) for p, c in listed] == [
        ("redaction", "scrub-secrets", 50)
    ]
    # A tool_result_transform is per-agent (wiring plane); it resolves through
    # activation_transforms, not process_hooks.
    tmap = pset.activation_transforms()
    (prio, name, fn) = tmap["redaction"][0]
    assert (prio, name) == (50, "scrub-secrets") and callable(fn)
    assert pset.process_hooks() == ((), ())


def test_redaction_is_pure_and_scrubs_secrets():
    mod = _load("redaction")
    clean = ToolResult(success=True, output={"note": "nothing here"}, summary="ok")
    assert mod.scrub_secrets(clean) == clean  # non-secret content untouched
    leaky = ToolResult(
        success=True, output={"token": _SECRET}, summary=f"got {_SECRET}"
    )
    scrubbed = mod.scrub_secrets(leaky)
    assert _SECRET not in scrubbed.summary
    assert _SECRET not in str(scrubbed.output)
    assert mod.REDACTED in scrubbed.summary
    # Deterministic: same input → identical output (the transform's contract).
    assert mod.scrub_secrets(leaky) == scrubbed


def test_redaction_through_toolruntime_leaves_no_secret_durable():
    """Acceptance 10, at the ToolRuntime boundary the example is meant to sit in."""
    mod = _load("redaction")
    log = InMemoryEventLog()
    store = InMemoryContentStore()
    rt = ToolRuntime(
        event_log=log, content_store=store, tool_result_transforms=(mod.scrub_secrets,)
    )

    class _Leaky:
        name = "leak"
        risk_level = "low"
        input_schema = {"type": "object", "additionalProperties": True}

        def invoke(self, a, c):  # noqa: ARG002
            return ToolResult(success=True, output={"token": _SECRET}, summary=_SECRET)

    result = rt.invoke(
        _Leaky(), ToolCall(tool_name="leak", arguments={}, call_id="c1"),
        task_id="t", lease_id="l", trace_id="tr",
    )
    assert _SECRET not in result.summary
    recorded = log.read("t")[1]
    assert recorded.type == "ToolResultRecorded"
    for env in log.read("t"):
        assert _SECRET not in str(env.payload)
    body = store.get(recorded.payload.output_ref)
    assert body is None or _SECRET.encode() not in body


# ===========================================================================
# checklist-reminder — compose-time reminder (track B / D8)
# ===========================================================================


def _todos(n: int, status: str = "pending"):
    return tuple({"id": str(i), "content": f"t{i}", "status": status} for i in range(n))


def test_checklist_reminder_lists_with_priority():
    pset = _pset("checklist-reminder")
    listed = pset.contributions("reminder")
    assert [(p, c.name, c.params.get("priority")) for p, c in listed] == [
        ("checklist-reminder", "long-checklist", 400)
    ]


def test_checklist_reminder_is_pure_over_the_projection():
    mod = _load("checklist-reminder")
    # Short / finished lists render nothing; a long unfinished list nudges.
    assert mod.long_checklist_reminder(ReminderView(todos=_todos(mod.THRESHOLD))) is None
    assert (
        mod.long_checklist_reminder(ReminderView(todos=_todos(mod.THRESHOLD, "completed")))
        is None
    )
    text = mod.long_checklist_reminder(ReminderView(todos=_todos(mod.THRESHOLD + 2)))
    assert text is not None and "unfinished items" in text


def test_checklist_reminder_renders_at_the_tail_after_the_builtins():
    mod = _load("checklist-reminder")
    registry = default_reminder_registry(
        extra=[ReminderSpec("long-checklist", 400, mod.long_checklist_reminder)]
    )
    rendered = registry.render_all(ReminderView(todos=_todos(mod.THRESHOLD + 2)))
    # priority 400 > the three built-ins (100/200/300), so it is the last item.
    assert "unfinished items" in rendered[-1]


# ===========================================================================
# memory-recall — RAG reminder_provider (track A / D7)
# ===========================================================================


def test_memory_recall_lists_provider_with_its_seam():
    pset = _pset("memory-recall")
    listed = pset.contributions("reminder_provider")
    assert [(p, c.name, c.params.get("seams")) for p, c in listed] == [
        ("memory-recall", "rag-recall", ("turn_intake",))
    ]
    # A reminder_provider is not identity-plane and not a transform.
    assert pset.identity_activations()["memory-recall"].tools == ()
    assert pset.activation_transforms() == {}


def test_stub_retriever_is_deterministic():
    mod = _load("memory-recall")
    hits = mod.RETRIEVER.query("how do we deploy to prod")
    assert hits and hits[0][0] == "deploy"
    # Deterministic ordering (score desc, id asc) — same query, same result.
    assert mod.RETRIEVER.query("how do we deploy to prod") == hits
    assert mod.RETRIEVER.query("nothing relevant zzz") == []


def test_memory_recall_provider_recalls_on_hit_and_is_silent_otherwise():
    mod = _load("memory-recall")
    registry = ReminderProviderRegistry(
        {TURN_INTAKE: [SeamProvider("memory-recall", "rag-recall", mod.rag_recall)]}
    )
    hit_view = RecallView(
        task_id="t1",
        message=(TextBlock(text="how do we deploy to prod?"),),
        task_state=None,
        workspace_path=None,
    )
    reminders = run_reminder_providers(hit_view, registry.providers(TURN_INTAKE))
    assert len(reminders) == 1
    assert reminders[0].origin == "memory"
    assert "staging gate" in reminders[0].text

    miss_view = dataclasses.replace(hit_view, message=(TextBlock(text="unrelated zzz"),))
    assert run_reminder_providers(miss_view, registry.providers(TURN_INTAKE)) == []
