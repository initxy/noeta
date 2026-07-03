# Composer & context caching

What the model sees each step is not an accumulating transcript — it is a
**View** the ContextComposer assembles on the spot from the folded state.
Like fold, the Composer is a pure function: the same state assembles the same
View. It runs once per `run_one_step`, and a `ContextPlanComposed` envelope
records exactly what the step was built from — which blocks were selected,
what was compacted, what was dropped — so every LLM call is auditable after
the fact.

## Three segments, ordered by volatility

The View is cut into three segments by how often each changes:

| Segment | Holds | Changes when |
| --- | --- | --- |
| `stable_prefix` | system prompt + tool definitions | identity or tool set changes |
| `semi_stable` | activated Skills, the memory index | the activated content set changes |
| `dynamic_suffix` | rolling conversation + tool results | every step |

The split exists for caching. Providers cache KV state by prefix: as long as
the prefix is byte-for-byte unchanged, the previous step's cache is reused
instead of re-encoded and re-billed. So the Composer pushes everything stable
to the front and keeps it byte-stable — sorted tool-schema keys, no
timestamps, a fixed field order — and pens all volatility into the tail. The
same determinism discipline that makes fold reproducible (see
[Fold & snapshot](fold-and-snapshot.md)) here buys cache hits instead.

Resident content enters the `semi_stable` segment through **content
channels**: an activation is recorded as an event, and a registered renderer
places the content into the segment on every subsequent assembly. Skills and
the memory index are the two in-tree tenants. The semi-stable segment is
exempt from compaction, so activated content survives long conversations.

## Compaction is an event, not an edit

When the conversation grows too long, something must be compacted. Noeta's
choice: compaction is **a recorded event, not an in-place edit of history**.
The Policy decides to compact; the Engine emits a compaction event carrying a
summary reference; fold reads it; and the next assembly swaps the compacted
stretch for the summary — while the stable prefix stays untouched and the
original messages stay in the log. Consequences:

- **Auditable and reproducible.** Compaction is in the log, so a recovered
  Task compacts the same way, and you can see afterwards exactly what was
  pared away.
- **Nothing is scrubbed.** The summary is a layer applied at assembly time,
  not an overwrite. The full history remains foldable underneath (see
  [Event sourcing](event-sourcing.md)).

A spin-guard backs this: if compaction keeps triggering while the recorded
boundary never advances, the Engine fails the Task rather than looping
forever.

Related: [Engine & execution](engine-execution.md) ·
[Provider neutrality](provider-neutrality.md) ·
[Event sourcing](event-sourcing.md)
