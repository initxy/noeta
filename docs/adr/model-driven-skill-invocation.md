# Model-driven skill invocation: two-stage on-demand loading (menu in the schema, body in the semi-stable segment)

## Context

To let the model select its own skills, we give it a `skill` control tool whose invocation reuses the existing activate_skills patch channel. The control-tool mechanism itself is covered in `control-tools-neutral-mechanism.md`. This decision settles the two-stage on-demand loading — "menu into the schema, body into the semi-stable segment" — and spells out the folding rules for the capability flag and the trade-offs of the four presets.

## Decision

### Invocation reuses the activate_skills patch; the body lands in the semi-stable segment

We add a `skill` **control tool** (of the same family as todo_write / spawn_subagent: visible to the model, intercepted and translated by the policy layer, and never reaching the ToolRuntime). The model calls it → `_control_translate` validates it → `StatePatchDecision(activate_skills=[name])` → the engine records it → on the next assembly, the renderer places the body into the semi-stable segment. The timing matches Claude Code (its body likewise only becomes visible on the next model request), but **immunity to compaction comes structurally for free**: the semi-stable segment is exempt from compaction, so we don't need Claude Code's trio of registry / re-injection / budget. Activation is recorded state, so fold/resume needs zero new machinery.

### The menu goes into the tool schema

Each callable skill's "name + one-line summary" is rendered into the `skill` tool's schema: the name into the enum, the summary into the description (following the precedent of spawn_subagent's agent_directory). This tool appears **only when the flag is on and the menu is non-empty** — a workspace with no skills never grows the tool, so pure-SDK users never see it. The menu is built in `build_session_inputs` from a single source (the skill registry), taking to heart the lesson from agent-directory ("three different source paths → ContextPlanComposed hash drift"; the source is already in the builder's hands anyway).

### The capability flag folds into the fingerprint conditionally; all four presets enable it; it coexists with pre-loop

`Capabilities.skill_invocation` (default False) controls whether the tool appears. The descriptor fold must be **conditional**: only when True do we write the new key into the descriptor, so that an agent without the flag keeps a byte-identical descriptor and the same agent identity (otherwise every agent's identity — including user-defined ones — would shift, and old recordings could no longer be folded against them). The four presets (main/explore/plan/general-purpose) **all enable it**: a skill is a manual of working methods, and the subagents doing the work need it too; enabling it only for main would create a capability gap where "main can call skills but the agents it dispatches can't." The pre-loop static-activation channel is **kept as-is**: it is a dependency for folding/resuming old recordings and is also the entry point for "user forces a preload" (`--skill`); both channels converge into the same `active_skills` state and the same rendering pipeline, and merging deduplicates them for free.

### Tool shape: named `skill`, single parameter, no deactivate

The only parameter is `skill: string` (enum = the menu). There is **no `args`** — parameterized execution is the job of the command mechanism (`noeta.execution.commands`); a skill loads a manual, and the two aren't mixed. There is **no `reason`** — the motivation for the call is already in the conversation context. Following the todo_write precedent, a `skill` call must be the **only** tool call in that turn (the sole-call rule). The receipt reuses the existing ack (`messages_after`): success returns "loaded"; a name not on the menu goes through a recoverable-error receipt (the model can retry, and the task isn't poisoned); a repeated call gets a uniform idempotent message. v1 **does not expose deactivate** (the `deactivate_skills` patch exists but isn't offered to the model) — a manual is harmless to keep loaded, whereas deactivation introduces the new risk of "the model forgot the rule."

### The engine backfills the content fingerprint for mid-loop calls

The pre-loop path has a helper that emits `SkillContentRecorded` before applying the patch; for mid-loop calls this job moves to the engine: before applying a patch that contains `activate_skills`, an injected **content resolver** (`name -> (version, content_hash)`, built by the builder from the registry — runtime does not import sdk) backfills the same event, reusing the "once per skill per task" deduplication. The pre-loop byte order is unchanged; the mid-loop causal order matches the pre-loop. **No existing event or decision shape changes**, so there is zero byte risk.

## Rationale

- **Reusing the activate_skills patch instead of opening a new Decision type keeps the decision surface neutral and restrained.** The patch channel's semantics already fully cover invocation; provenance is backfilled by an engine-side resolver. Opening a `SkillInvocationDecision` would be redundant kernel expansion.
- **Putting the body in the semi-stable segment rather than a tool result buys compaction immunity for free and makes "which skills are active" recorded state.** A body stuffed into a tool result would be compacted away, forcing Claude Code's registry / re-injection / budget cleanup; even Claude Code itself doesn't put the body in the tool result (the tool result only returns "Launching skill: X," and the body enters the rolling history via the newMessages channel).
- **The menu goes into the schema rather than the message stream because noeta indexes its skill set at startup and it is static within a session.** Claude Code uses the message stream because its skill set changes mid-session (plugin installs / dynamic discovery), and mutating the schema would break the prompt cache — noeta has neither problem, and a schema enum throws in parameter validation for free.
- **Conditional fingerprint folding is an iron law.** Adding a new key unconditionally would shift every agent's fingerprint — including user-defined ones — and falsely flag drift on all old recordings; writing only when the flag is True gives agents without the flag zero fingerprint drift.
- **Enabling it in all four presets rather than only in main eliminates the subagent capability gap.** The cost is merely re-pinning four golden fingerprints (a deliberate one-time identity break), which is smaller than "dispatched agents can't use skills."

## Alternatives considered

1. **An executable tool, with the body stuffed into the rolling history as a tool result (closer to Claude Code's literal form).** Rejected: the body would be compacted away, forcing the registry / re-injection / budget trio; the activation state would be invisible on resume (fold can't recover it); and even Claude Code doesn't put it there.
2. **Injecting the menu through the message stream (Claude Code's form).** Rejected: its motivations (skill set changing mid-session, preserving the prompt cache) don't exist in noeta, whereas a schema enum throws in parameter validation for free.
3. **Enabling the flag only for main.** Rejected: subagents couldn't use skills, and the cost of the capability gap outweighs re-pinning three golden fingerprints.
4. **Opening a new `SkillInvocationDecision` decision type.** Rejected: the decision surface must stay neutral and restrained, and the patch channel's semantics already fully cover it.
5. **A tool with `args` / `reason` parameters.** Rejected: `args` muddles the semantics with the command mechanism; `reason` has no consumer (Claude Code doesn't record it either).

## Consequences

- The landing points are `noeta.policies.control_tools` (the `skill` control-tool schema + sole-call) and `noeta.policies.react` (translating `activate_skills` into a `StatePatchDecision`).
- Menu construction and rendering the body into the semi-stable segment land in `noeta.context.composer`; skill registry indexing and the `SkillContentRecorded` origin label land in `noeta.context.skills.indexer`.
- The conditional fingerprint fold for `Capabilities.skill_invocation` lands in `noeta.agent.spec`.
- The engine-side resolver seam that backfills content for mid-loop calls is built by the builder from the registry (runtime does not import sdk).
- Watch-outs: conditional folding is an iron law; enabling all four presets means one deliberate golden-fingerprint re-pinning; v1 does not expose deactivate.
