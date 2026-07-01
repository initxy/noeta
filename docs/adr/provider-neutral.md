# Provider-neutral: every external provider comes in through an adapter; the internal Protocol is canonical in Noeta-shape

## Context

Noeta connects to multiple LLM vendors (OpenAI-compat, OpenAI Responses, Anthropic, Bedrock, Gemini, local models…), and each one has its own wire vocabulary: `stop_reason` vs `finish_reason`, the shape of content blocks, the nesting depth of `tool_calls`, the location of `function_call.arguments`, and so on. If any one vendor's shape became Noeta's internal common shape, the whole kernel's mental model would be pinned to that one SDK's vocabulary, and the cost of switching providers would spread to every call site.

## Decision

When connecting any external LLM, **first define a Noeta-shape internal typed protocol** (`LLMRequest` / `LLMResponse` / `Message` / `Block` (`TextBlock` / `ToolUseBlock` / `ToolResultBlock` / `ThinkingBlock`) / the `LLMProvider` Protocol), then have each provider implement the `LLMProvider` Protocol as an **adapter**.

- Engine, Policy, ContextComposer, and RuntimeLLMClient **see only the Noeta-shape protocol**. Any provider's wire vocabulary may appear only inside its `noeta.providers.<name>` adapter file, and never leaks into the kernel (L0/L1/L2).
- Adapters physically live in `noeta.providers`, peers of `noeta.runtime` / `noeta.tools` — they are adapters, not upstream consumers. The isolation is enforced by two forbidden contracts in `.importlinter`: (1) `noeta.runtime ↛ noeta.providers` (`RuntimeLLMClient` holds only the `LLMProvider` Protocol, with the concrete implementation supplied via dependency injection); (2) `noeta.providers` may import only `noeta.protocols.*` (an adapter depends on no upstream service).
- Noeta's protocol vocabulary uses its own names (`call_id` / `tool_name` / `arguments`, `stop_reason: Literal["tool_use", "end_turn", "max_tokens", "error"]`), copying no provider's field names.

The result: **adding a provider = one file `noeta/providers/<name>.py` plus its unit tests, with zero kernel changes and zero Engine/Policy changes.**

## Rationale

- **Don't get permanently shackled to one SDK's semantic debt.** Once some provider's field names become Noeta's internal canonical, switching providers leaves only two paths: break the Noeta protocol, or do lossy translation in the adapter; and the team's mental model gets pinned to that one SDK's vocabulary. Noeta-shape makes switching/adding providers purely additive.
- **A single canonical recording shape is the prerequisite for folded state being portable.** `RuntimeLLMClient` records each LLM round-trip once, in Noeta-shape, onto the EventLog. Every downstream consumer of that recording — `fold` re-deriving task state on every wake/resume/inspect, the stable-prefix prompt cache, reproducing a session across hosts — sees the same shape rather than each vendor's wire vocabulary. If each vendor's raw shape leaked into the recording, then a session recorded against one provider could not be folded or re-driven anywhere that doesn't import that provider's SDK.
- **A new capability passes the Noeta gate before it lands.** When a provider exposes a capability Noeta hasn't planned for (Anthropic thinking blocks, OpenAI logprobs / reasoning summaries, etc.): **first** decide whether Noeta wants the capability, what to call it, and which typed Block to add; **then** have the adapter fill it in. An adapter **must not** add a variant to the `Block` union on its own initiative. This ensures the kernel's shape is gatekept by Noeta rather than dragged along by some provider's new feature.

## Alternatives considered

1. **Take one provider's protocol shape directly as Noeta's internal canonical** (copy Anthropic Messages' content block, or OpenAI Chat Completions' flat message + tool_calls). Rejected: it freezes that vendor's semantic debt permanently into the kernel; switching providers would ripple to every call site; it violates the provider-neutral founding principle.
2. **Skip the LLM layer and have each Policy hold the provider SDK and call it directly.** Rejected: each Policy would record its own ad-hoc shape (or not record at all), and the EventLog would no longer carry a single canonical recording — folded state would no longer be portable across providers or hosts; and the Engine could no longer append `LLMRequestStarted` / `LLMResponseRecorded` / `LLMRequestFinished` on each LLM round-trip.
3. **Keep the protocol, but pass the provider's raw request/response object through as a Noeta-shape field** (e.g. `LLMResponse.raw_anthropic: AnthropicResponse | None`). This looks like "keep the Noeta protocol and lose no information." Rejected: the Noeta-shape field is polluted by provider types, forcing the kernel to import the provider SDK to stay type-correct; the recording then smuggles in a vendor object that can't be folded without that SDK; and switching providers still requires changing every call site.

## Consequences

- Where this principle bears weight: `noeta.providers.*` (`openai_compat` / `openai_responses` / `anthropic` / `codecs` / `catalog`) is the adapter layer, where all wire translation and error translation of wire vocabulary is sealed; `noeta.runtime.llm` (`RuntimeLLMClient`) records each round-trip once, in Noeta-shape, onto the EventLog; `noeta.protocols.messages` / `noeta.protocols.errors` / `noeta.protocols.token_estimate` / `noeta.protocols.step_transition` are the Noeta-shape protocol itself, **containing no vendor fields**; the kernel-side consumers `noeta.core.engine`, `noeta.policies.react`, `noeta.context.composer`, `noeta.guards.repetition` see only Noeta-shape.
- The isolation is held by path-dimension forbidden contracts, independent of wheel membership. `noeta.providers` now ships with the `noeta-runtime` wheel (see `docs/adr/runtime-sdk-app-restructure.md`), but the import path `noeta.providers.<name>` is unchanged, so the two path contracts above still hold.
- The OpenAI Responses adapter + image input is another application of the same principle; details in `docs/adr/provider-adapters-and-multimodal.md`.
- The cost: every added typed Block or protocol field must first be defined on the Noeta side and then followed up by each adapter — one extra step compared with "just pass the vendor object through" — a cost paid deliberately in exchange for portability.
