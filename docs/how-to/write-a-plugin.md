# Write a plugin

**Goal:** package a bundle of contributions — tools, guards, reminders, a
policy, prompt fragments, child agents — as a **Plugin** carrying a static
manifest, then **activate** it on the agents that should use it. From a single
file, a plugin directory, or an installed package.

**Before you start:** you are comfortable with `Options` and `Client` from
[Your first agent](../tutorials/first-agent.md), and with at least one
contribution type — a [custom tool](build-custom-tools.md) or a
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

## A single-file plugin

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
print(pset.names())               # ('brevity',)
print(pset.contributions())       # every contribution — no plugin code ran
```

`contributions()` answers "what does this plugin contribute?" **without
importing its code**.

### Activate it on an agent

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

## The surfaces you can contribute to

`PluginBuilder` has one method per surface. The full contract (plane,
activation scope, collision, ordering) is in the [Plugins
reference](../reference/plugins.md); the common ones:

| Method | Contributes | Follows activation? |
| --- | --- | --- |
| `tool(fn)` | a `@tool` or a built-in name | yes (per-agent) |
| `contribute("agent", defn, name=...)` | a child `AgentDefinition` (the generic path — there is no dedicated method) | yes |
| `prompt_fragment(text, name=...)` | text appended after the prompt | yes |
| `reminder(fn, priority=...)` | a compose-time, **pure** reminder | yes |
| `reminder_provider(fn, seams=[...])` | a **recorded** injection provider (may query a DB) | yes |
| `tool_result_transform(fn, priority=...)` | a `ToolResult → ToolResult` stage before recording | yes |
| `session_pack(factory, priority=...)` | a session-build contribution (tools, backends, residents) | yes |
| `policy(factory)` | the agent's decision policy (single-valued) | yes |
| `guard(obj)` / `observer(fn)` | governance hooks | **no — process-wide** |
| `sandbox_provider(obj)` | a sandbox backend the host selects | no — host wiring |

> **Governance is not opt-out.** A loaded `guard` or `observer` is in force for
> **every** agent in the process, whether or not that agent activated the
> plugin — an agent author must not be able to skip compliance interception or
> audit by omitting an activation. Everything else follows per-agent activation.

## Contributing several things

One plugin can fill several surfaces. Here a guard plugin — governance, so it
applies process-wide once loaded:

```python
# block_shell.py
from noeta.sdk import PluginBuilder, ProposedToolCall, VerdictResult

plugin = PluginBuilder("block-shell")


class BlockShellGuard:
    name = "block_shell"
    priority = 25

    def check(self, action, ctx) -> VerdictResult:
        if isinstance(action, ProposedToolCall) and action.call.tool_name == "shell_run":
            return VerdictResult.deny("shell_run is disabled by block-shell")
        return VerdictResult.allow()


plugin.guard(BlockShellGuard(), name="block_shell")
```

```python
pset = load_plugins(modules=["./block_shell.py"])
client = Client(options, provider=my_provider, workspace_dir=".", plugins=pset)
# the guard gates shell_run for every agent — no activation needed
```

## Config

A manifest may declare a `config-schema` table describing the operator config
the plugin expects. Host-supplied config reaches a `session_pack` contribution
through `SessionBuildContext.config("<plugin name>")` — each pack parses only
its own entry. Validate it and raise on bad input: the loader wraps the raise in
a `PluginError` naming your plugin, so a misconfiguration fails the client build
**loudly at startup** rather than a mid-session turn.

## Packaging an installable plugin

A single file is enough for local use. To distribute a plugin so a host
discovers it after `pip install`, ship a package with three parts:

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
house-style = "house_style"          # membership marker; the manifest is the source of truth

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

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

```
house_style/
├── __init__.py
├── noeta-plugin.toml        # verbatim copy of [tool.noeta] as a bare-key table
└── tools.py
```

The loader locates `noeta-plugin.toml` by its basename in the distribution's
file list (regular install) or beside the package via
`importlib.util.find_spec` (editable install); either way the `ref` strings
resolve **only** at the execution boundary, so discovery and listing never
import `house_style`. `python -m noeta.sdk.plugin_check` (there is no console
script) derives the TOML from a plugin's declarations and checks the shipped
manifest matches, so it cannot drift from the code.

A host discovers every installed plugin with `entry_points=True`. Installed
plugins are arbitrary code, so a server-style host also passes an `enabled`
allow-list — only approved plugins load, everything else is skipped **before it
is imported**:

```python
pset = load_plugins(entry_points=True, enabled=["house-style"])
```

## Loading from a directory, and trust

Local and dev hosts can drop plugins into a directory instead of installing
them. A scanned directory takes two kinds of entry:

- a **sub-directory** with a `noeta-plugin.toml` (read with zero execution), or
- a top-level **single-file** `.py` plugin (files starting with `_` are
  skipped).

There are two directory sources, differing by trust:

- **`user_dirs`** — scanned unconditionally, e.g. a host's own
  `~/.noeta/plugins`.
- **`workspace_dirs`** — a `.noeta/plugins` under a checkout the agent operates
  on. Because that directory travels with untrusted code, it is scanned **only**
  when its absolute path is recorded in the trust store; otherwise it is skipped
  with a loud `UntrustedPluginDirWarning`, never silently.

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")           # writes ~/.noeta/trust.json
pset = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

> A directory plugin is arbitrary Python the host process runs. The trust gate
> makes loading one a deliberate act, but it is **not** a sandbox — grant trust
> only to a workspace you would run code from. Server-style hosts should stay on
> entry points plus an `enabled` allow-list, and leave directory sources off.

## Testing it

Load end-to-end and assert on the `PluginSet` — all public surface, no
discovery internals. Listing is execution-free, so you can assert on a plugin's
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

To exercise a contribution's behaviour, construct it directly and call it — for
a guard, drive its `check` against `ProposedToolCall`s. The directories under
`packages/noeta-sdk/noeta/builtins/` (one per built-in, each holding its
`MANIFEST`) are the canonical worked declarations: every surface has a built-in
reference.

## What activation changes

Activating a plugin changes the agent's identity — its tools, child agents,
prompt fragments, policy — which turns over the KV-cache prefix. Plan an
agent's plugin set the way you plan its tool set, not per turn. A loaded
`guard` / `observer` is process wiring and does **not** touch identity, so
governance can be added without a prefix turnover.

## See also

- [Plugins reference](../reference/plugins.md) — the manifest format, the full
  surface catalog, the sources, `PluginSet`, and the trust store
- [Build custom tools](build-custom-tools.md) — the `@tool` a plugin contributes
- [Guard vs Observer](../concepts/guard-observer.md) — the governance hooks a
  plugin bundles
