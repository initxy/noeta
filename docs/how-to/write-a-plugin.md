# Write a plugin

**Goal:** package a bundle of contributions — tools, guards, observers, a
provider, content kinds, child agents — as a **Plugin** a host discovers and
merges into its `Options`, from a single file, a plugin directory, or an
installed entry point.

**Before you start:** you are comfortable with `Options` and `Client` from
[Your first agent](../tutorials/first-agent.md), and with at least one
contribution type — a [custom tool](build-custom-tools.md) or a
[Guard](../concepts/guard-observer.md).

## What a plugin is

A plugin is a Python module that exports one factory, `noeta_plugin(api)`. The
factory receives a `PluginAPI` accumulator and calls its `add_*` / `set_*`
methods to record what the plugin contributes. Loading the plugin runs the
factory; `merge_plugins` folds every loaded plugin's contributions into a base
`Options`; you compile and run that `Options` as usual.

A plugin adds **no new power** to the engine — it only populates the extension
surfaces `Options` already exposes. It is the *packaging* of contributions, not
a new kind of contribution. What it buys you is discovery (ship it, a host finds
it) and a deterministic, collision-checked merge.

## A single-file plugin

The smallest plugin is one `.py` file that exports `noeta_plugin`:

```python
# my_plugin.py — a plugin contributing one Guard.
from noeta.protocols.hooks import (
    GuardContext, ProposedAction, ProposedToolCall, VerdictResult,
)


class BlockShellGuard:
    name = "block_shell"
    priority = 25

    def check(self, action: ProposedAction, ctx: GuardContext) -> VerdictResult:
        if isinstance(action, ProposedToolCall) and action.call.tool_name == "shell_run":
            return VerdictResult.deny("shell_run is disabled by the block-shell plugin")
        return VerdictResult.allow()


def noeta_plugin(api) -> None:
    api.add_guard(BlockShellGuard())
```

Load it by path and merge it into an `Options`:

```python
from noeta.sdk import Options, load_plugins, merge_plugins

plugins = load_plugins(modules=["./my_plugin.py"])
options = merge_plugins(Options(system_prompt="You are a helpful agent."), plugins)
```

`options.guards` now carries `BlockShellGuard`. Hand `options` to `Client` the
way you would any other.

## The factory and `PluginAPI`

The factory is the only required export. It receives a `PluginAPI`, records
contributions, and returns nothing. `PluginAPI` holds **no live engine
handles** — each method is a pure accumulation:

| Method | Contributes | Lands on |
| --- | --- | --- |
| `add_tool(tool)` | a built-in name string, or a `@tool`-decorated tool | `Options.allowed_tools` |
| `add_guard(guard)` | a `Guard` | `Options.guards` |
| `add_observer(observer)` | a post-commit `Observer` (a callable) | `Options.observers` |
| `set_provider(provider)` | the single `LLMProvider` | `Options.provider` |
| `add_content_kind(spec)` | a `ContentKindSpec` | `Options.content_channels` |
| `add_agent(name, definition)` | a child `AgentDefinition` | `Options.agents` |
| `add_mcp_server(alias, spec)` | a host-plane MCP server spec | host `HostConfig` (not `Options`) |
| `add_skill_dir(path)` | a host-plane skill directory | host `HostConfig` (not `Options`) |

Validation is eager: an unknown built-in tool name, a non-`ContentKindSpec`
content kind, a second `set_provider`, or a duplicate name *within one plugin*
raises `PluginError` at factory time, naming the plugin. Cross-plugin collisions
are caught later, by `merge_plugins`.

A plugin can contribute several things at once:

```python
def noeta_plugin(api) -> None:
    api.add_tool(fetch_weather)             # a @tool (see Build custom tools)
    api.add_agent("researcher", researcher) # an AgentDefinition
    api.add_guard(BlockShellGuard())
```

> **Two planes.** Most methods land on `Options` and become part of the agent's
> identity or wiring. `add_mcp_server` and `add_skill_dir` are **host-plane**:
> they do not enter `Options`. A host reads them with `merged_mcp_servers` /
> `merged_skill_dirs` and wires them into its `HostConfig`. See the
> [Plugins reference](../reference/plugins.md).

## Reading config

A factory may declare a second parameter to receive operator config:

```python
def noeta_plugin(api, config) -> None:
    threshold = config.get("threshold", 10)
    api.add_guard(ThresholdGuard(threshold))
```

The loader inspects the factory signature: only a factory that declares a second
positional parameter is called with config; a one-argument factory is called
with just the API. Pass config keyed by plugin name:

```python
plugins = load_plugins(
    modules=["./my_plugin.py"],
    config={"my_plugin": {"threshold": 5}},
)
```

Validate config inside the factory and raise on bad input — the loader wraps the
raise in a `PluginError` naming your plugin, so a misconfiguration fails the
client build **loudly at startup** rather than a mid-session turn. The
first-party [`approval-modes`](https://github.com/initxy/noeta/tree/main/examples/plugins/approval-modes)
plugin is the reference for a config-driven plugin.

The plugin's name defaults to the module stem (`my_plugin` for `my_plugin.py`).
To fix a stable name — the key the `config` map and the `enabled` allow-list use,
independent of the filename — set `noeta_plugin_name` at module top level:

```python
noeta_plugin_name = "block-shell"
```

## Testing it

Load the plugin end-to-end and assert on the merged `Options` — all public
surface, no discovery internals:

```python
from noeta.sdk import Options, load_plugins, merge_plugins

def test_block_shell_lands_a_guard():
    plugins = load_plugins(modules=["./my_plugin.py"])
    options = merge_plugins(Options(system_prompt="root"), plugins)
    names = [getattr(g, "name", None) for g in options.guards]
    assert "block_shell" in names
```

To exercise a contribution's behaviour, construct it directly and call it —
e.g. build the guard and drive its `check` against `ProposedToolCall`s. The
first-party
[`tests/test_example_approval_modes.py`](https://github.com/initxy/noeta/blob/main/tests/test_example_approval_modes.py)
shows both halves: per-verdict unit tests on the guard, plus a
`load_plugins` + `merge_plugins` end-to-end test.

## Packaging with an entry point

A single file is enough for local use. To distribute a plugin so a host
discovers it after `pip install`, ship it as a package that declares an entry
point in the SDK-owned `noeta.plugins` group:

```toml
# pyproject.toml
[project]
name = "noeta-plugin-block-shell"
version = "0.1.0"
dependencies = ["noeta-sdk"]

[project.entry-points."noeta.plugins"]
block-shell = "block_shell.plugin:noeta_plugin"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

The entry-point value points at the `noeta_plugin` factory. A host started with
`load_plugins(entry_points=True)` discovers every installed plugin in the group.
(A dash is not a valid Python module name, so the importable package is
`block_shell` even though the plugin names itself `block-shell` via
`noeta_plugin_name`.)

Installed plugins are arbitrary code the host runs, so a server-style host also
passes an explicit `enabled` allow-list — only operator-approved plugins load,
everything else is skipped before it is imported:

```python
plugins = load_plugins(entry_points=True, enabled=["block-shell", "approval-modes"])
```

See the packaged first-party example at
[`examples/plugins/approval-modes/pyproject.toml`](https://github.com/initxy/noeta/blob/main/examples/plugins/approval-modes/pyproject.toml).

## Loading from a directory, and trust

Local and dev hosts can drop plugin files into a directory instead of installing
them. There are two directory sources, and they differ by trust:

- **Trusted dirs** (`trusted_dirs=`) — scanned unconditionally, e.g. a host's
  own `~/.noeta/plugins`.
- **Workspace dirs** (`workspace_dirs=`) — a `.noeta/plugins` under a checkout
  the agent operates on. Because that directory travels with untrusted code, it
  is scanned **only** when its absolute path is recorded in the trust store;
  otherwise it is skipped with a loud `UntrustedPluginDirWarning`, never
  silently.

Record trust once, then the directory loads:

```python
from noeta.sdk import grant_trust, load_plugins

grant_trust("./workspace/.noeta/plugins")      # writes ~/.noeta/trust.json
plugins = load_plugins(workspace_dirs=["./workspace/.noeta/plugins"])
```

Each top-level `.py` in a scanned directory (files starting with `_` are
skipped) must export `noeta_plugin`.

> A directory plugin is arbitrary Python the host process runs. The trust gate
> makes loading one a deliberate act, but it is **not** a sandbox — grant trust
> only to a workspace you would run code from. Server-style hosts should stay on
> entry points plus an `enabled` allow-list, and leave directory sources off.

## What happens on merge

`merge_plugins(options, plugins)` sorts contributions by
`(plugin name, contribution name)` before folding, so the compiled `AgentSpec`
is invariant under plugin load order. Any name collision — two plugins
contributing the same tool / agent / content kind / MCP alias, a second
provider, or a name already present on the base `Options` — raises `PluginError`
naming **both** sources. There is no override flag: a collision is always an
error.

> Changing the plugin set changes the agent's identity (its tools and agents),
> which turns over the KV-cache prefix. That is intended — but plan a plugin set
> the way you plan a tool set, not per turn.

## See also

- [Plugins reference](../reference/plugins.md) — the full `PluginAPI`,
  `load_plugins`, the merge and trust-store API
- [Build custom tools](build-custom-tools.md) — the `@tool` a plugin contributes
- [Guard vs Observer](../concepts/guard-observer.md) — the hook roles a plugin bundles
- [ADR: Plugin contribution bundles](https://github.com/initxy/noeta/blob/main/docs/adr/plugin-contribution-bundles.md)
  — the design rationale
