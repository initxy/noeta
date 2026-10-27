# Noeta

**Durable AI agents for Python.** Every task is an append-only event log with exactly one
writer, so an agent survives crashes without re-running a tool, waits days for a person while
holding no worker, and runs on one laptop or many machines without changing the agent.

> 1.0 is in development on `main`. The last 0.x release is `v0.6.42`.

## Use it

```bash
uv tool install 'noeta[tui]'
export NOETA_MODEL=claude-sonnet-4-6 NOETA_API_KEY=sk-ant-...
noeta                      # full-screen terminal UI
noeta -p "fix the failing test"   # one prompt, print the answer
noeta acp                  # an Agent Client Protocol agent for Zed and other editors
```

Settings come from `NOETA_*` variables or `~/.noeta/config.toml`; tasks live in
`~/.noeta/tasks.db` unless `--store` names another SQLite file or a Postgres URL.

## Embed it

```python
import asyncio
from noeta.client import Client
from noeta.events import ModelReplied
from noeta.presets import default_agents
from noeta.providers import provider_for
from noeta.runner import Runtime
from noeta.store.sqlite import SqliteStore

async def main() -> None:
    store = SqliteStore("tasks.db")
    agents = {a.name: a for a in default_agents("claude-sonnet-4-6")}
    provider = provider_for("anthropic", api_key="sk-ant-...")
    runtime = Runtime(store=store, agents=agents, provider_for=lambda _model: provider)
    client = Client(store)
    task = await client.create("main", "What does this repository do?",
                               config={"cwd": "."})
    await runtime.run_until_idle()
    replies = [r.event for r in await client.events(task)
               if isinstance(r.event, ModelReplied)]
    print(replies[-1].response.message.text)

asyncio.run(main())
```

Run more workers on the same Postgres store (`noeta worker --store postgres://…`) to spread
tasks across machines; serve the client API over HTTP with `noeta serve`.

## Develop

```bash
make install   # uv sync with every extra
make check     # every gate CI runs
```

`AGENTS.md` is the working agreement; `CONTEXT.md` holds the domain vocabulary.

## License

Apache-2.0
