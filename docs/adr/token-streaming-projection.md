# Token streaming is an ephemeral projection: deltas ride a product-layer side channel, the EventLog stays the only truth

## Context

Noeta renders an assistant turn only after the full LLM round-trip completes: the provider's `complete()` returns a whole `LLMResponse`, the Engine records `MessagesAppended`, and the web UI derefs the message body from the ContentStore. For long completions the user stares at a typing indicator for tens of seconds. Every provider wire protocol (Anthropic Messages, OpenAI Chat, OpenAI Responses) offers token streaming; Anthropic additionally *requires* streaming for large `max_tokens` requests. The question is how streamed deltas enter a system whose spine is "the EventLog is the single source of truth, the wire is a projection" (`event-sourced-truth.md`, `transport-neutral-fanout.md`) without perturbing that spine.

Standing constraints: `noeta.runtime` must not import `noeta.providers` (the capability seam must live in `noeta.protocols`); `EnvelopeBroadcaster` knows only `EventEnvelope` (AST-guarded); the SSE `id:` is the resume cursor (a `{task_id: last_seq}` map) that `Last-Event-ID` replays from; one logical LLM request emits exactly one Started/Recorded/Finished trio, with `call_id` stable across the LIVE-only retry loop.

## Decision

**Deltas are ephemeral: they are never persisted, never folded, and never replayed.** The final `MessagesAppended` event remains the only durable record of assistant output; a delta is a preview of bytes whose truth arrives later by the normal path.

- **Provider capability, push-shaped.** `noeta.protocols.messages` gains a `@runtime_checkable` `StreamingProvider` Protocol: `complete_streaming(request, on_delta, request_headers=None) -> LLMResponse`. The call keeps the blocking one-shot shape and still returns the complete `LLMResponse`; deltas are side effects of the in-flight call. `request_headers` is part of the signature so the streaming capability does not form a matrix with `HeaderAwareProvider`. The delta vocabulary is `StreamDelta(kind: "text" | "thinking", text, index)` — a frozen dataclass with **no** canonical tag, because it never enters EventLog/ContentStore. Tool-call arguments are not streamed (they accumulate silently until the block completes; `decode_tool_arguments` requires whole JSON).
- **Runtime seam, probe-shaped.** `RuntimeLLMClient` accepts an optional `delta_sink(ctx, call_id, delta)` injection and uses `complete_streaming` only when the sink is present, the provider `isinstance`-matches `StreamingProvider`, and the call site allows it (`complete(..., allow_stream=True)`; the compaction summarize call passes `False`). No sink — the exact code path of today. Sink exceptions are swallowed: a delta consumer can never fail an LLM call. The three-event contract, retry loop, fold, and resume are untouched; deltas carry the trio's `call_id` so a live consumer can correlate and can reset on `LLMRetryScheduled`.
- **The delta channel is product-layer.** The hub that fans deltas out to SSE connections lives in `noeta.agent.backend`, wired as the sink through `HostConfig` (host wiring, never `AgentSpec` identity). `EnvelopeBroadcaster` is not touched — deltas are not envelopes and must not pretend to be.
- **On the wire, deltas are named SSE frames without an `id:`.** `event: delta` + a JSON body (`task_id` / `call_id` / `kind` / `text` / `index`); envelope frames stay unnamed. No `id:` means the resume cursor never moves for a delta and a reconnect replays none of them — the final event repaints the truth. Delta frames may be dropped under backpressure (bounded enqueue); envelope frames are never dropped.

## Rationale

- **Push (callback) rather than pull (iterator) is what keeps the blast radius near zero.** An iterator-shaped streaming API inverts control through every layer — Policy would consume chunks, the Engine would own partial state, the trio would need a "streaming in progress" story. The callback shape leaves `Policy → RuntimeLLMClient → provider` a single blocking call returning a whole response; streaming becomes invisible to the Engine, fold, resume, and every headless SDK user.
- **Deltas must not be events because events are truth and deltas are not.** Persisting per-token deltas would bloat the EventLog by orders of magnitude, force fold to understand and skip them, and create a second (partial, reorderable) record of the same bytes `MessagesAppended` already records once. The ledger's value is exactly that there is one record.
- **The `id:`-less frame is what keeps resume correct by construction.** The SSE cursor exists so `Last-Event-ID` can resume the envelope stream exactly. A delta frame carrying an id would advance the cursor past envelopes that never reached the client. Omitting the id makes loss-on-reconnect a wire-format property instead of a bug class.
- **Product-layer hub, because only the product has a consumer.** The runtime/sdk carry a one-field seam (`delta_sink`), mirroring `provider_headers`: the deep module stays deep, and the fanout ADR's "broadcaster knows only EventEnvelope" guard keeps holding without amendment.
- **Suppressing the compaction summarize call at the call site** keeps the mechanism dumb: RuntimeLLMClient forwards deltas for whatever it is told to; the one caller that knows its round-trip is not user-facing opts out with one argument.

## Alternatives considered

1. **Iterator/generator streaming API (`complete_stream() -> Iterator[Delta]`).** Rejected: inverts control through Policy/Engine, forces a partial-response state machine into the kernel, and breaks the "one blocking call, one response" contract that fold/resume rely on.
2. **Deltas as durable EventLog events.** Rejected: order-of-magnitude log bloat, fold must learn to ignore them, and the same bytes get two records — against `event-sourced-truth.md`.
3. **Deltas through `EnvelopeBroadcaster`.** Rejected: the fanout layer is AST-guarded to know only `EventEnvelope`; teaching it a second frame kind re-opens the transport-neutrality decision for no gain — the product hub is ~a hundred lines.
4. **Delta frames with SSE `id:` + replay buffer.** Rejected: deltas would need durable storage to honor `Last-Event-ID`, which is alternative 2 in disguise. Loss-on-reconnect is acceptable because the final event always repaints.
5. **Streaming tool-call arguments to the UI.** Rejected for v1: `ToolCallStarted` already covers the "what is it doing" signal moments later, and argument JSON is unrenderable until complete anyway.

## Consequences

- Load-bearing landings: `noeta.protocols.messages` (`StreamDelta`, `StreamingProvider`), `noeta.runtime.llm` (sink injection, `allow_stream`, probe order streaming → header-aware → plain), `noeta.providers.{anthropic,openai_responses,openai_compat}` (`complete_streaming` accumulating into the same batch parsers so streamed and batch responses are shape-identical), `noeta.client.host_config`/`host` (sink pass-through), `noeta.agent.backend.delta_hub` + `stream.py` (named frames, drop guard), `apps/web` (delta listener + streaming buffer beside the reducer — deltas never enter `reduceEvents`).
- Recording invariant: a streamed exchange and a batch exchange of the same content produce byte-identical EventLog + ContentStore records. Tests pin this.
- A provider that does not implement `StreamingProvider` (any third-party `Options.provider`) continues to work unchanged; streaming is a pure capability upgrade.
- Anthropic's "streaming required for large `max_tokens`" ceases to be a latent limitation once the adapter streams.
