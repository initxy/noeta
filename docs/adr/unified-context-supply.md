# Resident context content rides one generic append-recorded channel, and the composer stays a read-only fold

## Context

"Getting content into the context" is three mechanisms wearing one name: an append into the event ledger, a rendering rule, and a content fingerprint. The content itself splits in two. *Material* — skills, a memory index, workspace instructions, environment facts, and whatever a deployment invents — is an open set the kernel cannot enumerate. *Mechanism* — messages, tool results, compaction summaries, re-attached thinking — is a closed set whose semantics drive the engine loop.

The kernel stays neutral to material while keeping composed bytes re-derivable from the ledger alone: a resumed task rebuilds its own context, and the prompt cache depends on the stable and semi-stable segments serialising identically across steps (see the Stable Prefix entry in `CONTEXT.md`).

## Decision

**A provider is one thing: an adapter for an external service.** Each external service (LLM, storage, vector store) implements one adapter against the matching internal Protocol (`provider-neutral.md`). Getting content into a context is not a provider; each path carries its own name — skill activation, memory recall, reminder injection.

**Material rides a generic content channel.** `TaskState.active_content` is `kind → {name → content_hash}`; activation is one `ContextContentRecorded(kind, name, version, content_hash, policy)` event. The hash is last-write-wins, so re-recording a name with new bytes is a refresh; the activation anchor is first-write-wins, so a refresh moves bytes and never placement. A resident may instead record activate-once (`record_content(refresh=False)`): the first hash wins for the task's whole life and later records with different bytes append nothing — for a source that is re-*captured* per process (the environment's wall clock and git snapshot) rather than re-read from a deterministic file, this is what keeps the semi-stable bytes identical across resumes and the prompt-cache prefix alive. What a kind *means* is SDK knowledge: one `ContentKindSpec` (renderer + hash resolver + drift policy) per kind, collected into a `ContentChannelRegistry` whose registration order is the semi-stable layout. Adding a kind changes no kernel code.

**Mechanism stays typed events.** Messages, tool results, compaction and thinking are not folded into the channel: the engine loop is driven by their semantics (a user message wakes it, a tool result continues the turn), compaction carries structural semantics ("replace the covered prefix") that operate on other entries, and the typed ledger is the currency of audit. Generics for the open dimension, types for the closed one.

**Renderers resolve their bytes from the recorded hash.** A renderer is `(names, resolve) -> RenderedContent`, where `resolve(kind, name)` derefs the resident's active hash through the `ContentStore`. Composed bytes are therefore a pure function of (folded state, content store): a refresh yields new bytes, and a backing file mutated on disk changes nothing until a re-record. Each recording carries its drift policy as provenance — `pinned` for skills, which render from their preloaded registry and ignore `resolve`; `evolving` for memory, instructions and environment, whose content moves day to day.

**The append-only red line: suppliers write only on the append side.** Content is recorded into the ledger first, and the composer is a read-only fold over folded state plus the content-addressed store. No external source is called back at compose time. Injectors — memory recall, reminders, wake summaries — run before the append and may be impure (read the clock, the disk, a retrieval service); once their output is recorded, a resume re-folds the ledger and never re-runs them.

**One generic write seam.** A pack's `PackContribution.init` hook runs once per build, including resume, and records its rendered bytes through the kernel-handed `SessionRecorder.record_content`, which stamps the plugin as actor and no-ops when the hash is unchanged — or, for an activate-once record (`refresh=False`), whenever the name is already active at any hash. Mid-loop provenance (a skill activated at turn 40) resolves through the `ContentHashesFn((kind, name) → (version, hash))` seam instead.

**Messages carry an origin.** `Message.origin` is `human` / `system` / `memory`, optional and omitted from canonical bytes when absent. Only the engine's append path writes it — a label stuffed into model or tool output is just text. Vendor label syntax never enters the ledger: each provider adapter maps `origin` onto its own wire form deterministically.

**Memory is four parts, none of them a skill.** Writing and reading are ordinary tools confined to the memory root; the resident index is a content-channel resident (kind `memory`, policy `evolving`) living in the semi-stable segment, where compaction cannot wash it out; auto-recall is a `turn_intake` reminder provider that reads the store at call time and lands its hits as one recorded turn tagged `origin="memory"`. The store is file-based, with no vector retrieval.

**Workspace instructions are another resident.** `NOETA.md` then `AGENTS.md` then `CLAUDE.md` at the workspace root, first non-empty wins, rendered inside a `<workspace-instructions source="…">` block under kind `instructions`, policy `evolving`. Instructions are workspace environment material, not agent identity, so they never enter the agent's activation tuple; a host opts in, and a workspace with no such file produces zero events and zero bytes.

**Oversized tool results are truncated before the append.** When `tool_output_inline_limit` is positive, the inline output is cut to the first N characters and given a deterministic marker (dropped / total / full ref); the truncated form is what enters `MessagesAppended`, so replay reconstructs it by construction. The full bytes stay in `ToolResultRecorded`, and audit loses nothing.

## Rationale

- **A single abstraction can unify the append protocol, never a fetch callback.** Pinning the three parts down separately means the kernel changes by zero for each new kind of content: a new kind is one registry entry, one rendering rule, one recording.
- **Generics for the open dimension, types for the closed one.** Material has unbounded kinds, so it must be generic. Anonymising mechanism would blind the engine to messages, erase compaction's structural semantics, and evaporate the typed ledger's audit value — the form would be generalised while the knowledge stayed exactly as large.
- **The red line is what makes re-derivation byte-equivalent.** A compose-time callback would stop the composer being a pure fold: the same ledger would compose to different bytes twice, breaking the stable-prefix prompt cache and preventing a resumed task from rebuilding its own context. Resolving bytes from the durable, content-addressed store is ledger-side reproduction, not a live fetch.
- **Memory and instructions are not skills because their drift policy is the opposite.** An unversioned skill change is an accident; a memory or instructions change is routine. Collapsing them into one kind would erase that distinction and pollute "skill" into meaning any injected text.
- **Origin's single-writer rule plus adapter-side wire syntax is provider neutrality applied to the message stream.** Nailing one vendor's reminder syntax into the ledger would bind the ledger to that vendor; a neutral marker plus a deterministic per-adapter mapping does not.

## Alternatives considered

1. **Fold messages, compaction and thinking into the generic channel too.** Rejected: the engine loop would go blind to messages and fail to run, and compaction would still have to recognise a summary kind to perform its replacement — a generalised shape carrying the same special knowledge, minus the typed audit trail.
2. **Call an external source back at compose time (pull-style middleware).** Rejected: the composer stops being a pure function, the same ledger composes to different bytes twice, and both prompt cache and resume break.
3. **Splice reminder or recall text straight into the user message string.** Rejected: once spliced it can never be separated again — audit cannot tell human speech from system speech, prompt-injection analysis has nothing to grip, and evaluation cannot recover a clean human turn.
4. **Give reminders their own typed event.** Rejected: a reminder is an entry in the message stream with no structural semantics of its own; after fold it must be merged back into the same message list anyway, so the event type buys a duplicated mechanism.
5. **Register the memory index and the instructions file as dynamically generated skills.** Rejected: cheapest to implement — one registration each — but it costs the per-kind drift distinction and the typed provenance that says where a byte came from.
6. **Splice the instructions file into the system prompt.** Rejected: it loses the file's provenance and puts volatile bytes inside the prompt-cache key.
7. **Abstract an injector interface, or add vector retrieval, before there is a second use case.** Rejected: a single-tenant abstraction is a guess.
8. **Truncate an oversized tool result at compose time, or give truncation its own event type.** Rejected: compose-time truncation breaks the composer's purity for the same reason a fetch does; truncating before the append lets the existing message event carry it with zero protocol expansion.
9. **Attach per-entry source labels to the composed View.** Rejected: they enter no segment hash, no `ContextPlan` body, and no wire format, so they are View metadata with no consumer — the ledger already attributes every recorded byte to the event that recorded it.

## Consequences

- Adding a kind of material touches only the SDK: build a `ContentKindSpec` and contribute it from a plugin's session pack, with a registration priority that fixes its place in the semi-stable layout.
- The kernel side of the channel is `noeta.protocols.events` (the content event), `noeta.protocols.task` (`active_content`), `noeta.core.fold` (the merge and the anchor), `noeta.context.content_channel` (the kind registry), `noeta.context.composer` (segment rendering and the byte-deref), and `noeta.execution.session_pack` (`init` plus `SessionRecorder`).
- The residents themselves are built-in plugins: `noeta.builtins.memory.impl` for the index and recall, `noeta.builtins.workspace.impl` for instructions and environment, `noeta.builtins.skills.impl` for skills.
- The `origin` → wire-format mapping is sealed inside each provider adapter, keeping the ledger neutral.
- Where mid-task activations land is `anchored-content-placement.md`; how compaction cooperates with the semi-stable segment and the tail budget is `context-compaction.md`.
