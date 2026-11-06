<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/initxy/noeta/main/docs/assets/logo/noeta-logo-dark.svg">
    <img alt="noeta" src="https://raw.githubusercontent.com/initxy/noeta/main/docs/assets/logo/noeta-logo-light.svg" height="88">
  </picture>
</p>

<h3 align="center">The open-source coding agent that never loses its place.</h3>

<p align="center">
  Claude Code–compatible · survives crashes · waits for free · one laptop or a fleet of servers
</p>

<p align="center">
  <a href="https://pypi.org/project/noeta/"><img alt="PyPI" src="https://img.shields.io/pypi/v/noeta?color=10b981"></a>
  <a href="https://pypi.org/project/noeta/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/noeta"></a>
  <a href="https://github.com/initxy/noeta/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/initxy/noeta/actions/workflows/ci.yml/badge.svg"></a>
  <a href="https://github.com/initxy/noeta/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache--2.0-blue"></a>
  <a href="https://initxy.github.io/noeta/"><img alt="Docs" src="https://img.shields.io/badge/docs-initxy.github.io%2Fnoeta-8b5cf6"></a>
</p>

<p align="center">
  <a href="https://initxy.github.io/noeta/">Docs</a> ·
  <a href="https://initxy.github.io/noeta/start/quickstart">Quickstart</a> ·
  <a href="https://initxy.github.io/noeta/how-it-works/">How it works</a> ·
  <a href="https://github.com/initxy/noeta/blob/main/docs/benchmarks.md">Benchmarks</a> ·
  <a href="https://github.com/initxy/noeta/blob/main/README.zh-CN.md">中文</a>
</p>

---

**noeta** is a coding agent you can run in your terminal, embed in Python, or deploy across many
machines. To the model it looks exactly like Claude Code: same tools, same prompt rules, same
reminders, same request shape. That means the habits models learned on Claude Code carry over
unchanged, and your Claude Code settings, hooks, skills, agents, MCP servers and plugins work
too. Underneath it is built like a database: every task is an append-only log, so nothing is
ever lost.

<p align="center">
  <img alt="noeta's terminal UI fixing a failing test" src="https://raw.githubusercontent.com/initxy/noeta/main/tui/screenshots/transcript.svg" width="760">
</p>

## Why noeta

- 🧠 **Claude Code–compatible.** Tool names and schemas, prompt rules, reminders and context
  compaction all match Claude Code, checked by replaying recorded Claude Code requests
  against noeta. Settings, hooks, skills, agents, MCP servers and plugin folders use Claude
  Code's formats.
- 💥 **Crash-proof.** Kill the process, pull the plug, lose the machine: another worker picks
  the task up where its log stops. A tool call that was in flight is reported as "outcome
  unknown", never silently run twice.
- ⏸️ **Waiting costs nothing.** An approval, a question for a person, a sub-agent or a timer
  parks the task. It holds no process and no worker until the answer arrives, minutes or
  weeks later.
- 🌐 **Laptop to fleet, same code.** SQLite on one laptop; Postgres with any number of workers
  behind an HTTP API in production. Any worker can continue any task.
- 🔌 **Every way in.** A full-screen terminal UI, Claude Code's `-p` and `stream-json`
  protocol (point an Agent SDK at it), ACP for Zed and other editors, an HTTP / WebSocket
  API, a web app, a desktop app and a Python SDK.
- 🤖 **Any model.** Claude, DeepSeek, or anything behind an OpenAI-compatible or Responses
  endpoint, including your own vLLM or gateway.
- 🧩 **Everything is a plugin.** Files, shell, permissions, hooks, skills, sub-agents, MCP,
  memory and compaction all sit behind one `Extension` interface. The built-ins load exactly
  like yours.
- 🔒 **Nothing phones home.** No telemetry, no log upload, no update check. The only traffic is
  to the model provider you configure, plus the tools the model calls under your permission
  rules.

## Benchmarks

Same model (DeepSeek V4.1 Flash, effort `high`), same tasks, same limits, same machine:

| Benchmark | noeta | Claude Code |
|---|---|---|
| SWE-rebench 07-2026 (111 tasks, 2 runs pooled) | **50.5 %** | 45.0 % |
| Cost per run of the 111 tasks | **$5.01–5.77** | $5.70–6.25 |

Every run, the standard errors, the retries and the tasks with leaked answers are in
[the full benchmark report](https://github.com/initxy/noeta/blob/main/docs/benchmarks.md).

## Quickstart

You need Python 3.11+.

```bash
uv tool install 'noeta[tui]'          # or: pip install 'noeta[tui]'

export ANTHROPIC_API_KEY=sk-ant-...   # or see "Other models" below
export NOETA_MODEL=claude-sonnet-5-5

noeta                                 # full-screen terminal UI in this directory
noeta -p "what does this repository do?"   # one prompt, print the answer
```

<details>
<summary><b>Other models</b> (DeepSeek, OpenAI-compatible, your own gateway)</summary>

```bash
export NOETA_PROVIDER=openai_compat NOETA_BASE_URL=https://api.deepseek.com/v1
export NOETA_MODEL=deepseek-chat NOETA_API_KEY=...
```

For `openai_compat` the base URL ends in `/v1`; for `anthropic` it is the host only; for
`openai_responses` it is the whole `…/v1/responses` URL. The same settings are flags
(`--model`, `--provider`, `--base-url`) or keys in `~/.noeta/config.toml`. More in
[Models and providers](https://initxy.github.io/noeta/guides/models).

</details>

<details>
<summary><b>Day-to-day</b></summary>

- `noeta -c` continues the newest conversation in this directory; `noeta -r` lists them.
- Project instructions come from `NOETA.md`, or `AGENTS.md` when there is none.
- Skills load from `.noeta/skills` and `.agents/skills`, in the project and in your home.
- A project's own `.noeta/` settings, agents, skills and plugins load only after `noeta trust`.
- `noeta -p --output-format stream-json --input-format stream-json --verbose` speaks Claude
  Code's SDK protocol, so a Claude Agent SDK can drive it.

</details>

<details>
<summary><b>Optional extras</b></summary>

| Extra | Adds |
|---|---|
| `tui` | the terminal UI (`noeta-tui`, released with noeta at the same version) |
| `acp` | `noeta acp`: an Agent Client Protocol agent for Zed and other editors |
| `server` | `noeta serve`: the HTTP / WebSocket API |
| `postgres` | a Postgres store, for many workers |
| `keyring` | `{keyring:service/user}` secret references in plugin settings |

Several at once: `uv tool install 'noeta[tui,server,postgres]'`.

</details>

## Run it on many machines

One Postgres store, any number of workers, an HTTP API in front:

```bash
export NOETA_STORE=postgres://noeta:...@db.internal/noeta
noeta worker --concurrency 16             # on every worker machine
noeta serve --no-worker --host 0.0.0.0    # the API (bearer token required)
noeta --server http://api.internal:8642   # the terminal UI against it
```

A worker that dies leaves nothing behind: another claims its tasks where their logs stop.
See [Run it as a server](https://initxy.github.io/noeta/guides/server).

## Embed it in Python

```python
import asyncio
import noeta
from noeta.core.events import ModelReplied

async def main() -> None:
    store = noeta.store("sqlite:///tasks.db")
    agents = {a.name: a for a in noeta.default_agents("claude-sonnet-5-5")}
    model = noeta.provider("anthropic", api_key="sk-ant-...")
    runtime = noeta.Runtime(store=store, agents=agents, provider_for=lambda _name: model)
    client = noeta.Client(store)
    task = await client.create("main", "What does this repository do?", config={"cwd": "."})
    await runtime.run_until_idle()
    replies = [r.event for r in await client.events(task) if isinstance(r.event, ModelReplied)]
    print(replies[-1].response.message.text)

asyncio.run(main())
```

`noeta.LocalApp` puts a store, a worker and a client in one process; `noeta.connect(url)` gives
a client for a store URL or a `noeta serve` server. See
[Embedding](https://initxy.github.io/noeta/start/embedding).

## Extend it

Write a plugin in Python and ship it as an entry point, or drop a single file into
`~/.noeta/plugins/`. Agents are markdown files in Claude Code's format, prompts have names you
can override, and Claude Code plugin directories load as they are.
[Write a plugin](https://initxy.github.io/noeta/guides/plugins) ·
[Compared with Claude Code, pi and DeepSeek Harness](https://initxy.github.io/noeta/operations/comparison)

## How it works

```
 terminal UI · -p / stream-json · ACP · HTTP API · web · desktop · Python
                              │  one client API
                              ▼
                ┌─────────────────────────────┐
                │  store: one log per task     │   SQLite or Postgres
                └─────────────────────────────┘
                   ▲ claim · fold · step · append
        worker  worker  worker …   (plugins build what the model sees)
```

A worker claims a task, replays its log into state, makes one model call, runs the tools and
appends what happened. Every write names the position it expects and the claim it holds, so a
frozen worker that lost its claim can never write again.
[How it works](https://initxy.github.io/noeta/how-it-works/) explains every part and why.

## Contributing

Issues and pull requests are welcome. Start with [CONTRIBUTING.md](https://github.com/initxy/noeta/blob/main/CONTRIBUTING.md); the
vocabulary is in [CONTEXT.md](https://github.com/initxy/noeta/blob/main/CONTEXT.md) and the design decisions in
[docs/adr/](https://github.com/initxy/noeta/blob/main/docs/adr/index.md). Security reports: [SECURITY.md](https://github.com/initxy/noeta/blob/main/SECURITY.md). Changes:
[CHANGELOG.md](https://github.com/initxy/noeta/blob/main/CHANGELOG.md).

## License

[Apache-2.0](https://github.com/initxy/noeta/blob/main/LICENSE)
