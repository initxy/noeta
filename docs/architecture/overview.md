# Architecture overview

A top-down tour of Noeta in one screen: how the two packages stack, what runs
during a turn, and where the extension points sit. Each section ends with a link
to the page that goes deep. For "what is X" questions start at
[Concepts](../concepts/index.md); for signatures, the
[SDK reference](../reference/sdk.md).

| Deep dive | Answers |
| --- | --- |
| [Packages and boundaries](packages.md) | Why two wheels, one namespace, and what the import rules forbid |
| [State and writers](state-and-writers.md) | How "state = fold(log)" is enforced rather than promised |
| [Extension planes](extension-planes.md) | The 16 surfaces, the loader, and what stays locked |

## The stack

Noeta is two libraries sharing one PEP 420 `noeta.` namespace.
**`noeta-runtime`** is the pure kernel — Engine, fold, snapshot, the
Worker/Dispatcher/Lease machinery, the context composer — and carries no
capability implementation and no HTTP client. **`noeta-sdk`** is the thin client
you import, plus `noeta.builtins`, where every official capability lives as a
plugin.

The load-bearing rule: **nothing statically imports `noeta.builtins`**. The only
doorway is the plugin loader's dynamic `ref` resolution, enforced by
import-linter in CI. Because every vendor adapter lives in that band, provider
neutrality is structural — the kernel *cannot* reach one.
→ [Packages and boundaries](packages.md)

## Ground truth

A task's ground truth is its append-only event log. State is computed by folding
that log, never stored as a first-class copy, which is what makes a crashed
worker recoverable and a six-month-old task resumable.

Two mechanisms keep the promise honest: state is cut into four typed slices with
exactly one writer each, and every EventLog append is fenced by an active lease,
so only one worker can write a task's stream at a time.
→ [State and writers](state-and-writers.md) ·
[Event sourcing](../concepts/event-sourcing.md)

## One step, and the waiting between

The **Engine** advances one task by one step — compose → decide → dispatch —
looping internally over `tool_calls` decisions so one call covers a whole turn.
It knows nothing of workers, the dispatcher, or HTTP.

The **Dispatcher** owns scheduling: enqueue, lease granting, wake delivery,
stale reclamation. A **Worker** leases a task, folds its log, drives one step,
and releases. The drain loop ships as the library primitive
`noeta.runtime.worker.WorkerLoop` — nothing launches it for you.

Between steps a task suspends on a wake condition — a human answer, a timer, a
subtask, an external event — and costs nothing while it sleeps. The match is held
durably, delivered at lease time, and committed by a `TaskWoken` write;
at-least-once delivery plus idempotent consumption gives exactly-once resume,
fenced to a single worker.
→ [Engine & execution](../concepts/engine-execution.md) ·
[Wake & resume](../concepts/wake-resume.md) ·
[WorkerLoop reference](../reference/worker-loop.md)

## Context assembly

Per step the `ThreeSegmentComposer` assembles the model's View from folded state
in three segments ordered by volatility — `stable_prefix`, `semi_stable`,
`dynamic_suffix` — keeping the prefix byte-stable so the provider's KV cache
survives. Compaction is a recorded event, not an in-place edit, so the originals
stay on the stream.
→ [Composer & cache](../concepts/composer-and-cache.md)

## What you can extend

Everything open is an `Options` field or a plugin contribution, across three
planes: **identity** (tools, agents, prompt fragments, content kinds, policy,
control tools — these enter the durable agent identity), **wiring** (guards,
observers, provider, reminders, session packs — process or host scope), and
**host** (MCP servers, skills, sandbox providers).

Noeta's own capabilities ride the identical path, so the extension surface is
exercised by every default agent on every run. The Engine main loop, the lease
protocol, and the composer stay locked.
→ [Extension planes](extension-planes.md) ·
[Write a plugin](../how-to/write-a-plugin.md)

## Deployment shape

The default is single-host: a SQLite file and a resident worker pool in one
process. Multi-host is a storage-adapter swap — point at Postgres and several
host processes share one database with in-transaction lease fencing. The Engine
does not change either way, because any process that can read the store can
rebuild any task by folding.
→ [Deploy a worker](../how-to/deploy-worker.md) ·
[Known limitations](../operations/limitations.md)

## Where to go next

- [Concepts](../concepts/index.md) — the vocabulary, one page per idea
- [SDK reference](../reference/sdk.md) · [Plugins reference](../reference/plugins.md)
- [Known limitations](../operations/limitations.md) — where the design stops
- [`docs/adr/`](https://github.com/initxy/noeta/tree/main/docs/adr) — the rationale per decision
