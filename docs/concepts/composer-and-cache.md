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

Resident content enters through **content channels**: an activation is
recorded as an event, and a registered renderer places the content on every
subsequent assembly. Skills, the memory index, workspace instructions, and
environment facts are the in-tree tenants. Resident content is exempt from
compaction, so it survives long conversations.

## Placement follows the activation anchor

*Where* a resident renders depends on *when* it was activated. Fold records
each resident's **anchor** — the rolling-history length at activation — and one
rule decides placement for every kind:

- Activated **pre-loop** (memory index, the root instructions file, seed-time
  skills — anchor at or before the first assistant message) → renders in
  `semi_stable`, as part of the head the session rides on.
- Activated **mid-task** (the model invokes a skill at turn 40) → renders
  inside the `dynamic_suffix`, **at its anchor position**: one message, at the
  point in the conversation where it was activated.

This is a caching decision. `semi_stable` sits ahead of the dialogue, so
rewriting it mid-conversation invalidates the provider's KV cache from there
through the entire transcript — a full re-prime per first activation,
proportional to everything already said. Anchored insertion pays only for the
inserted tokens. The tax was worst exactly where sessions are longest, which is
also where skills actually get invoked.

Two details make it safe:

- **Insertion never splits a tool round-trip.** The index slides forward past
  any `role="tool"` message, so content can never land between an assistant
  `tool_use` and its results (providers reject that shape). The slide is
  deterministic — same folded state, same bytes.
- **Compaction re-hangs the content** at the summary's edge, in activation
  order. That is free at the moment it happens (compaction already invalidated
  the cache) and automatic: resident content is re-rendered from folded
  activation state rather than stored in history, so compaction cannot lose it
  and there is no "remember to re-attach" step.

### Instruction files are discovered as the model reads

A default-off host switch (`HostConfig.instructions_discovery`) arms a
post-tool hook: after a successful `read` of a file **inside the workspace**,
the runtime walks from that file's directory up to the workspace root and
activates every not-yet-active `NOETA.md` / `AGENTS.md` on the way. This is the
monorepo case — a subtree carrying its own conventions. Each activation is an
ordinary content-channel event emitted after the turn's tool results, so it
anchors right after the read that triggered it and appends instead of rewriting
the head.

Discovery is fenced to the workspace even though `read` itself is not
(see [Built-in tools](../reference/tools.md)). Reading is observation;
instructions steer the agent. Auto-loading them from any path the model happens
to glance at would let an arbitrary directory program the agent, so the
auto-load scope stays inside the directory the session was actually given.

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
