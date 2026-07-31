# Storage backend relocation — durable backends become a built-in, the kernel keeps the SPI

> **Status: Shipped** — landed on `main` 2026-07-30 (branch
> `storage-backend-relocation`, dbe443d + 7d1ea4e, fast-forwarded; the durable
> decisions live in the plugin-contribution-bundles / package-layout /
> storage-protocols-l0 ADR addenda). Designed 2026-07-30 with the owner. Per the owner's
> explicit directive this spec carries **no historical compatibility**: import
> paths, helper names, and re-export lists change outright, with no deprecation
> shims. Base the work on `main` after the `kernel-final-form` branch merges
> (the two efforts touch disjoint modules, but doc/status references here
> assume the final-form tree).

> **Implementation status (2026-07-30, branch `storage-backend-relocation`,
> stacked on `kernel-final-form`).** Shipped green: §4 steps 1–9 all landed;
> `make check` 3397 passed / 129 skipped (postgres-DSN + live-LLM, unchanged),
> mypy clean, import-linter 10 kept / 0 broken with zero contract edits (D7);
> gates G1–G6 verified (G5's three surviving grep hits are the install smoke's
> own negative assertions). Two notes beyond the spec text: (1) the `storage`
> manifest also had to be registered in the catalogue plumbing —
> `noeta.builtins._BUILTINS` + `options._INERT_BUILTIN_ACTIVATIONS` (the
> `providers` row of each); (2) the coverage total moved 88% → 85.36% **in a
> no-DSN environment only** because the doorway's re-export identity test now
> imports the postgres impl, pulling ~700 previously *unmeasured* statements
> into scope (never-imported modules under a PEP 420 namespace are invisible
> to coverage) — a measurement-scope widening, not lost coverage; a DSN-bearing
> run recovers it. Step 10 (release: minor bump both wheels, sdk pin advance,
> noeta-agent sweep) remains for the maintainer.

## 1. Goal

Finish the microkernel thought for storage. Today `noeta.storage`
(noeta-runtime wheel) ships three complete backends — InMemory, sqlite,
Postgres — which makes `psycopg[binary]` a hard dependency of the *kernel*
wheel and leaves first-party durable backends in a different position from
every other first-party implementation (all of which live in
`noeta.builtins.*.impl` since the microkernel migration). After this change:

- **noeta-runtime keeps**: the storage Protocols (`noeta.protocols`,
  unchanged), the shared backend domain rules as a **public SPI**
  (`noeta.storage.spi`), and the **InMemory reference backend**
  (`noeta.storage.memory`). The kernel wheel drops the `psycopg` dependency
  entirely.
- **noeta-sdk gains**: the durable backends as
  `noeta.builtins.storage.impl.{sqlite,postgres}` — a declaration-only
  built-in on the `providers` precedent — and a rewritten `noeta.sdk.storage`
  as the single public doorway (lazy re-exports + stack builders).
- **A third-party backend** needs exactly: implement the `noeta.protocols`
  storage Protocols, use `noeta.storage.spi` for the shared domain rules, ship
  a `build_stack(**config) -> (EventLogFull, ContentStore, Dispatcher)`
  factory, and have the host inject the triple through `HostConfig`. Nothing
  else — no plugin machinery, no registration.

### Non-goals

- **No `storage` contribution surface.** ADR
  `plugin-contribution-bundles.md` Alternative 5 stands unchanged: the triple
  is the truth substrate every plugin guarantee stands on; it is a single host
  injection with no merge semantics, so the plugin mechanism (activation,
  collision, ordering) adds nothing. This spec moves *files*, not trust.
- **No `HostConfig` changes.** A `storage_backend: str` +
  `storage_config: Mapping` pair was considered and rejected (§3 D4).
- **No transcript backend.** A file-based
  EventLog/ContentStore/Dispatcher backend is a genuinely new feature with its
  own design risks — the single-process assumption versus the
  `multi-host-lease-fencing` ADR, fsync-per-emit throughput, crash consistency
  between `events.jsonl` and its indexes (indexes must be rebuildable caches),
  and in-memory-only `subscribe` semantics. It gets its own spec; nothing here
  blocks it (it would be a third module under `noeta.builtins.storage.impl`
  plus one `_BACKENDS` row).
- **No shipped contract-test kit.** The backend contract suites
  (`tests/test_event_log_contract.py` etc.) stay in-repo; packaging them for
  third-party backend authors is a possible follow-up, not this work.

## 2. Why the InMemory backend stays in the kernel

The original sketch moved *all* backends out. Three hard constraints say the
reference backend cannot leave the noeta-runtime wheel:

1. **Package topology.** `noeta.testing.profile` ships in noeta-runtime and
   must build a storage stack; noeta-runtime cannot depend on noeta-sdk, and a
   runtime module importing `noeta.sdk.*` inverts the layers contract.
2. **The Client default.** `noeta.client.client` builds the default in-memory
   triple with a *static* import. `noeta.client` is a source module of the
   universal `sdk-core-not-builtins` contract, so it may never reach
   `noeta.builtins` (statically or by private lazy import — the loader's `ref`
   doorway is the only sanctioned dynamic path); routing through
   `noeta.sdk.storage` instead would invert layers (`noeta.client` sits below
   `noeta.sdk`). The current static import of `noeta.storage.memory` is legal
   (client is not a `storage-adapters-isolated` source) and stays.
3. **Kernel self-sufficiency.** An event-sourced kernel with zero EventLog
   implementation cannot run or verify anything. InMemory is the kernel's
   reference backend — the executable definition of the Protocols' semantics —
   not a capability implementation, so its presence does not contradict the
   "pure kernel" claim (which is about *capability* impls).

Consequence: goal 1 is amended from "no backend implementations in the
runtime" to "no **durable** backend implementations in the runtime".

## 3. Decisions

### D1 — What moves, what stays

| Module | Disposition |
| --- | --- |
| `noeta.protocols.*` (EventLog\* / ContentStore / Dispatcher / Lease / wake) | unchanged |
| `noeta.storage.memory` | stays (kernel reference backend); gains `build_stack()` |
| `noeta.storage._payload_restore` / `_reclaim` / `_wake_match` | stay as private impl files; **facaded by the new public `noeta.storage.spi`** (D5) |
| `noeta.storage.stacks` | **deleted**; builders move to `noeta.sdk.storage` (D3) |
| `noeta.storage.sqlite/` (incl. `readonly`, `migrations`, `_connection`, `_transaction`) | → `noeta.builtins.storage.impl.sqlite/` |
| `noeta.storage.postgres/` (incl. `readonly`, `migrations`, `_connection`) | → `noeta.builtins.storage.impl.postgres/` |
| `psycopg[binary]>=3.2` dependency | noeta-runtime → noeta-sdk (keep the maintainer's zero-setup `[binary]` call; an optional `noeta-sdk[postgres]` extra is possible later since the impl is lazily imported — maintainer's call, not taken here) |

### D2 — The `storage` built-in is declaration-only (the `providers` precedent)

`noeta/builtins/storage/__init__.py` is a reference `PluginManifest` with
**zero contributions** — exactly like `providers`, whose surface is host-wired
and whose manifest exists for catalogue documentation. Storage has no surface
at all; the manifest documents the two backend factories for host discovery:

```
noeta.builtins.storage.impl.sqlite.stack:build_stack
noeta.builtins.storage.impl.postgres.stack:build_stack
```

The built-in catalogue grows 17 → 18. The built-in is never activated, never
loaded per-agent, and never enters `AgentSpec` identity; the only doorway is
`noeta.sdk.storage` (lazy import — the same discipline as
`noeta.sdk.providers`, and legal because `noeta.sdk` is deliberately not a
source of `sdk-core-not-builtins`).

Layout:

```
noeta/builtins/storage/
  __init__.py                  # declaration-only reference MANIFEST
  impl/
    __init__.py                # empty, no re-exports
    sqlite/
      __init__.py              # re-exports Sqlite* + readonly types (as today)
      eventlog.py contentstore.py dispatcher.py readonly.py migrations.py
      _connection.py _transaction.py
      stack.py                 # build_stack(path: str) -> triple
    postgres/
      __init__.py
      eventlog.py contentstore.py dispatcher.py readonly.py migrations.py
      _connection.py
      stack.py                 # build_stack(dsn: str) -> triple
```

Every backend's `stack.py` has the uniform signature
`build_stack(**config: Any) -> tuple[EventLogFull, ContentStore, Dispatcher]`
and wires the triple's one internal invariant itself (the event log takes the
dispatcher as `lease_validator`). `noeta.storage.memory` gains the same
`build_stack()` (no config), so "a backend = protocols impl + `build_stack`"
holds uniformly for first- and third-party backends.

### D3 — `noeta.sdk.storage` is the single public doorway

Rewritten on the `noeta.sdk.providers` pattern (PEP 562 module
`__getattr__`, lazy so the universal no-static-import rule and the
import-light SDK root both hold):

```python
_BACKENDS = {
    "memory":   "noeta.storage.memory",                        # runtime wheel
    "sqlite":   "noeta.builtins.storage.impl.sqlite.stack",    # sdk wheel
    "postgres": "noeta.builtins.storage.impl.postgres.stack",
}

def build_storage_stack(backend: str, **config: Any) -> StorageTriple:
    """Build a named backend's triple; unknown name raises with the known set."""

def open_storage_stack(storage_path: Optional[str]) -> StorageTriple:
    """Value-shape dispatch: None/":memory:" -> memory; postgres URL ->
    postgres; anything else -> a sqlite file path."""

is_memory_path(...)   # moved from noeta.storage.stacks
is_postgres_url(...)  # moved from noeta.storage.stacks
```

Lazy class re-exports for hosts that construct adapters directly:
`SqliteEventLog` / `SqliteContentStore` / `SqliteDispatcher` /
`SqliteReadOnlyStore` (+ its two error types), and the Postgres equivalents.
`open_storage_stack` keeps the value-shape dispatch because it has real hosts
today (the reference-host example, `noeta.testing.profile`, noeta-agent's
storage URL config) — it is a convenience over `build_storage_stack`, not a
second mechanism.

### D4 — `HostConfig` is unchanged (rejected: `storage_backend` string field)

A `storage_backend: str` + `storage_config: Mapping` pair on `HostConfig` was
rejected: the name table can only ever hold built-in backends (a third party
cannot register into it without re-creating exactly the mini-surface ADR
Alternative 5 rejected), so the field would serve four names while adding a
second, mutually-exclusive configuration path to `HostConfig`. The host writes
one line either way:

```python
hc = HostConfig(*build_storage_stack("sqlite", path=db_path))   # vs
hc = HostConfig(storage_backend="sqlite", storage_config={"path": db_path})
```

The all-or-none triple injection plus the Client's in-memory default stay
byte-identical. This is the repo constraint verbatim: the interface is the
test surface; no seam without a real substitution need.

### D5 — The shared domain rules become a public SPI: `noeta.storage.spi`

"Third parties only need the Protocols" is nominally true and practically
false: any EventLog backend must restore typed payloads from canonical bytes,
and any Dispatcher backend must apply the reclaim cap and the wake-match rule
— or silently drift from the built-ins. The three private modules stay as
implementation files; a new `noeta.storage.spi` module is the small public
facade over them (deep-module rule: one documented entry, substantial hidden
implementation):

| SPI name | Backing | Contract it carries |
| --- | --- | --- |
| `restore_payload(event_type, body)` | `_payload_restore._restore_payload` | typed-payload restore table; the reflection test that fails CI when a new `*Payload` class lacks an entry keeps guarding it |
| `enforce_payload_cap(task_id, event_type, body)` | `_payload_restore._enforce_payload_cap` | `PayloadTooLarge` cap every persistent EventLog applies on emit |
| `reclaim_hits_cap(reclaim_count, reclaim_max)` | `_reclaim` | poison-task stale-reclaim terminal decision |
| `wake_matches(wake_on, event)` | `_wake_match._matches` (delegates to `noeta.protocols.wake.matches_wake`) | the projection-matching invariant, with the None-guard |

The moved sqlite/postgres backends import **only** `noeta.protocols` and
`noeta.storage.spi` — never `noeta.storage._*`. Since they now live in a
different wheel from the privates, this is enforceable and makes them the
standing proof that the SPI is sufficient for an external backend author
(§6 gate G4).

### D6 — `noeta.testing.profile` follows its own governance precedent

`build_runtime(sqlite_path=...)` keeps its signature. It already resolves the
governance guards through a call-time dynamic import of
`noeta.builtins.governance.impl` ("calling `build_runtime` requires noeta-sdk,
which every test run has"); storage joins that pattern — the durable branch of
its stack construction dynamically imports the builtin `stack` modules (memory
stays a static `noeta.storage.memory` import). The static re-exports of
`build_sqlite_stack` / `build_postgres_stack` / `open_storage_stack` /
`is_memory_path` leave `noeta.testing.profile` and `noeta.testing`'s `__all__`
— tests import them from `noeta.sdk.storage` (no compatibility owed).

### D7 — import-linter: zero contract changes

- `layers` — `noeta.storage` stays in the kernel-services band;
  `noeta.builtins.storage.impl` importing `noeta.storage.spi` /
  `noeta.protocols` is a normal downward edge; `noeta.sdk.storage` importing
  `noeta.storage.memory` likewise.
- `storage-adapters-isolated` — unchanged; it now guards the memory backend +
  SPI (comment update only).
- `sdk-core-not-builtins` — unchanged and automatically covers the moved
  impls; `noeta.sdk` remains deliberately absent from its sources (the
  `sdk.providers` precedent).
- `kernel-vocabulary-diet`, `protocols-isolation`, etc. — untouched.

Only comments/docstrings referencing `noeta.storage.{sqlite,postgres}` paths
are updated.

## 4. Migration steps

1. **Move the backends** (`git mv`):
   `noeta/storage/sqlite/` → `packages/noeta-sdk/noeta/builtins/storage/impl/sqlite/`,
   `noeta/storage/postgres/` → `.../impl/postgres/`. Add the two `stack.py`
   factories; add the declaration-only manifest `__init__.py`; empty
   `impl/__init__.py`.
2. **Public SPI**: add `noeta/storage/spi.py` (D5 table); rename the two
   private callables it fronts to their public names inside the private
   modules or at the facade; switch the sqlite/postgres backends to import
   `noeta.storage.spi` only. `noeta.storage.memory` may keep importing the
   privates directly (same module family, same wheel) but using the SPI there
   too is cheaper to explain — do that.
3. **`noeta.storage.memory`**: add `build_stack()`. Delete
   `noeta/storage/stacks.py`. Update `noeta/storage/__init__.py` docstring
   (memory + SPI; durable backends live in the `storage` built-in).
4. **Rewrite `noeta/sdk/storage.py`** per D3 (lazy exports + `_BACKENDS` +
   `build_storage_stack` + `open_storage_stack` + the two predicates).
5. **`noeta.testing.profile`**: D6 — drop the stack re-exports, dynamic-import
   the durable builders inside `build_runtime`; keep the memory path static.
6. **Dependencies**: remove `psycopg[binary]` from
   `packages/noeta-runtime/pyproject.toml`, add it to
   `packages/noeta-sdk/pyproject.toml` (carry the `[binary]` rationale comment
   over).
7. **Callers** (no-compat sweep; ~20 files by grep):
   - tests importing `noeta.storage.sqlite` / `noeta.storage.postgres` /
     `noeta.testing`'s stack re-exports → `noeta.sdk.storage` (the contract
     suites' backend parametrisation included);
   - `tests/_host_storage.py`, `tests/_sdk_session.py`;
   - `examples/reference-host/host.py`, `examples/crash_resume.py`,
     `examples/_internal/real_provider_subtask_demo.py`.
   Client and `HostConfig` need **zero** edits.
8. **Install smoke** (`tests/test_install_smoke.py`): extend the
   runtime-standalone closure — `import noeta.storage.memory, noeta.storage.spi`
   succeeds, `noeta.storage.sqlite` / `noeta.storage.postgres` do **not**
   exist, and the runtime dist metadata carries no `psycopg` requirement. The
   sdk closure line already imports `noeta.sdk.storage`; extend it to touch one
   lazy attribute (e.g. `SqliteEventLog`) so the doorway is exercised.
9. **Docs**: CONTEXT.md — the noeta-runtime description (storage backends
   line), the public-surface paragraph (the `noeta.storage` escape hatch
   becomes "the backend SPI + the InMemory reference backend; durable backends
   are wired via `noeta.sdk.storage`"), the Provider/adapter vocabulary entry.
   ADR — an addendum on `plugin-contribution-bundles.md` (catalogue 17 → 18,
   the declaration-only-builtin pattern now has two members, Alternative 5
   reaffirmed with the wording fix "the `noeta.protocols` storage Protocols
   plus the `noeta.storage.spi` domain rules"); a matching addendum on
   `package-layout.md` / `storage-protocols-l0.md` for the wheel move.
10. **Release note**: moving modules across wheels is the version-skew case
    the sdk pin guards — bump both wheels' minor (0.4 → 0.5), advance the sdk
    pin to `noeta-runtime>=0.5.0,<0.6.0` (maintainer confirms per
    `docs/releasing.md`). Add to the pending noeta-agent sweep: the
    `open_storage_stack` import path, `noeta.storage.sqlite.readonly` →
    `noeta.sdk.storage` re-exports.

## 5. Target state at a glance

```
noeta-runtime wheel                     noeta-sdk wheel
  noeta.protocols.*        (unchanged)    noeta.sdk.storage        <- the only public doorway
  noeta.storage                             build_storage_stack / open_storage_stack
    memory  (+ build_stack)                 lazy: Sqlite* / Postgres* / readonly types
    spi     (public facade)               noeta.builtins.storage   <- declaration-only manifest
    _payload_restore/_reclaim/_wake_match   impl/sqlite  (stack.py)
                                            impl/postgres(stack.py)
  noeta.testing.profile                   HostConfig: unchanged (triple, all-or-none)
    memory static; durable via             Client default: unchanged (static
    call-time dynamic import                noeta.storage.memory)
```

## 6. Acceptance criteria

- **G1** `make check` green (run it **unpiped**), test count and coverage not
  below the baseline at branch point; import-linter passes with zero contract
  edits (comment-only diffs).
- **G2** Runtime purity: the extended install smoke passes — runtime wheel
  standalone imports `noeta.storage.{memory,spi}`, has no
  `noeta.storage.{sqlite,postgres}`, and declares no `psycopg` dependency.
- **G3** Zero-execution catalogue: listing the `storage` built-in's manifest
  imports no impl module (same property test shape as the other built-ins);
  `import noeta.sdk.storage` alone imports neither `noeta.builtins.storage.impl`
  nor `psycopg` (lazy doorway).
- **G4** SPI sufficiency:
  `grep -R "noeta\.storage\._" packages/noeta-sdk/noeta/builtins/storage/` is
  empty — the durable backends build against `noeta.protocols` +
  `noeta.storage.spi` only, standing in for the third-party author.
- **G5** Dead paths: zero references to `noeta.storage.stacks`,
  `noeta.storage.sqlite`, `noeta.storage.postgres` anywhere in the tree
  (docs included).
- **G6** Behaviour: the storage contract suites (event log / content store /
  dispatcher, incl. the multi-host and durability suites) pass unmodified in
  assertion content — only their backend import/parametrisation lines change.
