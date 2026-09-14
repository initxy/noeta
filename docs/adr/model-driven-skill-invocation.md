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

## Amendment (2026-09-14): the menu has a budget and a keep order

### Context

The per-skill cap (1024 characters per summary) bounded one entry; nothing
bounded the roster. A workspace that indexes a hundred skills — borrowed
ecosystems, plugin packs, a tenant's own — paid for a hundred summaries in the
stable prefix of every turn. Claude Code's listing has a total budget (1 % of
the context window), a per-skill cap, protected entries, and a usage-ranked
degrade to name-only; the question was which of that fits Noeta's constraints
(byte-stable composition, a tenancy-agnostic kernel, the ledger as the source
of truth).

### Decision

- **A total budget in estimated tokens, fitted at session build.** The host
  derives `plugin_config["skills"]["menu_budget_tokens"]` as 1 % of the bound
  model's catalog context window (the skills built-in must not import the
  providers built-in); the pack defaults to 2000 when a host passes nothing;
  an operator override rides `HostConfig.plugin_config`. The estimate is
  CJK-aware — a Han / Kana / Hangul character counts as one token — because
  the kernel's `chars/4` heuristic undercounts a Chinese roster three- to
  four-fold, and Chinese summaries are the common case.
- **Degrade is name-only, never absent.** Over budget, summaries are kept
  greedily in keep order while their increment fits (a later, shorter summary
  may still fit after a longer one was skipped); the rest keep their name in
  the `enum` and roster. Reachability is preserved, activation renders the
  summary with the body, and the tool description says so to the model.
  Under budget the roster bytes are unchanged. Going over logs one warning
  per build naming the knob.
- **Keep order = host rank > merge tier > frontmatter `priority` > name.**
  The rank is the host's `menu_rank` (`skill → score`), per task via
  `HostConfig.skill_menu_rank_resolver` — the same tenancy seam and contract
  as `memory_root_resolver`: cheap, total, deterministic per task id. The
  tier is the merge **scope**, stamped by `load_workspace_skills` (every
  built-in, plugin-contributed and borrowed pack shares the lowest tier; the
  global `.agents` / `.noeta` dirs and the workspace `.agents` dir sit above
  it; the workspace `.noeta` pack on top), so a workspace-local skill keeps
  its summary ahead of a borrowed one and the host's own pack never loses
  to a borrowed ecosystem merely for having been folded first. The menu
  itself stays name-sorted; rank decides only which summaries survive, so an
  under-budget roster is rank-independent.
- **Rank is an input fixed per task.** The roster sits in the tool schema,
  in the stable prefix; re-reading a mutable rank mid-task would rotate the
  prefix. The Engine is built per turn (the engine-per-turn ADR), so the
  host enforces "fixed" in code: the resolver is asked once per task and its
  first non-empty answer is memoised for the task's life in that process (a
  declining resolver is asked again on the next build), because the score
  the SDK itself ships decays with the clock and a per-call `now` would
  otherwise rotate the prefix from one build to the next. Across processes
  the resolver's own determinism is the contract, as for
  `memory_root_resolver`. A static `plugin_config["skills"]["menu_rank"]`
  and a resolver are mutually exclusive (the static override is applied last
  and would silently replace every resolved rank).
- **A skill that appears mid-process is in the roster on every task's next
  turn, with a note.** The roster is read at every per-turn build, so
  install / edit / remove need no refresh verb; the skills built-in records
  a "new skills" `turn_intake` reminder naming what joined the roster since
  the task last saw it (the roster the pack composed rides the task's local
  slot — `noeta.runtime.task_local`, via `plugin_config["skills"]["task_slot"]`
  — so it is process-local: silent on the opening turn and after a restart —
  Claude Code's "New skills discovered" behaviour). A removed skill leaves
  the enum silently.
- **The budget is the honest 1 %.** Claude Code's 1 % is a character proxy
  (`1 % × window × 4` characters); Noeta's is 1 % in estimated tokens.
  Equal for an ASCII roster, four times tighter for a CJK roster — the
  point of the CJK-aware estimate. An operator who wants CC's looser fit
  overrides `menu_budget_tokens`.
- **Usage is folded from the ledger, not counted by the kernel.** Every
  activation is already a durable `TaskStatePatched(activate_skills=…)`; a
  multi-replica server shares the event store. `noeta.sdk` ships the pure
  fold (`skill_usage_from_events`) and Claude Code's decay score
  (`rank_skills_by_usage`: `count × max(0.5^(days/7), 0.1)`); the host picks
  which streams belong to a tenant. Only activations after a task's first
  `ContextPlanComposed` count, so host preloads (`Options.skills`) never rank
  themselves first.

### Alternatives considered

1. **A kernel-side usage counter (Claude Code's `skillUsage` file).**
   Rejected: the kernel is tenancy-agnostic and multi-replica; the ledger
   already carries the signal, so a second write path would only drift.
2. **Dropping over-budget skills from the menu entirely.** Rejected: the
   `enum` is the model's only view of what exists; a name costs a few tokens
   and keeps the skill reachable.
3. **Re-fitting the roster as usage changes within a task.** Rejected: the
   roster is stable-prefix bytes; a mid-task change re-primes the cache and
   makes a resumed task compose different bytes.
4. **Truncating activated bodies at compaction (Claude Code).** Not needed:
   activated content is state-derived and re-hangs after the summary
   (`anchored-content-placement.md`).
5. **Model- or host-driven deactivation.** Declined by the owner
   (2026-09-14); the original "no deactivate" rationale above stands.

### Consequences

- `noeta.builtins.skills.impl.control_tool` owns the estimate
  (`estimate_menu_tokens`), the fit (`fit_menu_to_budget`), the keep order
  (`menu_keep_order`) and the warning; `wiring.py` reads the two config keys
  and fails loudly on a malformed value.
- `SdkHost` derives `menu_budget_tokens` (`skill_menu_budget_tokens`) and
  threads the per-task rank into the pack and the cache scope.
- `noeta.client.skill_usage` is the host-facing fold, exported from
  `noeta.sdk`.
