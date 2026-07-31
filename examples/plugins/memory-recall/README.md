# `memory-recall` — RAG-backed recall on the way into the ledger

An example manifest plugin that contributes one provider on the
**`reminder_provider`** surface. As a user turn is being recorded, the provider
looks up relevant cross-task notes and injects them alongside it — the standing
constraints a team would otherwise have to repeat in every prompt.

## Why this surface exists

A `reminder_provider` runs at a named **recording seam**. It receives a narrow
read-only view of the incoming turn (task id, the message, a `TaskState`
projection, the workspace path) and returns zero or more reminders. That output
goes through the Engine's sole origin-writer seam, so it lands in the ledger.

Being recorded is what buys the provider the right to be **impure**. It can
query a vector store, a RAG index, a memory service — anything with a network
call and a mutable corpus behind it. Resume and replay fold the reminder back
*from the ledger* and never re-invoke the provider, so a retrieval that returns
different results tomorrow cannot rewrite yesterday's task.

If your injection is pure and deterministic, use the `reminder` surface instead:
it composes at request time and costs nothing durable.

noeta's own memory auto-recall is a tenant of this same surface, not a
privileged path — the `memory` built-in plugin contributes a provider on
`turn_intake` exactly like this one does.

## The stub retriever

`StubRetriever` scores fixed corpus notes by keyword overlap and returns the top
two. It stands in for an embedding index so the example runs offline and
deterministically. Point `RETRIEVER` in [`plugin.py`](./plugin.py) at a real
client — anything with a `query(text) -> [(id, text, score)]` shape — and nothing
else changes.

`TOP_K` is capped at two on purpose: recall competes with the turn's own content
for the model's attention, and a long recall block reliably wins that fight.

## Seam, ordering, and failure

The provider binds to the **`turn_intake`** seam — the moment a user message is
being recorded, and the only point at which injected material can enter the
ledger alongside the turn it belongs to. `view.text` is the recall key.

Multiple providers on one seam run in `(plugin, name)` order. A provider that
raises fails the turn loudly; a provider that prefers degradation catches
internally and returns nothing. For retrieval, silence is usually the right
default.

Reminders are tagged `origin="memory"` because the seam is the single writer of
that author tag — recalled material must stay distinguishable from what the user
actually said.

To keep this example clear of runtime internals it returns a structural
`(text, origin)` pair the seam reads by duck typing; a shipped plugin returns
noeta's own `Reminder`.

## Loading and activating it

`reminder_provider` is a **per-agent** surface: loading the plugin is not enough,
an agent must also activate it by name.

```python
import dataclasses
from noeta.sdk import Client, load_plugins, presets

pset = load_plugins(builtins=False, modules=["examples/plugins/memory-recall/plugin.py"])
base = presets.main_options()
options = dataclasses.replace(base, plugins=tuple(base.plugins) + ("memory-recall",))
client = Client(options, plugins=pset)
```

The activation key is the builder name — `PluginBuilder("memory-recall")` — not
the filename. The [reference host](../../reference-host/README.md) activates its
plugins the same way.

A real distribution ships the `[tool.noeta]` manifest mirrored into
[`noeta-plugin.toml`](./noeta-plugin.toml) as wheel package data, plus an entry
point in the `noeta.plugins` group (see [`pyproject.toml`](./pyproject.toml));
a host then discovers it with `load_plugins(entry_points=True)`.

## Files

- [`plugin.py`](./plugin.py) — `StubRetriever`, the `rag_recall` provider, and
  the single-file `PluginBuilder` manifest.
- [`noeta-plugin.toml`](./noeta-plugin.toml) — the shipped static manifest the
  loader reads without importing plugin code.
- [`pyproject.toml`](./pyproject.toml) — the packaging convention (not built here).

Keep the two manifests in agreement with:

```bash
python -m noeta.sdk.plugin_check examples/plugins/memory-recall
```
