# Unified context supply: a generic content channel, message origin, the append-only red line, and memory/instructions as new tenants

## Context

"Getting new content into the context" has long been loosely called a provider, but it was never something a single abstraction could unify. The real mechanism is made of three parts: writing into the event ledger (append), the rendering rule, and the fingerprint guard. Once those three are teased apart, the runtime does not need to change for each new kind of content added.

At the same time, the content in a context naturally falls into two categories: one is "material" (unbounded kinds), the other is "mechanism" (a closed set). Expressing these two categories with different means is the through-line of this decision. The memory and instructions files are the first two external validating use cases of this unified mechanism.

The meaning of "provider" is covered in `docs/adr/provider-neutral.md`; the stable-prefix constraint for prompt-cache friendliness is covered in `CONTEXT.md` (the Stable Prefix entry).

## Decision

### Provider is narrowed to "an adapter for an external service"

The meaning of provider is fixed to: each external service (LLM, storage, vector store, …) implements one adapter for the corresponding internal Protocol. The old entry ("dynamically-queried context source (RAG / memory / external API)") is retired. Future memory / RAG external services still follow the adapter pattern, but "getting content into the context" is **not called a provider**; each has its own mechanism name (skill invocation, memory recall, reminder injection).

### Separate material from mechanism: make the resident content channel a generic runtime mechanism

Context content is split in two. The runtime must stay neutral to **material** (open kinds) and use typed events for **mechanism** (a closed set).

- **material** (skills, memory index, future personas / example libraries, …): `TaskState` gains a generic activation table `active_content: kind → tuple of names` (the `activate_skills` patch is kept as syntactic sugar specific to skills, folded into this generic table; old recordings are unaffected). `SkillContentRecorded` is generalized into a generic content-fingerprint event `ContextContentRecorded` with two extra fields — **kind** and **drift policy** (the policy rides with the recording: `pinned` means "a hash change without a version bump is a hard failure," which skills use; `evolving` means "record the hash but allow it to change," which memory / instructions use — the policy travels with the recording rather than being hard-coded by kind in the runtime). The hash-resolution seam is widened to `ContentHashesFn((kind, name) → (version, hash))`. How each kind of material is loaded, in what format, and which segment it renders into are all replaceable parts in the SDK registry, with zero runtime change.
- **mechanism** (messages, tool results, compaction, thinking re-attach) stays typed events and is **not** folded into the generic channel: the engine loop is driven by their semantics (a user message wakes it, a tool result continues the current turn); compaction carries structural semantics ("replace the first N entries" is an operation on other entries); the typed ledger is the currency of audit. **Use generics for the open dimension, types for the closed dimension.**

### Context entries carry a source label

A view entry carries its own source: an entry from the message-stream channel passes through its origin; an entry from the content channel is labeled `kind:name`. From this, audit can attribute every byte in the context to a source.

### Add an origin field to Message; hand wire-format rendering to the adapter

`Message` gains an optional `origin` (`human` / `system` / `memory`; default = the natural author of that role), recorded with `MessagesAppended`. **single-writer guard**: only the engine's append path can write origin; a fake label stuffed into model / tool output is just text. Vendor label syntax does not enter the ledger (provider-neutral): the anthropic adapter renders origin=system as a `<system-reminder>` placed in the adjacent user turn; openai_compat renders it as a system-role message. Replay safety comes free: it rides on the existing message event — no new event, no new state.

### The append-only red line: suppliers may only write on the append side

All content is **recorded into the event ledger first**; the composer is always a read-only fold over state. **No external source is called back at compose time** (pull-style middleware). Injectors (memory recall, reminders, wake-style summaries) run before append and may be impure (read the clock, read disk, retrieve) — once their output is recorded, resume only re-folds the ledger and never re-runs the injectors. Re-deriving a byte-identical context from the ledger (what both prompt cache and resume rely on) is the moat; this red line is non-negotiable. v1 does not abstract an "injector" interface; the host calls the engine's append directly (rule of two: abstract when the second and third use cases appear).

### Memory v1, four parts, not disguised as a skill

Writing memory = an ordinary tool (writes a file into the memory directory); reading the full text on demand = an ordinary tool (the result goes through the tool-result channel); the resident index = the second tenant of the content channel (kind `memory`, policy `evolving`, living in the semi-stable segment so compaction does not flush it out); auto-recall = a use case of the origin channel (the host retrieves at the user-message append seam, and a hit is recorded with origin=memory). Not disguised as a skill: their drift policies are opposite (an unversioned skill change is an accident; a memory change is routine), and forcing it would lose this per-kind drift distinction. v1 is file-based, with no vector retrieval.

### Project instructions file = the third tenant of the content channel (kind="instructions", evolving)

In the workspace root, search in the order `NOETA.md` → `AGENTS.md`; the first non-empty one wins, rendered as a user message in the semi-stable segment (wrapped in `<workspace-instructions source="...">`), with source label `instructions:<filename>` and policy evolving. **Not part of Capabilities / fingerprint** — the instructions file is workspace environment material (the same nature as the skill directory), not agent identity. The switch is layered: the SDK's `build_session_inputs(instructions_enabled=False)` is an explicit opt-in; the noeta-agent product defaults it on (when the file does not exist: zero events, zero byte change). This is the first external payoff of the generalization promise, with zero runtime-side change.

### Request-level bindings: output_schema / thinking / effort (not in the fingerprint)

`Options.output_schema` (JSON Schema) / `thinking` (adaptive/disabled) / `effort` (low..max) are pure wiring fields, in the same tier as model / provider / cwd, and excluded from the fingerprint. `LLMRequest`'s three new fields declare `__canonical_omit_none__` (like `Message.origin`); None does not enter the canonical bytes — old-recording replay is unaffected. Structured output goes through a binding, not a control-plane tool (the provider's native constraint is equally expressive and does not consume a tool slot); when a schema is present, the Policy's end_turn branch runs `json.loads` on the answer text (deterministic, replay-safe), and on failure keeps the raw text instead of raising. No local schema validation (no jsonschema dependency introduced).

### Oversized tool results are truncated before append (noeta-shaped microcompact)

When `tool_output_inline_limit` (host-level, default None=off) is positive, `wrap_tool_result_block` truncates the inline output to the first N characters and adds a deterministic suffix marker (three fields: dropped/total/full_ref). The truncated form goes **directly into `MessagesAppended`** — append is the fact, and replay reruns the same construction (the tool output is replayed back from `ToolResultRecorded.output_ref` + the same config), so it is byte-equivalent by construction. The full bytes always remain in `ToolResultRecorded` (audit loses nothing). `ToolResult` gains an optional `output_ref` field to hold full_ref.

## Rationale

- **A single unified abstraction can only unify the append protocol, not the fetch callback.** The real mechanism for getting new content into the context has always been three parts — ledger append + rendering rule + fingerprint guard — not "one provider." Nailing this down means the runtime changes zero for each new kind of content added: a new kind = one SDK registry + one rendering rule + one kind registration.
- **Use generics for the open dimension, types for the closed dimension.** Material has unbounded kinds (future AI paradigm shifts all land here), so it must be generic. Mechanism (messages / compaction / thinking) has structural semantics and drives the engine loop; anonymizing them would make the engine blind to messages, lose compaction's "replace the first N entries" semantics, and evaporate the typed ledger's audit currency — the form is generalized but the knowledge is not shrunk.
- **The append-only red line keeps re-derivation byte-equivalent.** Calling back an external source at compose time would make the composer no longer a pure fold over state; the same ledger would compose to different bytes twice, breaking the stable-prefix prompt cache and preventing a resumed task from re-deriving its own context.
- **Memory / instructions are not disguised as skills, because their drift policy is opposite.** An unversioned skill change is an accident; a memory / instructions change is routine. Forcing it collapses this per-kind drift distinction and pollutes the meaning of "skill = a static workflow template."
- **origin's single-writer guard + handing vendor syntax to the adapter is a continuation of provider neutrality.** Nailing the `<system-reminder>` syntax into the ledger would bind one vendor; origin is a neutral marker, and the wire format is a deterministic mapping internal to the adapter.
- **Don't generalize prematurely on an empty basis.** A single-tenant abstraction is a guess; memory arriving as the second tenant is the validating use case, and instructions as the third tenant fulfills the promise (rule of two / three).
- **The request-level bindings and the truncation are both additive, touching neither identity nor the ledger protocol.** output_schema / thinking / effort are the same nature as model (environment, not identity); truncating before append lets `MessagesAppended` carry it naturally, with zero protocol expansion. identity (fingerprint) is unaffected, re-derivation from the ledger stays byte-stable, so these can ship independently.

## Alternatives considered

1. **Full anonymization (fold messages / compaction / thinking into generic content).** Rejected: the engine loop would blind itself to messages and fail to run; even anonymized, compaction still has to recognize `kind=summary` and perform the replacement — the form is generalized but the knowledge isn't shrunk, and the typed audit currency is lost.
2. **Call back the provider at compose time (pull-style middleware).** Rejected: the composer is no longer a pure function; the same ledger composes to different bytes twice, breaking the stable-prefix prompt cache, and a resumed task can no longer re-derive the same context from its own EventLog.
3. **Splice reminder text directly into the user-message string (Claude Code's literal form).** Rejected: once spliced in, it can never be separated again — audit can't tell human speech from system speech, prompt-injection analysis has nothing to grip, and eval can't get a clean human turn.
4. **Give reminders their own typed event.** Rejected: it is essentially an entry in the message stream with no special structural semantics; after fold it still has to be merged back into the same message list — a duplicated mechanism.
5. **Disguise the memory index as a dynamically-generated skill / the instructions file as a skill.** Rejected: the implementation is cheapest (one line of registration), but the cost is losing each channel's typed source label (audit can no longer tell where content came from) + semantic pollution.
6. **Splice instructions into system_prompt.** Rejected: it loses provenance and the source label.
7. **Abstract an injector interface now / do vector memory now.** Rejected: single-use-case abstraction, rule of two.
8. **Make structured output a control-plane tool / validate output_schema locally.** Rejected: the provider's native constraint is equally expressive and does not consume a tool slot; no jsonschema dependency introduced, trusting the provider-side enforcement.
9. **Truncate the tool result at compose time / give truncation its own event type.** Rejected: truncating at compose time makes the same ledger compose to different bytes twice, breaking the composer's purity (and with it prompt cache + resume re-derivation); truncating before append lets `MessagesAppended` carry it naturally, with zero protocol expansion.

## Consequences

- Adding a kind of material only touches the SDK: register a kind in `noeta.context.content_channel` (the replaceable kind table + rendering rule), with the runtime untouched. `noeta.context.composer` handles semi-stable segment rendering, source labels, and content-channel tenants; `noeta.context.memory` is the memory-index tenant, and `noeta.context.instructions` is the third tenant, the instructions file.
- Protocol and fold side: `Message.origin` in `noeta.protocols.messages`; the generic content-fingerprint event `ContextContentRecorded` in `noeta.protocols.events`; the generic activation table `TaskState.active_content` in `noeta.protocols.task`; the merge fold for origin / active_content in `noeta.core.fold`.
- The origin → vendor wire-format mapping is sealed inside each adapter (`noeta.providers.anthropic` / `noeta.providers.openai_compat` / `noeta.providers.openai_responses`), keeping the ledger neutral.
- memory reads and writes are ordinary tools (`noeta.tools.memory`), and the recall injection seam is in `noeta.execution.memory`.
- Request-level bindings are in `noeta.client.options`; the canonical `__canonical_omit_none__` is in `noeta.protocols.canonical`; tool-result truncation is carried by `wrap_tool_result_block` + `tool_output_inline_limit`.
- How compaction cooperates with the semi-stable segment and the tail budget is covered in `docs/adr/context-compaction.md`.
