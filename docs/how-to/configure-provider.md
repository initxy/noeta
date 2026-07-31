# Configure a provider

**Goal:** point your SDK agent at a real LLM — Anthropic, or any
OpenAI-compatible / OpenAI-Responses endpoint.

**Before you start:** you have run the offline, zero-credential example (see
[Your first agent](../tutorials/first-agent.md)), and you have a provider
endpoint and an API key.

## The provider is wiring, not identity

`compile_options` never reads `Options.provider`, and the field is excluded
from equality. Two recipes differing only in their provider compile to the
same agent identity, so swapping vendors leaves agent code, tools, and
recorded history untouched.

The adapters live in `noeta.sdk.providers`, a submodule so that importing the
SDK does not pull in `httpx` unless you actually build a network provider:

```python
from noeta.sdk import Options
from noeta.sdk.providers import AnthropicProvider

options = Options(
    system_prompt="You are a helpful assistant.",
    name="my-agent",
    provider=AnthropicProvider(api_key="sk-ant-…"),
)
```

`AnthropicProvider` falls back to the `ANTHROPIC_API_KEY` environment variable
when `api_key` is omitted, and raises rather than constructing a client that
would fail its first call with an opaque 401.

For OpenAI-style `/chat/completions` endpoints use `OpenAICompatProvider`; for
the Responses API use `OpenAIResponsesProvider`, whose `base_url` is the
**complete** responses endpoint (it is POSTed verbatim, with no path appended):

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

`OpenAICompatProvider` requires `base_url` and falls back to `OPENAI_API_KEY`
for the credential, which it sends as `Authorization: Bearer …`.
`OpenAIResponsesProvider` requires both `base_url` and `api_key`, and sends the
credential as an `api-key` header. All three take `extra_headers` for
gateway-specific or proxy-specific headers.

Construct a provider once and reuse it: each holds a shared `httpx.Client`,
and the model is chosen per call from the request.

## Passing it to the client

Either set `Options.provider` as above, or hand it to `Client` / `query`
directly — the explicit argument wins, and with neither present the `Client`
constructor raises `ValueError`:

```python
from pathlib import Path

from noeta.sdk import Client

client = Client(
    options,
    provider=chat,
    workspace_dir=Path("./workspace"),
    model="gpt-4o",
)
```

`workspace_dir` and `model` are both optional. `workspace_dir` falls back to
`Options.cwd` and then to the process working directory; `model` falls back to
`Options.model` and then to `"sonnet"`. Pass an explicit `model` in
production — the fallback is only useful if your endpoint happens to serve
that id.

## Offline testing

The deterministic double lives in `noeta.sdk.testing`, kept out of the
`noeta.sdk` root so production imports never pull test material by accident:

```python
from noeta.sdk.testing import FakeLLMProvider
```

Script its `responses` with the public message types (`LLMResponse`,
`TextBlock`, `Usage` — all on `noeta.sdk`) and a whole run is network-free.
That is how the `examples/` smoke tests run.

## Troubleshooting

- **401 / authentication error** — check the key you passed the adapter, or
  the environment variable it falls back to. The adapters use `httpx`, so
  `HTTPS_PROXY` in the environment is honoured for a corporate proxy.
- **Model not found** — the `model` you pass must be an id the endpoint
  serves. Anthropic ids carry a date suffix, e.g.
  `claude-sonnet-4-5-20250929`.
- **Context grows without compaction** — the compaction knobs are derived from
  the model catalog, and a model the catalog does not describe gets compaction
  turned off. Register your own row:

  ```python
  from noeta.sdk.providers import CATALOG, ModelSpec

  CATALOG["my-gateway-model"] = ModelSpec(
      real_model_id="my-gateway-model",
      context_window=200_000,
      max_output_tokens=8_192,
      input_price_per_mtok=3.0,
      output_price_per_mtok=15.0,
      cache_read_price_per_mtok=0.3,
      cache_write_price_per_mtok=3.75,
  )
  ```

  The same row also drives per-run cost accounting; leave the prices at `0.0`
  if you do not need it.

## See also

- [Provider neutrality](../concepts/provider-neutrality.md) — why the
  internal protocol is vendor-agnostic
- [SDK reference](../reference/sdk.md) — the full `Options` surface
- [Swap providers](swap-providers.md) — before/after code example
