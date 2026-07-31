# Two distributions over one PEP 420 namespace, with the layer topology enforced by import-linter on import paths

## Context

Boundaries have to bind mechanically, or they are decoration. Three constraints shape how they are drawn: a cross-cutting dataclass change must not become a multi-package coordination exercise; an embedder must not inherit dependencies for machinery it does not use; and the storage backend must be replaceable without touching call sites.

## Decision

Two distributions, `noeta-runtime` and `noeta-sdk`, both contribute subpackages to the shared PEP 420 `noeta.` namespace. `noeta-runtime` is dependency-free — stdlib only, no HTTP client and no database driver. Every third-party dependency rides `noeta-sdk`, alongside the capability implementations that need it.

Layering is an **import constraint**, not a package carve-up. `.importlinter` enforces it against import paths, which stay invariant regardless of which wheel ships a module:

- The `layers` contract stacks `noeta.protocols` (L0, importing nothing else in the project), `noeta.core`, `noeta.agent.spec`, `noeta.agent.registry`, the kernel-services band (`runtime` / `storage` / `observers` / `read_models`), the materials band (`context` / `policies` / `tools`), `noeta.execution`, `noeta.client`, `noeta.builtins` with `noeta.presets`, and `noeta.sdk` on top. Upper layers may depend downward only.
- What a layer stack cannot express is written as `forbidden` contracts: `noeta.protocols` importing no in-project module, `noeta.core` reaching only `noeta.protocols`, the kernel vocabulary modules staying on protocols, `noeta.observers` and `noeta.read_models` isolated, and SDK core barred from `noeta.presets`.
- `sdk-core-not-builtins` forbids every band from statically importing `noeta.builtins`. The plugin loader's dynamic `ref` resolution is the only doorway into a built-in, and the lazy re-export modules `noeta.sdk.storage` and `noeta.sdk.providers` are how a host reaches an implementation by name.

The typed storage boundary sits at L0: the `EventLog`, `ContentStore`, `Dispatcher`, and `LeaseRegistry` Protocols live in `noeta.protocols`. The kernel wheel holds the InMemory reference backend in `noeta.storage.memory` plus the shared domain rules every backend routes through in `noeta.storage.spi`; the durable sqlite and Postgres adapters are the `storage` built-in, reached through `noeta.sdk.storage`. Production code sees only the Protocols — `storage-adapters-isolated` forbids the kernel bands from importing `noeta.storage` at all.

## Rationale

- **Import contracts give layering its binding force without the multi-package tax.** Carving the layers into their own distributions would turn a single cross-layer dataclass change into a release-ordering problem. A checker in the verification gate delivers the same discipline inside one repository, and a breach fails immediately rather than at review time.
- **Path-based contracts survive re-homing.** Because the constraints name import paths and PEP 420 keeps those paths stable across distributions, which wheel ships a module is a packaging decision no contract has to re-litigate.
- **A dependency-free kernel is what "install only what you need" actually means.** The driver ships with the adapter that needs it, and the lazy doorway means a host that picks sqlite never imports the Postgres driver, even though the wheel carries it.
- **Storage Protocols at L0 make the backend genuinely swappable.** A new backend implements the Protocols, routes the shared rules through the SPI, and exposes a stack factory; nothing at a call site changes.
- **Banning static imports of `noeta.builtins` keeps the kernel free of capability implementations.** With one dynamic doorway, a capability cannot quietly become a kernel dependency, and the doorway is the single place to enforce trust and configuration.

## Alternatives considered

1. **One distribution per module or per layer.** Rejected: a dozen-plus `pyproject.toml` files to keep in sync, and cross-package changes become painful long before any consumer benefits.
2. **One big package holding everything.** Rejected: every consumer inherits the full third-party dependency set, and there is no distribution boundary to hang a public surface on.
3. **Physically splitting by layer into protocols / core / services / deployment distributions.** Rejected: a single consumer would have to install several packages to get started. Layering is a constraint the project enforces, not an assembly job pushed onto the user.
4. **A registry of named storage backends inside the SDK.** Rejected: a third-party backend that implements the Protocols and ships a stack factory needs no entry anywhere, and a registry would only add a place to forget.
5. **Depending on bare `psycopg` and leaving libpq to the user.** Rejected in favour of the binary distribution: zero-setup Postgres is worth more here than the flexibility, since the adapter loads only when a host chooses it.

## Consequences

- The enforcement point is `.importlinter` — the `layers` contract plus the `forbidden` contracts named above — and it runs in the verification gate alongside the tests, type check, and naming lint.
- The one typed boundary for storage, and for the project as a whole, is `noeta.protocols`.
- The kernel wheel carries no database driver and no HTTP client, so an embedder of the engine alone inherits neither.
- Import contracts stop at the repository edge; the half of the public surface that says user code imports only `noeta.sdk` is carried by wheel packaging (see `library-sdk-architecture.md`).
