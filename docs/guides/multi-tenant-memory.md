# Per-tenant memory

Serve many end users from one `Client` and keep each tenant's long-term memory
in its own directory — recall, the memory tools and consolidation all stay
within the tenant. Noeta knows tasks, not users, so your backend supplies the
task → tenant mapping.

| `HostConfig` field / argument | Signature | What it scopes |
| --- | --- | --- |
| `memory_root_resolver` | `(task_id) -> Path \| None` | which memory directory a task reads and writes |
| `mcp_scope_resolver` | `(task_id) -> str \| None` | which tasks may share a pooled MCP connection |
| `run_consolidation(include_task=...)` | `(task_id) -> bool` | which tasks one consolidation pass digests |

A single-tenant host sets none of these.

## Map tasks to tenant directories

```python
from pathlib import Path

from noeta.sdk import Client, HostConfig, presets
from noeta.sdk.providers import AnthropicProvider

TENANT_ROOTS = Path("/var/lib/myapp/memories")   # one subdirectory per tenant
task_tenants: dict[str, str] = {}                # task_id -> tenant; your DB in production

def memory_root_for(task_id: str) -> Path | None:
    tenant = task_tenants.get(task_id)
    return TENANT_ROOTS / tenant if tenant else None

client = Client(
    presets.with_consolidation_agent(presets.main_options()),
    provider=AnthropicProvider(),
    model="claude-sonnet-5",
    host_config=HostConfig(
        storage_path="/var/lib/myapp/noeta.sqlite",
        memory_root_resolver=memory_root_for,
        mcp_scope_resolver=task_tenants.get,     # tenant id as the MCP scope
    ),
)

print(client.memory_root(some_task_id))
```

```
/var/lib/myapp/memories/acme-corp
```

- The resolver must be cheap, never raise, and give the same answer for the same
  task id every time — it runs on every turn, and a resumed task must land on the
  same store.
- `None` falls back to the host-wide chain: `memory_dir` > `global_memory_dir` >
  `~/.noeta/memories`.
- Every turn builds its engine with the root its task resolves to, so two tenants
  never share a store.
- Without `mcp_scope_resolver`, two tenants whose MCP specs are identical share
  one connection — and a stateful server (a browser, a login) would carry one
  tenant's state into another's turn.

## Register the first turn

The task id is created inside `start` / `seed_start`, so a plain lookup can't know
it yet. Two ways around that:

- **Seed, register, then run.** Call `seed_start`, record
  `task_tenants[seeded.task_id] = tenant`, then `drive_seeded` or
  `dispatch_seeded`. No worker can build the turn's engine in between. Recall
  during the seed itself uses the fallback chain, so point `global_memory_dir`
  at an empty directory.
- **Derive it from the workspace.** Pass the tenant's workspace as
  `start(goal=..., workspace_dir=...)`. That path is recorded on the task before
  the first recall runs, so your resolver can read it back and map workspace →
  tenant.

## Consolidate per tenant

Run one consolidation pass per tenant. The client's options must include the
`__consolidation__` agent (`presets.with_consolidation_agent`, as above):

```python
from noeta.sdk import run_consolidation

def consolidate_tenant(tenant: str) -> bool:
    return run_consolidation(
        client,
        memory_root=TENANT_ROOTS / tenant,
        include_task=lambda tid: task_tenants.get(tid) == tenant,
        on_seeded=lambda tid: task_tenants.__setitem__(tid, tenant),
    )
```

- `on_seeded` gives you the consolidation task's id before any worker can pick it
  up — register it so its `memory_*` tools write to the same tenant.
- The debounce marker lives in each tenant's root, so tenants debounce independently.
- Tasks outside `include_task` are left out of the digest entirely.

If consolidation is the only writer, set `HostConfig(memory_read_only=True)`: the
agent gets `memory_read` and `memory_search` only, while the consolidation agent
keeps all four memory tools.

## Things to watch

- Isolation is directory isolation, not access control. Keep the roots under a
  directory your service owns.
- A task the resolver can't map falls back to the shared chain. In a strict
  deployment, make that fallback an empty, monitored directory.
- Subagents resolve with their own task ids. The official presets enable memory
  only on `main`; if you enable it on a custom subagent, map child ids too (e.g.
  walk up to the root task via `client.task_status(tid).parent_task_id`).

## Next

- [Deploy](deploy.md) — the worker pool consolidation runs on
- [MCP](mcp.md) — the per-turn connections `mcp_scope_resolver` partitions
- [ADR: memory consolidation](https://github.com/initxy/noeta/blob/main/docs/adr/memory-consolidation.md)
