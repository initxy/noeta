# Memory recall: one shared word is not evidence

Status: SHIPPED 2026-09-18 in noeta-sdk 0.6.27 (`make check` green 4018/87.02%;
proposed the same day and revised after a review against the code). Distilled
into `CONTEXT.md` ("Memory") and `docs/adr/unified-context-supply.md` (2026-09-18
amendment). `recall_text` stays deferred — see "Deferred" below.
Asked for by the Aisthon host after an audit of its live requests; see
`docs/specs/2026-09-18-context-audit-fixes.md` there. Built together with
`memory-recall-names-links-size.md`: both touch the same matcher, and the
evidence rule here has to land no later than the non-ASCII names there (see
"Ordering" below).

## Goal

Measured on a live store of 36 pages over 56 conversations (2026-09-16 to 09-18):
recall injected 266 whole page bodies, 13% of the final context, and most were noise.
A conversation about drawing an architecture diagram was handed seven ByteIO
fault-report pages in full. Three causes in `builtins/memory/impl/`, and one on the
host's side:

1. **Tier 1 spends a whole body on one shared word.** `match_memories_tiered`
   (`matching.py`) hits when *any* filtered token of the page NAME is in the text.
   Names are slugs of ordinary technical words — `byteio-approval-cc-drs-schema-403`,
   `byteio-event-shell-no-requirement-deadlock`, `applog-web-report-channel` — so a
   text that says "schema", "shell" or "report" buys each of those pages whole.
2. **A shared prefix makes tier 1 alphabetical.** 24 of the 36 names start with
   `byteio-`. A text containing "byteio" tier-1-hits all 24; the cap keeps the first
   five in index order, which is the alphabet, not relevance.
3. **A page the host already rides is recalled again.** Aisthon records the page
   named `owner` as a resident of its own kind; recall only knows residents of the
   `memory` kind and pages loaded with `memory_read`, so the body rode twice in 47
   of 56 conversations. The host has no way to say "never recall this name".
4. **(Host side.) The host's receipts are matched like the user's words.**
   `RecallView.text` is the whole goal. Aisthon's goals carry a worker's report
   (2 000 characters dense with English identifiers) and bookkeeping lines; 99 of
   the 266 bodies were triggered by such a receipt, 69 by a person's message. This
   one needs no change here — see "Deferred".

A side effect of 1–2: the semantic judge (`recall.py`, `if not hits and judge`) almost
never runs, because a noisy tier-1 hit is still a hit.

Once done: a body rides only on evidence that names the page; one shared word earns
a pointer at most; a name the host excludes is silent in every tier.

## Scope

- **Tier-1 evidence** (`matching.py`). A name token carried by more than
  `max(3, len(entries) // 10)` names is *common* and counts for nothing as name
  evidence. A page tier-1-hits when the text shares **two** distinct non-common
  name tokens, or **all** of them when the name has fewer than two (a one-word
  name like `deploy` still hits on its word; a name made only of common tokens
  never hits by name). One shared non-common token is a tier-2 hit — a pointer.
  The CJK bigram path counts bigrams the same way. Commonness is counted over
  the **whole** store, including names that are excluded or already resident, so
  which pages happen to be in context never changes what counts as common.
- **Order.** Tier 1 sorts by the number of distinct matched name tokens,
  descending, then index order. Tier 2 sorts keyword-phrase hits first (a
  curator's alias is name-grade signal), then by distinct matched tokens across
  name and summary, descending, then index order. The cap then keeps the best,
  not the first — which also makes true what `recall_memories` already claims,
  that the body budget is spent on the high-confidence hits first.
- **Judge.** Runs when tier 1 is empty **and** the pointers have not already
  filled `max_hits` (today: only when there are no hits at all). Its candidates
  leave out excluded names, residents and the pages that are already pointers;
  its picks ride after the tier-2 pointers, under the same cap. A tier-1 hit
  never spends the call.
- **Exclusions.** `HostConfig.recall_exclude: Collection[str] = ()` — names silent
  in tier 1, tier 2, `related` pointers and the judge's candidates. The index
  still lists them and `memory_read` still reads them.
- **Near-duplicate note** (`MemoryWriteTool`). The write-time "similar existing
  memories" probe copies the recall rule inline ("symmetric with recall"), so it
  has the same flaw: in a store where 24 names share `byteio`, every new
  `byteio-*` page is reported as similar to the first three. It moves to the
  shared evidence helper: an existing page is similar when the new entry's
  name-plus-summary line would recall it in either tier, common tokens
  discounted, strongest first.

Non-goals: vector or embedding recall; changing the byte budgets; renaming or
migrating any store; the host's own page-naming advice.

## Deferred: `recall_text`

The first draft added an optional `recall_text` to `seed_start` / `seed_send_goal` /
`inject_goal` so a host could say which part of a goal is worth matching. Not built,
because the split already exists: `attachment_texts` (on those driver verbs and on
the `Client` send verbs since 0.6.11) records each text as its own
`origin="system"` message **before** the goal, and `IntakeGoalPrelude` documents
that attachments "never feed the recall key". A host that moves its receipts and
bookkeeping lines into `attachment_texts` and keeps the person's words as the goal
gets cause 4 fixed with no release. The cost is ordering: the receipt reads before
the person's words, as a separate message.

Revisit only if a host cannot live with that ordering. Two facts for whoever does:
it is a runtime change (`RecallView`, the driver verbs), so it forces a lockstep
release where everything else here is sdk-only; and the mid-turn landing of
`inject_goal` (`_inject_running`) runs no recall at all — only its fall-through to
`send_goal` does — so there is nothing to override there.

## Decisions

- Two tokens rather than a stopword list for technical English: a closed list cannot
  know that "schema" is common in one store and distinguishing in another; document
  frequency across the store's own names can, and it needs no dictionary.
- The demotion is to a pointer, never to silence: a single shared word is still a
  lead the model may follow with `memory_read`.
- This changes the default for every host, on purpose: a two-word name such as
  `deploy-notes` rides whole today on "deploy" alone and becomes a pointer. No
  knob — the old rule is the bug. Tests that pin a one-word tier-1 hit are
  re-pinned, and the changelog says so.
- The judge is not called when the pointers already fill the cap: its picks
  would be dropped, and the call sits on the turn-entry path.

## Ordering

`memory-recall-names-links-size.md` admits CJK page names. A CJK name tokenises
into character bigrams — `工具技能同步做法` yields seven — and under today's rule any
one of them ("工具", "做法") buys the whole body, which is worse than the English
case. The two-token rule must ship in the same release as, or before, the wider
name check; never after.

## Acceptance criteria

1. Fixture of 30 names, 20 sharing the prefix token `acme`: a text containing only
   "acme" yields no hit by name; "acme" plus one other token of a name yields that
   page as a pointer; two non-common tokens of one name yield its body.
2. A one-token name hits tier 1 on its token unless that token is common.
3. Five candidates with 1, 2, 2, 3, 1 matched tokens and `max_hits=3` return the
   3-token page first, then the two 2-token pages in index order.
4. Commonness does not move when a page is excluded or resident: the same text
   yields the same tier for every other page either way.
5. A name in `recall_exclude` is returned by no tier, is never a `related`
   pointer, and is absent from the judge's candidate list; the index text is
   unchanged.
6. With tier 1 empty and fewer than `max_hits` pointers the judge is called once,
   and its candidates omit the pointer pages; with a tier-1 hit, or with
   `max_hits` pointers, it is not called.
7. Writing a new `acme-*` page into the AC-1 fixture reports no similar page on
   the strength of `acme` alone; a new page sharing a non-common name token with
   an existing one still reports it.
8. `make check` green; changelog entry naming the default change; the memory
   docs state the two-token rule, the judge's trigger and `recall_exclude`.
