# Extension planes

Everything you can extend in Noeta goes through one mechanism: a named
**surface** that a plugin contributes to, or that you hand-wire through an
`Options` field. There are sixteen surfaces, grouped into three **planes** by
what a contribution *means* — whether it changes who the agent is, how it is
mounted onto a host, or what resources the host supplies.

This page covers the planes, the loader that fills them, and how Noeta's own
capabilities ride the same path as yours.

<p align="center">
  <img src="../assets/diagrams/plugin-system.svg" alt="Noeta plugin system — manifest to loader to registry to per-agent activation, 3 planes and 16 surfaces" width="820">
</p>

## The cut that everything else follows

Before the surfaces, one distinction runs through the whole design: **identity
versus wiring**.

- **Identity** decides how the agent thinks — system prompt, tool set, skills,
  activated plugins, the decision policy. It enters the durable recording and is
  reproduced verbatim on fold.
- **Wiring** only mounts the agent onto a host — the provider instance, the
  working directory, an approval callback, observers, storage. It is excluded
  from identity (`compare=False`), so swapping it never perturbs a recording.

The cut is mandatory, not stylistic: recordings must be reproducible. Mix the
two and a replay fails to line up because someone changed a working directory.
It is also why swapping LLM vendors is free — see
[Swap providers](../how-to/swap-providers.md).

## The three planes

| Plane | Surfaces | Scope | In agent identity? |
| --- | --- | --- | --- |
| **Identity** | `tool`, `agent`, `content_kind`, `prompt_fragment`, `policy`, `control_tool` | per-agent | **yes** |
| **Wiring** | `guard`, `observer`, `provider`, `reminder_provider`, `reminder`, `tool_result_transform`, `session_pack` | process, host-wired, or per-agent | no |
| **Host** | `mcp_server`, `skills`, `sandbox_provider` | host-wired | no |

Two details in that table earn their place.

**`guard` and `observer` are process-wide, and deliberately not opt-out.** Once
a plugin carrying them is loaded, they are in force for every agent in the
process, whether or not that agent activated it. Governance is operator
authority: an agent author must not be able to skip compliance interception or
audit by omitting a name from a tuple. Everything else on the wiring plane is
per-agent or host-wired.

**The wiring plane has exactly two process-wide channels.** A plugin that tries
to register a third process-scoped surface is refused at construction rather
than quietly filed under one of the two.

Each surface is fully described by a `SurfaceSpec`: its plane, activation scope,
validator, collision key, ordering, and — for identity surfaces — which
activation channel a contribution feeds. Full per-surface detail is in the
[plugin surfaces reference](../reference/plugin-surfaces.md).

## The loader is surface-agnostic

`load_plugins(...)` reads static manifests into a `PluginSet` **without
importing any plugin code**. A contribution's `ref` is a string; it resolves
only at the client-build boundary. That is what makes a plugin listable,
auditable, and collision-checkable before any of it runs.

The loader consults exactly one `SurfaceRegistry` — a `name → SurfaceSpec`
map — and nothing else. Adding a surface is registering a `SurfaceSpec`, never
editing the loader. A host adds its own by taking `standard_registry().copy()`,
registering its surface, and passing that registry to `load_plugins`; a
contribution on it is validated and collision-checked by the same pipeline and
then handed to the host, never entering agent identity.

Contributions merge deterministically, ordered by `(plugin, name)` or by an
integer `priority` where the surface declares one. A collision names both sides
and fails — there is no override, and no last-writer-wins.

## Activation is identity

Loading makes a plugin *available* in the process. **Activation** decides which
agents use it: `Options.plugins` and `AgentDefinition.plugins`, tuples of names
that fold into the single `AgentSpec.plugins` identity tuple.

Feature gating then reads that tuple through `agent_activates(agent, plugin)` —
membership *is* the capability. There is no second registry of flags, no runtime
restriction bolted on afterwards.

An unrecognised name **fails compilation loudly**, so a typo can never silently
turn a capability off. `DEFAULT_PLUGINS = ("fs", "web")` is the default and is
identity-inert, which is why a bare `Options()` compiles byte-identically to
what it always did.

Because activation is identity, it turns over the KV-cache prefix. Plan an
agent's plugin set the way you plan its tool set — not per turn.

## How a built-in is laid out

Noeta's own capabilities are plugins, with no privileged path. One directory per
built-in under `packages/noeta-sdk/noeta/builtins/<name>/`:

```
noeta/builtins/memory/
├── __init__.py        # MANIFEST — inert data, zero execution
└── impl/              # the code the manifest's refs point at
```

`__init__.py` declares a `PluginManifest` and nothing else, so importing the
band imports zero implementation modules. The manifest's `ref` strings name
sibling modules under `impl/`, resolved by the loader's dynamic import at client
build. `.importlinter` forbids any static import of `noeta.builtins` from
anywhere in the tree.

Adding a first-party capability is therefore adding a directory — the same act
as shipping a third-party plugin, minus the packaging. See
[Write a plugin](../how-to/write-a-plugin.md) for the authoring side.

## The agent layer

An agent's identity is an `AgentSpec`: a name plus the identity-side
configuration — instructions, policy ref, tools, the `plugins` activation tuple,
the `spawnable` roster. It is compiled from `Options` and collected in a
registry. The layer sits low in the runtime and depends only on the protocol
layer, because an agent is a *class* of task, not a network surface.

Four official agents ship, each with a deliberately trimmed surface:

| Agent | Role | Tool surface | Delegates? |
| --- | --- | --- | --- |
| `main` | the conversational controller | full built-ins + `TodoWrite` / `AskUserQuestion` / `skill_invocation` / `memory` / `mcp` | yes |
| `general-purpose` | a self-contained worker | read / write / edit + shell + web | no — a leaf |
| `explore` | a read-only scout | read-only tools | no |
| `plan` | a read-only planner | read-only tools | no — produces a plan |

Two more identities exist outside that set: `web`, the browser subagent that
only `sandbox_browser_options()` puts on main's roster, and `__consolidation__`,
the internal memory curator a host registers with `with_consolidation_agent()`.

Delegation takes two shapes. **Single**: the parent spawns one subtask, suspends,
and wakes when it completes. **Fan-out**: the parent spawns a group that runs
concurrently on a bounded in-process pool (`min(8, CPU count)`, overridable with
`NOETA_MAX_SUBTASK_CONCURRENCY`), and the results return together, each paired
to its original tool call. Every subtask is a full event-sourced task with its
own log and fold, related to its parent only by `parent_task_id`.

## What stays locked

Not everything is a surface, and the exceptions are principled:

- **The Engine main loop.** Its control flow only routes Decisions; changing
  what the agent decides is what the `policy` surface is for.
- **The Dispatcher / Worker / Lease protocol.** A host tunes concurrency and
  lease timing through `HostConfig` and nothing more — the single-writer fence
  depends on it.
- **`ThreeSegmentComposer`.** Replacing the composer wholesale is not offered,
  because stable-prefix KV-cache reproducibility is a protocol-level hard
  constraint. Its open hooks are registry-only and append-only: a
  `ContentKindSpec` (a semi-stable resident) or a compose-time `reminder` (the
  dynamic-suffix tail). Neither touches the stable prefix.
- **Storage backends.** Wired through `HostConfig`, never a plugin surface, and
  never part of `AgentSpec` identity. The public doorway is `noeta.sdk.storage`.

## Where to go next

- [Packages and boundaries](packages.md) — the import rules that keep the
  builtins band reachable only through the loader
- [Write a plugin](../how-to/write-a-plugin.md) — the authoring walkthrough
- [Plugin surfaces reference](../reference/plugin-surfaces.md) — all sixteen,
  one section each
- [Plugin manifest reference](../reference/plugin-manifest.md) — the manifest
  shape, loading sources, and versioning
