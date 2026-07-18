# Configure a provider

**Goal:** point Noeta at a real LLM — the platform at an
OpenAI-Responses-compatible gateway, or your own SDK agent at any supported
provider.

**Before you start:** you have run the zero-credential mock mode from the
[quickstart](../tutorials/quickstart.md), and you have a gateway URL + API
key.

## Option A: the platform (`python -m noeta.agent`)

The platform speaks to **OpenAI-Responses-compatible gateways** (the public
OpenAI API or any self-hosted/vendor gateway speaking the Responses wire
shape). Configure `apps/noeta-agent/.env`:

```dotenv
LLM_PROVIDER=auto
LLM_BASE_URL=https://your-gateway.example.com/v1
LLM_API_KEY=sk-…
```

- `LLM_BASE_URL` is the **gateway root** — the provider appends
  `/responses`. Auth goes through the `api-key` header.
- `LLM_PROVIDER=auto` (the default) uses the gateway when both values are
  set and falls back to the offline mock otherwise, so an empty `.env` never
  breaks boot.
- The model menu users pick from is `apps/noeta-agent/models.json`: `id`,
  `label`, one `default: true`, `efforts` (reasoning levels), and — for
  models the SDK catalog does not know — `context_window` /
  `max_output_tokens` so context compaction can engage.

Restart the server and check the effective provider:

```bash
curl -s http://127.0.0.1:8000/api/v1/health
# {"ok": true, "provider": "openai"}   ← "mock" means credentials didn't take
```

[`examples/openai-compatible/`](https://github.com/initxy/noeta/tree/main/examples/openai-compatible)
is a copy-paste version of this setup.

### A second gateway

Models can route to a second Responses-compatible gateway (different host,
`Authorization: Bearer` auth): set `SECONDARY_LLM_BASE_URL` +
`SECONDARY_LLM_API_KEY` and tag the routed models with
`"gateway": "secondary"` in `models.json`. The secondary only stacks on top
of an active primary. See the
[configuration reference](../reference/configuration.md#llm-gateway).

## Option B: programmatic (SDK)

When building your own agent on `noeta.sdk`, the provider is an `Options`
field. The adapters are exported via `noeta.sdk.providers`:

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider

options = Options(
    system_prompt="You are a helpful assistant.",
    name="my-agent",
    provider=AnthropicProvider(api_key="sk-ant-…"),
)
```

For OpenAI-compatible chat-completions endpoints, use
`OpenAICompatProvider`; for the Responses API, `OpenAIResponsesProvider`
(its `base_url` is the **full** responses endpoint):

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

Offline tests and demos use the deterministic double from
`noeta.sdk.testing`:

```python
from noeta.sdk.testing import FakeLLMProvider
```

## Switching providers

Provider is **wiring, not identity**: swap the instance and nothing else
changes — agent code, tools, and recorded history are portable across
vendors. See [Swap providers](swap-providers.md) for a before/after example.

## Troubleshooting

- **`/health` says `"provider": "mock"`** — `LLM_BASE_URL` or `LLM_API_KEY`
  is empty (auto fell back), or the `.env` you edited is not
  `apps/noeta-agent/.env`. Environment variables override the file.
- **401 / authentication error** — check the key; the primary gateway
  authenticates via the `api-key` header, the secondary via
  `Authorization: Bearer`.
- **Model not in the composer** — the menu comes from `models.json`, not
  from the gateway; add the entry there.
- **Context grows without compaction on a custom model** — give the
  `models.json` entry `context_window` / `max_output_tokens`.

## See also

- [Provider neutrality](../concepts/provider-neutrality.md) — why the
  internal protocol is vendor-agnostic
- [Configuration reference](../reference/configuration.md) — every key
- [Swap providers](swap-providers.md) — before/after code example
