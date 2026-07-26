# Configure a provider

**Goal:** point your SDK agent at a real LLM — Anthropic, or any
OpenAI-compatible / OpenAI-Responses endpoint.

**Before you start:** you have run the offline, zero-credential example (see
[Your first agent](../tutorials/first-agent.md)), and you have a provider
endpoint + API key.

## The provider is an `Options` field

When you build an agent on `noeta.sdk`, the provider is **wiring, not
identity** — it mounts the agent onto a host and stays out of the recording.
The adapters are exported via `noeta.sdk.providers`:

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider

options = Options(
    system_prompt="You are a helpful assistant.",
    name="my-agent",
    provider=AnthropicProvider(api_key="sk-ant-…"),
)
```

For OpenAI-compatible chat-completions endpoints, use `OpenAICompatProvider`;
for the Responses API, `OpenAIResponsesProvider` (its `base_url` is the
**full** responses endpoint):

```python
from noeta.sdk.providers import OpenAICompatProvider, OpenAIResponsesProvider

chat = OpenAICompatProvider(
    base_url="https://api.openai.com/v1",
    api_key="sk-…",
)
responses = OpenAIResponsesProvider(
    base_url="https://api.openai.com/v1/responses",
    api_key="sk-…",
)
```

Pass the provider to `query` / `Client` (or set it on `Options.provider`);
`workspace_dir` and `model` are required alongside it.

## Offline testing

Offline tests and demos use the deterministic double from
`noeta.sdk.testing`:

```python
from noeta.sdk.testing import FakeLLMProvider
```

Script its `responses` with the public message types (`LLMResponse` /
`TextBlock` / `Usage`, all on `noeta.sdk`) so a whole run is network-free —
exactly how the
[`examples/`](https://github.com/initxy/noeta/tree/main/examples) smoke tests
run.

## Switching providers

Provider is **wiring, not identity**: swap the instance and nothing else
changes — agent code, tools, and recorded history are portable across
vendors. See [Swap providers](swap-providers.md) for a before/after example.

## Troubleshooting

- **401 / authentication error** — check the key passed to the adapter; for a
  corporate proxy, set `HTTPS_PROXY` in the environment.
- **Model not found** — the `model` you pass must be an id the endpoint
  serves (Anthropic ids carry the date suffix, e.g.
  `claude-sonnet-4-5-20250929`).
- **Context grows without compaction on a custom model** — a model the SDK
  catalog does not know needs its `context_window` / `max_output_tokens`
  supplied so context compaction can engage.

## See also

- [Provider neutrality](../concepts/provider-neutrality.md) — why the
  internal protocol is vendor-agnostic
- [SDK reference](../reference/sdk.md) — the full `Options` surface
- [Swap providers](swap-providers.md) — before/after code example
