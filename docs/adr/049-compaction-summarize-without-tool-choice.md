# ADR-049: Summarize without `tool_choice`

- Status: accepted
- Date: 2026-09-25

## Context

Refines [ADR-003](003-context-compaction.md) (Context compaction: a token-only trigger, real recorded usage converted into the estimator's unit, and a protected verbatim tail). Where the two disagree, this record wins.

## Decision

- **The first summarize attempt carries no `tool_choice`.** On Anthropic a changed `tool_choice` invalidates the message-layer cache entry, so a summarize request that always set it to none could reuse only the tools and system layers of the main loop's cached prefix, however much of the conversation it shared. Without it, the request can also read back the message-layer entry an earlier main-loop request wrote inside the collapsed prefix. The prompt's closing no-tools instruction is what keeps that attempt from calling a tool.
- **One retry, with `tool_choice: none`.** If the first response contains any tool call, or has no usable text, the request is sent again exactly once with `metadata["tool_choice"] = "none"`, which the first-party adapters enforce at the wire. An errored response is not retried (the LLM client has already spent its transient retries), and a text reply that is not a note is not retried either. Whatever the retry returns goes through the existing empty-summary and note-shape guards, so a retry that still answers with a tool call fails the step as before.
- **The retry is recorded.** It is an ordinary LLM round-trip through the same client (`LLMRequestStarted` / `LLMResponseRecorded` / `LLMRequestFinished`), and whether it happens is a pure function of the recorded first response, so a resumed run takes the same branch. Providers that ignore `tool_choice` (the fake providers, third-party adapters) behave as before, except that the first request's `metadata` no longer carries the key; a fake provider recognises the summarize call by its trailing instruction turn.
