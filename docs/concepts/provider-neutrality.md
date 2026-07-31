# Provider neutrality

Noeta does not speak any vendor's API internally. It has its own neutral
request and response shape, and each vendor gets an **adapter** that translates
at the boundary in both directions: outbound (neutral `LLMRequest` → that
vendor's wire format) and inbound (wire response → neutral `LLMResponse`).

Three adapters ship, all reachable from `noeta.sdk.providers`:
`AnthropicProvider` (Anthropic Messages), `OpenAICompatProvider` (OpenAI Chat
Completions), and `OpenAIResponsesProvider` (OpenAI Responses).

<p align="center">
  <img src="../assets/diagrams/provider-neutrality.svg" alt="Provider neutrality — the Engine talks one neutral LLM protocol to three vendor adapters, which never leak into the kernel" width="820">
</p>

## Choosing one is wiring, not a rewrite

Pick an adapter, construct it, and hand it to the `Client`:

```python
from noeta.sdk import Client, Options
from noeta.sdk.providers import OpenAICompatProvider

client = Client(
    Options(system_prompt="You are a helpful assistant."),
    provider=OpenAICompatProvider(base_url="https://api.example.com/v1"),
)
```

You can also set it as `Options.provider`; the `Client` kwarg wins when both
are present. Either way the choice is excluded from agent identity — two
otherwise-equal recipes differing only in provider compile to the same
`AgentSpec`.

## The rule: no vendor's format becomes the internal contract

Lift Anthropic's message shape straight into the internal types and every other
provider is second-class by birth, with vendor quirks seeping through into the
Engine. So the internal shape is neutral and the quirks stay penned inside the
adapters. Four places where that shows:

- **Errors fold into a neutral taxonomy.** `TransientError`,
  `ContextOverflowError`, and `FatalError` carry `category` values
  `"transient"` / `"overflow"` / `"fatal"`. The runtime wrapper turns a provider
  exception into an error `LLMResponse` (`stop_reason="error"`,
  `raw["category"]`) rather than letting it escape, so the Policy branches on
  the category and the retry and compaction logic never cares who is on the
  other end.

- **Vendor mechanics never enter the core.** Anthropic cache breakpoints are
  applied to the outbound wire body only and never reach the ledger.
  Extended-thinking round-trips, per-model vision gates, and reasoning-effort
  tiers all live inside their adapter.

- **Even pricing is neutral.** One `ModelSpec` row describes a model regardless
  of vendor — context window, output cap, per-MTok prices with cache reads and
  writes priced separately. No vendor wire key is ever a field name; each
  adapter maps its own usage into the neutral `Usage`, and `CATALOG` prices it.

- **Streaming is an optional capability, not a second contract.** An adapter
  that can stream implements
  `StreamingProvider.complete_streaming(request, on_delta, request_headers=None)`,
  which still returns the complete `LLMResponse`; the deltas are ephemeral side
  effects that never touch the ledger. The runtime probes with `isinstance`
  (streaming → header-aware → plain `complete`), so a provider without the
  capability works unchanged and the recorded exchange is identical either way.

## Enforced by architecture, not discipline

Neutrality is nailed down by an import rule rather than a convention.

The adapters live in `noeta.builtins.providers.impl`, and `.importlinter`'s
`sdk-core-not-builtins` contract forbids **any** static import of
`noeta.builtins` from the kernel and the SDK core. The kernel physically cannot
depend on a vendor; the plugin loader's dynamic `ref` resolution is the only
doorway.

Even `noeta.sdk.providers` re-exports the three classes lazily through a module
`__getattr__`, so importing the SDK never drags in an HTTP client — only a
caller that actually builds a network provider pays for `httpx`.

## Why an event-sourced system cares extra

The events written to the EventLog are of neutral shape, so the recording
itself is vendor-free. A Task that ran against Anthropic can be folded,
inspected, and audited in a process that never imports an Anthropic adapter
(see [event sourcing](event-sourcing.md)).

Wire-level artifacts such as cache breakpoints are deliberately kept out of the
log, so vendor details are never welded into what is meant to be long-lived
ground truth.

The cost is honest: one adapter layer per vendor to build and maintain. The
return is recordings that outlive any vendor relationship, and an Engine that
is provably — not just conventionally — vendor-ignorant.

## Next

- [Configure a provider](../how-to/configure-provider.md) — credentials, base
  URLs, and model aliases.
- [Swap providers](../how-to/swap-providers.md) — moving an existing agent to a
  different vendor.
- [Composer & context caching](composer-and-cache.md) — what the adapter
  receives on each call.
- [SDK options](../reference/sdk-options.md) — where `provider` sits among the
  other fields.
