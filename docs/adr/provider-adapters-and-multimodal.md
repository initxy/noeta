# Provider adapters are split by wire protocol; images travel as a `ContentRef` and are inlined only at wire assembly

## Context

Two premises frame this decision: provider neutrality with one file per provider
(see `provider-neutral.md`), and a log that stores only refs while the real bytes
live in the ContentStore indexed by content hash (see `event-sourced-truth.md`).
Two questions follow — how a second OpenAI wire protocol is carried, and how
image input crosses the whole stack without bloating the log.

## Decision

**Adapters are named by protocol, not vendor.** The Chat-compatible adapter and
the Responses-compatible adapter are separate files under the `providers`
built-in, side by side, neither importing the other's helpers. A gateway's
Azure-flavoured transport (`api-key` header, `?api-version` query) is a
construction detail, not a protocol identity, so nothing is named "azure".
Transport is entirely constructor parameters: the complete endpoint URL (posted
verbatim, with only the api-version query appended), credentials, timeout
(default 300s, because high-effort reasoning routinely runs past a minute),
extra headers, an image resolver, and the reasoning-continuation mode. Error
classification maps onto the shared neutral taxonomy: 429 and 5xx are transient,
a 400 context overflow is `ContextOverflowError`, other 4xx are fatal.

**The Responses wire translation is independent of the Chat one.** The shapes
diverge too far to share code (`messages` vs `input`, `choices` vs `output[]`,
`tool_calls` vs top-level `function_call` items, different usage field names).
Responses carries no usable `finish_reason`, so the stop reason is inferred by
priority: an `incomplete` response whose reason is `max_output_tokens` wins, then
the presence of a `function_call` item, then `completed`, otherwise error.
Reasoning effort passes `low`/`medium`/`high` through and collapses the higher
levels to `high`.

**Reasoning continuity rides the native encrypted content.** The summary
segments become the thinking text and the opaque ciphertext becomes its
signature; the request asks for the encrypted reasoning content and sets
`store: false`. Echoing that ciphertext back is on by default on Responses,
because the protocol requires it to continue a reasoning chain; the Chat adapter
defaults it off, because native OpenAI rejects the echo there.

**Image input extends the core block union.** `ImageBlock` carries a
`ContentRef` and nothing else. **The log stores only that handle**; the bytes
live in the ContentStore. A provider is constructed with a narrow
`ContentRef → bytes` resolver and, only while assembling the wire body, derefs
and base64-inlines. The resolver holds no ContentStore and no step context, and
the base64 never re-enters the log.

**The user-turn seam takes typed blocks.** Appending a user message takes
`content: list[Block]`, validating that only blocks a user turn may legitimately
carry — text and image — reach it; thinking, tool-use and tool-result blocks and
an empty list are refused, so no model-side or tool-side block can be smuggled
into the user channel. That seam is the sole writer of `Message.origin`. Bytes
enter the system through the SDK's content-put call, which returns the
`ContentRef` a caller wraps in an `ImageBlock`.

**Vision capability is gated, and a mismatch is loud.** The model catalog
carries `supports_vision`; an unlisted model is treated as non-vision, failing
closed. Both image-capable adapters refuse a top-level `ImageBlock` bound for a
non-vision model with a fatal error before anything goes on the wire, and the
Chat-compatible adapter refuses every `ImageBlock` outright. Tool-result images
ride nested on the tool-result block rather than as top-level content, so the
guard deliberately does not see them; their non-vision degrade is the tool
renderer's job. The catalog is honest about what it cannot know: a gateway model
with no published pricing carries zero rates and reports zero cost, rather than
a guess.

## Rationale

Splitting adapters by protocol is what "wire details are fully sealed inside the
adapter" means in practice. Tangling two sets of wire rules into one file breaks
that seal, and pinning transport to a vendor mistakes one gateway's carrying
detail for a protocol identity.

Storing only a ref in the log is the red line of the ContentStore as single
source of truth. Inlining a multi-megabyte blob into a log entry would bloat
every recording and make every fold expensive; the deref is transient and
wire-only.

Server-side state breaks resume. Resume works by folding the log forward to
re-derive state and never re-calls the gateway, so any state parked on the
provider is state the recording cannot rebuild once it expires. Forcing
`store: false` keeps everything in the log.

A mismatch must make noise. A Task is locked to one provider, so an
image-carrying Task pointed at a model that cannot read images has to fail
immediately rather than silently drop content the user attached.

## Alternatives considered

1. **Branch between both protocols inside one adapter file, or pin the
   transport as vendor-specific.** Rejected: it breaks one file per provider and
   confuses a gateway with a protocol.
2. **Store base64 image bytes directly in the log.** Rejected: it violates the
   ContentStore as single source of truth and makes fold pay for every image.
3. **Upload images through a Files API and reference a file id.** Rejected: the
   server-side handle expires, leaving state the log never captured and resume
   cannot rebuild.
4. **Keep server-side conversation state and chain on a previous response id.**
   Rejected for the same reason — the state never enters the log.
5. **Let non-vision adapters silently drop an `ImageBlock`.** Rejected: silent
   loss of user-attached content is worse than a refusal.
6. **Add image input as an optional extra parameter beside the text seam.**
   Rejected: two conventions for the same channel, and the origin-writer
   guarantee would have two doors.

## Consequences

- The Responses adapter owns its wire translation, stop-reason inference,
  effort and reasoning mapping, and the image-inlining primitive; the
  Chat-compatible adapter carries an explicit image refusal; the Anthropic
  adapter carries the same vision guard and its own image conversion. All live
  under the `providers` built-in, with the catalog holding `supports_vision`.
- The block union, its canonical registration, and the typed user-turn seam sit
  in the protocols and engine layers; the origin-writer validation stays in the
  engine.
- The SDK's content-put call is the only doorway for image bytes, and the
  per-model vision projection the host reads is derived from the same catalog
  lookup the adapters use, so one place cannot advertise vision another refuses.
- The inlining primitive is deliberately general rather than bound to a user
  turn, so wiring a pull path (a tool that reads an image) reuses it.
