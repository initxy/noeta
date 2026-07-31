# Provider neutrality

Noeta talks to LLMs through its own vendor-agnostic **internal protocol**. Each
vendor gets an **adapter** that translates at the boundary in both directions:
outbound (neutral `LLMRequest` → wire format) and inbound (wire response →
neutral `LLMResponse`). Three ship, reachable from `noeta.sdk.providers`:
`AnthropicProvider` (Anthropic Messages), `OpenAICompatProvider` (OpenAI Chat
Completions), and `OpenAIResponsesProvider` (OpenAI Responses).

The design intent in one line: **no vendor's wire format becomes the internal
contract.** Lift Anthropic's message shape straight into the internal types and
every other provider is second-class by birth, with vendor quirks seeping
through into the Engine. Instead the internal shape is neutral and the quirks
stay in the adapters:

- **Errors fold into a neutral taxonomy.** `TransientError`,
  `ContextOverflowError`, and `FatalError` carry `category` values
  `"transient"` / `"overflow"` / `"fatal"`. The runtime wrapper translates a
  provider exception into an error `LLMResponse` (`stop_reason="error"`,
  `raw["category"]`) rather than letting it escape, so the Policy branches on
  the category and the retry and compaction logic never cares who is on the
  other end.
- **Vendor mechanics never enter the core.** Anthropic cache breakpoints are
  applied to the outbound wire body only and never reach the ledger;
  extended-thinking round-trips, per-model vision gates, and reasoning-effort
  tiers all live inside their adapter.
- **Even pricing is neutral.** One `ModelSpec` row describes a model regardless
  of vendor — context window, output cap, per-MTok prices with cache reads and
  writes priced separately. No vendor wire key is ever a field name; each
  adapter maps its own usage into the neutral `Usage`, and `CATALOG` prices that.
- **Streaming is an optional capability, not a second contract.** An adapter
  that can stream implements `StreamingProvider.complete_streaming(request,
  on_delta, request_headers=None)`, which still returns the complete
  `LLMResponse`; the deltas are ephemeral side effects that never touch the
  ledger. The runtime probes with `isinstance` (streaming → header-aware → plain
  `complete`), so a provider without the capability works unchanged and the
  recorded exchange is identical either way.

## Enforced by architecture, not discipline

Neutrality is nailed down by an import rule rather than a convention. The
adapters live in `noeta.builtins.providers.impl`, and `.importlinter`'s
`sdk-core-not-builtins` contract forbids **any** static import of
`noeta.builtins` from the kernel and SDK core. The kernel physically cannot
depend on a vendor; the plugin loader's dynamic `ref` resolution is the only
doorway. Even `noeta.sdk.providers` re-exports the three classes lazily through
a module `__getattr__`, so importing the SDK never drags in an HTTP client.

Selecting a vendor is therefore wiring, not a rewrite: pass one concrete adapter
as `Client(provider=…)` or `Options.provider`. The choice is excluded from agent
identity — two otherwise-equal recipes differing only in provider compile to the
same `AgentSpec`.

## Why an event-sourced system cares extra

Because the events written to the EventLog are of neutral shape, the recording
itself is vendor-free: a Task that ran against Anthropic can be folded,
inspected, and audited in a process that never imports an Anthropic adapter (see
[Event sourcing](event-sourcing.md)). Wire-level artifacts such as cache
breakpoints are deliberately kept out of the log, so vendor details are never
welded into what is meant to be long-lived ground truth.

The cost is honest: one adapter layer per vendor to build and maintain. The
return is recordings that outlive any vendor relationship, and an Engine that is
provably — not just conventionally — vendor-ignorant.

Related: [Composer & cache](composer-and-cache.md) ·
[Event sourcing](event-sourcing.md) ·
[Architecture overview](../architecture/overview.md)
