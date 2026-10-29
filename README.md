# noeta

**An open-source Claude Code that runs on servers.** The model sees what Claude Code shows it —
the same tools, prompts' rules, reminders and request shape (prompt text in our own words) — and
reads Claude Code's formats for settings, hooks, skills, agents, MCP servers and plugins. Under
it, every task is an append-only event log with exactly one writer, so a task survives a crash
without re-running a tool, waits days for a person while holding no worker, and runs on one
laptop or many machines unchanged.

> 1.0 is in development and not on PyPI yet; install it from the repository. (The 0.x releases
> were the separate `noeta-runtime` / `noeta-sdk` packages.)

## Five-minute start

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install 'noeta[tui,images] @ git+https://github.com/initxy/noeta'
# or from a checkout:  git clone https://github.com/initxy/noeta && uv tool install './noeta[tui,images]'
```

Pick a model and a provider. With an Anthropic API key:

```bash
export NOETA_MODEL=claude-sonnet-5-5 NOETA_API_KEY=sk-ant-...   # or ANTHROPIC_API_KEY
```

With an OpenAI-compatible endpoint (vLLM, a gateway, DeepSeek, …), name the provider and the
base URL. For `openai_compat` the base URL ends in `/v1` (noeta appends `/chat/completions`);
for `anthropic` it is the host only (noeta appends `/v1/messages`); for `openai_responses` it is
the whole `…/v1/responses` URL:

```bash
export NOETA_PROVIDER=openai_compat NOETA_BASE_URL=https://llm.example.com/v1
export NOETA_MODEL=deepseek-chat NOETA_API_KEY=...
```

The same settings are flags (`--model`, `--provider`, `--base-url`) or keys in
`~/.noeta/config.toml` (`model`, `provider`, `base_url`, `api_key`). Then:

```bash
noeta                                    # full-screen terminal UI in the current directory
noeta -p "what does this repository do?" # one prompt, print the answer (Claude Code's -p)
noeta -p --output-format stream-json --input-format stream-json --verbose
                                         # Claude Code's SDK protocol: point an Agent SDK at it
```

A model noeta has no catalog entry for runs with a warning (default limits);
`NOETA_MODELS_FILE` describes more. Tasks are kept in `~/.noeta/tasks.db`; `noeta -c` continues
the newest conversation in this directory, `noeta -r` lists them. A project's own
`.noeta/` settings, agents, skills and plugins load only after `noeta trust`.

| Extra | For |
|---|---|
| `tui` | the terminal UI (`noeta` without `-p`) |
| `images` | shrinking large images the way Claude Code does (Pillow) |
| `acp` | `noeta acp`: an Agent Client Protocol agent for Zed and other editors |
| `server` | `noeta serve`: the HTTP / WebSocket API |
| `postgres` | a Postgres store, for many workers |
| `keyring` | `{keyring:service/user}` secret references in plugin settings |

Install several at once: `'noeta[tui,images,acp,server,postgres] @ git+…'`.

## Embed it in Python

```python
import asyncio
import noeta
from noeta.core.events import ModelReplied

async def main() -> None:
    store = noeta.store("sqlite:///tasks.db")
    agents = {a.name: a for a in noeta.default_agents("claude-sonnet-5-5")}
    model = noeta.provider("anthropic", api_key="sk-ant-...")   # base_url=... for a gateway
    runtime = noeta.Runtime(store=store, agents=agents, provider_for=lambda _name: model)
    client = noeta.Client(store)
    task = await client.create("main", "What does this repository do?", config={"cwd": "."})
    await runtime.run_until_idle()
    replies = [r.event for r in await client.events(task) if isinstance(r.event, ModelReplied)]
    print(replies[-1].response.message.text)

asyncio.run(main())
```

`noeta.LocalApp` is a store, a worker and a client in one process; `noeta.connect(url)` gives a
client for a store URL or a `noeta serve` server. Everything an embedder needs is outside
`noeta.cli`, which is the `noeta` command itself.

## Serve it on many machines

One Postgres store, any number of workers, an HTTP API in front:

```bash
export NOETA_STORE=postgres://noeta:...@db.internal/noeta
noeta worker --concurrency 16             # on every worker machine
noeta serve --no-worker --host 0.0.0.0    # the API; bearer token required
noeta --server http://api.internal:8642   # the TUI against it (also: noeta acp --server URL)
```

A worker that dies leaves nothing behind: another claims its tasks where their logs stop. A
front end attached with `--server` needs the token in `NOETA_SERVER_TOKEN` and no model of its
own: its tasks run on the server's model and in the server's directory unless `--model` or
`--cwd PATH` (a directory on the server) say otherwise.
Workspaces, tokens and tenants: [docs/server.md](docs/server.md).

## Extend it

Everything outside the small kernel is a plugin behind one `Extension` interface, found by name
through Python entry points or as a single file in `~/.noeta/plugins/` — the built-in ones
exactly like yours. Agents are markdown files in Claude Code's format, prompts have names you
can override, and Claude Code plugin directories load from noeta's own places. How to write and
ship each kind, and how it compares with pi, DeepSeek Harness and Claude Code:
[docs/extending.md](docs/extending.md).

## Privacy

noeta sends nothing anywhere except requests to the model provider you configure: no
telemetry, no log upload, no update check. Tools reach out only when the model calls them
(`WebFetch`, `WebSearch`, MCP servers, your commands), under the permission rules you set.

## Docs

- [docs/architecture.md](docs/architecture.md) — the kernel, plugins, layers, ways in, many machines
- [docs/extending.md](docs/extending.md) — writing and shipping plugins
- [docs/server.md](docs/server.md) — workers, workspaces, tokens and tenants
- [CONTEXT.md](CONTEXT.md) — the vocabulary; [docs/adr/](docs/adr/index.md) — the decisions and why
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md)

## License

Apache-2.0
