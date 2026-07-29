# `memory-recall` — a RAG-style `reminder_provider` (track A)

A first-party example [manifest plugin](../../../docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md)
demonstrating the new **`reminder_provider`** surface — **track A** (spec D7):
*recorded injection*. At a named recording seam the provider receives a narrow
read-only `RecallView` (task id, incoming message, a `TaskState` projection,
workspace path) and returns zero or more reminders. Because the output is
**recorded** through the Engine's sole origin-writer seam, the provider **may be
impure** — query a vector DB, an external RAG index, a memory store — and
resume/replay folds the reminder back **from the ledger, never re-invoking the
provider**. This is the seam that opens RAG-backed memory plugins; noeta's own
memory auto-recall is the built-in tenant of the same surface.

## The stub retriever

This example wires a **`StubRetriever`** — a tiny in-process keyword-overlap
"vector store" over a fixed corpus — in place of a real embedding index, so the
shape is complete and offline-runnable. Swap `RETRIEVER` in
[`plugin.py`](./plugin.py) for a real client and the provider is
production-shaped (`query(text) -> [(id, text, score)]`).

The provider is bound to the `turn_intake` seam (a user message being recorded);
`view.text` is the recall key. On a hit it returns one reminder tagged
`origin="memory"`; on a miss it stays silent. Multiple providers on one seam run
in `(plugin, name)` order; a provider raise fails the turn loudly.

## Public-surface note

A shipped plugin returns noeta's `Reminder(text, origin)`. To keep this example
on the `noeta.sdk` public surface (the recorded-reminder type is a runtime
internal), it returns a **structural** stand-in — a `(text, origin)` pair the
recording seam reads by duck typing. `origin` is `"memory"` (recalled cross-task
material); the seam is the single writer of that author tag.

It is unit-tested against the runtime `ReminderProviderRegistry` in
[`tests/test_example_new_surfaces.py`](../../../tests/test_example_new_surfaces.py).

## Note on wiring

`reminder_provider` is a **per-agent activation** surface (spec D6): name the
plugin in `Options.plugins` (or an `AgentDefinition.plugins`) and the Client binds
its providers to that agent's recording seams. At `turn_intake` they run after
noeta's own memory recall (which the host binds to a live store) and before the
incoming turn enters the ledger. The end-to-end path — activate, provider runs
once, reminder is *recorded* — is pinned by
[`tests/test_plugin_wiring_contract.py`](../../../tests/test_plugin_wiring_contract.py).

## Verify the shipped manifest

```bash
python -m noeta.sdk.plugin_check examples/plugins/memory-recall
```
