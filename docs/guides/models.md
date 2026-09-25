# Connect a model

Point an agent at Anthropic, any OpenAI-compatible `/chat/completions` gateway,
or the OpenAI Responses API. The provider is one argument; your `Options`,
tools and recorded history do not change when you switch.

<NtProviders />

## Pick an adapter

All three live in `noeta.sdk.providers`:

| Adapter | Endpoint | Key | `base_url` |
| --- | --- | --- | --- |
| `AnthropicProvider` | Anthropic Messages | `api_key=` or `ANTHROPIC_API_KEY` | optional, default `https://api.anthropic.com` |
| `OpenAICompatProvider` | any `/chat/completions` | `api_key=` or `OPENAI_API_KEY`, sent as `Authorization: Bearer` | required |
| `OpenAIResponsesProvider` | OpenAI Responses | `api_key=` required, sent as `api-key` header | required; the full endpoint URL, posted as-is |

```python
from noeta.sdk.providers import (
    AnthropicProvider, OpenAICompatProvider, OpenAIResponsesProvider,
)

anthropic = AnthropicProvider()                      # reads ANTHROPIC_API_KEY
chat = OpenAICompatProvider(base_url="https://api.openai.com/v1", api_key="sk-...")
responses = OpenAIResponsesProvider(
    base_url="https://api.openai.com/v1/responses", api_key="sk-...",
)
```

- A missing key raises `ValueError` at construction, not a 401 on the first call.
- All three take `extra_headers={...}` for gateway or proxy headers, and
  `timeout_seconds`.
- Build an adapter once and reuse it. It holds a shared HTTP client and serves
  any model; the model is chosen per client, not per adapter.

## Hand it to the agent

```python
from noeta.sdk import Client, Options, query

options = Options(system_prompt="You are a concise assistant.")

result = query(options, goal="Reply with the word OK.",
               provider=anthropic, model="claude-sonnet-5")
print(result.answer())

with Client(options, provider=chat, model="gpt-4o") as client:
    turn = client.start(goal="Reply with the word OK.")
```

- `provider=` on `Client` / `query` wins over `Options.provider`. With neither,
  `Client` raises `ValueError`.
- `model` falls back to `Options.model`, then to `"sonnet"`. Pass an id your
  endpoint actually serves.

## Swap providers

Change the provider and the model id; nothing else.

```python
for provider, model in [(anthropic, "claude-sonnet-5"), (chat, "gpt-4o")]:
    print(query(options, goal="Say hello.", provider=provider, model=model).answer())
```

| Stays the same | May differ |
| --- | --- |
| `Options`, tools, permission mode, the compiled agent identity | wording, token counts, price |
| the event log format: a log written under one vendor loads and resumes under another | edge cases of tool calling, e.g. parallel calls |

`OpenAICompatProvider` drops earlier reasoning blocks unless you pass
`reasoning_continuation="chat"`; `OpenAIResponsesProvider` sends them back by
default. `examples/swap_provider.py` is a runnable demo.

## Register your own model ids

Noeta sizes the context window and prices each call from its model catalog. An
id it does not know (a gateway name, a fine-tune, a self-hosted model) falls
back to a 128,000-token window and a price of `0.0`, with a one-time warning.
Describe it on the host config:

```python
from noeta.sdk import Client, HostConfig
from noeta.sdk.providers import ModelSpec

host = HostConfig(extra_models={
    "my-gateway-model": ModelSpec(
        real_model_id="my-gateway-model",
        context_window=200_000,
        max_output_tokens=8_192,
        input_price_per_mtok=3.0,
        output_price_per_mtok=15.0,
        cache_read_price_per_mtok=0.3,
        cache_write_price_per_mtok=3.75,
        provider_family="anthropic",   # optional: what really answers behind the id
    ),
})
client = Client(options, provider=chat, model="my-gateway-model", host_config=host)
```

- Leave prices as `None` when the gateway publishes none: that means "unknown"
  (warn once, charge `0.0`), not "free".
- `register_models({...})` from `noeta.sdk.providers` does the same at process
  start. Don't edit `CATALOG` directly; registration rejects a name that
  clashes with a shipped row.
- Register the same rows on every run. The catalog shapes the prompt, and a
  resumed task must see the same one.

## Use cheaper models for side calls

| `Options` field | What it does |
| --- | --- |
| `compaction_model` | model for summarizing long history; default: the main model |
| `webfetch_model` | model that digests fetched pages for `WebFetch`; default: the main model |
| `recall_model` | turns on the model-based memory picker; default: off |
| `effort` | reasoning effort: `low`, `medium`, `high`, `xhigh`, `max` |
| `thinking` | `"adaptive"` or `"disabled"` |

All of these are wiring: they never change the agent's identity.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ValueError` when building the adapter | No key found. Pass `api_key=` or set the env var from the table above. |
| 401 / authentication error | Wrong or expired key. `HTTPS_PROXY` is honored for corporate proxies. |
| Model not found | `model` must be an id the endpoint serves. |
| Cost stays at `$0.00` | The model is not in the catalog. Register it as above. |

## Next

- [Test offline](testing.md): swap in a scripted model for tests
- [Options reference](../reference/options.md)
- [Why provider neutrality](https://github.com/initxy/noeta/blob/main/docs/adr/provider-neutral.md) (ADR)
