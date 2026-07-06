# Token streaming: ephemeral deltas over the SSE stream, EventLog stays the truth

## Goal

Stream assistant text/thinking token deltas from the LLM providers to the web UI in real time, while the EventLog, the three-event LLM contract, fold, and resume remain byte-identical to the non-streaming path. Deltas are an ephemeral projection; the final `MessagesAppended` event remains the only durable record.

## Non-goals

- No tool-argument deltas on the wire (arguments accumulate silently inside the provider; `decode_tool_arguments` requires complete JSON — `noeta/providers/codecs.py:50-95`).
- No delta replay on SSE reconnect (deltas lost on disconnect by design; the final event is the truth).
- No streaming for the compaction summarize round-trip (suppressed at the call site).
- No async rewrite: providers stay synchronous `httpx`; the backend stays `ThreadingHTTPServer`.
- No subtask streaming bubbles in the web UI v1 (deltas are routed with `task_id`, the frontend renders the root task only).
- No zh docs updates in this round (the docs translate cycle is separate).

## Context

- `LLMProvider.complete()` is a blocking full-response call (`packages/noeta-runtime/noeta/protocols/messages.py:286-295`). The optional-capability precedent is `HeaderAwareProvider` (`messages.py:298-321`), probed with `isinstance` in `RuntimeLLMClient._call_provider` (`packages/noeta-runtime/noeta/runtime/llm.py:207-215`).
- Import topology forces the seam: `noeta.runtime` must NOT import `noeta.providers` (`packages/noeta-runtime/noeta/providers/catalog.py:16-19`), so the streaming capability must be a Protocol in `noeta/protocols/messages.py`.
- All three providers are raw synchronous `httpx` (no vendor SDKs). Each already has complete batch parsers reusable for the streamed final response (Anthropic `_parse_response_content`/`_translate_usage`; Responses `_parse_response` fed by the terminal `response.completed` payload; Chat `_parse_response` shape helpers).
- `RuntimeLLMClient.complete` (`llm.py:265-349`) owns the three-event contract (`LLMRequestStarted` / `LLMResponseRecorded` / `LLMRequestFinished`), generates `call_id` once per logical request (stable across retries), and retries transient errors in `_invoke_with_retry` (`llm.py:351-405`), emitting observational `LLMRetryScheduled` events.
- The whole execution chain is synchronous on a per-turn drive thread (`apps/noeta-agent/.../engine_room.py:355-385`); the SSE writer is a blocking generator per connection draining a `queue.Queue` (`apps/noeta-agent/noeta/agent/backend/stream.py:110-171`).
- The SSE `id:` is a stream-level cursor (base64url `{task_id: last_seq}` map, `stream.py:36-58`); the heartbeat comment frame (`stream.py:72`) is the precedent for non-envelope bytes on the wire.
- `EnvelopeBroadcaster` is transport-neutral and knows only `EventEnvelope` (ADR `transport-neutral-fanout.md`, AST-guarded) — deltas must NOT ride it.
- Frontend: `EventSource` with `es.onmessage` for envelopes (`apps/web/src/app/chat-data.js:329-366`); the reducer folds envelopes only (`src/domain/reducer.js`); assistant text is derefed lazily via `/content/{hash}` from `MessagesAppended.messages_ref`.
- `docs/reference/comparison.md:89` currently states "no token streaming yet".

## Decisions

1. **Callback (push), not iterator (pull).** The streaming call keeps the blocking one-shot shape and still returns the complete `LLMResponse`; deltas are side effects. Engine, ReActPolicy, the three-event contract, retry, fold, and resume change zero lines.

   ```python
   @dataclass(frozen=True, slots=True)
   class StreamDelta:
       kind: Literal["text", "thinking"]
       text: str
       index: int   # content-block index within the response

   @runtime_checkable
   class StreamingProvider(Protocol):
       def complete_streaming(
           self,
           request: LLMRequest,
           on_delta: Callable[[StreamDelta], None],
           request_headers: Optional[dict[str, str]] = None,
       ) -> LLMResponse: ...
   ```

   `StreamDelta` is **not** canonical-registered — it never enters EventLog/ContentStore. `request_headers` is folded into the signature to avoid a capability matrix with `HeaderAwareProvider`.
2. **Runtime probe mirrors `HeaderAwareProvider`.** `RuntimeLLMClient` gains a keyword-only injectable `delta_sink: Callable[[StepContext, str, StreamDelta], None] | None = None` (args: step context, `call_id`, delta). `_call_provider` uses `complete_streaming` only when `delta_sink is not None and isinstance(provider, StreamingProvider) and allow_stream`; otherwise the existing paths. The provider-facing `on_delta` is a closure binding `ctx` + `call_id`. Sink exceptions are swallowed (deltas are observational; they must never fail the call).
3. **Stream suppression is per call site.** `RuntimeLLMClient.complete` gains keyword-only `allow_stream: bool = True`; the compaction summarize call (`packages/noeta-runtime/noeta/policies/react.py:494`) passes `allow_stream=False`. The `_LLMClientP` structural protocol (`react.py:123-134`) is updated accordingly.
4. **The delta channel is product-layer.** A small `DeltaHub` (pub/sub, thread-safe, in `apps/noeta-agent`) is wired as the sink through host config (the same wiring column as `provider_headers` / storage — never `AgentSpec` identity). The runtime/sdk only carry the optional sink seam.
5. **Wire format: named SSE frames without `id:`.**

   ```
   event: delta
   data: {"task_id": "...", "call_id": "...", "kind": "text", "text": "...", "index": 0}
   ```

   No `id:` line → the resume cursor never moves; `EventSource.onmessage` never sees them (envelope frames stay unnamed). Deltas bypass the seq dedup. On reconnect deltas are simply lost — the final event repaints the truth.
6. **Backpressure: drop, never block.** The per-connection delta enqueue checks the pending queue size (threshold ~500) and drops delta frames when the consumer is slow. Envelope frames are never dropped.
7. **Retry interplay.** `call_id` is stable across retry attempts; the frontend clears the accumulated buffer for a task when `LLMRetryScheduled` arrives (already on the stream), so a half-streamed failed attempt never sticks.
8. **Frontend: deltas never enter the reducer.** A streaming buffer lives beside it in `chat-data.js` (ref + throttled state, one repaint per animation frame at most). The Transcript overlays one streaming bubble for the root task; the buffer is cleared when `MessagesAppended` for that task arrives (per tool-loop round: stream → clear → stream again).
9. **Providers reuse batch parsers for the final response.** Streaming code = shared SSE line parser + per-provider accumulator that rebuilds the vendor-shaped terminal payload; the existing parse/translate helpers produce the final `LLMResponse`, guaranteeing streamed and batch results are shape-identical. Mid-stream transport failures translate to `TransientError` so the existing retry loop handles them.
10. **ADR first.** `docs/adr/token-streaming-projection.md` records stances 1, 2, 4, 5 and the delta vocabulary (text/thinking only).

## Implementation plan

### Slice 0 — ADR

`docs/adr/token-streaming-projection.md`, following the house format (Context / Decision / Rationale / Alternatives considered / Consequences). Alternatives to record as rejected: iterator-shaped streaming API; deltas as durable events; deltas through `EnvelopeBroadcaster`; delta frames carrying SSE `id:`.

### Slice 1 — protocol + runtime seam (vertical slice root)

- `protocols/messages.py`: `StreamDelta`, `StreamingProvider` (docstrings state: not canonical, never persisted, providers stay pure).
- `runtime/llm.py`: `delta_sink` constructor kwarg; `allow_stream` on `complete`; probe order in `_call_provider`: streaming (with headers when injected) → header-aware → plain. Sink exceptions swallowed.
- `policies/react.py`: summarize call passes `allow_stream=False`; `_LLMClientP` updated.
- `packages/noeta-sdk/noeta/client/host.py` (+ the host-config surface `EngineRoom` already uses): optional `delta_sink` field threaded into the `RuntimeLLMClient` construction site (`host.py:1435-1441`).
- Tests: fake streaming provider — probe matrix (sink×capability×allow_stream), ctx/call_id binding, sink exception swallowed, ledger trio byte-identical with and without streaming.

### Slice 2 — Anthropic streaming

- Shared helper `providers/_sse.py`: incremental `event:`/`data:` line parser over `httpx` `iter_lines()` (~50 lines; also reusable by the other two).
- `providers/anthropic.py`: `complete_streaming` — POST `stream: true` via `client.stream(...)`; handle `message_start` (input usage), `content_block_start/delta/stop` (`text_delta`→`StreamDelta("text")`, `thinking_delta`→`StreamDelta("thinking")`, `input_json_delta`/`signature_delta` accumulate silently), `message_delta` (stop_reason + output usage), `message_stop`, `ping`, `error`. Accumulate the vendor-shaped message dict; feed the existing `_parse_response_content`/`_translate_usage`/`_STOP_REASON_MAP`.
- Tests: recorded SSE fixtures (text-only, tool-use, thinking+signature, redacted thinking, mid-stream disconnect → `TransientError`, vendor `error` event, `max_tokens` stop); assert delta sequence and final `LLMResponse` equal to the batch parse of the same content.

### Slice 3 — backend delta hub + SSE frames

- New `apps/noeta-agent/noeta/agent/backend/delta_hub.py`: `publish(task_id, call_id, delta)` / `subscribe(callback) -> unsubscribe`; thread-safe; no HTTP imports.
- `engine_room.py`: construct the hub; pass `delta_sink` through host config into the `Client`.
- `stream.py`: delta frame formatter (named event, no `id:`); second subscription filtered by tree membership; queue-size drop guard; unsubscribe in `finally`.
- Tests: frame shape, cursor untouched, tree filtering, drop-when-flooded, unsubscribe on disconnect.

### Slice 4 — frontend

- `chat-data.js`: `es.addEventListener("delta", ...)`; buffer `Map<taskId, {callId, blocks: Map<index, {kind, text}>}>` in a ref; rAF-throttled version bump; clear on `MessagesAppended`, reset on `LLMRetryScheduled`.
- `Transcript.jsx`: streaming bubble for the root task keyed `stream-${callId}` (text blocks via the existing Markdown renderer; thinking blocks via the existing thinking presentation, auto-open while streaming); replaces the bare `ResponseIndicator` while deltas flow.
- Tests: buffer unit tests (accumulate / clear / retry-reset); transcript smoke.

### Slice 5 — remaining providers (independent, parallelizable)

- `providers/openai_responses.py`: `response.output_text.delta` → text, reasoning summary deltas → thinking; terminal `response.completed` payload fed to the existing `_parse_response`; `response.failed`/HTTP errors through the existing taxonomy; verify the `invalid_encrypted_content` single retry still works when the 400 arrives pre-stream.
- `providers/openai_compat.py`: `stream: true` + `stream_options: {"include_usage": true}`; accumulate `choices[0].delta` fragments (content / `reasoning_content` / tool_calls) and terminal usage; rebuild the message dict for the existing parse helpers.
- Same fixture-driven test pattern as Slice 2.

### Docs

- Update `docs/reference/comparison.md:89` (remove "no token streaming yet").
- Update the wire-protocol sentence in `CONTEXT.md` (SSE stream carries canonical envelopes **plus ephemeral named delta frames**).

## Task breakdown

| # | Task | Depends on |
|---|------|-----------|
| 0 | ADR | — |
| 1 | Protocol + RuntimeLLMClient + host wiring + tests | 0 |
| 2 | Anthropic streaming + `_sse.py` + tests | 1 |
| 3 | Backend delta hub + SSE frames + tests | 1 |
| 4 | Frontend buffer + Transcript + tests | 3 |
| 5a | openai_responses streaming + tests | 1 (helper from 2) |
| 5b | openai_compat streaming + tests | 1 (helper from 2) |
| 6 | Docs (comparison.md, CONTEXT.md) | 2–4 |

2 ∥ 3 can run in parallel; 5a ∥ 5b in parallel after 2 lands the shared helper. End-to-end visible after 1+2+3+4.

## Acceptance criteria

- With each of the three providers, assistant text (and thinking) renders incrementally in the web UI; the final bubble content equals the batch-path content.
- The EventLog is byte-identical between streaming and non-streaming runs of the same exchange (same trio, same `request_ref`/`response_ref` canonical bytes, `MessagesAppended` unchanged); fold/resume tests untouched and green. Precision: `LLMResponse.raw` is diagnostics-only and already varies per vendor round-trip (response ids), so the byte-identity invariant is pinned at two levels — mechanism (the same `LLMResponse` records the same bytes with or without a sink) and shape (streamed `stop_reason`/`content`/`usage` equal the batch parse of the same content).
- SSE reconnect resumes envelopes via the cursor exactly as before; no delta is replayed; delta frames carry no `id:`.
- Headless SDK use (no sink) calls `complete()` exactly as today; a non-streaming custom provider works unchanged.
- The compaction summarize call emits no deltas.
- A mid-stream transport failure retries transparently; the UI clears the partial text on `LLMRetryScheduled`.
- Full test suite + import-linter pass (no new cross-layer imports; `noeta.runtime` still does not import `noeta.providers`; the delta hub imports no runtime internals beyond the sanctioned surface).

## Risks

- **Vendor stream-event drift** (esp. Responses event names): mitigate with recorded fixtures and tolerant unknown-event skipping (mirror the batch parsers' "unknown types silently skipped" stance).
- **Unbounded queue growth on slow consumers**: covered by the delta drop guard; envelope path is unchanged (pre-existing exposure, not widened).
- **React bubble remount** when the streaming key hands over to the seq-keyed final bubble: acceptable flicker; keep the handover in one state update (clear buffer + envelope append in the same tick).
- **Thinking-delta rendering** may reveal provider differences (Responses summaries arrive in bursts, not tokens): purely presentational; no protocol impact.
- Release note: merged behavior change to runtime/sdk/agent → cut a release afterwards (patch bump by default; minor is the maintainer's call).

## Files / areas to inspect

- `packages/noeta-runtime/noeta/protocols/messages.py`
- `packages/noeta-runtime/noeta/runtime/llm.py`
- `packages/noeta-runtime/noeta/policies/react.py` (`react.py:494`, `_LLMClientP`)
- `packages/noeta-runtime/noeta/providers/{anthropic,openai_responses,openai_compat}.py`, new `_sse.py`, `codecs.py` (read-only constraint)
- `packages/noeta-sdk/noeta/client/host.py:1435-1441`, host-config surface, `apps/noeta-agent/.../engine_room.py:88-96`
- `apps/noeta-agent/noeta/agent/backend/stream.py`, new `delta_hub.py`
- `apps/web/src/app/chat-data.js`, `src/app/Transcript.jsx`, `src/domain/reducer.js` (must stay untouched), `src/domain/multiplex.js` (read-only; confirm no snapshot-immutability interaction)
- `docs/adr/transport-neutral-fanout.md`, `docs/adr/provider-adapters-and-multimodal.md` (stance alignment), `docs/reference/comparison.md:89`, `CONTEXT.md:19`
