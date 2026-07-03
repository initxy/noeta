# Provider neutrality

Noeta talks to LLMs through its own vendor-agnostic **internal protocol**.
Each vendor — Anthropic Messages, OpenAI Chat Completions, the OpenAI
Responses gateway — gets an **adapter** that translates at the boundary, in
both directions: outbound (neutral request → wire format) and inbound (wire
response → neutral shape).

The design intent in one line: **no vendor's wire format becomes the internal
contract.** Had Anthropic's message shape been lifted straight into the
internal types, every other provider would be a second-class citizen by
birth, and vendor quirks would seep through that type into the Engine.
Instead, the internal shape is neutral and the quirks stay in the adapters:

- **Errors are folded into a neutral taxonomy** — transient,
  context-overflow, fatal — so the Engine's retry and compaction logic never
  cares who is on the other end.
- **Vendor-specific mechanics never enter the core** — Anthropic cache
  breakpoints stay wire-only and never reach the ledger; extended-thinking
  round-trips, per-model vision gates, and reasoning-effort tiers all live
  inside their adapter.

## Enforced by architecture, not discipline

Neutrality is nailed down by an import rule: **the runtime kernel is
forbidden to import a provider package**, checked by import-linter in CI. The
kernel physically cannot depend on a vendor. Providers live in an adapter
band at the edge, and only the outermost wiring layer connects a concrete
vendor in — which is why swapping providers is a wiring change
(`Options.provider`), not a rewrite.

## Why an event-sourced system cares extra

Because the events written to the EventLog are of neutral shape, the
recording itself is vendor-free: a Task that ran against Anthropic can be
folded, inspected, and audited on a machine that has no Anthropic SDK
installed (see [Event sourcing](event-sourcing.md)). Wire-level artifacts
such as cache breakpoints are deliberately kept out of the log, so vendor
details are never welded into what is meant to be long-lived, readable ground
truth.

The cost is honest: one adapter layer per vendor to build and maintain. The
return is recordings that outlive any vendor relationship, and an Engine that
is provably — not just conventionally — vendor-ignorant.

Related: [Composer & cache](composer-and-cache.md) ·
[Event sourcing](event-sourcing.md) ·
[Architecture overview](../architecture/overview.md)
