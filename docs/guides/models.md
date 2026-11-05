# Models and providers

How to point noeta at a model: Anthropic, an OpenAI-compatible endpoint (vLLM, DeepSeek, a
gateway), OpenAI's Responses API, or several at once; how to switch models, set effort, and pick
cheaper models for the side work plugins do. How routing works inside, and why, is
[Providers and model routing](../how-it-works/models.md).

## One provider

noeta needs a model id and a provider to send it to. Each setting can be an environment variable,
a flag or a key in `~/.noeta/config.toml`; a flag wins over the environment, which wins over the
file (`noeta/cli/config.py`).

| Setting | Variable | Flag | `config.toml` |
|---|---|---|---|
| Model id | `NOETA_MODEL` | `--model` | `model` |
| Provider family | `NOETA_PROVIDER` | `--provider` | `provider` |
| Base URL | `NOETA_BASE_URL` | `--base-url` | `base_url` |
| API key | `NOETA_API_KEY` | — | `api_key` |
| Effort | `NOETA_EFFORT` | `--effort` | `effort` |
| Models file | `NOETA_MODELS_FILE` | — | `models_file` |

The model may also come from a settings file's `model` key (`~/.noeta/settings.json`, a
trusted project's `.noeta/settings.json`), Claude Code's place for it, when nothing above sets
one. Without any, `noeta` stops with `no model configured`.

### Anthropic

```bash
export NOETA_MODEL=claude-sonnet-5-5 NOETA_API_KEY=sk-ant-...    # or ANTHROPIC_API_KEY
noeta
```

`anthropic` is the default provider. Its base URL is the host only (noeta appends
`/v1/messages`), so a gateway that speaks the Messages API is
`NOETA_BASE_URL=https://gateway.example.com`. The request is the one Claude Code sends an
API-key user: its beta headers, adaptive thinking, prompt-cache breakpoints, effort.

The aliases `opus`, `sonnet`, `haiku` and `fable` name the current models
(`noeta/core/models.py` `ALIASES`): `--model opus` is `claude-opus-5-5`.

The prompt cache lives five minutes by default. `prompt_cache_ttl = "1h"` in `config.toml` (or a
provider table) asks for the one-hour cache, which costs more per write and pays off when a task
sits idle between turns.

### OpenAI-compatible endpoints: vLLM, DeepSeek, gateways

`openai_compat` speaks `/chat/completions`. Its base URL ends in `/v1`; noeta appends
`/chat/completions`. The key is `NOETA_API_KEY`, else `OPENAI_API_KEY`.

```bash
# DeepSeek
export NOETA_PROVIDER=openai_compat NOETA_BASE_URL=https://api.deepseek.com/v1
export NOETA_MODEL=deepseek-chat NOETA_API_KEY=sk-...

# a local vLLM
export NOETA_PROVIDER=openai_compat NOETA_BASE_URL=http://localhost:8000/v1
export NOETA_MODEL=Qwen/Qwen3-Coder-30B-A3B-Instruct NOETA_API_KEY=unused
```

A model noeta's catalog does not know runs with conservative default limits (a 128,000-token
window, 16,384 output tokens) and a warning, unless something tells noeta its real limits:

- its provider's `/models` listing, when the endpoint publishes the window there (read from your
  endpoint only, kept a day in `~/.noeta/cache/models`);
- a provider table's `models` rows (below);
- a models file:

```json
{"models": [
  {"id": "deepseek-chat", "context_window": 128000, "max_output_tokens": 8192},
  {"id": "Qwen/Qwen3-Coder-30B-A3B-Instruct", "context_window": 262144,
   "max_output_tokens": 65536, "supports_vision": false}
]}
```

```bash
export NOETA_MODELS_FILE=~/.noeta/models.json
```

A row needs both limits to count; a row for a model the catalog knows changes nothing. A row may
also say `"provider": "<table name>"`, the [provider table](#several-providers-at-once) that serves
the model; that table must exist in `config.toml`, or noeta stops with a config error.

A gateway that needs its own headers: `headers = {"X-Team" = "infra"}` in a provider table, or
`extra_headers` for the default provider.

### OpenAI Responses

`openai_responses` speaks OpenAI's `/v1/responses`. Its base URL is the whole endpoint, and it has
no key variable of its own: set `NOETA_API_KEY` or `api_key`.

```bash
export NOETA_PROVIDER=openai_responses NOETA_BASE_URL=https://gateway.example.com/v1/responses
export NOETA_MODEL=gpt-5.5-2026-04-24 NOETA_API_KEY=sk-...
```

The key goes in an `api-key` header, as Azure OpenAI and gateways like it take it
(`noeta/backends/providers/openai_responses.py`). OpenAI's own `api.openai.com` wants
`Authorization: Bearer …` instead: add it with `extra_headers = {Authorization = "Bearer sk-..."}`
in `config.toml` (or `headers` in a provider table).

## Several providers at once

More providers go in `config.toml` tables. The top-level settings stay the default provider;
`provider = "<table name>"` makes a table the default instead.

```toml
model = "claude-sonnet-5-5"            # the default provider: anthropic
                                       # (its key: NOETA_API_KEY, else ANTHROPIC_API_KEY)

[providers.deepseek]
family = "openai_compat"               # anthropic | openai_compat | openai_responses
base_url = "https://api.deepseek.com/v1"
api_key = "{env:DEEPSEEK_API_KEY}"     # a literal, {env:NAME} or {keyring:service/user}
models = ["deepseek-chat", {id = "deepseek-reasoner", context_window = 128000, max_output_tokens = 64000}]

[providers.local]
family = "openai_compat"
base_url = "http://localhost:8000/v1"
api_key = "unused"
list_models = true                     # read its /models (the default); false skips it
```

A table takes exactly `family`, `base_url`, `api_key`, `headers`, `models`, `list_models` and
`prompt_cache_ttl`; anything else is a config error, and so is a table named `default` (that name
is the top-level provider's). `{env:…}` and `{keyring:…}` are read in a table's `api_key` and
`headers` only: a top-level `api_key` is sent as written. `{keyring:…}` needs the `keyring` extra.

Which provider a model id goes to:

1. the provider whose table lists it in `models`, or whose models-file row names it with
   `"provider"` (one model named for two providers is a config error);
2. else the first provider whose `/models` listing has it, the default first;
3. else the default provider.

With more than one provider, that choice is made once, when a task is started on a model or
switched to one, and recorded in the task's config (`model_route`) next to the model id. Every
worker then sends the model to the recorded provider, whatever its own listings say, and fails the
call if that provider is not configured on that worker, so give every worker the same
`config.toml`. With one provider nothing is recorded, unless its `/models` listing gave the model's
limits (they are recorded, so every worker sizes requests alike).

Mixing providers in one conversation works: reasoning a provider signed is replayed only to that
provider, and tool-call ids another provider wrote are rewritten to the form Anthropic takes.

## Switching models

- **In the terminal UI:** `/model` lists the aliases and every provider's models, with the
  provider each comes from; `/model deepseek-chat` switches directly. The switch takes effect from
  the next request and is recorded in the log, so a resumed task keeps it.
- **In the web app and editors:** the model picker; over ACP, `/model [name]` too.
- **Headless:** `--model`, or the SDK's `set_model` control request.
- **Per sub-agent:** an agent file's `model:` (`inherit`, an alias or an id), or the `model`
  parameter of the model's `Agent` call; otherwise a sub-agent runs on its caller's model
  ([sub-agents](./subagents.md)).
- **Per skill:** a skill's `model:` frontmatter runs the rest of the turn that loads it on that
  model ([skills](./skills-and-agents.md)).

A switch to a model noeta knows nothing about says so in the transcript (the default limits it
will use), so you can add a models-file row.

## Effort and thinking

Effort is how hard the model reasons: `low`, `medium`, `high`, `xhigh`, `max`. Set it with
`--effort`, `NOETA_EFFORT`, `effort` in `config.toml`, `/effort` in the terminal UI, an agent
file's or a skill's `effort:`, or the `effort` parameter of an `Agent` call. With none set, a
request uses the catalog row's default for the model (Claude Code's), else `high`
(`noeta/core/models.py` `default_effort`).

What each provider makes of it:

| Provider | Sent as | Notes |
|---|---|---|
| `anthropic` | `output_config.effort` | adaptive thinking, summarized; old thinking kept in the context |
| `openai_compat` | `reasoning_effort` | `xhigh` and `max` become `high`; the model's reasoning text is shown but not sent back (DeepSeek refuses an echoed `reasoning_content`) |
| `openai_responses` | `reasoning.effort` | same mapping; reasoning summaries requested |

The word `ultrathink` in your prompt asks for deeper reasoning, as in Claude Code: the model is told
so, and when a person follows the task live the rest of that turn runs at `xhigh` (unless the
effort is that high already; `noeta/toolkit/ultrathink.py`). Claude Code's
`--max-thinking-tokens` and the SDK's `set_max_thinking_tokens` are accepted and have no effect:
thinking is set by effort.

## Side models

Some plugins call a model on the side, outside the task's own turn. Most use the task's model
unless you name a cheaper one (the table below has the exceptions); set them as plugin arguments in `config.toml`:

```toml
[plugins.compaction]
compaction_model = "claude-haiku-5-5"    # writes the summary when the context is compacted

[plugins.title]
model = "claude-haiku-5-5"               # names the task once

[plugins.permissions]
classifier_model = "claude-sonnet-5-5"   # the auto-mode classifier

[plugins.web]
webfetch_model = "claude-haiku-5-5"      # digests a fetched page for WebFetch

[plugins.memory]
recall_model = "claude-haiku-5-5"        # picks memory files for each message (off when unset)
```

| Plugin | Argument | Default |
|---|---|---|
| `compaction` | `compaction_model` | the task's model |
| `title` | `model` | the task's model |
| `permissions` | `classifier_model` | `claude-sonnet-5` when the task runs a Claude model, else the task's; a failing classifier model falls back to the task's |
| `web` | `webfetch_model` | the task's model |
| `memory` | `recall_model` | none: recall is off |
| `hooks` | a `prompt` or `agent` hook's `model` field | the task's model |

A side model is resolved on the worker that runs it (it is not recorded as a route), so with
several providers name it in a provider table's `models` or a models file, and it goes to the same
provider everywhere.
