# Write a plugin

This guide shows you how to package a bundle of contributions — tools, guards,
reminders, a policy, prompt fragments, child agents — as a **plugin** carrying a
static manifest, and activate it on the agents that should use it. You need
`Options` and `Client` from [Your first agent](../tutorials/first-agent.md) and
at least one thing to contribute: a [custom tool](build-custom-tools.md) or a
[Guard](../concepts/guard-observer.md).

## The model: manifest, load, activate

A plugin is a package (or a single `.py` file) carrying a **static manifest** —
a name plus a list of *contributions*, each naming a **surface** (`tool`,
`guard`, `reminder`, …) and pointing at the code that fills it. Three steps put
a plugin to work:

1. **Declare** — write the manifest (a `[tool.noeta]` table, or `PluginBuilder`
   calls in a single file).
2. **Load** — `load_plugins(...)` reads the manifests into a `PluginSet`
   *without running any plugin code*. This is a host-level step: it decides
   which plugin code is available in the process.
3. **Activate** — name the plugins an agent uses in `Options.plugins`, and hand
   the loaded set to `Client(options, plugins=...)`. Activation is per-agent and
   enters the agent's identity.

A plugin adds **no new power** to the engine — it only populates the sixteen
extension surfaces. What it buys you is discovery, a zero-execution listing, a
deterministic collision-checked merge, and per-agent activation.

## 1. Write a single-file plugin

The smallest plugin is one `.py` file with a module-level `PluginBuilder`:

```python
# brevity.py — contributes one prompt fragment.
from noeta.sdk import PluginBuilder

plugin = PluginBuilder("brevity", requires_noeta=">=0.4")

# a static prompt fragment, appended after the agent's system prompt
plugin.prompt_fragment("Answer in at most three sentences.", name="be-brief")
```

`PluginBuilder(name)` is the manifest; each method records one contribution.
Load it by path — `builtins=False` keeps the built-in catalog out so you see
only your plugin:

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

`contributions()` answers "what does this plugin contribute?" **without importing
its code**.

## 2. Activate it on an agent

Loading makes the plugin *available*; activation decides which agents use it.
Add its name to `Options.plugins` and pass the loaded set to `Client`:

```python
from noeta.sdk import Options, Client, DEFAULT_PLUGINS

# built-ins on, plus the local plugin
pset = load_plugins(modules=["./brevity.py"])

options = Options(
    system_prompt="You are a coding agent.",
    plugins=DEFAULT_PLUGINS + ("brevity",),   # fs, web, and brevity
)

client = Client(options, provider=my_provider, workspace_dir=".", plugins=pset)
```

The agent's compiled instructions end with *"Answer in at most three
sentences."* — the prompt fragment is folded into the agent's identity. A
sibling agent that does **not** list `"brevity"` does not get it: feature
surfaces follow activation.

> `DEFAULT_PLUGINS = ("fs", "web")` is the default of `Options.plugins`. Both
> are identity-inert — the default tool set comes from the built-in tool
> catalogue either way — so a bare `Options()` carries no extra identity. You
> add identity only by activating something with an effect.

## 3. Pick your surfaces

`PluginBuilder` has one method per surface. The full contract (plane, activation
scope, collision, ordering) is in the
[plugin surfaces reference](../reference/plugin-surfaces.md); the common ones:

| Method | Contributes | Follows activation? |
| --- | --- | --- |
| `tool(fn)` | a `@tool` or a built-in name | yes (per-agent) |
| `contribute("agent", defn, name=...)` | a child `AgentDefinition` (the generic path — there is no dedicated method) | yes |
| `prompt_fragment(text, name=...)` | text appended after the prompt | yes |
| `reminder(fn, priority=...)` | a compose-time, **pure** reminder | yes |
| `reminder_provider(fn, seams=[...])` | a **recorded** injection provider (may query a DB) | yes |
| `tool_result_transform(fn, priority=...)` | a `ToolResult → ToolResult` stage before recording | yes |
| `session_pack(factory, priority=...)` | a session-build contribution (tools, backends, residents) | yes |
| `control_tool(factory, priority=...)` | a control-tool mount | yes |
| `policy(factory)` | the agent's decision policy (single-valued) | yes |
| `guard(obj)` / `observer(fn)` | governance hooks | **no — process-wide** |
| `contribute("skills", name=..., path="/abs/dir")` | a directory of `SKILL.md` packs | no — host-wired, in force once loaded |
| `contribute("mcp_server", server, name="<alias>")` | an in-process `SdkMcpServer` (`create_sdk_mcp_server`) | no — host-wired, in force once loaded |
| `sandbox_provider(obj)` | a sandbox backend the host selects | no — host-resolved listing |

> **Governance is not opt-out.** A loaded `guard` or `observer` is in force for
> **every** agent in the process, whether or not that agent activated the
> plugin — an agent author must not be able to skip compliance interception or
> audit by omitting an activation. Everything else follows per-agent activation.

> **Host-wired is not opt-out either, for a different reason.** `skills` and
> `mcp_server` are the host's catalogue, not an agent's feature: loading the
> plugin is what puts them in the process. The `skills` path must be
> **absolute** (build it from `Path(__file__).parent`), and its packs sit at the
> lowest tier — the user's own `~/.noeta/skills` and workspace `.noeta/skills`
> still shadow a same-named skill. `provider` and `sandbox_provider` are
> **host-resolved listings**: declaring one makes it discoverable and
> collision-checked, and the host wires the one it chose by hand.

> **A skill's `allowed-tools` cannot name your plugin's own tools.** The
> recognition set is the static list of tool names a session can mount, so a
> `SKILL.md` you ship declaring `allowed-tools: [Read, my_plugin_tool]` grants
> `Read` and drops the rest with a warning. Gate a plugin tool with a `guard`
> instead.

## 4. Contribute something with teeth

One plugin can fill several surfaces. Here is a guard plugin — governance, so it
applies process-wide once loaded:

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

```python
pset = load_plugins(modules=["./block_shell.py"])
client = Client(options, provider=my_provider, workspace_dir=".", plugins=pset)
# the guard now gates Bash for every agent — no activation needed
```

## 5. Take operator config

A manifest may declare a `config-schema` table describing the operator config
the plugin expects. The host supplies it as `HostConfig.plugin_config`, keyed by
plugin name, and it reaches a `session_pack` contribution through
`SessionBuildContext.config("<plugin name>")` — each pack parses only its own
entry:

```python
# the host side
client = Client(
    options,
    provider=my_provider,
    plugins=pset,
    host_config=HostConfig(plugin_config={"house-style": {"max_words": 120}}),
)
```

```python
# your pack side
def build_house_style_pack(ctx):
    max_words = ctx.config("house-style").get("max_words")
    if max_words is None:
        return PackContribution()        # self-gate: unconfigured ⇒ contribute nothing
    ...
```

A name the SDK derives nothing for — every third-party plugin — is passed
through verbatim. For the four built-ins the SDK configures itself (`fs`,
`skills`, `workspace`, `memory`) the host's keys are overlaid **per key**, so
overriding one leaves the rest in place.

Validate it and raise on bad input: the loader wraps the raise in a
`PluginError` naming your plugin, so a misconfiguration fails the client build
**loudly at startup** rather than a mid-session turn.

## 6. Package it for distribution

A single file is enough for local use. To distribute a plugin so a host discovers
it after `pip install`, ship a package with three parts:

1. the manifest under **`[tool.noeta]`** in `pyproject.toml` (the authoring
   source, and what `plugin_check` verifies);
2. a **matching `noeta-plugin.toml`** shipped as package data *inside* the
   package (`house_style/noeta-plugin.toml`) — this is what the loader reads off
   the installed distribution, **without importing `house_style`**;
3. an **entry point** in the SDK-owned `noeta.plugins` group, which is only a
   group-membership marker — the plugin's name comes from the manifest, not the
   entry-point key.

```toml
# pyproject.toml
[project]
name = "noeta-plugin-house-style"
version = "0.1.0"
dependencies = ["noeta-sdk"]

[project.entry-points."noeta.plugins"]
house-style = "house_style"   # membership marker only; the manifest names the plugin

# the authoring manifest (mirror it verbatim into house_style/noeta-plugin.toml)
[tool.noeta]
name = "house-style"
requires-noeta = ">=0.4"

[[tool.noeta.contributions]]
surface = "prompt_fragment"
name    = "house-style"
ref     = "house_style:HOUSE_STYLE"

[[tool.noeta.contributions]]
surface = "tool"
ref     = "house_style.tools:LintTool"
```

```
house_style/
├── __init__.py
├── noeta-plugin.toml        # verbatim copy of [tool.noeta] as a bare-key table
└── tools.py
```

The loader finds `noeta-plugin.toml` by basename in the distribution's file list
(regular install) or beside the package via `importlib.util.find_spec` (editable
install). Either way the `ref` strings resolve **only** at the execution
boundary, so discovery and listing never import `house_style`.
`python -m noeta.sdk.plugin_check` (there is no console script) derives the TOML
from your declarations and checks the shipped manifest matches, so the two cannot
drift.

A host discovers every installed plugin with `entry_points=True`. Installed
plugins are arbitrary code, so a server-style host also passes an `enabled`
allow-list — only approved plugins load, everything else is skipped **before it
is imported**:

```python
pset = load_plugins(entry_points=True, enabled=["house-style"])
```

## 7. Optional: load from a directory, and trust

Local and dev hosts can drop plugins into a directory instead of installing them.
A scanned directory takes either a **sub-directory** with a `noeta-plugin.toml`
(read with zero execution) or a top-level **single-file** `.py` plugin (files
starting with `_` are skipped).

There are two directory sources, differing by trust: **`user_dirs`** is scanned
unconditionally (a host's own `~/.noeta/plugins`), while **`workspace_dirs`** — a
`.noeta/plugins` under a checkout the agent operates on — is scanned **only** when
its absolute path is recorded in the trust store. Otherwise it is skipped with a
loud `UntrustedPluginDirWarning`, never silently.

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")           # writes ~/.noeta/trust.json
pset = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

> A directory plugin is arbitrary Python the host process runs. The trust gate
> makes loading one a deliberate act, but it is **not** a sandbox — grant trust
> only to a workspace you would run code from. Server-style hosts should stay on
> entry points plus an `enabled` allow-list, and leave directory sources off.

## 8. Test it

Load end-to-end and assert on the `PluginSet` — all public surface, no discovery
internals. Listing is execution-free, so you can assert on a plugin's
contributions without running it:

```python
from noeta.sdk import load_plugins

def test_block_shell_declares_its_guard():
    pset = load_plugins(builtins=False, modules=["./block_shell.py"])
    listed = [(c.surface, c.name) for _plugin, c in pset.contributions()]
    assert ("guard", "block_shell") in listed

def test_guard_is_process_wide():
    pset = load_plugins(builtins=False, modules=["./block_shell.py"])
    guards, _observers = pset.process_hooks()
    assert [type(g).__name__ for g in guards] == ["BlockShellGuard"]
```

```
2 passed
```

To exercise a contribution's behaviour, construct it directly and call it — for a
guard, drive its `check` against `ProposedToolCall`s. The directories under
`packages/noeta-sdk/noeta/builtins/` are the canonical worked declarations: every
surface has a built-in reference.

## What activation changes

Activating a plugin changes the agent's identity — its tools, child agents,
prompt fragments, policy — which turns over the KV-cache prefix. Plan an agent's
plugin set the way you plan its tool set, not per turn. A loaded `guard` /
`observer` is process wiring and does **not** touch identity, so governance can
be added without a prefix turnover.

## Next steps

- [Plugin manifest reference](../reference/plugin-manifest.md) — the manifest
  shape, loading sources, and versioning
- [Plugin surfaces reference](../reference/plugin-surfaces.md) — all sixteen,
  one section each
- [Extension planes](../architecture/extension-planes.md) — how the loader and
  the planes fit together
- [Guard vs Observer](../concepts/guard-observer.md) — the governance hooks a
  plugin bundles
