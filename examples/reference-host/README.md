# Reference host

The smallest complete Noeta embedding host, assembled from the `noeta.sdk`
public surface alone. It stands up a durable, plugin-extended, streaming agent
the way a real embedding product does, in one readable file — so it serves as
both the host-builder tutorial and the contract-test bed that
[`tests/test_reference_host.py`](../../tests/test_reference_host.py) drives
end-to-end.

Everything it imports comes from `noeta.sdk`, `noeta.sdk.storage`, and
`noeta.presets`. It reaches no runtime internal, which is the point: if the
reference host can build an agent from the public surface, a third-party host
can too.

## What it wires

| Concern | Public surface | In `host.py` |
| --- | --- | --- |
| Durable storage | `noeta.sdk.storage` — the sqlite triple | `SqliteDispatcher` / `SqliteEventLog` / `SqliteContentStore` over one file, injected via `HostConfig` |
| Token streaming | `HostConfig.delta_sink` + `StreamDelta` | `StdoutDeltaSink` — writes live token deltas to stdout |
| Manifest plugins | `load_plugins` → `PluginSet` | the example plugins are loaded by path, then handed to `Client(plugins=…)`, which routes each contribution by its effect scope |
| Plugin config | the environment (`plugin_env_scope`) | a manifest keeps config orthogonal to identity, so the host injects it as environment variables before the plugin modules import |
| Agent identity | `noeta.presets.main_options()` | the official `main` agent as the base recipe |
| Session driving | `noeta.sdk.Client` | `ReferenceHost.run(goal)` drives one turn |

## Run the offline demo

```bash
python examples/reference-host/host.py
```

It drives one turn against a network-free scripted streaming provider, printing
the live token stream and then a summary of what was persisted and wired:

```
--- streaming (live token deltas) -------------------------
Hello from the Noeta reference host.
-----------------------------------------------------------
task:          task-522118d276d246ca9d589d946429b0ac
status:        suspended
deltas seen:   6
sqlite file:   /tmp/noeta-reference-host-…/noeta.sqlite  (exists=True)
events stored: 14
loaded plugins:  ['approval-modes', 'checklist-reminder', 'git-checkpoint', 'memory-recall', 'protected-paths', 'redaction']
process guards:  ['approval_modes', 'protected_paths']
process observers: ['git_checkpoint']
activated:       ['fs', 'web', 'todo_write', 'ask_user_question', 'skill_invocation', 'memory', 'mcp', 'redaction', 'checklist-reminder', 'memory-recall']
```

`status: suspended` is the healthy outcome for a turn that finished: the task
suspends on the next-goal handle, meaning a live session waiting for the next
message rather than a closed one. The streamed answer is already durable in the
event log by that point — the deltas were only a preview of it.

## Swapping in a real provider

`build_reference_host(provider=...)` takes anything satisfying `LLMProvider`,
and streams tokens if it also implements `complete_streaming` (the optional
`StreamingProvider` capability). The offline demo injects
`_ScriptedStreamingProvider`, a scripted double built from the public message
types. A production host injects a real provider instead — one argument,
nothing else changes:

```python
from noeta.sdk.providers import OpenAICompatProvider   # or AnthropicProvider

host = build_reference_host(
    provider=OpenAICompatProvider(base_url=..., api_key=...),
    workspace_dir=Path("/srv/work"),
    db_path=Path("/var/lib/noeta/state.sqlite"),
)
```

The same `delta_sink` then starts carrying real token deltas with no further
wiring. Point the sink at your transport — an SSE hub, a websocket — instead of
stdout, and the host streams to a browser.

## The plugins it loads

Loaded from [`examples/plugins/`](../plugins/) by explicit path, because they
are not installed and so entry-point discovery cannot find them. Each is wired
according to the scope of the surface it contributes to:

- **`protected-paths`** — a `guard` fencing file writes to a set of roots. The
  host points it at the session workspace via `NOETA_PROTECTED_PATHS_ROOTS`.
- **`approval-modes`** — a `guard` implementing operator-chosen tool-approval
  modes.
- **`git-checkpoint`** — an `observer` that snapshots the workspace around
  mutating tool calls, pointed at the workspace via `NOETA_GIT_CHECKPOINT_REPO`.
- **`redaction`** — a `tool_result_transform` that scrubs secrets out of tool
  results before they are recorded.
- **`checklist-reminder`** — a compose-time `reminder` rendered at the tail of
  the dynamic suffix.
- **`memory-recall`** — a `reminder_provider` bound to the `turn_intake` seam;
  its output is recorded, so resuming a task folds the recall back without
  re-querying.

The first three are governance and take effect for every agent in the process
the moment they load. The last three contribute per-agent surfaces, which fire
only for an agent that opts in — hence `default_activation()`, which the builder
folds into `Options.plugins` on top of the preset's own activations. A name
collision or a broken plugin fails the build loudly, never a mid-session turn.

Plugin config rides the environment, and the host sets it inside
`plugin_env_scope(...)` so the variables are in force exactly while the plugin
modules import and are restored afterwards. Without that, a host built against a
temporary workspace would leave a stale path behind for the next one.
