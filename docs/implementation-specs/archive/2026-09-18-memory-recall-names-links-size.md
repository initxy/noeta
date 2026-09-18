# Memory recall: non-ASCII names, one hop along `related`, a write-time size cap

Status: SHIPPED 2026-09-18 in noeta-sdk 0.6.27 (`make check` green 4018/87.02%;
proposed 2026-09-17, revised 2026-09-18 after a review against the code).
Distilled into `CONTEXT.md` ("Memory") and `docs/adr/unified-context-supply.md`. Asked for by the Aisthon host, whose owner keeps a
Chinese-language memory store; see Aisthon's ADR-0018 and
`docs/specs/2026-09-17-one-desk-and-one-door-out.md` there. Built together with
`memory-recall-evidence.md`, whose two-token rule must ship no later than the
wider name check here (see "Ordering" in that spec).

## Goal

Three gaps in the memory builtin, measured on a live store of 36 pages:

1. **Pages with non-ASCII names are invisible, silently.** `_NAME_RE` in
   `packages/noeta-sdk/noeta/builtins/memory/impl/store.py` admits
   `^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`; `_iter_memories` skips anything else
   as an invalid slug, with no error anywhere. Four pages named in Chinese were
   in the directory, in git, and never in the index, the tiered recall, or
   `memory_search`.
2. **No second hop.** `match_memories_tiered` matches the message against page
   names, then summaries and keywords, then the judge; a page that is only
   *linked from* a hit page never comes along. With no vector path, links are
   the store's only multi-hop.
3. **Size is a convention.** `MemoryWriteTool` accepts any body; the host's
   3 000-byte rule is enforced by a nightly prompt, so pages grow until recall
   degrades them to a one-line pointer (`RECALL_BODY_MAX_BYTES = 4096`,
   `impl/index.py`).

Once done: a page named `工具技能同步做法` is indexed, recalled and searched
like any other; a body hit on page A brings the pages A's front matter names
under `related` as one-line pointers inside the existing hit cap; a write whose
body exceeds the store's cap is refused with a reason the model can act on.

## Scope

- **Name check.** Admit any Unicode letter or digit as the first character and
  letters, digits, `.`, `_`, `-` thereafter, at most 128 code points **and** 240
  UTF-8 bytes (a 128-character CJK name is 384 bytes, past the 255-byte file
  name limit once the suffix and the atomic write's temp name are added). Still
  refused: path separators, a leading dot, whitespace, control characters, and
  — newly — a trailing newline, which `$` let through. Existing ASCII names are
  unchanged. Tokenisation of a name keeps the current rule: ASCII words plus
  CJK bigrams (`_MIN_TOKEN_LEN` does not apply to CJK). Letters of other
  scripts make a legal name that simply carries no name tokens; such a page is
  reachable through its summary, keywords and `memory_search`.
- **`related`.** One front-matter line naming pages: `related: a, b`, with
  `[a, b]` and `[[a]], [[b]]` read the same way (brackets and quotes are
  stripped; separators are the keyword separators). It has to be one line: the
  fence parser is `key: value` lines only, and a YAML block list makes it drop
  the whole fence — description included — exactly as any other malformed fence
  does today. For each **tier-1** hit, in hit order, the names it lists that
  exist, are not already hits, and are neither resident nor excluded are added
  as summary-only pointers (the tier-2 shape), never bodies. They come after
  the direct hits and share `max_hits` with them, so the cap stays "items per
  goal" and a goal with five direct hits brings no neighbours. One hop only;
  dangling names are ignored silently. A pointer hit's links are not followed,
  and the judge is unchanged.
- **Cap.** `HostConfig.memory_max_bytes: Optional[int] = None` (`None` = today's
  behaviour). When set, `memory_write` with a body over the cap — the text after
  its optional fence, UTF-8 bytes — returns a failed result naming the size and
  the cap and writes nothing. `「这页 3 812 字节，上限 3 000：先压，或者拆成两页」` is
  the host's wording; the builtin's message is English and the host may map it.
  `memory_read` and archive are unaffected.

Non-goals: vector or embedding recall, retrieval scoring formulas, changing
the injection budgets, migrating existing stores, Unicode normalisation of
names (the file name is the name, byte for byte).

## Decisions

- One hop, pointers only, from body hits only: a full second-hop body would blow
  the per-turn budget on any well-linked store, and the model can `memory_read`
  a pointer it wants. Links are followed from tier 1 alone because a tier-2
  pointer is already a guess — after the two-token rule a single shared word
  earns one — and a neighbour of a guess is noise.
- Neighbours share the hit cap rather than the byte budget. The first draft
  counted them against `RECALL_TOTAL_MAX_BYTES`, but that budget has only ever
  counted bodies; pointers are bounded by the cap (five index lines), which is
  the tighter bound.
- Cap in the builtin, not the host: the host's after-check sees the write only
  after it is committed; refusing before the commit is the point. The body is
  what is measured because the fence carries fields the tool stamps itself
  (`created`, `updated`, `source_task`) — a refusal the model cannot fix by
  editing its text would be useless.
- The knob lives on `HostConfig` beside `global_memory_dir`, not on `Options`:
  it is a property of the store, not of one agent.
- The name regex widens rather than being replaced by a normaliser: the file
  name stays the name (`basic-memory`'s rule, which the host relies on for
  `git` attribution).

## Acceptance criteria

1. `store.py`: a page named `工具技能同步做法.md` lists, reads, indexes, matches on
   tier 1 for a message containing 「技能同步」, and is found by `search`; a name
   with `/`, a leading `.`, a space, a trailing newline, or more than 240 UTF-8
   bytes is refused. Golden index unchanged for the ASCII fixture.
2. `recall.py`: fixture A `related: [B, C]`, message names A → recall returns A's
   body and B, C as summary pointers; D linked from B is not returned; a
   `related` entry that does not exist is dropped without an error; a related
   name that is resident or in `recall_exclude` is dropped; with `max_hits`
   direct hits no neighbour is added; a page that is only a pointer brings none.
3. `memory_write` with `memory_max_bytes=3000` and a 3 001-byte body fails with
   both numbers in the message and writes nothing; a 3 000-byte body under a
   fence writes; with the option unset it writes as today.
4. `make check` green; changelog entry; the memory docs name `related` (and that
   it is one line) and `memory_max_bytes`.
