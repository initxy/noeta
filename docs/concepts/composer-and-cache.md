# Composer & context caching

What the model sees each turn is not an accumulating transcript — it is a
**View** the ContextComposer assembles on the spot from the folded state. Like
fold, the Composer is a pure function: the same state assembles the same View.
Its one side effect is writing the plan body to the ContentStore so the Engine
has a ref to attach. The Composer runs once per compose → decide turn, so a
single `run_one_step` composes as many times as the Policy keeps the loop
turning, and the Engine records a `ContextPlanComposed` envelope for each —
making every LLM call auditable after the fact.

## Three segments, ordered by volatility

The View is cut into three segments by how often each changes:

| Segment | Holds | Changes when |
| --- | --- | --- |
| `stable_prefix` | the system prompt | identity or the tool set changes |
| `semi_stable` | pre-loop resident content | the resident set changes |
| `dynamic_suffix` | rolling conversation, tool results, reminders | every turn |

Tool schemas ride alongside on `View.provider_tool_schemas` rather than inside a
segment, but they are hashed together with the system prompt — so swapping a
tool rotates the `stable_prefix` hash even when the prompt text is identical.

The split exists for caching. Providers cache KV state by prefix: as long as the
prefix is byte-for-byte unchanged, the previous turn's cache is reused instead of
re-encoded and re-billed. So the Composer pushes everything stable to the front
and keeps it byte-stable — sorted schema keys, no timestamps, a fixed field
order, a tool description omitted entirely when empty — and pens all volatility
into the tail. The same determinism discipline that makes fold reproducible (see
[Fold & snapshot](fold-and-snapshot.md)) here buys cache hits.

Resident content enters through **content channels**: an activation is recorded
as a `ContextContentRecorded` event, and the renderer registered for that kind
places the content on every subsequent assembly. Registration order is the
`semi_stable` layout, and the built-in tenants occupy fixed bands — `skill`,
then `memory`, then `instructions`, then `environment`, with any host-registered
kind after them. Resident content is exempt from compaction, so it survives long
conversations.

## Placement follows the activation anchor

*Where* a resident renders depends on *when* it was activated. Fold records each
resident's **anchor** — the rolling-history length at activation — and one rule
decides placement for every kind:

- Activated **pre-loop** (memory index, the root instructions file, seed-time
  skills — anchor at or before the first assistant message) → renders in
  `semi_stable`, as part of the head the session rides on.
- Activated **mid-task** (the model invokes a skill at turn 40) → renders inside
  the `dynamic_suffix`, **at its anchor position**: one message, at the point in
  the conversation where it was activated.

This is a caching decision. `semi_stable` sits ahead of the dialogue, so
rewriting it mid-conversation would invalidate the provider's KV cache from there
through the entire transcript — a full re-prime per activation, worst in exactly
the long sessions where skills actually get invoked. Anchored insertion pays only
for the inserted tokens.

Two details make it safe:

- **Insertion never splits a tool round-trip.** The index slides forward past
  any `role="tool"` message, so content can never land between an assistant
  `tool_use` and its results (providers reject that shape). The slide is
  deterministic — same folded state, same bytes.
- **Compaction re-hangs the content** at the summary's edge, in anchor order.
  That is free at the moment it happens (compaction already invalidated the
  cache) and automatic: resident content is re-rendered from folded activation
  state rather than stored in history, so compaction cannot lose it.

### Instruction files are discovered as the model reads

A default-off host switch (`HostConfig.instructions_discovery`) arms a post-tool
hook: after a successful `read` of a file **inside the workspace**, the runtime
walks the workspace root down to that file's directory — shallowest first, root
itself excluded, since the root file is loaded pre-loop — and activates the first
`NOETA.md` / `AGENTS.md` it finds in each directory that has not contributed one
yet. This is the monorepo case: a subtree carrying its own conventions. Each
activation is an ordinary content-channel event emitted after the turn's tool
results, so it anchors right after the read that triggered it and appends instead
of rewriting the head.

Discovery is fenced to the workspace even though `read` itself is not (see
[Built-in tools](../reference/tools.md)). Reading is observation; instructions
steer the agent. Auto-loading them from any path the model happens to glance at
would let an arbitrary directory program the agent, so the auto-load scope stays
inside the directory the session was given.

## Compaction is an event, not an edit

When the conversation grows too long, something must be compacted, and Noeta
records it as an event rather than editing history in place. The Policy decides
to compact and hands back the summary plus the boundary it covers; the Engine
emits `CompactionRequested` then `Compacted` with a reference to the summary
body; fold projects both onto the context slice; and the next assembly swaps the
covered prefix for a single summary message — while the stable prefix stays
untouched and the original messages stay in the log. Consequences:

- **Auditable and reproducible.** Compaction is in the log, so a recovered Task
  compacts the same way, and you can see afterwards exactly what was pared away.
- **Nothing is scrubbed.** The summary is a layer applied at assembly time, not
  an overwrite. The full history remains foldable underneath (see
  [Event sourcing](event-sourcing.md)).

A spin-guard backs this: a compaction whose boundary would not advance past what
is already collapsed fails the Task rather than looping forever. A separate
detector latches a "thrashing" flag when several compactions land within a few
turns of each other, which a reminder turns into a hint to stop re-reading the
same bulk content.

## Two tail-only mechanisms

Both live strictly in the `dynamic_suffix`, so neither can churn the cached head:

- **Tail pruning** is a relief valve, not a clamp. Only once the request
  approaches the model's usable window does the Composer clear tool outputs older
  than a token budget to a lean `[tool output cleared]` marker; the blocks keep
  their call ids so the conversation stays well-formed, and every cleared body's
  ContentStore ref goes into the plan — deref-able for audit, absent from the
  prompt. Below the window nothing is cleared, so a half-empty context never
  forces the model to re-run a tool.
- **Compose-time reminders** are pure renderers appended at the very end of the
  tail: unfinished todos, a delegation nudge, a read-strategy hint while
  compaction thrashes. They are View-only — never written to the message stream,
  never recorded as events — and re-derived from folded state on every compose,
  so resume reproduces them for free.

Everything a turn was built from lands in the plan body: the segment hashes, the
skills that actually rendered, the messages kept versus cleared, the cleared
bodies' refs, and any skill resource inlined or skipped.

Related: [Engine & execution](engine-execution.md) ·
[Provider neutrality](provider-neutrality.md) ·
[Event sourcing](event-sourcing.md)
