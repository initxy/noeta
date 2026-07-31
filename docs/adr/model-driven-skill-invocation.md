# Model-driven skill invocation loads two-stage on demand: the menu in the tool schema, the body in the semi-stable segment

## Context

The model selects its own skills through a `skill` control tool. The control-tool mechanism itself — a model-visible action the policy layer intercepts and translates into a neutral Decision, never reaching the ToolRuntime — is covered in `control-tools-neutral-mechanism.md`. This decision settles the two-stage on-demand loading (menu into the schema, body into the semi-stable segment), how the capability folds into agent identity, and which agents carry the tool.

## Decision

### Invocation reuses the activate_skills patch; the body lands in the semi-stable segment

The `skill` control tool is of the same family as `todo_write` and `spawn_subagent`: visible to the model, intercepted and translated by the policy layer, and never invoked by the ToolRuntime. The model calls it, the translate validates it into a `StatePatchDecision(activate_skills=[name])`, the engine records the patch, and on the next assembly the renderer places the skill body into the semi-stable segment. Immunity to compaction is structural: the semi-stable segment is exempt from compaction, so there is no separate registry, re-injection pass, or budget to maintain. Activation is recorded state, so fold and resume need no extra machinery.

### The menu goes into the tool schema

Each callable skill's name and one-line summary render into the `skill` tool's schema — the name into the `enum`, the summary into the description (the same pattern `spawn_subagent`'s agent directory uses). The menu is derived from the single skill registry by the skills plugin's own control-tool factory, so there is one source and the composed schema bytes stay stable.

### The tool appears only when the activation is present and the menu is non-empty

`skill_invocation` is an activation folded into `AgentSpec.plugins`; membership *is* the capability. A workspace with no indexed skills never grows the tool, so pure-SDK users never see it. The identity fold is **conditional** — the activation is written into the spec only when present — so an agent without it keeps a byte-identical spec and the same identity. main, explore, and general-purpose activate it (as does the web subagent), which closes the gap where main could call skills but the agents it dispatches could not. A pre-loop forced-activation channel coexists: a deterministic `/skill-name`-style activation, or a host force-preload, produces the same `activate_skills` recording, so both channels converge into one skill activation map and one rendering pipeline, and the merge deduplicates them.

### Tool shape: named `skill`, single parameter, no deactivate

The only parameter is `skill: string` (enum = the menu). There is no `args` — a skill loads a manual, and parameterized execution is a separate concern. There is no `reason` — the motivation is already in the conversation context. A `skill` call must be the only tool call in the turn (the sole-call rule shared with the other control tools). Success returns a "loaded" ack; a name not on the menu returns a recoverable error listing the available names, so the model can retry without poisoning the task; a repeated activation returns the same success ack and the state merge deduplicates. Deactivate is not offered — the `deactivate_skills` patch exists but is not exposed to the model, because a manual is harmless to keep loaded whereas deactivation introduces the risk of the model forgetting a rule.

### The engine backfills the content fingerprint for mid-loop calls

Before applying a patch that carries `activate_skills`, an injected content resolver (`(kind, name) → (version, hash)`, built from the skill registry) backfills a first-only content-provenance event — the generic `ContextContentRecorded` with kind `skill`, policy `pinned` — reusing the once-per-skill-per-task deduplication. The runtime does not import the SDK; the resolver is handed in.

## Rationale

- **Reusing the activate_skills patch instead of a new Decision type keeps the decision surface neutral and restrained.** The patch channel's semantics already cover invocation, and provenance is backfilled by an engine-side resolver. A dedicated skill-invocation decision would be redundant kernel expansion.
- **Putting the body in the semi-stable segment rather than a tool result buys compaction immunity for free and makes "which skills are active" recorded state.** A body stuffed into a tool result would be compacted away, forcing a registry / re-injection / budget to keep it alive.
- **The menu goes into the schema because the skill set is indexed at startup and static within a session.** A schema enum also throws in parameter validation for free; mutating the schema mid-session would break the prompt cache, but the set does not change mid-session.
- **Conditional identity folding is an iron law.** Writing the activation only when present gives agents without it zero identity drift; folding a new key unconditionally would shift every agent's identity — including user-defined ones — and falsely flag drift on every old recording.
- **Enabling it in the working subagents, not only main, eliminates the subagent capability gap.** A skill is a manual of working methods, and the agents doing the work need it too.

## Alternatives considered

1. **An executable tool with the body stuffed into the rolling history as a tool result.** Rejected: the body would be compacted away, forcing the registry / re-injection / budget trio, and the activation state would be invisible on resume because fold cannot recover it.
2. **Injecting the menu through the message stream.** Rejected: the skill set is static within a session, so a schema enum is both sufficient and gives parameter validation for free; the message stream would be motivated only by a set that changes mid-session, which does not happen here.
3. **Enabling the flag only for main.** Rejected: the subagents doing the work could not use skills, and that gap outweighs re-pinning the golden agent identities once.
4. **Opening a new `SkillInvocationDecision` decision type.** Rejected: the decision surface must stay neutral and restrained, and the patch channel's semantics already cover invocation.
5. **A tool with `args` / `reason` parameters.** Rejected: `args` muddles a manual-load with parameterized execution; `reason` has no consumer.

## Consequences

- The `skill` control-tool schema and its translate into an `activate_skills` patch live in the `skills` built-in (`noeta.builtins.skills.impl.control_tool`); the ReAct policy that runs the control-tool translates lives in the `react` built-in (`noeta.builtins.react.impl.react`).
- Rendering the body into the semi-stable segment is the composer's job (`noeta.context.composer`); skill registry indexing lives in `noeta.builtins.skills.impl.indexer`.
- The conditional identity fold for the `skill_invocation` activation lives in `noeta.agent.spec`.
- The engine-side resolver that backfills content for mid-loop calls is built from the registry and injected, keeping the runtime free of any SDK import.
