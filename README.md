# noeta

**An open-source Claude Code that runs on servers.** The model sees what Claude Code shows it —
the same tools, prompts' rules, reminders and request shape — and
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
export NOETA_MODEL=deepseek-chat NOETA_API_KEY=...       # or OPENAI_API_KEY
```

The same settings are flags (`--model`, `--provider`, `--base-url`) or keys in
`~/.noeta/config.toml` (`model`, `provider`, `base_url`, `api_key`). Then:

```bash
noeta                                    # full-screen terminal UI in the current directory
noeta -p "what does this repository do?" # one prompt, print the answer (Claude Code's -p)
noeta -p --output-format stream-json --input-format stream-json --verbose
                                         # Claude Code's SDK protocol: point an Agent SDK at it
```

More providers go in `config.toml` tables; the settings above stay the default provider, and
`provider = "<table name>"` makes a table the default instead:

```toml
[providers.claude]
family = "anthropic"                 # or openai_compat, openai_responses
api_key = "{env:ANTHROPIC_API_KEY}"  # a literal, or {env:…} / {keyring:…}
models = ["claude-opus-5-5"]         # optional; rows {id, context_window, max_output_tokens} too
# base_url, headers = {…}, list_models = false (skip reading its /models)
```

A model id resolves to the provider whose table names it (or whose models-file row says
`"provider"`; one model named for two providers is a config error), else the first provider
whose `/models` listing has it (the default first), else the default. A model's context window
comes from noeta's catalog, then a models file (`NOETA_MODELS_FILE`) or table rows, then its
provider's `/models` listing when that publishes one (read from your endpoint only, kept a day in
`~/.noeta/cache/models`), else default limits with a warning.

What is deterministic, with more than one provider: when a task is started with a model or
switched to one (`/model`, the SDK's `set_model`, an editor's pick), and when a sub-agent is
spawned (on its parent's model or its own), that resolution is made once and recorded in the
task's config (`model_route`: the provider, and limits a listing gave) beside the bare model id.
Every worker sends that model to the recorded provider at those limits, whatever its own
listings say, and fails the call if that provider is not configured there. Only the server
records a route; a client (HTTP, stream-json, an editor) cannot set one. The exceptions, which
resolve on the worker that runs them and are the same everywhere only when a table or models
file names the model: a model a plugin calls on the side (`compaction_model`, a title or
permission-classifier model, a skill's `model`), and the agent's own model after an editor
picks "default". With one provider nothing is recorded (the log is as before); a model's
listed limits are then looked up by each process. Reasoning a provider signed is replayed only to that provider, and tool-call ids
another provider wrote are rewritten to the form Anthropic takes. `/model` alone lists every
provider's models.

Tasks are kept in `~/.noeta/tasks.db`; `noeta -c` continues
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
Workspaces and the token: [docs/server.md](docs/server.md).

## Extend it

Everything outside the small kernel is a plugin behind one `Extension` interface, found by name
through Python entry points or as a single file in `~/.noeta/plugins/` — the built-in ones
exactly like yours. Agents are markdown files in Claude Code's format, prompts have names you
can override, and Claude Code plugin directories load from noeta's own places. How to write and
ship each kind, and how it compares with pi, DeepSeek Harness and Claude Code:
[docs/extending.md](docs/extending.md).

## Benchmarks

noeta 1.0.0.dev0 at commit 34f8acee next to Claude Code 2.1.312 on the same model
(`opensource/deepseek_v4_1_flash`, effort `high`), same tasks, caps and machine, run through
harbor (2026-10-30 to 2026-11-01). Each run is one graded attempt per task (harbor retried an
attempt that ended without a grade, and some tasks passed only on that retry; bench/README.md lists them); cost is
DeepSeek's list price (read 2026-10-30) per run, off-peak throughout:

| Benchmark | noeta | Claude Code | noeta − Claude Code (SE) |
|---|---|---|---|
| SWE-rebench 07-2026, 111 tasks, 50 min, 2 runs each pooled | 50.5 % | 45.0 % | +5.4 (3.6) |
| same, 91 tasks without leaked answers | 51.1 % | 44.0 % | +7.1 (3.6) |
| same, one same-time run | 49.5 % | 42.3 % | +7.2 (3.8) |
| Terminal-Bench 4.0, 54 of 66 tasks, 2 h cap, 1 run | 3.7 % | 1.9 % | +1.9 (3.2) |

Cost per run of 111 SWE-rebench tasks: noeta $5.01–$5.77, Claude Code $5.70–$6.25; per
Terminal-Bench run of 54 tasks: noeta $5.23, Claude Code $6.50.

The first run, at 54121930, scored −2.7 (SE 4.3) on SWE-rebench. 34f8acee differs in two
ways the model sees: `Bash` keeps the image's environment variables (54121930 dropped
`CARGO_HOME`, `JAVA_HOME`, …), and the token-budget notes go only to Claude models (784c0cd8);
the gain is the two together. Terminal-Bench 4.0 is near zero for both on this model and does
not separate them. Versions, every run, retries, the leaked-answer tasks, what each agent reads
from the repo, and a second model:
[bench/README.md](bench/README.md#results-2026-10-30-to-2026-11-01).

## Privacy

noeta sends nothing anywhere except requests to the model provider you configure: no
telemetry, no log upload, no update check. Tools reach out only when the model calls them
(`WebFetch`, `WebSearch`, MCP servers, your commands), under the permission rules you set.

## Docs

- [docs/architecture.md](docs/architecture.md) — the kernel, plugins, layers, ways in, many machines
- [docs/extending.md](docs/extending.md) — writing and shipping plugins
- [docs/server.md](docs/server.md) — workers, workspaces and the token
- [CONTEXT.md](CONTEXT.md) — the vocabulary; [docs/adr/](docs/adr/index.md) — the decisions and why
- [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), [CHANGELOG.md](CHANGELOG.md)

## License

Apache-2.0
