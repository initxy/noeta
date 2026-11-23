---
status: done
created: 2026-11-23
branch: fix/request-size-limit
---

# Requests stay under the provider's size limit

## Goal

A conversation that has collected many images keeps working: before a request is sent, older
images give way to a placeholder so the body stays under the provider's request size limit, a
body still over the limit is never sent, and a failure names the sizes.

## Non-goals

- Dropping an older read of the same path first. Claude Code drops oldest first whatever the
  path; so does this.
- Changing when old tool results are cleared (`noeta/plugins/compaction/clearing.py`) or the
  per-image token estimate (`IMAGE_TOKENS`): tokens and bytes are separate budgets.
- Guessing from a dropped connection that the request was too large, or learning a limit from a
  413. The size is known before sending.
- Claude Code's limit on re-reading an unchanged image after images were dropped (2.1.321): a
  later step if it turns out to be needed.
- A switch to turn the cap off.

## Context

Incident, 2026-11-23 (a host on noeta 1.2.3, `anthropic` family through a relay): a task read 44
rendered PNGs over two hours, 0.2-1.3 MB each in base64, 33.79 MB together, every one resent on
every call. By tokens the context was about 290K, far from compaction. The body reached
34.64 MB; the relay refuses over 32 MiB by the declared length and closes the connection, so
the client saw `httpx.ReadError` with an empty message instead of the 413. That is a
`Transient`: nine attempts, then `llm_error: ReadError: `. The image sizes of that request are
the regression data (`tests/backends/providers/`).

What is missing in noeta 1.2.4:

1. Nothing measures a request in bytes. Compaction and clearing count tokens, an image 2,000.
2. A refusal that arrives as a dropped connection is retried as a network failure; the
   existing "413 -> compaction" path is never reached.
3. An image stays in every request until a compaction.
4. The failure says neither that size was the cause nor any number.

Claude Code 2.1.330 (read from its bundle) applies one rule to every request it sends, a pure
function of the message list: walking images and documents oldest to newest, when their base64
size passes 24 MiB (the 32 MiB request limit less 8 MiB) the oldest are replaced by a text
placeholder until at most 14 MiB remain; when they number more than 100 (600 on models with a
larger window) the oldest go until 80 remain. A decision depends only on the messages before
it, so appending messages never changes an earlier one and the prompt cache breaks once per
cut. It applies the byte cap only against Anthropic's own API and relies on the server's
"too large" answer elsewhere; noeta's providers are mostly relays, where that answer often
does not arrive, so here the cap applies to every provider (owner, 2026-11-23).

## Decisions

- **Where.** In the provider adapters (`noeta/backends/providers/`), on the way to the wire:
  everything any caller sends is covered (the main request, a summary, a plugin's own call) with
  that endpoint's own limit, and the task's log and state are untouched. Shared code in
  `noeta/backends/providers/_request_size.py`.
- **Limit.** `max_request_bytes`, a constructor argument of the three adapters, default
  33,554,432 (32 MiB, Claude Code's constant). `config.toml` takes it at top level (the default
  provider) and in a `[providers.<name>]` table, like `prompt_cache_ttl`; a positive integer.
- **Media budget.** Derived from the limit: cap `limit * 3 // 4`, and a cut goes down to
  `cap - min(10 MiB, cap // 2)`; with the default that is Claude Code's 24 MiB and 14 MiB. Item
  cap 100, or 600 when the model's context window is over 200,000 tokens; a cut leaves 20 fewer.
- **Measure.** An image counts its base64 length (`noeta.core.images.base64_size`). noeta has no
  document blocks; PDF pages are images already.
- **The sweep** (Claude Code's `gJn`): images in conversation order, top-level ones and those in
  tool results alike, but only the ones the adapter sends: Chat Completions sends a tool result
  as text only, and a model catalogued without vision gets no tool-result image, so there those
  take no room and stay as they are. Each joins the kept list and the total. Over the item cap: the oldest
  kept go until `cap - 20` remain. Then, over the byte cap: the oldest kept go until the total
  is at most the lower mark. An image is "seen" when an assistant message lies between it and
  the image whose arrival removed it.
- **What replaces an image.** In a tool result: a text block in its place, on a line of its own
  between the result's texts (an adapter may send them joined); the result keeps its shape
  (`block_list` as it was), so its text is escaped as before. At a message's top
  level: the block is dropped, and only a message left empty gets the placeholders (each
  distinct text once) as its content. Message count and order never change. The texts are
  Claude Code's, verbatim:
  - seen: `[media removed: request limit. You have already seen this, and what you concluded
    from it still stands. It made room for newer images and documents. Re-reading pushes out
    others and re-sends everything, so don't re-read them all.]`
  - not seen: `[media removed: request limit. You have not seen this: it did not fit in the
    request. If you need it, get it again once, by itself. If it is dropped again, go on
    without it and say so. Re-reading pushes out others and re-sends everything, so don't
    re-read them all.]`
- **Applied in `build_body`**, so the body an adapter builds is the body it sends.
- **Body check.** An adapter serializes the body itself (the bytes httpx would produce from
  `json=`: `json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False)` in
  UTF-8) and sends it as `content=` with `Content-Type: application/json`. Over the limit:
  nothing is sent and `RequestTooLarge` is raised, a `ContextOverflow` subclass
  (`noeta/core/errors.py`) carrying `body_bytes`, `limit_bytes`, `media_bytes`. Its message:
  `Request too large: 34.6 MB, over the 32.0 MB request limit (24.1 MB of it images)`.
- **Recovery.** `RequestTooLarge` takes the existing overflow path (`_on_overflow`, compaction),
  except when `body_bytes - media_bytes > limit_bytes`: the text alone does not fit, a summary
  request would not either, so the turn fails once, reason `context_overflow`, with a detail
  that says so and gives the three sizes (Claude Code 2.1.229).
- **Messages of other failures.** A 413 the server did answer names the body size. A transport
  failure before any response, on a body of 1 MiB or more, says the body size and that a server
  refusing an oversized request may drop the connection this way, naming `max_request_bytes`.
  Both stay what they are (`ContextOverflow`, `Transient`); only the text changes.

## Plan

- [x] `_request_size.py`: the budgets, the sweep (`cap_media`), `fit_request`, `encode_body`;
      `RequestTooLarge` in `noeta/core/errors.py`. Unit tests, with the incident's sizes.
- [x] The three adapters: `max_request_bytes`, the cap in `build_body`, the body sent as
      `content=` after the check, sizes in the 413 and transport messages. Adapter tests.
- [x] `_on_overflow`: the fail-once case. Runner test.
- [x] `config.toml`: `max_request_bytes` at top level and per provider table. Config tests.
- [x] Docs, English and Chinese: `docs/reference/config.md`, `docs/guides/models.md`, one
      explanation in `docs/how-it-works/context.md`; `CHANGELOG.md` under Unreleased.
- [x] `make check` green; `make parity` unchanged at 0; `make eval` and the provider `make e2e`
      tests with no regression.

## Acceptance criteria

1. The incident's request shape (516 messages, the 44 image sizes, all in tool results) builds
   an Anthropic body under 32 MiB: the oldest images are placeholders with the "seen" text, the
   newest are intact. The same holds on the Responses wire; on Chat Completions, which sends no
   tool-result image, the body is under the limit with nothing replaced.
2. For every prefix of that conversation, an image removed there is removed with the same text
   in every longer prefix, and a request under both caps is returned unchanged (the same
   object).
3. Item cap: 101 small images leave 80; with a model whose window is over 200,000, 600 stay.
4. An image removed by the image that arrived in the same step, with no assistant message
   between them, carries the "not seen" text.
5. A top-level image is dropped; a message left empty holds the placeholder; no message is
   added or removed.
6. A body over `max_request_bytes` raises `RequestTooLarge` with the three sizes and no HTTP
   request is made (each adapter, streaming and `complete_whole`).
7. `RequestTooLarge` whose text alone is over the limit fails the turn once with
   `context_overflow` and the sizes in the detail, and no extension's `on_context_overflow` is
   asked; otherwise the hook is asked as for any overflow.
8. `max_request_bytes = N` at top level and in a provider table reaches the adapter; a value
   that is not a positive integer is a `ConfigError` naming where it is set.
9. A transport failure before the response on a body of 1 MiB or more names the body size and
   `max_request_bytes`; on a small body the message is as before. A 413 response names the body
   size.
10. Bodies under the caps are byte-for-byte what was sent before this change (golden requests
    and `make parity` unchanged).

## Risks

- Serializing the body ourselves must not change a byte: httpx's own encoding is copied, and
  the golden requests and parity hold it.
- The Anthropic adapter marks cache breakpoints by message and block position; dropping a
  top-level image moves later blocks of that message. The mark must still land on the message's
  last block.
- A summary request gives cleared tool results back (`clearing.uncleared`), images included; the
  cap runs again in the adapter on that request, so it is covered there.
- A relay with a limit under 32 MiB still fails until `max_request_bytes` is set; the transport
  message is what points there.

## Progress log

- 2026-11-23: shaped with the owner: default 32 MiB with a config override, no guessing from a
  dropped connection, the cap on every provider. `make check` green on main before starting.
- 2026-11-23: the failed request read back from the host's request store: 516 messages, 44
  images, 33,787,028 base64 characters, none top-level.
- 2026-11-23: `make check` before starting: one red not from this change,
  `tests/plugins/test_workflow.py::test_a_replay_has_a_time_limit` (a 0.5 s replay timed out
  under load average 27); it passes alone.
- 2026-11-23: `_request_size.py`, `RequestTooLarge`, the three adapters, `_on_overflow`,
  `config.toml` and the docs done; the incident's request keeps its 30 newest images (22.7 MB)
  and replaces the 14 oldest with the "seen" placeholder on every wire.
- 2026-11-23: acceptance, decision change: the sweep counts only the images an adapter sends.
  As first built it also replaced the "[image omitted …]" notes of Chat Completions and of
  text-only models with a placeholder saying the model had seen an image it never got.
- 2026-11-23: acceptance, decision change: a result whose image was replaced keeps its shape.
  As first built it was switched to a block list, a path of the Anthropic wire that does not
  escape reserved tags in the result's text; the placeholder now goes on its own line instead.
- 2026-11-23: `media_bytes` is read from the body as sent (each wire's image shape), so a wire
  that leaves an image out does not count it.
- 2026-11-23: gates on the tree after the two changes above: `make check` green (3917 passed,
  coverage 90.76%; TUI 177), `make parity` 0 mismatches, `pytest -m live` 7 passed, `make eval`
  0.968 against a 0.953 baseline, no regression. One `make check` run had three TUI timing tests
  red ("UI did not settle") while a 34 MB replay ran beside it; green when run alone.
- 2026-11-23: the failed request itself, rebuilt from the host's request store: 34,638,069 bytes
  as 1.2.4 built it, 23,568,901 now, the 14 oldest images replaced as "seen", 30 kept.
- 2026-11-23: independent review: no P0-P2 in the code; the ledger was behind (eval and e2e had
  run after its last entry). From its P3s: a provider package's adapter without the argument
  now gives a `ConfigError` instead of a bare `TypeError`; the guide no longer repeats the
  default. Left as is: sizes under 0.05 MB print as `0.0 MB` (only with a limit nobody sets).
