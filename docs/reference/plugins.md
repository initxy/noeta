# Plugins

A plugin is a package — or a single `.py` file — that carries a **static
manifest** listing what it contributes to Noeta. Loading a plugin reads those
manifests; it does not run any plugin code. An agent then *activates* the
plugins it wants. That two-step split is the whole idea: a host decides what is
available, an agent decides what it uses.

Noeta ships its own capabilities the same way. The 11 default tools, the
default guards, and the three compose-time reminders are all built-in plugins,
resolved through the same loader as anything you write. (Provider adapters
ship in the SDK too, but a host constructs them directly from
`noeta.sdk.providers` — the `providers` built-in contributes nothing to a
surface.)

<p align="center"><img src="../assets/diagrams/plugin-system.svg" alt="Plugin system: manifest to loader to registry to per-agent activation, across three planes and sixteen surfaces" width="820"></p>

## The three planes

Every contribution lands on one of sixteen **surfaces**, and each surface sits
on one of three planes. The plane decides where the contribution has effect.

| Plane | Surfaces | Effect |
| --- | --- | --- |
| **identity** | `tool`, `agent`, `content_kind`, `prompt_fragment`, `policy`, `control_tool` | enters the recorded `AgentSpec`; follows per-agent activation |
| **wiring** | `guard`, `observer`, `provider`, `reminder_provider`, `reminder`, `tool_result_transform`, `session_pack` | behaviour, not identity. `guard` and `observer` are process-wide once loaded — governance is operator authority, not an agent-author opt-in |
| **host** | `mcp_server`, `skills`, `sandbox_provider` | the host selects and binds them; never per-agent |

Full contract for each surface, with a worked example:
[Plugin surfaces](plugin-surfaces.md).

## A minimal plugin

Two files. The manifest declares the contribution; the module holds the code.

```toml
# pyproject.toml
[tool.noeta]
name = "house-style"
requires-noeta = ">=0.4"

[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
text    = "Answer in at most three sentences."

[[tool.noeta.contributions]]
surface = "tool"
ref     = "house_style.tools:LintTool"    # module:attr import string
```

Load it, then activate it on an agent:

```python
from noeta.sdk import Client, DEFAULT_PLUGINS, Options, load_plugins

pset = load_plugins(entry_points=True)      # built-ins + installed plugins
print(pset.names())
# → ('app', 'ask_user_question', …, 'house-style', …)

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("house-style",),
)
client = Client(options, provider=my_provider, workspace_dir=".", plugins=pset)
```

Manifest shape, the loader's five sources, the trust gate and version pinning:
[Plugin manifest](plugin-manifest.md).

## Load, then activate

| Step | Where | What it decides |
| --- | --- | --- |
| **Load** | `load_plugins(...) -> PluginSet`, host level | which plugin *code* is available in this process |
| **Activate** | `Options.plugins` / `AgentDefinition.plugins`, agent level | which loaded plugins *this agent* uses |

`Client(options, plugins=<PluginSet>)` binds the two. An activation name that is
not in the loaded set fails the build — loudly, at startup, never mid-turn.
Activation enters `AgentSpec` identity, so an agent that gains a plugin is a
different agent in the record. The full activation vocabulary is in
[Options](sdk-options.md#plugin-activation).

A `PluginSet` is **listable and collision-checkable without executing plugin
code**: `.contributions()` and `.merged()` read only the static manifests, and
`.resolve()` is the single import boundary, called once at client build.

```python
for plugin_name, contribution in pset.contributions("tool"):
    print(plugin_name, contribution.name)   # no plugin body imported
# → fs read
# → fs glob
# → …
```

## Where to go next

| Page | Covers |
| --- | --- |
| [Plugin manifest](plugin-manifest.md) | the `[tool.noeta]` tables, `PluginBuilder`, `load_plugins`, the trust store, `requires-noeta`, packaging |
| [Plugin surfaces](plugin-surfaces.md) | all sixteen surfaces, one section each, with the built-in that demonstrates it |
| [Write a plugin](../how-to/write-a-plugin.md) | the task-oriented guide |
| [Extension planes](../architecture/extension-planes.md) | why the planes are drawn where they are |
