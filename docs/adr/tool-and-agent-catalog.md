# The tool and agent catalog is assembled from existing protocol fields: no tool object carries capability or provider knowledge

## Context

A coding agent needs a tool set, a set of agent identities, and a way to keep a
read-only identity read-only. Each of those could be expressed on the tool
object itself — a capability tag, a provider hint, a render hint. The catalog is
also the most churn-prone part of the system: tools and agents come and go far
more often than the mechanism that carries them, so whatever the catalog costs
to change is paid repeatedly.

## Decision

**A tool is a thin object.** It carries `name` / `description` / `input_schema`
/ `risk_level` / `invoke(ctx)` and nothing else. There is no `capability`, no
`provider_hints`, no `render_hint` field.

**Capability isolation is physical, not persuasive.** An identity's reach is the
`AgentSpec.tools` allowlist (which tools exist for it at all) plus `risk_level`,
graded `low` / `medium` / `high`: everything above `low` lands in the
approval-required set, and `PermissionGuard` enforces an identity's ceiling. A
read-only identity is one whose allowlist omits the write family; the tools are
out of reach rather than discouraged.

**Control tools are activations, not allowlist entries.** `todo_write`,
`ask_user_question`, `spawn_subagent`, `skill` and `run_workflow` are gated by
`AgentSpec.plugins` membership (`structured_output` is the data-driven exception,
gated on a per-helper schema being present). An activated control tool mounts its
schema into the model-visible tool list — the model sees it and calls it like any
other tool — and its call translates into a neutral Decision instead of a
ToolRuntime invocation.

**Provider differences resolve during assembly, invisibly to the model.** The fs
built-in owns the mutually exclusive edit pair and the table that drops one of
them per model family (`apply_patch` for Anthropic-family models, `edit` for
OpenAI-family ones); an unrecognized family drops neither. No provider's shape
is written into a tool field or into prompt text.

**Tool descriptions are standalone `.md` resources** shipped beside each tool's
implementation and loaded through the shared resource loader. The form is a
one-line summary plus a few bullets, and it states this system's real semantics
only.

**A subagent's final text is a return value, not a message for a human.** Large
returns spill through the ContentStore (see `event-sourced-truth.md`).

**The catalog.** The base packs are `fs` (`read` / `glob` / `grep` — `low`;
`edit` / `write` / `apply_patch` / `shell_run` / `shell_kill` — `high`;
`shell_poll` — `low`) and `web` (`webfetch` / `web_search`). Capability packs
such as `memory` append their own tools past the allowlist filter. The shell
trio is the template for the rest: one tool per responsibility, each graded on
its own risk, `shell_run` carrying `run_in_background` and `shell_poll` reading
back status.

**The agents.** `main` plus three subagents — `general-purpose` (main's full
tool surface, no delegation: a leaf worker that returns a value),
`explore` (read-mostly, reports facts), `plan` (the same read-mostly surface,
writes no file at all, and opens only `ask_user_question`). The browser
specialist `web` and the memory curator `__consolidation__` exist but sit
outside `main`'s default roster; a product registers them explicitly.

## Rationale

- **The tool object is the wrong place for capability.** Everything a capability
  tag would express is expressible where it can be enforced: the agent allowlist
  removes the tool, `risk_level` gates approval, and the assembly layer filters
  by model. A field would be a second, weaker copy of a decision made elsewhere.
- **A prompt is the weakest constraint available.** "Please do not modify files"
  fails silently the first time a model ignores it; an absent tool cannot be
  called. Isolation therefore lives in the allowlist.
- **`risk_level` is a first-class field.** Approval is decided from the recorded
  tool descriptor, so the risk grade has to be a field rather than a convention
  parsed out of the tool name.
- **Descriptions as files review like code.** They diff cleanly, non-engineers
  can edit them, and their bytes are pinned by goldens — which matters because a
  description folds into the stable-prefix hash (see
  `tool-description-canonical.md`).
- **Keeping the catalog free of new primitives keeps its churn cheap.** Adding a
  tool or an agent touches a manifest and a description file, never a protocol.

## Alternatives considered

1. **`capability` / `provider_hints` / `render_hint` fields on the tool
   object.** Rejected: each duplicates a constraint that the allowlist,
   `risk_level`, or the assembly-time filter enforces, and the copy on the tool
   object is the one that cannot be enforced.
2. **Isolation by prompt instruction.** Rejected: the weakest possible
   constraint, and it fails without a trace.
3. **Making the control tools ordinary allowlist tools.** Rejected: they produce
   no artifact and their result is known to the Policy immediately, so routing
   them through ToolRuntime would demand an artifact-less special case. Gating
   them by activation keeps one identity axis.
4. **Writing provider differences into a tool field or the prompt.** Rejected:
   it welds a vendor's tool shape into the neutral contract (`provider-neutral.md`).
5. **Keeping descriptions as Python string literals.** Rejected: they diff
   badly, cannot be edited outside the source tree, and invite drift from the
   golden-pinned bytes.
6. **Folding `poll` / `kill` into an action enum on `shell_run`.** Rejected: one
   description would then have to route three behaviours, and a single tool
   cannot carry both a `low` and a `high` risk grade.
7. **Registering the browser specialist in `main`'s default roster.** Rejected:
   it would enter `main`'s `spawn_subagent` schema and churn `main`'s stable
   prefix for every deployment, including those with no sandbox at all.

## Consequences

- The tool contract is in `noeta.protocols.tool`; `AgentSpec` (with `tools`,
  `plugins`, `spawnable`, `guards`, `default_budget`, `default_model`) is in
  `noeta.agent.spec`; the four official identities and the two out-of-roster
  ones are in `noeta.presets`.
- Tool implementations and their description resources ship inside their
  built-in package; the provider edit-tool mutex table is fs's own, applied
  mechanically by the kernel builder.
- Adding a tool or an agent reuses these fields. A proposal that needs a new
  tool-object field or a new runtime primitive is a proposal to change the
  mechanism, and belongs in a decision of its own.
