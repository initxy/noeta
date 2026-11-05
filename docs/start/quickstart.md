# Quickstart

Five minutes from nothing to a working agent: install noeta, point it at a model, use the
terminal UI and `noeta -p`, then come back to a conversation later.

## 1. Install

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). 1.0 is not on PyPI yet,
so install from the repository. This puts the `noeta` command and its terminal UI on your
`PATH`:

```bash
uv tool install --with 'noeta-tui @ git+https://github.com/initxy/noeta#subdirectory=tui' \
  'noeta @ git+https://github.com/initxy/noeta'
```

From a checkout instead:

```bash
git clone https://github.com/initxy/noeta && cd noeta
uv tool install '.[tui]'
```

Check it: `noeta --version`.

The terminal UI is its own package, `noeta-tui` (in `tui/`); without it `noeta` still runs
`-p`, `serve`, `worker` and `acp`, and tells you how to add the UI when you start it bare.
Optional extras add the rest:

| Extra | For |
|---|---|
| `tui` | the terminal UI (`noeta-tui`) |
| `server` | `noeta serve`, the HTTP / WebSocket API |
| `acp` | `noeta acp`, the Agent Client Protocol for Zed and other editors |
| `postgres` | a Postgres store, for many workers |
| `keyring` | `{keyring:service/user}` secrets in config |

Several at once: `uv tool install 'noeta[acp,server,postgres] @ git+https://github.com/initxy/noeta'`.
A `uv tool install --reinstall` keeps only the extras and `--with` packages it names, so name
all of them each time. When a command needs an extra you do not have, noeta says which one
and prints the install line.

## 2. Pick a model and a provider

noeta needs a model id and a way to reach it. The quickest is environment variables.

**Anthropic's API:**

```bash
export NOETA_MODEL=claude-sonnet-5-5
export NOETA_API_KEY=sk-ant-...          # or ANTHROPIC_API_KEY
```

**An OpenAI-compatible endpoint** (vLLM, a gateway, DeepSeek, …): name the provider and the
base URL too.

```bash
export NOETA_PROVIDER=openai_compat
export NOETA_BASE_URL=https://llm.example.com/v1
export NOETA_MODEL=deepseek-chat
export NOETA_API_KEY=...                 # or OPENAI_API_KEY
```

The base URL is different per provider family:

| `NOETA_PROVIDER` | `NOETA_BASE_URL` | noeta sends to |
|---|---|---|
| `anthropic` (the default) | the host only, e.g. `https://api.anthropic.com` | `<base>/v1/messages` |
| `openai_compat` | ending in `/v1` | `<base>/chat/completions` |
| `openai_responses` | the whole `…/v1/responses` URL | that URL |

The same settings work as flags (`--model`, `--provider`, `--base-url`) or as keys in
`~/.noeta/config.toml`, which is the better home once you settle on them:

```toml
# ~/.noeta/config.toml
model = "claude-sonnet-5-5"
provider = "anthropic"
# the key: NOETA_API_KEY or ANTHROPIC_API_KEY in the environment,
# or api_key = "sk-ant-..." here (a literal: this top-level key takes no {env:…} reference)
```

Flags win over environment variables, which win over `config.toml`
([Configuration](./configuration.md)). Several providers at once, models the catalog does not
know, and the aliases `opus`, `sonnet`, `haiku` are covered in
[Models and providers](../guides/models.md).

A model the catalog does not know still runs, with a conservative 128,000-token window and a
16,384-token output cap, and noeta says so at start. Give it its real limits in a
`[providers.<name>]` table or a models file ([Configuration keys](../reference/config.md)).

## 3. First run in the terminal UI

```bash
cd ~/code/some-project
noeta
```

The first time you open a folder, noeta asks whether you trust it. Saying yes runs
`noeta trust` for that folder: from then on its own `.noeta/` settings, agents, skills,
prompts and plugins load. Saying no exits. (You can also run `noeta trust` yourself before
starting.)

Then type what you want, as you would in Claude Code:

```text
> what does this repository do, and how are its tests run?
```

A few things to know on the first day:

- **Permission mode.** New tasks start in `auto`. Shift+Tab cycles the modes
  (`default` asks before changes, `acceptEdits`, `plan`, `auto`, `bypassPermissions`);
  `--permission-mode` picks the starting one. Approvals show as cards you answer in place
  ([Permissions and hooks](../guides/permissions.md)).
- **Slash commands.** `/help` lists them; `/model`, `/effort`, `/context`, `/cost`, `/compact`,
  `/rewind`, `/resume` and the rest are in [The noeta command](../reference/cli.md#slash-commands).
- **Stop and leave.** Esc stops the current turn; `/exit` quits. The task stays in the store.

Everything noeta keeps lives under `~/.noeta/` (or `$NOETA_HOME`): the store
`~/.noeta/tasks.db`, settings, trusted folders, plugins. noeta creates that folder readable by
you only and warns if it, or the store file, is open to other users.

## 4. One prompt from a script: `-p`

`-p` runs one prompt without the UI and prints the answer, as `claude -p` does:

```bash
noeta -p "what does this repository do?"
echo "summarize the README" | noeta -p          # the prompt from stdin
noeta -p --output-format json "list the TODOs"  # one JSON result object
```

The JSON result carries the answer (`result`), `is_error`, `num_turns`, `total_cost_usd`,
`usage` and per-model usage, as Claude Code's does. Add `--json-schema '<schema>'` to get the
answer as structured output, and `--max-turns` / `--max-budget-usd` to cap a run.

For programs, the stream-json protocol is the same as Claude Code's, so an Agent SDK pointed at
the `noeta` binary drives it:

```bash
noeta -p --output-format stream-json --input-format stream-json --verbose
```

`-p` refuses anything that would ask a person unless the client answers prompts
(`--permission-prompt-tool stdio`), as Claude Code's headless mode does. Pick the permission
mode you mean, for example `--permission-mode acceptEdits`, or allow specific tools with
`--allowedTools "Bash(git *) Edit"`. Every flag is listed in
[The noeta command](../reference/cli.md).

## 5. Continue and resume

Every conversation is a task in the store, so it is still there after you quit, reboot or
crash.

```bash
noeta -c                 # continue the newest conversation started in this folder
noeta -r                 # pick one from a list (the terminal UI)
noeta -r <task id>       # open that task
noeta -p -c "and now fix the first TODO"   # -c works with -p too
noeta -p -r <task id> --fork-session "try another way"   # go on in a new task
```

Inside the terminal UI, `/resume` lists earlier conversations and `/rewind` (or Esc Esc) goes
back to before one of your prompts, restoring the conversation, the files, or both.

## 6. Trust, in one paragraph

A cloned repository must not be able to configure your agent or start a process by itself.
So a project's `.noeta/settings.json`, `.noeta/settings.local.json`, `.noeta/agents/`,
`.noeta/skills/`, `.agents/skills/`, `.noeta/prompts/` and `.noeta/plugins/` load only after
`noeta trust` (or "yes" to the first-run question). Trusted folders are listed in
`~/.noeta/trusted.json`. Even then, the MCP servers in a project's `.noeta/mcp.json` start only
with consent in the project's own settings (`enabledMcpjsonServers` or
`enableAllProjectMcpServers`). `NOETA.md` / `AGENTS.md` are read either way, as Claude Code
reads `CLAUDE.md` ([Configuration](./configuration.md#trust)).

## Next

- [Configuration](./configuration.md): where every setting comes from and which one wins.
- [The terminal UI and -p](../guides/terminal.md): the UI's keys, the headless protocol.
- [Run it as a server](../guides/server.md): workers, `noeta serve`, `noeta --server`.
- [Embed it in Python](./embedding.md): the same kernel in your own program.
