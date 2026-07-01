# Rebaselining the tool and agent catalog = picking materials, not building structure (including alignment with Claude Code's description form)

## Context

After benchmarking against Claude Code, pi, and opencode, we set a baseline for "which tools and which agents should exist" — and we **only rebaseline the catalog itself, without touching the mechanism that carries it**.

The carrying mechanism still follows existing conventions: the AgentSpec from `agent-identity-and-provenance.md`, the "description is canonical" rule from `tool-description-canonical.md`, and the "adapters live outside the kernel" rule from `provider-neutral.md`.

## Decision

- **This is "picking materials," not "building structure": tools and agents reuse existing protocol fields, with zero additions.** A tool uses the existing `name / description / input_schema / risk_level / invoke(ctx)`; an agent uses `AgentSpec` (`tools` allowlist / `capabilities` / `instructions` / `default_model` / `guards` / `default_budget`), whose structural equality is its identity. We do **not** add `capability` / `provider_hints` / `render_hint` fields to the tool object.

- **Capability isolation = the `AgentSpec.tools` allowlist (physical) + `risk_level` (permission), not prompt.** A read-only role is simply one that drops the write-class tools from its allowlist — they are physically out of reach, rather than a system prompt begging the model "please don't modify files."

- **`delegation / todo_write / ask_user_question / skill_invocation / memory` are `Capabilities` switches, not ordinary tools.** When a switch is on, the builder **injects** the corresponding tool into the tool list sent to the model (the model actually sees it and can actually tool_call `todo_write` / `ask_user_question` / `spawn_subagent` (+`run_workflow`) / `skill` / `memory_*`), aligning at the surface with Claude Code's TodoWrite / AskUserQuestion / Task / Skill / memory tools; the only differences are naming (snake_case vs PascalCase) and internal implementation (the first four are control tools that, once called, translate into a neutral Decision).

- **Provider differences and mutually exclusive tools sink down to the registry layer, invisible to the model.** Cross-provider tool variants (`edit` ↔ `apply_patch`) and system-prompt variants are **filtered by model at registration/assembly time** — never written into a tool field or the prompt (there is no "if you are GPT, use apply_patch").

- **Tool descriptions are split out into standalone text resources; the content aligns with Claude Code's short form (this batch reverses the earlier four-part form).** Descriptions move from Python strings into standalone `.md` resources (clean git diffs, editable by non-engineers); the **content** changes from the earlier symmetric four-part `What/When/When-NOT/Preconditions` prose to Claude Code's current "one-line summary + a few bullets" short form. Constraint: describe noeta's **real semantics**, and do not copy Claude Code capabilities that noeta does not have.

- **A subagent's output = a return-value contract, not a conversation for humans to read.** The system prompt says it plainly: "your final text is data returned to the caller, not a message for a human"; large answers reuse the ContentStore offload from `event-sourced-truth.md`.

- **Tool catalog: 8 real MVP tools + 4 in phase two; the shell trio stays as is.** MVP: `read` (low) / `write` (high) / `edit` (high) / `shell_run` (high, incl. `run_in_background`) / `shell_poll` (low) / `shell_kill` (high) / `grep` (low) / `glob` (low). Phase two: `webfetch`/`websearch` (low) / `lsp` (low) / `apply_patch` (high, enabled only by the registry layer for providers that need it). `shell_run/poll/kill` is the model case for "single-responsibility split + risk grading"; treat it as the template, and don't fold back into an action enum.

- **Agent catalog: reuse the existing presets set of four + add four internal agents.** The main/sub agents keep their existing names `main` / `plan` / `explore` / `general-purpose` (not renamed to build/worker): `plan` is physically isolated (write tools are not in its allowlist), `explore` is read-only search, and `general-purpose`'s output = a return value. Four new internal agents (no tool allowlist; they are effects of the host/runtime layer): `compaction` (new hard constraint: preserve safety/permission directives verbatim), `memory-retrieval` (transport, no reasoning), `permission-judge` (default-allow + two levels HARD/SOFT block + rulings must cite the transcript), and `title` (session title).

- **The Capabilities layer is confirmed to already align with Claude Code's tool surface; this batch does not touch it.** Modeling those five meta-capabilities as Capabilities switches does not mean noeta lacks these tools — flipping a switch on injects the tool.

## Rationale

- **All three benchmark targets have extremely thin tool objects; not one of them puts "capability/provider" on the tool object**: capability isolation lives entirely outside the tool (physically removing tools / read-only vs writable preset groups / agent allowlists / registry-layer filtering), and noeta already has a ready-made counterpart for each (`AgentSpec.tools` / `Capabilities` / `risk_level` / the registry layer). So this batch is material selection: zero new fields, zero new runtime primitives. `risk_level` (whether approval is required) is mandatory for noeta because of event-sourcing, so it is kept.

- **Prompt is the weakest constraint**: doing read-only isolation with a physical allowlist rather than "please don't modify" is the strongest shared consensus among the three benchmark targets.

- **Meta-capabilities go through Capabilities, not ordinary tools**: others build `task`/`todowrite` tools because they lack a Capabilities layer; delegation and orchestration reuse noeta's own workflow, with no need to reinvent them.

- **Provider differences sink to the registry layer**: this is provider neutrality applied at the tool layer — no provider's tool shape is nailed into the kernel contract.

- **Description form aligns with Claude Code's short form (reversing the earlier four-part form)**: after re-extracting and diffing the Claude Code 2.1.178 binary word by word, we found it had already shortened its descriptions (to save tokens); noeta's original four-part form was longer and pointed the opposite way, so after the comparison we follow its tone and structure. Cost: giving up the structured AI-navigation benefit of the four-part form, recorded here as "deliberately following, not inventing our own template."

- **The four internal agents do not go through the Subtask channel**: they have no Policy and are effects of the host/runtime layer (same test as background processes, see `shell-permission-and-background.md`).

## Alternatives considered

1. **Add `capability`/`provider_hints`/`render_hint` fields to the tool object.** Rejected: none of the three benchmark targets do this, and noeta already has cleaner mechanisms to carry it.

2. **Do isolation by persuading with prompt.** Rejected: prompt is the weakest constraint.

3. **Make the five meta-capabilities ordinary tools in the allowlist.** Rejected: others do that only because they lack a Capabilities layer.

4. **Write provider differences into tool fields / into the prompt.** Rejected: it breaks provider neutrality.

5. **Bury descriptions in code strings / keep the earlier self-invented four-part form.** Rejected: you can't iterate on it via git diff and non-engineers can't edit it / it is longer than Claude Code and points the opposite way; we deliberately follow its short form.

6. **Fold `poll`/`kill` back into an action enum on `shell_run`.** Rejected: it hurts description routing and departs from "one tool, one responsibility + risk grading."

7. **Route the four internal agents through the Subtask channel.** Rejected: they have no Policy; they are host/runtime effects.

## Consequences

- Where the tool structure and AgentSpec land: the tool field structure (zero additions) is in `noeta.protocols.tool`, `AgentSpec`/`Capabilities` are in `noeta.agent.spec`, and the existing preset-of-four names are in `noeta.presets`.

- Where tool implementations and descriptions land: `read/write/edit/grep/glob` are in `noeta.tools.fs`, and the standalone short `.md` description resources are in `noeta.tools.descriptions` / `noeta.policies.descriptions`.

- Where assembly and internal agents land: switching tool variants by model and injecting Capabilities tools is in `noeta.execution.builder`, and the title internal agent is in `noeta.execution.title`; compaction's verbatim-preservation constraint is in `context-compaction.md`.

- This decision's boundary is "only rebaseline the catalog, don't touch the mechanism": when adding or removing tools or agents, reuse existing protocol fields and do not add tool-object fields or runtime primitives.

- The short description form is a "deliberately follow Claude Code, give up the structured benefit of the four-part form" trade-off; write new tool descriptions in the short form, and describe only noeta's real semantics.
