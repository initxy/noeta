# ADR-022: The model catalog is kernel data

- Status: accepted
- Date: 2026-10-29
- Refines: [ADR-019](019-tenant-limits-are-enforced-by-the-kernel.md)

## Context

The model catalog (`noeta/core/models.py`: context windows, output limits, prices) is a table
of facts about vendors' models, which looks like a capability rather than kernel state.

## Decision

It stays in the kernel. ADR-019 makes budgets and output ceilings kernel scheduling: the
kernel prices every call it counts against a tenant's budget (`noeta/core/tenants.py`) and caps
every call's output (`catalog_output_ceiling` in `noeta/core/runner/runtime.py`), and the
kernel imports nothing above it, so the table those numbers come from sits beside it.

## Rationale

Behind a plugin, the budget and the output cap would depend on a plugin being loaded, which
ADR-019 rejects for the budget. Plugins and providers read the same table (`noeta.core` is
importable from every layer), so a model has one row everywhere.

Rejected: moving the catalog to `noeta/toolkit` or a plugin and injecting prices and limits
into `Runtime` (two places to keep in step for every model, for no change in behavior).

## Consequences

- Adding a model's row is a kernel change; hosts add rows at run time with
  `register_models` instead of editing the table.
