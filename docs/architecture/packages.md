# Packages and boundaries

Noeta ships as two libraries that share one import namespace. This page covers
how they are cut, why the cut is where it is, and the import rules that keep it
honest — including the one rule that makes provider neutrality structural rather
than a promise.

You never need this to *use* the SDK. You need it when you are extending Noeta,
packaging it, or auditing what the kernel can reach.

<p align="center">
  <img src="../assets/diagrams/architecture.svg" alt="Noeta architecture — noeta-sdk over noeta-runtime, builtins reaching the kernel only through the plugin loader" width="820">
  <br>
  <em>A host drives the SDK in-process; the SDK forwards into the runtime's engine, materials, and storage. Built-ins reach the kernel only through the loader.</em>
</p>

## Two libraries

| Package | Role | Depends on |
| --- | --- | --- |
| `noeta-runtime` | The pure kernel — everything needed to host one agent in-process, with **no capability implementation of its own**. | stdlib only |
| `noeta-sdk` | The thin client and the only thing users import — plus `noeta.builtins`, where every official capability actually lives. | `noeta-runtime`, `httpx`, `psycopg` |

The runtime's top-level modules are `protocols` (the typed boundary, importing
nothing else in-project), `core` (Engine, fold, snapshot), the kernel services
`runtime` / `storage` / `observers` / `read_models`, the materials band
`context` / `policies` / `tools`, the injection-only `execution` builder, the
`agent` identity layer, and `testing`.

The SDK adds four: `client` (the assembly boundary), `sdk` (the public façade),
`presets` (the official agents), and `builtins` (the capability catalogue).

**Users install `noeta-sdk` and import only `noeta.sdk`.** `noeta-runtime`
arrives as a transitive dependency they never touch. On PyPI the bare `noeta`
name belongs to an unrelated project, which is why both dists carry a suffix.

## One namespace, two wheels

Both packages contribute subpackages to one shared **PEP 420 namespace
package**, `noeta`. There is no `noeta/__init__.py` in either wheel; Python
merges the two trees at import time.

The consequence is worth stating plainly: `noeta.core`, `noeta.context`,
`noeta.builtins` are stable import paths regardless of which wheel ships them.
Moving a module across the distribution boundary changes *packaging*, not any
import statement — and every contract below reruns unchanged.

## The import rules

The dependency direction is not left to discipline. `.importlinter` runs in CI
and fails the build on a violation. Ten contracts hold the topology; these are
the load-bearing ones.

**The layer stack** (`layers`), top to bottom:

```
noeta.sdk
noeta.builtins | noeta.presets
noeta.client
noeta.execution
noeta.context | noeta.policies | noeta.tools
noeta.runtime | noeta.storage | noeta.observers | noeta.read_models
noeta.agent.registry
noeta.agent.spec
noeta.core
noeta.protocols
```

A module may import downward, never upward.

**`noeta.protocols` imports nothing in-project** (`protocols-isolation`). It is
the typed boundary every other layer speaks, so it cannot depend on any of them.

**`noeta.core` may import only `noeta.protocols`** (`core-uses-only-protocols`),
with one documented exception: the Engine lazily imports
`noeta.runtime.tool.ToolRuntime` when a caller did not wire one, and the
contract whitelists that single edge by name rather than opening the layer.

**Nothing statically imports `noeta.builtins`** (`sdk-core-not-builtins`). This
is the universal microkernel contract, and the next section is what it buys.

Smaller contracts keep the leaves narrow: `noeta.observers` and
`noeta.read_models` see only protocols (plus one whitelisted `core.fold` edge),
`noeta.agent.spec` / `registry` see only protocols, the kernel vocabulary sinks
`noeta.runtime.governance` / `mcp` see only protocols, and production code talks
to the storage Protocols rather than the in-memory adapters.

## The kernel carries no capability

Every official capability — the fs and web tool packs, the provider adapters,
the default guards, memory, browser, MCP, sandbox backends, skills, the ReAct
policy, the durable storage backends — is a **built-in plugin** under
`packages/noeta-sdk/noeta/builtins/<name>/`.

Nothing imports that band statically. The only doorway is the plugin loader's
dynamic `ref` resolution, resolved once at client build. Importing
`noeta.builtins` therefore imports zero implementation modules, and
`.importlinter` rejects any static edge that would change that.

Two things follow, and both are structural rather than aspirational:

- **Provider neutrality.** Every vendor adapter lives in the `providers`
  built-in. The kernel *cannot* import one, so it cannot grow a vendor
  assumption. See [Provider neutrality](../concepts/provider-neutrality.md).
- **First-party capabilities are not privileged.** Noeta's own built-ins ride
  the identical loader, validation, and merge path as a third-party plugin, so
  the extension path is exercised by every default agent on every run.

### The catalogue

Eighteen built-ins ship in `noeta-sdk`:

| Group | Built-ins | Fills |
| --- | --- | --- |
| Tool packs | `fs`, `web`, `memory`, `browser`, `app`, `workspace` | `tool`, `session_pack`, `prompt_fragment`, `reminder_provider` |
| Control tools | `todo_write`, `ask_user_question`, `delegation`, `react` | `control_tool` |
| Context | `skills`, `reminders` | `session_pack`, `reminder` |
| Governance | `governance` | `guard`, `observer` |
| Agents | `presets` | `agent` |
| Declaration-only | `providers`, `mcp`, `storage`, `sandbox` | (host-wired) |

The last row is the interesting one. `providers`, `mcp`, and `storage` carry
**zero contributions**: an LLM adapter, an MCP connector, and a storage backend
are all host wiring, not agent identity, so they are reached through
`noeta.sdk.providers` / `noeta.sdk.storage` and the loader's dynamic doorway
rather than merged onto a surface. They live in the catalogue anyway, because
that is where implementation code belongs. (`sandbox` contributes one
`sandbox_provider` on the host plane.)

`react` refuses to be disabled: it supplies the default decision policy that
every compiled agent's identity pins, so the brain is *replaceable* through the
`policy` surface, not removable.

## Distribution

Because ground truth is a fold over a durable log, distribution is mostly a
scheduling problem: any process that can read the store can rebuild any task,
and execution assumes nothing about which machine it runs on.

The default shape is single-host — a local SQLite file and a resident worker
pool in one process. Reaching multi-host is a storage-adapter swap: point the
deployment at Postgres and several host processes share one database, their
writes fenced in-transaction. The Engine does not change either way.

Neither wheel speaks HTTP or ships a daemon. A host that wants an API builds it
on top of `noeta.sdk`; `examples/reference-host` is the smallest such host.

## Where to go next

- [State and writers](state-and-writers.md) — what the single-writer invariant
  actually protects
- [Extension planes](extension-planes.md) — the 16 surfaces the loader fills
- [Architecture overview](overview.md) — the top-down tour
- [Plugins reference](../reference/plugins.md) — the manifest format and the
  loader's sources
