# Provider-neutral: every LLM vendor enters through an adapter, and the internal protocol is Noeta-shaped

## Context

Noeta talks to several LLM vendors, and each brings its own wire vocabulary: different names for the terminal signal, different content-block shapes, different nesting for tool calls, different places to find call arguments. Whichever vocabulary becomes the internal one pins the whole kernel's mental model to that vendor's SDK and spreads the cost of switching to every callsite.

## Decision

Connecting any LLM starts from a Noeta-shape typed protocol — `LLMRequest`, `LLMResponse`, `Message`, the `Block` union (`TextBlock`, `ThinkingBlock`, `ToolUseBlock`, `ToolResultBlock`, `ImageBlock`), `Usage`, and the `LLMProvider` Protocol — and each vendor implements `LLMProvider` as an adapter.

- Engine, Policy, ContextComposer and the runtime LLM client see only Noeta-shape. A vendor's wire vocabulary appears only inside that vendor's adapter module.
- The protocol uses its own names: `call_id` / `tool_name` / `arguments`, matching the fields a `ToolCall` decision already carries, and a `stop_reason` normalised to `tool_use` / `end_turn` / `max_tokens` / `error`. No vendor field name is copied. `LLMResponse.raw` keeps the vendor's own response for diagnostics as a plain dictionary, never as a vendor type, so nothing downstream needs that vendor's SDK to read a recording.
- Adapters live in the `providers` built-in plugin; nothing statically imports `noeta.builtins`, so the kernel can never reach one. The runtime LLM client holds only the `LLMProvider` Protocol and receives a concrete implementation by injection through the SDK.
- Vendor extras arrive as **optional capability Protocols** — per-call request headers, response streaming — probed by `isinstance` and falling back to the plain call when absent. That admits a vendor feature without widening the base Protocol or forcing every adapter to implement it.
- Model metadata is neutral too: one `ModelSpec` shape describes a model of any vendor — context window, output cap, per-token prices with cache reads and writes priced separately — and no vendor wire key is ever a field name. Pricing reaches the runtime as an injected callback rather than an import.
- A capability the protocol has not planned for passes the Noeta gate first: decide whether Noeta wants it, what to call it and which typed `Block` carries it, and only then have the adapter fill it in. An adapter does not add a `Block` variant on its own.

Adding a provider is therefore one adapter module plus its unit tests, with no kernel change and no Engine or Policy change.

## Rationale

- **Adopting a vendor's shape as the internal one imports its semantic debt permanently.** Once its field names are canonical, switching vendors leaves only two moves — break the internal protocol, or translate lossily in the adapter — and the team's mental model is pinned to that SDK. A neutral shape makes adding or switching providers purely additive.
- **A single canonical recording shape is what makes folded state portable.** The runtime LLM client records each round-trip once, in Noeta-shape, onto the EventLog. Everything downstream — fold re-deriving task state on each wake, the stable-prefix prompt cache, reproducing a task on another host — sees that one shape. Let each vendor's raw shape into the recording and a task recorded against one provider can no longer be folded anywhere that does not import that provider's SDK.
- **Gatekeeping new capabilities keeps the kernel's shape ours.** Otherwise every vendor feature release drags the protocol along behind it, and the union of block types becomes the union of what vendors happened to ship.

## Alternatives considered

1. **Adopting one vendor's protocol shape as the internal canonical** — its content-block model, or its flat message plus tool-call list. Weighed and rejected: it freezes that vendor's semantic debt into the kernel, and switching vendors then ripples to every callsite.
2. **No LLM layer at all, each Policy holding a vendor SDK and calling it directly.** Rejected: each Policy would record its own ad-hoc shape or nothing at all, the EventLog would stop carrying one canonical recording, and the per-round-trip event trio the Engine relies on would have no single place to be written.
3. **Keeping the neutral protocol but threading the vendor's own request and response objects through as typed fields**, on the theory that no information is lost. Rejected: a vendor type on a neutral field forces the kernel to import that SDK to stay type-correct, smuggles an object into the recording that cannot be folded without it, and still leaves every callsite to change when the vendor does.

## Consequences

- `noeta.builtins.providers.impl` holds the adapters and the model catalog, and is where all wire and error translation is sealed; `noeta.runtime.llm` records each round-trip once, in Noeta-shape; `noeta.protocols.messages`, `errors`, `token_estimate` and `step_transition` are the neutral protocol itself and carry no vendor field. The kernel-side consumers — `noeta.core.engine`, the ReAct policy, `noeta.context.composer`, the repetition guard — see only Noeta-shape. `noeta.sdk.providers` is the supported import path.
- The isolation is a path-level import contract, independent of which distribution an adapter ships in.
- The cost is one extra step per new field: define it on the Noeta side, then follow up in each adapter. That is paid deliberately in exchange for portability.
- Multimodal input and the response-API adapter apply the same principle; see `provider-adapters-and-multimodal.md`.
