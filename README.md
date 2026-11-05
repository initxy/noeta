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
uv tool install --with 'noeta-tui @ git+https://github.com/initxy/noeta#subdirectory=tui' \
  'noeta @ git+https://github.com/initxy/noeta'
# or from a checkout:  git clone https://github.com/initxy/noeta && cd noeta && uv tool install '.[tui]'
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

More providers, models files, side models and how a model id picks its provider:
[Models and providers](https://initxy.github.io/noeta/guides/models) and, for the design,
[Providers and model routing](https://initxy.github.io/noeta/how-it-works/models).

Tasks are kept in `~/.noeta/tasks.db`; `noeta -c` continues
the newest conversation in this directory, `noeta -r` lists them. A project's own
`.noeta/` settings, agents, skills and plugins load only after `noeta trust`.

Project instructions come from `NOETA.md` (or, in a directory without one, the cross-tool
`AGENTS.md`); skills from `.noeta/skills` and the cross-tool `.agents/skills`, in the project, the
directories above it, and `~/.noeta/skills`.

| Extra | For |
|---|---|
| `acp` | `noeta acp`: an Agent Client Protocol agent for Zed and other editors |
| `server` | `noeta serve`: the HTTP / WebSocket API |
| `postgres` | a Postgres store, for many workers |
| `keyring` | `{keyring:service/user}` secret references in plugin settings |
| `tui` | the terminal UI (`noeta-tui`, in `tui/`) |

Install several at once: `'noeta[acp,server,postgres] @ git+…'`. The terminal UI (`noeta` without `-p`) is its own package, `noeta-tui` in `tui/` (`tui/README.md`).

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
Workspaces and the token: [Run it as a server](https://initxy.github.io/noeta/guides/server).

## Extend it

Everything outside the small kernel is a plugin behind one `Extension` interface, found by name
through Python entry points or as a single file in `~/.noeta/plugins/` — the built-in ones
exactly like yours. Agents are markdown files in Claude Code's format, prompts have names you
can override, and Claude Code plugin directories load from noeta's own places. How to write and
ship each kind: [Write a plugin](https://initxy.github.io/noeta/guides/plugins); how it compares with pi,
DeepSeek Harness and Claude Code: [Compared with other agents](https://initxy.github.io/noeta/operations/comparison).

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

**[initxy.github.io/noeta](https://initxy.github.io/noeta/)** (中文：[initxy.github.io/noeta/zh](https://initxy.github.io/noeta/zh/))
— quickstart, guides, how every part is designed and why, and the full reference. The source
is [docs/](docs/index.md); `make docs-dev` serves it locally.

For contributors: [CONTEXT.md](CONTEXT.md) — the vocabulary; [docs/adr/](docs/adr/index.md) —
the decisions and why; [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md),
[CHANGELOG.md](CHANGELOG.md).

## License

Apache-2.0
