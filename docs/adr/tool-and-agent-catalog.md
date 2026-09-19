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

**Provider differences never reach the catalog.** Every model family receives
the identical fs tool set: one precise edit tool, one write tool, the same
shell trio. The resolved family is still handed to pack assembly
(`SessionBuildContext.provider_family`) so a pack *can* branch on it, but no
built-in pack does, and no provider's shape is written into a tool field or
into prompt text. (Through 0.5.x the fs built-in owned a mutually exclusive
edit pair and a table that dropped one of them per family — `apply_patch` for
Anthropic-family models, `edit` for OpenAI-family ones. The 0.6.0 Claude Code
tool-surface alignment deleted `apply_patch`, because a model trained on that
surface has a strong prior for a single precise edit tool; with one tool left
there was no pair to choose between, so the mutex went with it.)

**Tool descriptions are standalone `.md` resources** shipped beside each tool's
implementation and loaded through the shared resource loader. The form is a
one-line summary plus a few bullets, and it states this system's real semantics
only.

**Where a tool's reach depends on the argument rather than the tool, the fence
is a per-call predicate, not a risk grade.** `Bash` established the shape: the
tool is `high`, but a command in the effective allowlist runs silently and only
an unlisted one routes through approval, decided per call from the arguments.
`WebFetch` is the second case and the sharper one — it is `low` (a read-only
GET), yet its `url` is entirely the model's, so "read a secret, put it in a
URL" needed no human at all. It is fenced the same way:
`HostConfig.webfetch_allowed_hosts` — operator configuration, trusted like
`shell_allowlist` — names the hosts a fetch reaches without asking a human, and
under a gating permission mode every other host asks, per call. The gate is by
**host, not by address**: an agent that holds `Bash` reaches any address with
one `curl`, so refusing addresses in `WebFetch` would protect nothing. A
deployment that needs a real egress boundary enforces it at the network or in
the sandbox container, where it also covers the shell.

The result always opens with a line naming it as external web content: a digest
answer reads like prose the system wrote, and the model has to know whose words
it is reading before it reads them.

**A subagent's final text is a return value, not a message for a human.** Large
returns spill through the ContentStore (see `event-sourced-truth.md`).

**The catalog.** The base packs are `fs` (`Read` / `Glob` / `Grep` — `low`;
`Edit` / `Write` / `Bash` / `KillShell` — `high`; `BashOutput` — `low`) and
`web` (`WebFetch` / `WebSearch` — both `low`). Capability packs such as
`memory` append their own tools past the allowlist filter. The shell trio is
the template for the rest: one tool per responsibility, each graded on its own
risk, `Bash` carrying `run_in_background` and `BashOutput` reading back status.

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
- **A grade cannot express "this call, with these arguments".** `risk_level` is
  a property of the tool and folds into the recorded descriptor; the reach of a
  `Bash` command or a `WebFetch` URL is a property of the call. Keeping the
  per-call judgment in a predicate leaves the recorded descriptor — and the
  stable-prefix bytes — untouched while deployment policy varies underneath it.
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
6. **Folding `BashOutput` / `KillShell` into an action enum on `Bash`.** Rejected: one
   description would then have to route three behaviours, and a single tool
   cannot carry both a `low` and a `high` risk grade.
7. **Registering the browser specialist in `main`'s default roster.** Rejected:
   it would enter `main`'s `spawn_subagent` schema and churn `main`'s stable
   prefix for every deployment, including those with no sandbox at all.
8. **Raising `WebFetch` to a gated `risk_level`.** Rejected: it prompts on every
   fetch with no way to open one, which trains an operator to click through,
   and it rewrites a recorded tool descriptor for a reason that is about
   deployment, not about the tool.
9. **Refusing loopback / link-local / private targets in `WebFetch`.**
   Rejected: the agent also holds `Bash`, which reaches the same targets with
   one `curl`, so the refusal protects nothing while adding address
   classification, a resolver seam and a documented rebinding window. Hosts
   that need an egress boundary enforce it at the network or the sandbox.

## Consequences

- The tool contract is in `noeta.protocols.tool`; `AgentSpec` (with `tools`,
  `plugins`, `spawnable`, `guards`, `default_budget`, `default_model`) is in
  `noeta.agent.spec`; the four official identities and the two out-of-roster
  ones are in `noeta.presets`.
- Tool implementations and their description resources ship inside their
  built-in package; the kernel builder assembles the packs mechanically and
  applies no per-provider filter of its own.
- `PermissionPolicy` holds one per-call `conditional_approval` slot, and the
  SDK host composes the gates that ride it (`Bash`'s allowlist, `WebFetch`'s
  host list) before handing it over. Each gate answers only for its own tool, so
  the composition is "any of them asks". The host vocabulary itself — URL-host
  normalisation and the allowlist forms — lives once in
  `noeta.client.webfetch_policy`, beside the host rather than in the `web`
  built-in because `noeta.client` cannot statically import `noeta.builtins`;
  the built-in imports down into it for the one thing it settles on its own, a
  scheme it does not fetch.
- **Upgrade impact.** With an empty `webfetch_allowed_hosts`, every fetch under
  `default` / `acceptEdits` now asks a human. No address becomes unreachable.
- Adding a tool or an agent reuses these fields. A proposal that needs a new
  tool-object field or a new runtime primitive is a proposal to change the
  mechanism, and belongs in a decision of its own.
