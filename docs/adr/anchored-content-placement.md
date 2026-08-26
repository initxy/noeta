# Content placement follows the activation anchor, and instruction files are discovered on read

## Context

The content channel's residents — skills, the memory index and recalled memory bodies, workspace instructions, environment facts — render into the composer's `semi_stable` segment, between the byte-frozen `stable_prefix` and the dialogue. *When* a resident activates matters more than how often its bytes change. A resident activated pre-loop lands before the first token of dialogue, so its bytes are part of the prompt the whole session rides on. A resident activated mid-task — the model invokes a skill at turn 40 — rendered in that segment would rewrite a prefix that precedes the entire transcript, invalidating the provider's KV cache from that point through everything already said. The cost of each first activation grows with session length, and long sessions are exactly where skills get invoked.

A second problem shares the same machinery. Workspace instruction files (`NOETA.md` / `AGENTS.md` / `CLAUDE.md`) load once, pre-loop, from the workspace root. Instruction files in subdirectories — the monorepo case, where a subtree carries its own conventions — are readable but not loaded, and the trigger for loading them can only be built by whatever sees the tool loop.

## Decision

**Placement follows the activation anchor.** When fold merges a resident into `TaskState.active_content`, it also records the current rolling-history length as that resident's anchor, keyed `"<kind>:<name>"` in `ContextState.content_anchors`, first-write-wins — a refresh moves bytes, never placement. One rule decides placement for every kind, with no per-kind flag:

* An anchor at or before the first assistant message ⇒ the resident renders in `semi_stable`. Pre-loop activations land there.
* An anchor after the first assistant message ⇒ the resident renders inside the dynamic suffix, at its anchor position — one message, at the point in the conversation where it activated. The head segments are untouched, so the provider pays only for the inserted tokens.

**Insertion never splits a tool round-trip.** The insertion index is computed in the post-summary coordinate space and slides forward past any `role="tool"` message, so rendered content can never land between an assistant `tool_use` and its results, a shape providers reject. The slide is deterministic: the same folded state yields the same bytes.

**Compaction re-hangs at the summary's edge.** An anchor covered by the compaction boundary clamps to the position right after the summary message, before the surviving turns, in activation order. It is free at the moment it happens — compaction has already invalidated the cache wholesale — and it is automatic, because the content is state-derived rather than stored in the transcript, so there is no re-attachment step to forget.

**Instruction files are discovered when the model reads near them.** A default-off host switch (`instructions_discovery`) arms a post-tool hook: after a successful `read` whose path resolves inside the workspace, the runtime walks from the workspace root down to the file's directory, shallowest first, and activates the first existing, non-empty `NOETA.md` / `AGENTS.md` / `CLAUDE.md` in every not-yet-active directory. The resident name is the workspace-relative path; the activation is an ordinary `ContextContentRecorded` (kind `instructions`, policy `evolving`) emitted after the turn's batched tool-result message, so the anchor — and therefore the rendered instructions — lands right after the read that triggered it.

**Discovery is fenced to the workspace even though reads are not.** `read` resolves an absolute path wherever it points by design (`workspace-write-authorization.md`). Auto-loading instruction files from arbitrary read targets would let any directory the model glances at program the agent, and write authorization is not instruction authority: a directory an owner granted write access to does not thereby get to inject instructions. Discovery therefore fires only for reads resolving inside the workspace root. Widening that scope is a host decision behind a future seam, not a default.

**Resume rebuilds from the ledger.** A discovered file is read at trigger time — the impure band — and its rendered bytes go into the `ContentStore` before the activation is recorded, so the renderer resolves them at the recorded hash and never touches disk at compose time. A preload step re-reads active-but-missing instruction files before compose, keeping the hash seam and the discovery walk's already-active check consistent across a fresh process. A name whose bytes cannot be resolved renders nothing: the `evolving` policy tolerates drift, and a degraded resolve may only omit, never fail the task.

## Rationale

- **Pay-per-activation beats pay-per-transcript.** Anchored insertion costs the new tokens once. Head placement costs a full downstream re-prime per first activation, proportional to everything already said — worst exactly when the session is longest.
- **One rule, no kind table.** "Anchor before or after the first assistant message" needs no per-kind configuration and no event-shape change: anchors are derived at fold time from the stream, so any folded stream replays deterministically under the rule.
- **The ledger is the re-attachment mechanism.** Content stored in the transcript is destroyed when the transcript is summarised, which forces dedicated repair machinery. Deriving it from `active_content` plus anchors turns the re-hang into a coordinate transform inside one pure function.
- **The trigger can only live in the harness.** Lazy instruction loading requires seeing every `read` as it happens; a host outside the tool loop cannot build it. Exposing it as a default-off switch keeps the mechanism in the deep module and the policy with the host.
- **The injection line is trust, not reachability.** Reads are unfenced because reading is observation. Instructions are not observation — they steer the agent — so their auto-load scope stays inside the one directory the owner actually endorsed.

## Alternatives considered

1. **Render every resident in `semi_stable` regardless of when it activated.** Rejected: each first mid-task activation re-primes the whole downstream transcript, and the tax rises with session length.
2. **Put activated content into the transcript as ordinary messages or tool results, never touching the head.** Rejected: transcript content dies with compaction, so it needs a registry, a re-injection pass and a budget to survive; state-derived residents get compaction immunity without any of it.
3. **Give each content kind a placement flag.** Rejected: configuration where one derived fact already answers the question for every kind, present and future.
4. **Discover instruction files next to any read, wherever it resolves.** Rejected: reads reach anywhere on the filesystem, so this would let an unrelated directory inject instructions into the agent.
5. **Read discovered instruction files at compose time.** Rejected: it breaks the composer's purity and with it both the prompt cache and resume re-derivation.

## Consequences

- A session that never activates content mid-task composes with head segments identical to a pure pre-loop session; a mid-task activation adds exactly its own message.
- Snapshots carry `content_anchors`; a rehydrated snapshot without them keeps its residents in `semi_stable`.
- `instructions_discovery` defaults off. Turning it on costs one directory walk per triggering read plus one file read per newly discovered file, once per session.
- Subdirectory instruction files become live context the moment the model works near them, hashed, folded and replayable like any other resident.
