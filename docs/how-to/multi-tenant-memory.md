# Multi-tenant memory

**Goal:** run one resident `Client` whose sessions belong to different end
users, with each tenant's long-term memory in its own store — recall, the
memory tools, and background consolidation all scoped per tenant.

**Before you start:** you understand the SDK from [Your first
agent](../tutorials/first-agent.md) and the curation pass described in
[Memory consolidation](https://github.com/initxy/noeta/blob/main/docs/adr/memory-consolidation.md).

## The two seams

The SDK is tenancy-agnostic: it knows tasks, not users. Two host-side seams
let your backend own the task → tenant mapping.

1. **Per-task memory-root resolution** — `HostConfig.memory_root_resolver`, a
   `(task_id) → Path | None` callable. Every consumer of the memory-root chain
   resolves through it first: the engine build (memory tool pack + resident
   index), goal-time recall, and `Client.memory_root(task_id)`. Returning
   `None` falls back to the host-level chain, `memory_dir` >
   `global_memory_dir` > `~/.noeta/memories`.
2. **Consolidation digest scoping** — `run_consolidation(...,
   include_task=...)`, a predicate over root-session task ids, so one curation
   pass digests only one tenant's sessions.

A single-tenant host sets neither and gets the host-level chain.

## Wire the resolver

```python
from pathlib import Path
from noeta.sdk import Client, HostConfig

TENANT_ROOTS = Path("/var/lib/myapp/memories")  # one subdirectory per tenant
task_tenants: dict[str, str] = {}               # task_id → tenant; your DB in production

def memory_root_for(task_id: str) -> Path | None:
    tenant = task_tenants.get(task_id)
    return TENANT_ROOTS / tenant if tenant else None

client = Client(
    options,
    provider=provider,
    workspace_dir=workspace,
    host_config=HostConfig(
        storage_path="/var/lib/myapp/noeta.sqlite",
        memory_root_resolver=memory_root_for,
    ),
)
```

The resolver must be **cheap, total, and deterministic** per task id — it runs
on the engine-build and goal paths, and a resumed task must resolve the same
store.

## Map the first turn

A new session's task id is minted inside `start` / `seed_start`, so a plain
dict lookup cannot know it yet. Two strategies:

- **Derive it from the durable record.** Pass the tenant's workspace as
  `start(goal=..., workspace_dir=...)`. The driver welds that absolute path
  onto the session's `TaskHostBound` event inside task creation — before the
  first turn's recall runs — so the resolver can read the workspace off the
  ledger and map workspace → tenant.
- **Seed, register, then drive.** If your backend drives turns itself (the
  `seed_start` → `drive_seeded` split), register the mapping between the two
  calls: the seed lease is held, so no worker can resolve the engine before the
  mapping exists. Seed-time recall and the seed-time resident index resolve
  through the host-level chain, so point the fallback (`global_memory_dir`) at
  an empty directory.

Engines are cached per resolved root, so two tenants never share a cached
engine's memory store.

## Consolidate per tenant

Run one pass per tenant. Register the `__consolidation__` agent on the recipe
first — `run_consolidation` seeds a root task under that reserved name:

```python
from noeta.sdk import presets, run_consolidation

options = presets.with_consolidation_agent(options)

def consolidate_tenant(tenant: str) -> bool:
    root = TENANT_ROOTS / tenant
    return run_consolidation(
        client,
        memory_root=root,
        include_task=lambda tid: task_tenants.get(tid) == tenant,
        on_seeded=lambda tid: task_tenants.__setitem__(tid, tenant),
    )
```

The debounce marker lives in each tenant's root, so tenants debounce
independently. `on_seeded` hands you the curation task's id **before** any
worker can claim it — register it in your mapping so the curation agent's
`memory_*` tools land in the same tenant store.

`include_task` rejects sessions outside the tenant's scope entirely — they
neither consume the session cap nor count as omitted — and the digest header
states that it was restricted to a host-selected subset.

## Caveats

- The memory store is filesystem material: per-tenant isolation is directory
  isolation, not an authorization layer. Keep the roots under a directory your
  service owns.
- A memory-enabled agent whose task the resolver cannot map falls back to the
  shared chain. In a strict multi-tenant deployment, treat the fallback root as
  a quarantine directory (empty, monitored) rather than a real store.
- Delegated subagents resolve with their own task ids. The official presets
  enable memory only on `main`, so children never touch the store; if you
  enable memory on a custom subagent, make your resolver map child ids too
  (e.g. walk the ledger to the root session).
