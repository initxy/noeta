# Noeta

**Durable AI agents for Python.** Every task is an append-only event log with exactly one
writer, so an agent survives crashes without re-running a tool, waits days for a person while
holding no worker, and runs on one laptop or many machines without changing the agent.

> 1.0 is in development on `main`. The last 0.x release is `v0.6.42`.

## Use it

```bash
uv tool install 'noeta[tui,images]'    # images: shrink large images as Claude Code does
export NOETA_MODEL=claude-sonnet-4-6 NOETA_API_KEY=sk-ant-...
noeta                             # full-screen terminal UI
noeta -p "fix the failing test"   # one prompt, print the answer
noeta acp                         # an Agent Client Protocol agent for Zed and other editors
```

Settings come from `NOETA_*` variables or `~/.noeta/config.toml`; tasks live in
`~/.noeta/tasks.db` unless `--store` names another SQLite file or a Postgres URL.

## Embed it

```python
import asyncio
import noeta
from noeta.core.events import ModelReplied

async def main() -> None:
    store = noeta.store("sqlite:///tasks.db")
    agents = {a.name: a for a in noeta.default_agents("claude-sonnet-4-6")}
    model = noeta.provider("anthropic", api_key="sk-ant-...")
    runtime = noeta.Runtime(store=store, agents=agents, provider_for=lambda _name: model)
    client = noeta.Client(store)
    task = await client.create("main", "What does this repository do?", config={"cwd": "."})
    await runtime.run_until_idle()
    replies = [r.event for r in await client.events(task) if isinstance(r.event, ModelReplied)]
    print(replies[-1].response.message.text)

asyncio.run(main())
```

Run more workers on the same Postgres store (`noeta worker --store postgres://…`) to spread
tasks across machines; serve the client API over HTTP with `noeta serve` and reach it with
`noeta.RemoteClient`. `noeta.LocalApp` is a store, a worker and a client in one process.
Everything an embedder needs is outside `noeta.cli`, which is the `noeta` command itself.

## Extend it

Everything outside the small kernel is a plugin, found by name through Python entry points —
the built-in ones exactly like yours.

- **Plugins** subclass `noeta.Extension`: tools, system-prompt parts, reminders, slash
  commands, gates on tool calls, hooks on events, state folded from the task's log. Register one
  under the `noeta.plugins` entry point group, or drop the file in `~/.noeta/plugins/` (a
  project's `.noeta/plugins/`, like its `.noeta/agents`, `prompts` and `skills`, loads after
  `noeta trust`), and name it in an agent file or `config.toml`. A tool can ask the person
  (`noeta.ask`) and tell them things (`notify`); the terminal UI and editors show both. `examples/hello-plugin` is a complete one.
- **Plugin settings** live in `~/.noeta/config.toml`:

  ```toml
  [plugins.shell]
  background = true          # a plugin's arguments
  [plugins.web]
  enabled = false            # off for every agent
  [agents.main]
  add_plugins = ["hello"]    # add one without copying the agent file
  ```

- **Agents** are markdown files: YAML frontmatter (`name`, `description`, `model`, `plugins`,
  optional `tools`) and the system prompt as body. Drop one in `~/.noeta/agents/` or
  `.noeta/agents/`; Claude Code agent files work as they are.
- **Prompts** have names (`system/main`, `tool/Read`, `reminder/todo`, …). A file
  `.noeta/prompts/tool/Read.md` replaces that text, `tool/Read.append.md` adds to it; so do
  `~/.noeta/prompts/`, a task's `prompts` config, and `--system-prompt` /
  `--append-system-prompt`. A task keeps the texts it started with.
- **Hooks and MCP servers** in Claude Code's format (`~/.noeta/settings.json`): hooks run
  commands on `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop` and `Notification`, and
  can add context or block; `mcpServers` are started for every task. A workspace's `.mcp.json`
  servers start only once named in `enabledMcpjsonServers` (or `enableAllProjectMcpServers`).
- **Backends** — model providers, execution environments, stores — register under
  `noeta.providers`, `noeta.execenvs` and `noeta.stores`. A store passes the tests in
  `noeta.testing.store_contract` (import them next to a `store` fixture).
- **Shared plugin code** — path handling, output limits, shell command checks, the context
  budget — is in `noeta.toolkit`, for your plugins as for the built-ins.

## Develop

```bash
make install   # uv sync with every extra
make check     # every gate CI runs
```

`AGENTS.md` is the working agreement; `CONTEXT.md` holds the domain vocabulary and the layers.

## License

Apache-2.0
