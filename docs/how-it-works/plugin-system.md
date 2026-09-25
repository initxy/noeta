# The plugin system

Everything you can extend in Noeta goes through one mechanism: a named **surface**
that a plugin contributes to, or that you fill directly through an `Options`
field. Noeta's own capabilities — file and web tools, memory, MCP, sandboxes,
storage, guards, model adapters — are plugins on the same path as yours.

<NtPlugins />

## What it guarantees

- **No privileged path.** Built-ins are loaded, validated and merged exactly like
  a third-party plugin. The kernel never imports `noeta.builtins` statically; an
  import linter fails the build if anything does.
- **Nothing runs at load time.** Manifests are inert data; code is imported only
  when the client is built.
- **No silent overrides.** Two contributions with the same key fail loudly, naming
  both sides. There is no last-writer-wins.
- **Typos fail.** Activating an unknown plugin name fails compilation instead of
  quietly turning a capability off.

## Identity versus wiring

Every surface is one of two kinds, and the difference matters for replay:

- **Identity** decides how the agent thinks — system prompt, tools, skills,
  activated plugins, decision policy. It is recorded and reproduced exactly on
  fold.
- **Wiring** only mounts the agent on a host — the model provider, working
  directory, approval callback, observers, storage. It is not part of identity,
  so changing it never breaks a recording. This is why switching model vendors is
  free.

## The sixteen surfaces

| Group | Surfaces | Scope | Part of agent identity |
| --- | --- | --- | --- |
| Identity | `tool`, `agent`, `content_kind`, `prompt_fragment`, `policy`, `control_tool` | per agent | yes |
| Wiring | `guard`, `observer` | whole process | no |
| Wiring | `provider` | set by the host | no |
| Wiring | `reminder_provider`, `reminder`, `tool_result_transform`, `session_pack` | per agent | no |
| Host resources | `mcp_server`, `skills` | whole process, joined automatically | no |
| Host resources | `sandbox_provider` | set by the host | no |

`guard` and `observer` apply to every agent in the process once their plugin is
loaded. That is deliberate: governance belongs to the operator, and an agent
author must not be able to skip it by leaving a name out. Details of each surface
are in the [plugin surfaces reference](../reference/plugin-surfaces.md).

## How a plugin is loaded

1. `load_plugins(...)` reads manifests into a `PluginSet` without importing any
   plugin code. Each contribution's `ref` is just a string.
2. Every contribution is validated against its surface's `SurfaceSpec` and
   checked for collisions. Merge order is deterministic: by `(plugin, name)`, or
   by `priority` where the surface has one.
3. When the client is built, `PluginSet.resolve()` imports the code each `ref`
   names.
4. **Activation** picks which agents use which plugins: `Options.plugins` and
   `AgentDefinition.plugins` decide every per-agent surface, identity or wiring;
   only the identity ones fold into the agent's identity. The default is
   `DEFAULT_PLUGINS = ("fs", "web")`. `guard` and `observer` apply to the whole
   process instead; `provider` and `sandbox_provider` are picked by the host, and
   `mcp_server` and `skills` contributions join automatically.

The loader knows nothing about specific surfaces; it only consults a
`SurfaceRegistry`. A host can add its own surface by copying
`standard_registry()` and registering a new `SurfaceSpec`.

A built-in is one directory under `packages/noeta-sdk/noeta/builtins/<name>/`: an
`__init__.py` holding only the `PluginManifest`, and an `impl/` package the
manifest's refs point at. Eighteen ship with `noeta-sdk`.

## What stays locked

| Locked | Why | What you can change instead |
| --- | --- | --- |
| The Engine main loop | it only routes decisions | the `policy` surface |
| Dispatcher, worker and lease protocol | the single-writer guarantee depends on it | concurrency and lease timing in `HostConfig` |
| The context composer | the cached prompt prefix must stay reproducible | add a `content_kind` or a `reminder` |
| Storage backends | host wiring, never agent identity | `HostConfig`, via `noeta.sdk.storage` |

## What this means for you

- Anything a built-in does, your plugin can do the same way.
- Activation changes identity and therefore the cached prompt prefix; choose an
  agent's plugins once, not per turn.
- Start with a `@tool` function in `Options.allowed_tools`; move to a plugin when
  you want to ship tools, prompts and guards together as one bundle.

Design records:
[library SDK architecture](https://github.com/initxy/noeta/blob/main/docs/adr/library-sdk-architecture.md) ·
[plugin contribution bundles](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md) ·
[package layout](https://github.com/initxy/noeta/blob/main/docs/adr/package-layout.md)

## Next

- [Write a plugin](../guides/plugins.md) — the authoring walkthrough.
- [Plugin manifest reference](../reference/plugin-manifest.md) — manifest shape and loading sources.
- [Plugin surfaces reference](../reference/plugin-surfaces.md) — all sixteen, one section each.
