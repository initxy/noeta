# Configure a provider

**Goal:** wire Noeta to a real LLM endpoint — Anthropic, OpenAI-compatible,
or OpenAI Responses API.

**Before you start:** you have installed Noeta and run the stub provider
from the [quickstart](../tutorials/quickstart.md). You have an API key for your chosen
provider.

## Option A: environment variables (coding agent)

The easiest way to configure a provider for `python -m noeta.agent` is
through `NOETA_AGENT_*` environment variables:

```bash
# Anthropic
NOETA_AGENT_PROVIDER=anthropic \
NOETA_AGENT_MODEL=claude-sonnet-4-5-20250929 \
NOETA_AGENT_API_KEY=sk-ant-… \
python -m noeta.agent

# OpenAI-compatible (any endpoint that speaks the OpenAI chat/completions format)
NOETA_AGENT_PROVIDER=openai \
NOETA_AGENT_MODEL=gpt-5.5 \
NOETA_AGENT_BASE_URL=https://api.openai.com/v1 \
NOETA_AGENT_API_KEY=sk-… \
python -m noeta.agent

# OpenAI Responses API
NOETA_AGENT_PROVIDER=openai-responses \
NOETA_AGENT_MODEL=gpt-5.5 \
NOETA_AGENT_API_KEY=sk-… \
python -m noeta.agent
```

`NOETA_AGENT_BASE_URL` is optional for the official OpenAI endpoint
(defaults to `https://api.openai.com/v1`); set it explicitly for
self-hosted or third-party OpenAI-compatible endpoints.

## Option B: JSON config file

Instead of individual env vars, you can point to a JSON config file:

```bash
NOETA_AGENT_CONFIG=./noeta-config.json python -m noeta.agent
```

Where `noeta-config.json` contains:

```json
{
  "provider": "anthropic",
  "model": "claude-sonnet-4-5-20250929",
  "api_key": "sk-ant-…",
  "workspace": "./my-project",
  "storage_url": "./session.sqlite",
  "write_mode": "dry_run"
}
```

## Option C: programmatic (SDK)

When using the SDK directly (`noeta.sdk`), pass a provider to `Options`
or `Client`:

```python
from noeta.sdk import Client, Options
from noeta.llm.anthropic import AnthropicProvider

provider = AnthropicProvider(
    model="claude-sonnet-4-5-20250929",
    api_key="sk-ant-…",
)

options = Options(
    system_prompt="You are a helpful assistant.",
    name="my-agent",
    provider=provider,
)

client = Client(options, workspace_dir="./workspace")
```

For OpenAI-compatible endpoints, use `OpenAICompatProvider`:

```python
from noeta.llm.openai_compat import OpenAICompatProvider

provider = OpenAICompatProvider(
    model="gpt-5.5",
    base_url="https://api.openai.com/v1",
    api_key="sk-…",
)
```

And for the OpenAI Responses API:

```python
from noeta.llm.openai_responses import OpenAIResponsesProvider

provider = OpenAIResponsesProvider(
    model="gpt-5.5",
    api_key="sk-…",
)
```

## Verify it works

Boot the agent with your real provider and send a simple message:

```bash
NOETA_AGENT_PROVIDER=anthropic \
NOETA_AGENT_MODEL=claude-sonnet-4-5-20250929 \
NOETA_AGENT_API_KEY=sk-ant-… \
NOETA_AGENT_STORAGE=./test.sqlite \
python -m noeta.agent
```

Open the chat UI and ask "What is 2 + 2?". If you get a real LLM response
(rather than the stub's canned reply), the provider is wired correctly.
Check the trace view to confirm the turn shows real token counts and
usage.

## Switching providers

To swap backends, change the provider instance — no other code needs to
change. See [Swap providers](swap-providers.md) for a before/after
example. The internal protocol is vendor-neutral, so your agent code,
tools, and recorded history are portable across providers.

All three built-in providers stream tokens to the web UI while a response
is being generated — no configuration needed. The stub provider does not
stream (it answers instantly), so don't expect a typing effect on a bare
boot.

## Troubleshooting

- **"No provider configured"** — `NOETA_AGENT_PROVIDER` is unset (defaults
  to `stub`) or `Options.provider` is `None`.
- **401 / authentication error** — check your API key. For Anthropic, it
  starts with `sk-ant-`; for OpenAI, `sk-`.
- **Model not found** — verify the model name. Anthropic model names
  include the date suffix (e.g. `claude-sonnet-4-5-20250929`).
- **Connection timeout** — check `NOETA_AGENT_BASE_URL`. For corporate
  proxies, you may need to set `HTTPS_PROXY`.

See [Troubleshooting](../operations/troubleshooting.md) for more.

## See also

- [Provider neutrality](../concepts/provider-neutrality.md) — why the
  internal protocol is vendor-agnostic
- [Swap providers](swap-providers.md) — before/after code example
- [SDK reference](../reference/sdk.md) — `Options` and `Client` constructor
  parameters
