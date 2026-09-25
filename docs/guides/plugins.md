# Write a plugin

A plugin bundles tools, guards, reminders, prompt text or child agents behind one
name, so a host loads it once and each agent opts in. Noeta's own capabilities
are built-in plugins on the same path.

## The smallest plugin

One `.py` file with a module-level `PluginBuilder`:

```python
# brevity.py
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity")
plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")
```

Load it and list what it contributes — without running any of its code:

```python
from noeta.sdk import load_plugins

pset = load_plugins(builtins=False, modules=["./brevity.py"])
print(pset.names())
print([(c.surface, c.name) for _plugin, c in pset.contributions()])
```

```
('brevity',)
[('prompt_fragment', 'be-brief')]
```

## Activate it on an agent

Loading makes a plugin available to the process; `Options.plugins` decides which
agent uses it:

```python
from noeta.sdk import DEFAULT_PLUGINS, Client, Options, load_plugins
from noeta.sdk.providers import AnthropicProvider

pset = load_plugins(modules=["./brevity.py"])        # built-ins + brevity

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("brevity",),          # ("fs", "web", "brevity")
)
client = Client(options, provider=AnthropicProvider(), model="claude-sonnet-5", workspace_dir=".", plugins=pset)
```

- The agent's instructions now end with "Answer in at most three sentences."
  An agent that doesn't list `"brevity"` doesn't get it.
- A name in `Options.plugins` that isn't loaded fails the `Client` build at startup.
- Activation is part of the agent's identity: changing it changes the cached
  prompt prefix, so set it per agent, not per turn.

## Pick a surface

`PluginBuilder` has one method per surface; the rest go through
`contribute(surface, value, name=...)`. Full contract per surface:
[Plugin surfaces](../reference/plugin-surfaces.md).

| You want to | Method | Applies to |
| --- | --- | --- |
| add a tool | `tool(fn)` | agents that activate the plugin |
| add a child agent | `contribute("agent", defn, name=...)` | agents that activate it |
| append text to the system prompt | `prompt_fragment(text, name=...)` | agents that activate it |
| inject a note each turn (pure function) | `reminder(fn, priority=...)` | agents that activate it |
| inject a recorded note (may read a DB) | `reminder_provider(fn, seams=[...])` | agents that activate it |
| rewrite tool results before they are recorded | `tool_result_transform(fn)` | agents that activate it |
| build tools or backends per task, with config | `session_pack(factory)` | agents that activate it |
| replace the decision policy | `policy(factory)` | agents that activate it (one policy per agent) |
| block or allow actions | `guard(obj)` | **every agent in the process** |
| watch events (audit, metrics) | `observer(fn)` | **every agent in the process** |
| ship `SKILL.md` packs | `contribute("skills", name=..., path="/abs/dir")` | the whole host, once loaded |
| ship an in-process MCP server | `contribute("mcp_server", server, name=alias)` | the whole host, once loaded |
| offer a sandbox backend | `sandbox_provider(obj)` | the host picks one |

::: warning Guards and observers can't be skipped
A loaded `guard` or `observer` applies to every agent, whether or not it
activated the plugin. That way an agent author can't opt out of compliance
checks or auditing.
:::

A guard plugin:

```python
# block_shell.py
from noeta.sdk import PluginBuilder, ProposedToolCall, VerdictResult

plugin = PluginBuilder("block-shell")


class BlockShellGuard:
    name = "block_shell"
    priority = 25

    def check(self, action, ctx) -> VerdictResult:
        if isinstance(action, ProposedToolCall) and action.call.tool_name == "Bash":
            return VerdictResult.deny("Bash is disabled by block-shell")
        return VerdictResult.allow()


plugin.guard(BlockShellGuard(), name="block_shell")
```

`load_plugins(modules=["./block_shell.py"])` is enough — no activation needed.

## Take operator config

The host passes per-plugin config in `HostConfig.plugin_config`; a
`session_pack` reads its own entry:

```python
host_config = HostConfig(plugin_config={"house-style": {"max_words": 120}})

def build_house_style_pack(ctx):              # your session_pack factory
    max_words = ctx.config("house-style").get("max_words")
    ...
```

Validate and raise on bad input: the error surfaces as a `PluginError` naming
your plugin when the `Client` is built.

## Package it

To ship a plugin that hosts find after `pip install`, publish a package with:

1. the manifest under `[tool.noeta]` in `pyproject.toml`;
2. the same manifest as `noeta-plugin.toml` inside the package — the loader reads
   this without importing your code;
3. an entry point in the `noeta.plugins` group (the plugin's name comes from the
   manifest, not the entry-point key).

```toml
[project]
name = "noeta-plugin-house-style"
version = "0.1.0"
dependencies = ["noeta-sdk"]

[project.entry-points."noeta.plugins"]
house-style = "house_style"

[tool.noeta]
name = "house-style"
requires-noeta = ">=0.6"

[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
text    = "Answer in at most three sentences."

[[tool.noeta.contributions]]
surface = "tool"
ref     = "house_style.tools:LintTool"
```

`python -m noeta.sdk.plugin_check PATH` checks a `PluginBuilder` file against its
shipped manifest; `--emit` prints the manifest derived from the builder.

A host loads installed plugins with an allow-list — anything not listed is
skipped before it is imported:

```python
pset = load_plugins(entry_points=True, enabled=["house-style"])
```

Dev hosts can also load plugins from a directory (`user_dirs=`, or a trusted
`workspace_dirs=` after `grant_trust(path)`). A directory plugin is ordinary Python
running in your process — trust only code you would run anyway. The
`examples/plugins/` directory has six complete plugins.

## Next

- [Plugin manifest](../reference/plugin-manifest.md) — manifest fields, load sources, trust, versioning
- [Plugin surfaces](../reference/plugin-surfaces.md) — every surface with a built-in example
- [Plugin system](../how-it-works/plugin-system.md) — how loading and activation fit together
