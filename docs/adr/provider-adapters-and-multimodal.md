# Provider adapters split by protocol; core's Block extended to support image input

## Context

This decision does two related things: it wires up a gateway speaking the OpenAI Responses API, and it adds support for local-file image input. The former adds a purely additive adapter for a new wire protocol; the latter extends core's `Block` protocol across the whole stack.

Two existing conventions are premises here: provider-neutral, one file per provider (see `provider-neutral.md`); and the log stores only refs while the real bytes live in the ContentStore indexed by content hash (see `event-sourced-truth.md`).

## Decision

### Add `OpenAIResponsesProvider` alongside the Chat-compatible adapter, named by protocol rather than vendor

We create `noeta.providers.openai_responses`; `openai_compat.py` (OpenAI **Chat** compatible) is left untouched. The naming follows the protocol: the new file is the **OpenAI Responses compatible** one. The gateway's Azure-flavored transport (`api-key` header, `?api-version` query) is just a construction detail and is **not called "azure"** — azure isn't a protocol, just one gateway that carries this protocol. The transport layer is all construction parameters: `base_url` (**the complete responses endpoint; the provider POSTs directly and does not append a `/openai/responses` path**) / `api_key` / `api_version` / `timeout_seconds` (**default 300s**: a high-effort call measured around 80s, and 60s would time out) / `extra_headers` / `image_resolver`. Error classification reuses the existing neutral taxonomy (429/5xx→Transient, 400 ctx_overflow→ContextOverflow, other 4xx→Fatal).

### The Responses wire translation is written from scratch, not reused from Chat

The two protocol shapes differ too much (`messages` vs `input`, `choices` vs `output[]`, `tool_calls` vs `function_call`, entirely different usage field names); both outbound and inbound are rewritten. Responses has no `finish_reason`, so stop_reason is **inferred by priority**: `incomplete+max_output_tokens`→`max_tokens` > a `function_call` item present→`tool_use` > `completed`→`end_turn` > otherwise→`error`. The usage mapping is **more complete** than Chat's (Chat originally dropped cache). Effort mapping: `low/medium/high` pass through, `xhigh/max→high` (following the request-level binding collapse).

### The reasoning chain uses Responses' native encrypted_content, aligned to `ThinkingBlock`

`ThinkingBlock.text ← the concatenated summary segments`, `ThinkingBlock.signature ← encrypted_content` (the opaque ciphertext for continuing reasoning). The request carries `reasoning:{effort,summary:"auto"}` + `include:["reasoning.encrypted_content"]` + `store:false`. **Outbound echo-back is on by default** — sending the ciphertext back is **required** for continuing Responses reasoning (unlike Chat: native OpenAI rejects Chat echo-back, so Chat defaults to off).

### Image input extends the core protocol: `ImageBlock(ContentRef)`, log stores only the ref, deref+base64 at request time

The `Block` union gains `ImageBlock` (field `source: ContentRef`) + canonical registration. **The log stores only `ImageBlock(ContentRef)`** (a small handle of roughly 100 bytes, keeping the log small and fold cheap); the real bytes live in the ContentStore, indexed by content hash. The provider is injected at construction with `image_resolver: Callable[[ContentRef],bytes]`, and only at the moment of assembling the wire does it deref → base64 → inline into `{type:input_image, image_url:"data:..."}`. This "fetch image → put it into the request" inlining primitive is built as a **general push/pull** (not bound to a user turn), so adding pull to some image-reading tool later costs almost nothing.

### Image entry point: base64 upload + a general `content` seam

`SendGoalRequest` gains `images:[{media_type,data_base64}]`; the host decodes → `content_store.put` → `ImageBlock(ContentRef)`. The log's entry point `append_user_message` **changes its signature to accept `content: list[Block]`** (a one-time clean break, migrating all callers together; plain-text call sites pass `[TextBlock(text)]`). Within the seam, only blocks allowed in a user turn are validated (TextBlock/ImageBlock), and it remains the sole writer of `Message.origin`. v1 only hooks the user turn and only does push (pull is deferred; the inlining primitive is already in place).

### Capability gate + defenses in the other adapters

`ModelSpec` gains `supports_vision: bool=False`; a request carrying an `ImageBlock` sent to a non-vision model → `FatalError` before sending. `anthropic` / `openai_compat` encountering an `ImageBlock` → **explicitly error** with "this provider does not support images," not silently drop it (a task is locked to one provider, so a mismatch must make noise). The catalog gains new vision/reasoning model entries, with **pricing left as a TODO pending sign-off**.

## Rationale

- **Splitting adapters by protocol, one file per provider, is a hard requirement of provider-neutral.** Tangling two sets of wire rules into one file violates "wire details are fully sealed inside the adapter"; pinning transport as azure-specific mistakes one gateway's carrying detail for a protocol identity.

- **The log storing only `ImageBlock(ContentRef)` and never base64 is the red line of the ContentStore as the single source of truth.** Inlining a multi-MB blob into every log entry would directly violate "bytes in the ContentStore, log stores only refs," bloat the log, and make every fold expensive; the wire-side deref+base64 is transient and never enters the log.

- **Rejecting the Files API/file_id is because server-side state breaks resume.** file_id/`previous_response_id` leaves state on the gateway rather than in Noeta's EventLog, but resume works by folding the log forward to re-derive state and never re-calls the gateway, so once that state expires, resume cannot rebuild it. In practice both Codex and Claude Code use base64 inlining. `store:false` forces no server-side state, keeping all state in the log.

- **encrypted_content echo-back is on by default because it is required for continuing Responses reasoning.** Without echoing the ciphertext, the reasoning chain breaks; it aligns to the existing `ThinkingBlock.signature` and round-trips verbatim.

- **Changing `append_user_message` to `list[Block]` as a clean break generalizes the image/multimodal entry point.** Adding an optional parameter would leave two conventions; migrating all callers at once unifies the seam.

- **The capability gate + explicit errors in other adapters, because a mismatch must make noise and not silently drop the image.** A task is locked to one provider, so pointing an image-carrying task at a non-vision provider must fail immediately.

## Alternatives considered

1. **Pin transport as azure-specific / add a dual-protocol branch inside `openai_compat.py`.** Rejected: azure is a gateway, not a protocol; tangling two sets of wire rules together violates "one file per provider."

2. **Store base64 image bytes directly in the log.** Rejected: inlining a multi-MB blob into every log entry violates "ContentStore as single source of truth," bloating the log and fold.

3. **Files API / file_id upload.** Rejected: server-side state expires, leaving state the EventLog never captured and resume cannot rebuild; in practice both Codex and Claude Code use base64 inlining.

4. **`store:true` + `previous_response_id`.** Rejected: state stays on the gateway, doesn't enter the EventLog, and resume cannot rebuild it.

5. **`anthropic`/`openai_compat` silently dropping ImageBlock.** Rejected: a mismatch must make noise, not silently swallow the image.

## Consequences

- The new adapter's full responsibilities (wire translation, stop_reason inference, effort/thinking mapping, encrypted_content continuation, the image inlining primitive) land in `noeta.providers.openai_responses`; `openai_compat` / `anthropic` each carry an `ImageBlock` defense branch; the catalog gains `supports_vision` and new model entries in `noeta.providers.catalog`.

- Core protocol layer: `ImageBlock(ContentRef)` and its canonical registration land in the message protocol; `append_user_message` changes its signature to `list[Block]`, and the sole-origin-writer validation stays in the engine.

- Host side: `SendGoalRequest.images` decoding → `content_store.put` is the only entry point for images into the system.

- The image inlining primitive is deliberately built as a general push/pull, but v1 only wires push (image attachments on a user turn); pull (an image-reading tool) is left for later, and reusing the same primitive when it lands costs almost nothing.

- Pricing for the new vision/reasoning model entries is still a TODO pending sign-off; do not treat it as confirmed pricing.
