# Web image attachment: paste / pick to send + preview in bubbles and the file panel

## Context

The underlying image-input protocol is already in place: `ImageBlock(ContentRef)`, `append_user_message(content: list[Block])`, the `openai_responses` provider, and the "ContentStore holds the bytes, the ledger holds only the fingerprint" convention (see provider-adapters-and-multimodal.md).

This decision only wires that backend channel through to the frontend: paste / pick an image in the composer, the model reads it and answers, the sent image stays visible / zoomable / reproducible in history, plus image preview in the workspace file panel. The model-side protocol is untouched.

## Decision

- **Scope is the frontend wiring for "send an image for the model to look at."** No new image-processing tools (resize / OCR / transcode), the model does not produce `ImageBlock`, and the model does not generate images. Images flow one way: user → model input.
- **The first message can carry images too: `CreateTaskRequest` gains an `images` field, symmetric with `SendGoalRequest`.** Creating a task immediately triggers the first turn, so "create a text-only task first, then add images" would make the images miss that turn (a race). One request carries the images; it reuses the existing `ImageInput` parse → `content_store.put` → `ImageBlock(source=ref)` path.
- **Input methods: paste + a pick button; multiple images; a thumbnail chip with delete.** Paste handles screenshots (the main case); the pick button handles local image files; drag-and-drop is deferred to v2.
- **The client gates with an allowlist + a per-image size cap.** Types are limited to `image/png|jpeg|gif|webp` (consistent with the backend `ImageInput` allowlist); the per-image cap is 5MB.
- **Vision gate: `/capabilities` exposes `supports_vision`, and the frontend greys out the image entry.** `supports_vision` currently lives only in the backend catalog; add this bool to each model in `/capabilities`. When the selected model can't read images, the frontend greys out the paste / pick entries with a hover tooltip (fail-fast, to avoid the user wasting a turn); there is no "paste an image and auto-switch model."
- **The user bubble renders an image thumbnail, with two byte-fetching channels for two moments.** Immediately after sending: the frontend holds a local base64 and displays it directly; reopening from history: the local copy is gone, so it fetches by hash via the new route. `canonicalProse` / `canonicalAssistantParts` now also extract and render `<img>` thumbnails for user messages.
- **A new message-scoped image-fetch route `/tasks/{id}/images/{hash}`, with the same security mechanism as artifacts.** `/artifacts/{hash}` only serves refs coming from `ToolResultRecorded` (a structurally-scoped allowlist), so a user-uploaded image stored in `MessagesAppended` is out of reach; the new route adds a collector that walks the `ImageBlock.source`s inside `MessagesAppended` to build the allowed hash set, and otherwise copies the rest (structural scoping + hash allowlist + using `ContentRef.media_type` as the Content-Type).
- **The lightbox is a shared dialog component** (source-agnostic, taking a URL), shared by conversation-bubble thumbnails and file-panel images.
- **File-panel image preview: the workspace file endpoint gains `?mode=raw` to output raw bytes.** `GET /tasks/{id}/file?path=` gains `?mode=raw` to return raw bytes + the correct Content-Type, reusing the `WorkspaceRoot` sandbox as-is; the frontend replaces "not previewed for now" with an `<img>` pointing at that raw URL, and a click reuses the lightbox.
- **The two image-fetching channels coexist and are not merged**: `/tasks/{id}/images/{hash}` (conversation images, content-fingerprint-addressed, message-scoped allowlist) and `/tasks/{id}/file?path=...&mode=raw` (workspace files, path-addressed, `WorkspaceRoot` sandbox) address two different kinds of resource.

### Red lines (unbreakable)

- **The ledger never stores base64** (per provider-adapters-and-multimodal.md): the ledger stores only `ImageBlock(ContentRef)`, the raw bytes are in the ContentStore, and base64 is generated only at the instant of wire serialization; the new send / fetch paths must not write base64 back into the ledger.
- **The new fetch route must not widen the allowlist**: `/tasks/{id}/images/{hash}` only serves hashes structurally referenced by some `ImageBlock.source` in `MessagesAppended`, and must never degrade into "know the hash, fetch any ContentStore bytes."
- **SVG only through `<img>`**: the file panel's SVG preview must never be inlined into the DOM (SVG can embed scripts; rendered via `<img src>`, scripts don't execute).

## Rationale

- **A symmetric `images` field on create-task**: creating a task immediately starts the first turn, otherwise the images would miss that turn — this is a correctness gap; and "the first message can't carry images" contradicts the "screenshot then ask" intuition.
- **Actually gate (expose `supports_vision` + grey out)**: adding a bool to an existing endpoint is cheap and clears up confusion before sending; "don't gate, let it fail" only surfaces the failure after sending, and "paste-auto-switch model" is a hidden override of the user's choice.
- **A new message-scoped route rather than stuffing base64 into the message view stream**: the latter would dump megabytes of base64 into the SSE / view stream on every reload and every image-bearing message, bloating the ledger view, and would overturn the existing decision to deliberately exclude `ImageBlock` from the client view.
- **File-panel raw mode folded into v1**: the endpoint already reserved a raw mode and the sandbox is reusable, so the cost is small.

## Alternatives considered

1. **A two-step frontend (create a text-only task first, then send-goal to add images).** Rejected: images miss the first turn.
2. **Don't gate, let it fail / paste-auto-switch model.** Rejected: the failure is only known after sending, poor UX / a hidden override of the user's choice.
3. **Stuff base64 into the message view stream.** Rejected: dumps megabytes into SSE on every reload, bloats the ledger view, overturns an existing decision.
4. **Open the original image in a new tab.** Rejected: per the user's preference, changed to a dialog preview, made into a shared component reused in two places.

## Consequences

- The backend host layer carries `CreateTaskRequest.images`, the `/tasks/{id}/images/{hash}` collector and route, `/tasks/{id}/file?mode=raw`, and the newly-added `supports_vision` on `/capabilities`.
- The frontend, in `apps/web`: image-attachment logic for paste / pick / allowlist / size gating, bubble thumbnails, the shared lightbox, and the file panel's raw image preview.
- This decision only does the frontend wiring, carrying forward provider-adapters-and-multimodal.md's image-input protocol; any later drag-and-drop, image-processing tools, or model-generated images are out of scope.
- Accepted costs: a single image is bound by the 5MB cap and the type allowlist; on a vision-unsupported model, the image entry is greyed out outright.
