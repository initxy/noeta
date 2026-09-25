# Context and caching

What the model sees on each call is not a buffer that grows. The **context
composer** (`ThreeSegmentComposer`) assembles it from scratch every turn, out of
the Task's folded state, and lays it out so the provider's prompt cache keeps
hitting. Every assembly is recorded, so you can see exactly what the model was
shown on turn 37.

<NtContext />

## What it guarantees

- **Same state, same bytes.** The composer is deterministic: sorted schema keys,
  no timestamps, fixed field order. The same folded state always produces the same
  prompt.
- **The cached head stays put.** Everything that changes during a conversation is
  added at the tail, so the provider can reuse its cache for the head.
- **Compaction is an overlay.** A summary replaces old messages only in what the
  model sees; the original messages stay in the log.
- **Every prompt is auditable.** Each assembly is stored and referenced by a
  `ContextPlanComposed` event.

## Three segments, ordered by how often they change

| Segment | Holds | Changes when |
| --- | --- | --- |
| `stable_prefix` | system prompt (hashed together with the tool schemas) | the agent's identity or tool set changes |
| `semi_stable` | content loaded before the conversation starts: skill list, memory index, project instructions, environment facts | that set changes |
| `dynamic_suffix` | the conversation, tool results, reminders | grows at the tail each step; earlier messages are never rewritten, so they stay cached |

Providers reuse cached work only while the prompt prefix is byte-for-byte
unchanged, so a single changed byte near the front costs the whole request. That
is why swapping one tool, or activating a plugin, invalidates the cache: plan an
agent's tools and plugins up front, not per turn.

## Content added mid-conversation

Where a piece of standing content renders depends on when it was activated:

- **Before the first model reply** (memory index, root instructions file, skills
  chosen at start) → in `semi_stable`, part of the cached head.
- **Mid-task** (the model invokes a skill at turn 40) → one message in the
  conversation at the point where it was activated.

Inserting at the activation point costs only the new tokens; rewriting the head
would throw away the cache for the whole transcript. The insertion never splits a
tool call from its result.

With `HostConfig.instructions_discovery=True`, a successful `Read` of a file
inside the workspace also activates the nearest unseen `NOETA.md` / `AGENTS.md` /
`CLAUDE.md` in each directory from the root down to that file — useful for
monorepos with per-folder conventions. It only looks inside the workspace.

## Compaction

When the conversation gets too long:

1. The policy returns a summary and the range it covers.
2. The Engine records `CompactionRequested`, then `Compacted` with a reference to
   the summary.
3. From the next turn, the composer shows one summary message in place of the
   covered range. The stable prefix is untouched and the originals stay in the log.

A recovered Task compacts the same way, and the model can read a collapsed range
back with the `RecallHistory` tool. A compaction that would not move the boundary
forward fails the Task instead of looping.

## Two tail-only relief valves

- **Tool output pruning.** Only when the request nears the model's usable window,
  old tool outputs are replaced by `[tool output cleared]`. The call ids stay so
  the conversation remains valid, and the originals remain in the content store.
- **Reminders.** Short notes rendered at the very end of each prompt — unfinished
  todos, a pointer to compacted history. They are recomputed from state every
  turn and never written to the log.

## What this means for you

- Keep the system prompt and tool set stable within a Task to keep the cache warm.
- Put per-turn or time-varying information in reminders, not in the system prompt.
- To add your own standing content or reminders, register a `content_kind` or a
  `reminder` in a plugin — the composer itself cannot be replaced.

Design records:
[unified context supply](https://github.com/initxy/noeta/blob/main/docs/adr/unified-context-supply.md) ·
[anchored content placement](https://github.com/initxy/noeta/blob/main/docs/adr/anchored-content-placement.md) ·
[context compaction](https://github.com/initxy/noeta/blob/main/docs/adr/context-compaction.md)

## Next

- [The engine](engine.md) — the loop the composer runs in.
- [The plugin system](plugin-system.md) — the `content_kind` and `reminder` surfaces.
- [Connect a model](../guides/models.md) — what happens to the prompt at the adapter.
