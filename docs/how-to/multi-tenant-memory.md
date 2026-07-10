# Multi-tenant memory

**Goal:** run one resident `Client` whose sessions belong to different end
users, with each tenant's long-term memory in its own store — recall, the
memory tools, and background consolidation all scoped per tenant.

**Before you start:** you understand the SDK from [Your first
agent](../tutorials/first-agent.md) and Memory v2 (the policy prompt, recall,
and consolidation described in `docs/adr/memory-consolidation.md`).

## The two seams

The SDK stays tenancy-agnostic — it knows tasks, not users. Two host-side
seams let your backend decide the task → tenant mapping:

1. **Per-task memory-root resolution** — `HostConfig.memory_root_resolver`,
   a `(task_id) → Path | None` callable. When set, every consumer of the
   memory-root chain resolves through it first: the engine build (memory tool
   pack + resident index), goal-time recall, and `Client.memory_root`.
   Returning `None` falls back to the existing chain
   (`memory_dir` > `global_memory_dir` > `~/.noeta/memories`).
2. **Consolidation digest scoping** — `run_consolidation(...,
   include_task=...)`, a predicate over root-session task ids, so one
   curation pass digests only one tenant's sessions.

Single-tenant hosts change nothing: no resolver and no filter reproduce
today's behaviour exactly.

## Wire the resolver

```python
from pathlib import Path
from noeta.sdk import Client, HostConfig, Options

TENANT_ROOTS = Path("/var/lib/myapp/memories")  # one subdir per tenant
task_tenants: dict[str, str] = {}               # task_id → tenant, your DB in production

def memory_root_for(task_id: str) -> Path | None:
    tenant = task_tenants.get(task_id)
    return TENANT_ROOTS / tenant if tenant else None

client = Client(
    options,
    provider=provider,
    workspace_dir=workspace,
    host_config=HostConfig(
        event_log=event_log, content_store=content_store, dispatcher=dispatcher,
        memory_root_resolver=memory_root_for,
    ),
)
```

The resolver must be **cheap, total, and deterministic** per task id — it
runs on the engine-build and goal paths, and a resumed task must resolve the
same store.

## Map the first turn

A brand-new session's task id is minted inside `start` / `seed_start`, so a
plain dict lookup cannot know it yet. Two strategies:

- **Derive from the durable record.** The genesis `TaskCreated` and the
  `TaskHostBound` workspace binding are written *before* the first turn's
  recall runs, so the resolver can read the session's workspace off the
  ledger and map workspace → tenant (natural when each tenant has its own
  workspace directory).
- **Seed, register, then drive.** If your backend drives turns itself (the
  async `seed_start` → `drive_seeded` split), register the mapping between
  the two calls — the seed lease is still held, so no worker can resolve the
  engine before the mapping exists. With this strategy the *seed-time* recall
  of the very first goal still falls back to the host-level chain; point the
  fallback (`global_memory_dir`) at an empty directory so it recalls nothing.

Engines are cached per resolved root: two tenants never share a cached
engine's memory store, and the resolver-fallback slot is shared exactly as
before.

## Consolidate per tenant

Run one pass per tenant. The debounce marker lives in each tenant's root, so
tenants debounce independently, and `on_seeded` hands you the curation task's
id **before** any worker can claim it — register it in your mapping so the
curation agent's `memory_*` tools land in the same tenant store:

```python
from noeta.sdk import run_consolidation

def consolidate_tenant(tenant: str) -> bool:
    root = TENANT_ROOTS / tenant
    return run_consolidation(
        client,
        memory_root=root,
        include_task=lambda tid: task_tenants.get(tid) == tenant,
        on_seeded=lambda tid: task_tenants.__setitem__(tid, tenant),
    )
```

`include_task` rejects sessions outside the tenant's scope entirely — they
neither consume the session cap nor count as omitted — and the digest header
states that it was restricted to a host-selected subset.

## Caveats

- The memory store is filesystem material: per-tenant isolation is directory
  isolation, not an authorization layer. Keep the roots under a directory
  your service owns.
- A memory-enabled agent whose task the resolver cannot map falls back to the
  shared chain. In a strict multi-tenant deployment, treat the fallback root
  as a quarantine directory (empty, monitored) rather than a real store.
- Delegated sub-agents resolve with their own task ids. The official presets
  enable memory only on `main`, so children never touch the store; if you
  enable memory on a custom sub-agent, make your resolver map child ids too
  (e.g. walk the ledger to the root session).
