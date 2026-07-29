# Reference host

A minimal but **real** Noeta host, assembled from the `noeta.sdk` public
surface only. It is the split spec's reference host
([`docs/implementation-specs/2026-07-26-sdk-only-repo-split.md`](../../docs/implementation-specs/2026-07-26-sdk-only-repo-split.md),
decision **D5**): the smallest program that stands up a durable,
plugin-extended, streaming agent the way an embedding product does — so it
serves three jobs at once:

- **Host-builder tutorial** — a worked example of the wiring a host owns.
- **Contract-test bed** — [`tests/test_reference_host.py`](../../tests/test_reference_host.py)
  drives it end-to-end; it stands in for the product after the agent moves to
  its own repo.
- **Integration bed** — the same seams the real product wires, in ~200 lines.

Everything it imports comes from `noeta.sdk`, `noeta.sdk.storage`, and
`noeta.presets`. It reaches **no** runtime internal — exactly the discipline the
split repo's import-linter enforces on the product. If the reference host can
build an agent, a third-party host can too.

## What it wires

| Concern | Public surface | In `host.py` |
| --- | --- | --- |
| Durable storage | `noeta.sdk.storage` — the sqlite triple | `SqliteEventLog` / `SqliteDispatcher` / `SqliteContentStore` over one file, injected via `HostConfig` |
| Token streaming | `HostConfig.delta_sink` + `StreamDelta` | `StdoutDeltaSink` — writes live token deltas to stdout |
| Manifest plugins | `load_plugin_set` → `PluginSet` | the example plugins are loaded, then handed to `Client(plugins=…)`, which wires them per the D6 effect-scoping rules (governance guards/observer process-wide; the `redaction` transform activated per-agent) |
| Plugin config | the environment (`apply_plugin_env`) | config is orthogonal to identity in the manifest mechanism, so the host injects it via env (points `protected-paths` at the workspace) |
| Agent identity | `noeta.presets.main_options()` | the official `main` agent as the base recipe |
| Session driving | `noeta.sdk.Client` | `ReferenceHost.run(goal)` drives one turn |

## Run the offline demo

```bash
python examples/reference-host/host.py
```

It drives one turn against a **network-free** scripted streaming provider,
printing the live token stream, then the persisted-event and plugin summary:

```
--- streaming (live token deltas) -------------------------
Hello from the Noeta reference host.
-----------------------------------------------------------
task:          task-…
status:        suspended        # the turn finished; the session awaits the next goal
deltas seen:   6
sqlite file:   /tmp/…/noeta.sqlite  (exists=True)
events stored: 14
loaded plugins:  ['approval-modes', 'git-checkpoint', 'protected-paths', 'redaction']
process guards:  ['approval_modes', 'protected_paths']
process observers: ['git_checkpoint']
activated:       ['fs', 'web', …, 'redaction']
```

`status: suspended` is the healthy multi-turn outcome: a finished turn suspends
on the next-goal handle (a live session waiting for the next message), and the
streamed answer is already durable in the event log.

## Swapping in a real provider

The host is **provider-agnostic** — `build_reference_host(provider=...)` takes
whatever satisfies `LLMProvider` (and, for streaming, the optional
`StreamingProvider` capability). The offline demo injects
`_ScriptedStreamingProvider`, a scripted double built from the public message
types. A production host deletes that and injects a real provider — one line,
nothing else changes:

```python
from noeta.sdk.providers import OpenAICompatProvider   # or an AnthropicProvider

host = build_reference_host(
    provider=OpenAICompatProvider(...),   # ← the only line that differs
    workspace_dir=Path("/srv/work"),
    db_path=Path("/var/lib/noeta/state.sqlite"),
)
```

Because a real provider that implements `complete_streaming` is a
`StreamingProvider`, the same `delta_sink` starts pushing real token deltas with
no further wiring. Point the sink at your transport (an SSE hub, a websocket)
instead of stdout, and the host streams to a browser.

## The plugins it loads

Loaded from [`examples/plugins/`](../plugins/) by explicit path (they are not
installed in this repo, so entry-point discovery does not apply). The Client
wires each per the [effect-scoping rules](../../docs/implementation-specs/2026-07-28-sdk-extensibility-redesign.md)
(spec D6):

- **`protected-paths`** — a `guard` that fences file writes to the workspace
  (governance, process-wide). The host points it at the workspace via
  `NOETA_PROTECTED_PATHS_ROOTS`.
- **`approval-modes`** — a `guard` implementing goose-style approval modes
  (governance, process-wide).
- **`git-checkpoint`** — an `observer` that snapshots the workspace around
  mutating tool calls (governance, process-wide).
- **`redaction`** — a `tool_result_transform` (per-agent) the host **activates**
  on the main agent, so tool results are scrubbed of secrets before recording.
- **`checklist-reminder`** — a compose-time `reminder` (per-agent, track B),
  activated on the main agent; it renders at the tail of the dynamic suffix.
- **`memory-recall`** — a recorded `reminder_provider` (per-agent, track A),
  activated on the main agent; it runs at the `turn_intake` seam and its output
  lands in the ledger, so resume folds it back without re-querying.

The three per-agent surfaces are all named in `default_activation()` — a
per-agent contribution fires only for an agent that opts in, while the governance
guards / observer apply process-wide once loaded. A name collision or a broken
plugin fails the build loudly, never a mid-session turn.

Plugin config rides the environment, and the host sets it inside
`plugin_env_scope(...)` — in force while the plugin modules import, restored
afterwards, so a host built against a temporary workspace does not leave that
path behind for the next one.
