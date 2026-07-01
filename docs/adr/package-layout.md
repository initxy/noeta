# Packages split by consumer role + layered import topology enforced by import-linter

## Context

How to draw package boundaries is a recurring question: splitting by physical layer turns a single cross-package dataclass change into a painful multi-package coordination exercise; not splitting at all forces a remote caller to drag in the entire runtime dependency set. This decision settles on "split packages by consumer role, and let import-linter enforce the layered topology."

The number and naming of physical packages later evolved into the three-layer runtime/sdk/product model; the final form is in `docs/adr/library-sdk-architecture.md`. The splitting criteria, layered import topology, placement of the storage seam at L0, and on-demand dependency installation described below all still hold.

## Decision

Package boundaries are **drawn by "who consumes it,"** not by physical layer. The current state is three core packages sharing the `noeta.` top level (PEP 420 namespace): `noeta-runtime` (the kernel: protocols / core / context / runtime / storage / providers / guards / observers), `noeta-sdk` (AgentSpec / registry / policies / tools / execution), and the product shell (the HTTP/SSE host in `apps/noeta-agent`).

- **The L0..L3 import topology is enforced by `.importlinter`, not by physically splitting packages.** L0 depends on nothing else in the project; upper layers may only depend downward; constraints like "no import edges within a layer" are written as forbidden contracts (the `layers` topology governs only up/down).
- **A downstream remote caller installs only the layer it needs**: a pure remote client should not be forced to drag in runtime dependencies like sqlite / anthropic / fastapi. A replaceable backend goes through an optional dependency (installed on demand).
- **The typed boundary of the storage seam lives at L0**: the `EventLog` / `ContentStore` / `Dispatcher` / `LeaseRegistry` Protocols live in `noeta.protocols`, and the concrete adapters (InMemory / Sqlite) live in `noeta.storage`. Production code sees only the Protocols; the `.importlinter` `storage-adapters-isolated` contract forbids any kernel layer from importing `noeta.storage` (details in `docs/adr/storage-protocols-l0.md`).

## Rationale

- **The splitting criterion is the consumer, not tidiness.** The number of packages serves the installer: a remote caller should not have to install the entire runtime backend just to use a typed client. Splitting by consumer role means each consumer can "install one package and get to work," which fits Python-ecosystem convention.
- **Layering via import-linter rather than physical package splits saves maintenance.** Strictly carving L0-L3 into 16 packages would turn a single cross-package dataclass change into a painful multi-package coordination exercise; import-linter mechanically enforces the topology within a single monorepo, delivering all the binding force of layering without paying the multi-package tax. CI must run this contract, so any breach of the layering fails immediately.
- **The storage seam lands at L0, so the backend is replaceable.** Pinning the three storage Protocols at L0 and having production code use only the Protocols means a new Sqlite/Postgres backend adapter also lands in `noeta.storage` with zero changes at the call sites.

## Alternatives considered

1. **16 fine-grained packages** (one module per package, strictly by L0-L3). Rejected: high maintenance cost, painful cross-package dataclass changes, and having to manage 16 `pyproject.toml` files before there is any real need.
2. **One big package, everything stuffed into `noeta`.** Rejected: a remote caller is forced to install runtime dependencies like sqlite / anthropic / fastapi, violating "on demand."
3. **Physically splitting into four packages by L0-L3** (`noeta-protocols` / `noeta-core` / `noeta-services` / `noeta-deployment`). Rejected: a single consumer has to install multiple packages just to get to work, which doesn't fit ecosystem convention — layering is an import constraint, not a burden the consumer has to assemble layer by layer.

## Consequences

- The mechanical enforcement point is `.importlinter`: the `layers` topology plus forbidden contracts like `storage-adapters-isolated`.
- L0's typed boundary (including the storage seam's Protocols) lives in `noeta.protocols.*`.
- The one and only home for concrete storage adapters is `noeta.storage`, isolated by a forbidden contract.
- The later evolution of package count/naming (the three layers runtime/sdk/app) is in `docs/adr/runtime-sdk-app-restructure.md`, which explains the relationship between shifting wheel membership and unchanged import paths.
