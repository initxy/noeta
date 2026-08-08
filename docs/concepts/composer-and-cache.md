# Composer & context caching

What the model sees on each call is not a transcript that grows in a buffer
somewhere. It is assembled from scratch every turn, out of the Task's folded
state, by a component called the **ContextComposer**. The thing it produces is
a **View**: the exact prompt, tool schemas, and message list that go out on the
wire.

Assembling instead of accumulating buys two things. The Composer is a pure
function — the same state always assembles the same View — and every assembly
is recorded, so you can go back later and see precisely what the model was
shown on turn 37.

<p align="center">
  <img src="../assets/diagrams/context-composer.svg" alt="Context composer — folded state assembled into a stable prefix, semi-stable segment, and dynamic suffix, then sent to the provider" width="820">
</p>

The Composer's only side effect is writing the plan body to the ContentStore so
the Engine has a reference to attach. It runs once per compose → decide turn,
so a single `run_one_step` composes as many times as the Policy keeps the loop
turning, and the Engine records a `ContextPlanComposed` envelope for each one.

## Three segments, ordered by volatility

The View is cut into three segments by how often each one changes:

| Segment | Holds | Changes when |
| --- | --- | --- |
| `stable_prefix` | the system prompt | identity or the tool set changes |
| `semi_stable` | resident content activated up front | the resident set changes |
| `dynamic_suffix` | rolling conversation, tool results, reminders | every turn |

Tool schemas ride alongside on `View.provider_tool_schemas` rather than inside
a segment, but they are hashed *together with* the system prompt — so swapping
one tool rotates the `stable_prefix` hash even when the prompt text is
identical.

## Why the layout is shaped like this

The split exists for **caching**. Providers cache KV state by prefix: as long
as the prefix is byte-for-byte unchanged from the previous call, it is reused
rather than re-encoded and re-billed. A single stray byte near the front throws
that away for the whole request.

So the Composer pushes everything stable to the front and keeps it byte-stable
— sorted schema keys, no timestamps, a fixed field order, a tool description
omitted entirely when it is empty — and pens all the volatility into the tail.
The `stable_prefix` hash is
`sha256(to_canonical_bytes((stable_content, provider_tool_schemas)))`.

This is the same determinism discipline that makes fold reproducible (see
[fold & snapshot](fold-and-snapshot.md)). There it buys replay; here it buys
cache hits.

## Resident content

Some content should sit in front of the conversation and stay there — the
skill catalogue, the memory index, project instructions, environment facts.
That is the `semi_stable` segment, and things enter it through a mechanism
called a **content channel**.

An activation is recorded as a `ContextContentRecorded` event, and the renderer
registered for that kind places the content on every subsequent assembly.
Registration order *is* the `semi_stable` layout, and the built-in tenants
occupy fixed bands: `skill`, then `memory`, then `instructions`, then
`environment`, with any host-registered kind after them.

Because resident content is re-rendered from folded state rather than stored in
the message history, compaction cannot flush it. It survives long
conversations for free.

## Placement follows the activation anchor

*Where* a resident renders depends on *when* it was activated. Fold records
each resident's **anchor** — the rolling-history length at the moment of
activation — and one rule covers every kind:

- Activated **pre-loop** (the memory index, the root instructions file,
  seed-time skills — anchor at or before the first assistant message) → renders
  in `semi_stable`, part of the head the whole session rides on.
- Activated **mid-task** (the model invokes a skill at turn 40) → renders
  inside the `dynamic_suffix`, **at its anchor position**: one message, at the
  point in the conversation where it was activated.

This is a caching decision, not an aesthetic one. `semi_stable` sits ahead of
the dialogue, so rewriting it mid-conversation would invalidate the provider's
cache from there through the entire transcript — a full re-prime per
activation, worst in exactly the long sessions where skills actually get used.
Anchored insertion pays only for the inserted tokens.

Two details make it safe:

- **Insertion never splits a tool round-trip.** The index slides forward past
  any `role="tool"` message, so content can never land between an assistant
  `tool_use` and its results (providers reject that shape). The slide is
  deterministic: same folded state, same bytes.
- **Compaction re-hangs the content** at the summary's edge, in anchor order.
  That is free at the moment it happens — compaction already invalidated the
  cache — and automatic, because resident content is state-rendered rather than
  stored in history.

### Instruction files are discovered as the model reads

A default-off host switch (`HostConfig.instructions_discovery`) arms a
post-tool hook. After a successful `Read` of a file **inside the workspace**,
the runtime walks from the workspace root down to that file's directory —
shallowest first, the root itself excluded, since the root file is loaded
pre-loop — and activates the first `NOETA.md` / `AGENTS.md` / `CLAUDE.md` it
finds in each directory that has not contributed one yet.

This is the monorepo case: a subtree that carries its own conventions. Each
activation is an ordinary content-channel event emitted after the turn's tool
results, so it anchors right after the read that triggered it and appends
instead of rewriting the head.

Discovery is fenced to the workspace even though `Read` itself is not (see
[built-in tools](../reference/tools.md)). Reading is observation; instructions
steer the agent. Auto-loading them from any path the model happens to glance at
would let an arbitrary directory program the agent, so the auto-load scope
stays inside the directory the session was given.

## Compaction is an event, not an edit

When a conversation grows too long, something has to give. Noeta records that
as an event rather than editing history in place:

1. The Policy decides to compact and hands back the summary plus the boundary
   it covers.
2. The Engine emits `CompactionRequested`, then `Compacted` with a reference to
   the summary body.
3. Fold projects both onto the context slice.
4. The next assembly swaps the covered prefix for a single summary message —
   while the `stable_prefix` stays untouched and the original messages stay in
   the log.

Two consequences follow. Compaction is **auditable and reproducible**: it is in
the log, so a recovered Task compacts the same way and you can see afterwards
exactly what was pared away. And **nothing is scrubbed**: the summary is a
layer applied at assembly time, not an overwrite, so the full history stays
foldable underneath (see [event sourcing](event-sourcing.md)).

A spin-guard backs this up. A compaction whose boundary would not advance past
what is already collapsed fails the Task rather than looping forever. A
separate detector latches a "thrashing" flag when several compactions land
within a few turns of each other, which a reminder turns into a hint to stop
re-reading the same bulk content.

## Two tail-only mechanisms

Both live strictly in the `dynamic_suffix`, so neither can churn the cached
head:

- **Tail pruning** is a relief valve, not a clamp. Only once the request
  approaches the model's usable window does the Composer clear tool outputs
  older than a token budget to a lean `[tool output cleared]` marker. The
  blocks keep their call ids so the conversation stays well-formed, and every
  cleared body's ContentStore ref goes into the plan — dereferenceable for
  audit, absent from the prompt. Below the window nothing is cleared, so a
  half-empty context never forces the model to re-run a tool.
- **Compose-time reminders** are pure renderers appended at the very end of the
  tail: unfinished todos, a delegation nudge, a read-strategy hint while
  compaction thrashes, and — once a compaction has collapsed a prefix — a
  pointer at the collapsed range the `RecallHistory` tool can read back. They
  are View-only — never written to the message stream, never recorded as
  events — and re-derived from folded state on every compose, so resume
  reproduces them for free.

Everything a turn was built from lands in the plan body: the segment hashes,
the skills that actually rendered, the messages kept versus cleared, the
cleared bodies' refs, and any skill resource inlined or skipped.

## Next

- [Engine & execution](engine-execution.md) — the loop the Composer runs inside.
- [Provider neutrality](provider-neutrality.md) — what happens to the View once
  it reaches an adapter.
- [Built-in tools](../reference/tools.md) — the tools whose schemas ride in the
  stable prefix.
- [Extension planes](../architecture/extension-planes.md) — the two
  registration hooks the Composer opens (`content_kind` and `reminder`).
