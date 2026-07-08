# Noeta

A runtime for long-horizon, task-oriented agents. It hosts, records, schedules, and replays agent execution, without prescribing how an agent is written.

## Distribution boundary: three layers — pure engine / thin client / product

Physically, distribution is **two libraries plus one application**, split along the **outward wheel boundary plus public surface** (the earlier mechanism-vs-material criterion has been retired). The model is **in-process**, like LangChain or the Claude Agent SDK: runtime and sdk are pure libraries with no HTTP.

- **noeta-runtime** — the **pure engine**: everything needed to run one agent in-process. `protocols` (the only typed boundary) + `core` (Engine / fold / snapshot) + kernel services (`runtime`'s Worker/Dispatcher/ToolRuntime/RuntimeLLMClient/compaction, `storage`, `guards`, `observers`, `read_models`), **plus every opinionated implementation**: `policies` (ReActPolicy), `tools` (builtin tool implementations: fs/shell/mcp/research), `providers` (anthropic/openai_compat = Noeta-shape adapters), `context` (ThreeSegmentComposer + ContentChannelRegistry + skills), the `execution` machine (driver/runner/resolver/builder/multi_turn/subtask_drain + the command mechanism), the `agent` identity layer (AgentSpec/registry), and the four official agents in `noeta.presets` (main/explore/plan/general-purpose). runtime keeps its internal 8-layer import topology intact (kernel ↛ adapter). It ships no HTTP/SSE server.
- **noeta-sdk** — the **thin client** on top of runtime, and the only thing users import. It exposes the library public surface `client` (query / Client / Options / `compile_options` / messages / parts), the authoring API (`@tool`, `create_sdk_mcp_server`), the re-exported open extension interfaces (Tool / LLMProvider / Policy / Guard / Observer / ContentChannel `ContentKindSpec`, plus the advanced `View` / `Decision`; along with `AgentDefinition` / `SystemPromptPreset` / `as_messages`), and the four re-exported presets. It **contains no engine and no HTTP**; it merely forwards into runtime (an in-process, legitimate dependency).
- **noeta-agent** (the application shell, `apps/noeta-agent`) — the **official coding-agent product**: a Python backend process that consumes noeta-sdk in-process + a frontend (`apps/web`) + a startup entrypoint, and it **owns the HTTP/SSE server** (the only layer with a network surface). Product code lives under the `noeta.agent.*` namespace (PEP 420: the `noeta.agent` identity layer's spec/registry are published by noeta-runtime, while the product's host/api/backend are published by noeta-agent).

Repository shape: `packages/{noeta-runtime,noeta-sdk}` (two libraries) + `apps/{web,noeta-agent}` (the application) + a repo-root `tests/` (the old shell package `packages/noeta` has been deleted). Distribution mapping: `noeta-sdk` ↔ claude-agent-sdk, `noeta-agent` ↔ claude-code; the dist names `noeta-runtime`/`noeta-sdk`/`noeta-agent` are unchanged.

**The only public surface is `noeta.sdk`.** Users install noeta-sdk and import only `noeta.sdk`; noeta-runtime is a transitive dependency they never touch. import-linter enforces the in-repo half of this: application code may not import runtime internals directly (`noeta.core` / `noeta.protocols` / `noeta.runtime` / `noeta.policies` / `noeta.tools` [the whole package — the `@tool`/`create_sdk_mcp_server` authoring API lives in `noeta.sdk`, not `noeta.tools`] / `noeta.providers` / `noeta.context` / `noeta.execution` / the `noeta.agent` identity layer) — it may only import `noeta.sdk`. **Deliberate exemptions** the application may import directly: `noeta.storage` (so the host can wire a concrete backend — wiring only, never a second writer) and `noeta.read_models` (the peripheral file-tree/preview projection). The external-user half ("import only `noeta.sdk`") is guaranteed by wheel packaging (runtime ships as a transitive wheel), since import-linter cannot reach external code. noeta-sdk itself may import runtime (legitimate in-process). Re-layering moves the **distribution, not the import path** (PEP 420 keeps every `noeta.<module>` path stable while the physical wheel moves), so the provider-neutral forbidden contract reruns as-is.

**Locked vs. open.** The **open** extension surfaces are all `Options` fields, re-exported through `noeta.sdk`: Tool / LLMProvider / Policy / Guard / Observer / ContentChannel (register a `ContentKindSpec`). Storage backends (EventLog/ContentStore/Dispatcher) — and the host's other non-identity runtime injections (preview gateway, live-MCP resolver) — are configured through **host config** (`noeta.sdk.HostConfig`, passed as `Client(..., host_config=...)`), not through Options; `HostConfig` never enters `AgentSpec` identity. **Locked**: the Engine main loop and Dispatcher/Worker/Lease (host config can only tune concurrency/lease), and `ContextComposer` — replacing the composer wholesale is **not** open on the user surface (stable-prefix KV-cache reproducibility is a hard constraint; the internal composer is still Protocol-injected, the Engine imports only the protocol, and the builder wires `ThreeSegmentComposer`); the composer's only open hook is registering a `ContentKindSpec`.

**There is no operator CLI.** `run/inspect/resume` are the library core of the runtime's capabilities (inspect / state reconstruction go through `noeta.core.fold`; drain/resume go through `noeta.runtime.worker`), with no argparse wrapper and no `noeta` console script. The only entrypoint is the `python -m noeta.agent` runner, which starts the **application's own backend** (its HTTP/SSE server, not a library server) → wires up the agent/component registry → serves `apps/web`. The frontend-backend wire protocol was fully redesigned: a single SSE envelope-mux stream (carrying canonical `EventEnvelope`s as-is, addressed by `taskId`, with the envelope `seq` doubling as the SSE id so `Last-Event-ID` can resume, and the stream itself as the single source of truth; the same stream additionally carries ephemeral named `delta` frames — token-streaming previews with no SSE id, never persisted and never replayed, the final `MessagesAppended` staying the only durable record) + a set of command endpoints aligned with the Client verbs (start / send_goal / approve / deny / answer / cancel / close / reopen, idempotent 202/ack); peripheral resources (`/content/{hash}`, `/files`+`/file`, `/preview/<token>/`, `/mcp/*`) live on their own endpoints.

Installation splits by authoring vs. product:
- **To write your own agent**: `uv pip install noeta-sdk`, then `import noeta.sdk` (noeta-runtime comes along as an untouchable transitive dependency).
- **To run the official product**: `uv pip install -e apps/noeta-agent`.

On PyPI the project is published under the dist names `noeta-runtime` / `noeta-sdk` / `noeta-agent` (all live at the current release); the bare `noeta` name is held by an unrelated package, so the three-wheel split doubles as the naming workaround.

## Vocabulary

### Core abstractions

**Task**:
One execution instance of an agent; it can spawn sub-tasks and can suspend and resume. The only first-class citizen in the system.
_Avoid_: Run, Job, Execution, Workflow Instance

**Subtask**:
A task spawned from a parent task via `spawn_subtask`. Structurally identical to a parent task, related only through `parent_task_id`.
_Avoid_: Child Run, Sub-agent, Worker (avoid Workflow Node too, even when unambiguous)

**Agent**:
A named, spawnable configuration (policy + tools + context spec + budget). **Not a runtime entity** — just the "class" of a task.
Every Agent carries a `description` (a one-line summary) used to render the schema of the subagent dispatch control tool (an enum plus each agent's summary), so the model knows who to hand work to.
_Avoid_: Bot, Assistant, AI

**Options**:
The declarative agent configuration (public surface `noeta.sdk.Options`; internal `noeta.client.options.Options`), compiled by `compile_options` into an `AgentSpec` and registered in the registry; **the sole way to express both the official agent set (`noeta.presets`) and custom agents** (its surface aligns with the Claude Agent SDK parameter table). Core fields:

| Field | Shape | Notes |
|---|---|---|
| `system_prompt` | `str \| SystemPromptPreset(preset="main", append=...)` | A string, or the preset form "official main preset + appended section" |
| `name` | `str` | Agent name (advanced field) |
| `agents` | `dict[str, AgentDefinition]` | A **flat dict**, not nested; `AgentDefinition` fields: `description` (required), `prompt`, `tools`, `model`, `capabilities`. The description is rendered into the spawn_subagent dispatch tool schema |
| `allowed_tools` | `list[str \| Tool]` | A **replacement** tool allowlist: setting it means *only* these tools (not additions to the default set); **omitting it = the full builtin tool set (`BUILTIN_TOOL_CLASSES`, 11 tools: read/glob/grep/edit/write/apply_patch/shell_run/shell_poll/shell_kill/webfetch/web_search)**. The memory/control tools are not in this set; they mount conditionally on `Capabilities` flags |
| `disallowed_tools` | `list[str]` | A subtractive tool denylist (removed from the full set or from allowed_tools) |
| `permission_mode` | `default \| acceptEdits \| bypassPermissions` | Three approval modes, mapped to the existing guard config (`plan` mode has been removed) |
| `can_use_tool` | `Callable[[str, dict], bool] \| None` | A programmatic approval callback (args: tool_name + arguments, True = allow); its ruling **is recorded as an ordinary approval event** (resolver="can_use_tool") |
| `max_turns` | `int \| None` | Upper bound on ReAct loop iterations, compiled into `BudgetSpec.max_iterations` |
| `cwd` | `str \| Path \| None` | Working directory; a **wiring** field, not part of behavior-affecting agent identity (in the same column as `provider`) |
| `provider` | `LLMProvider \| None` | Noeta-specific: the provider adapter (the basis of provider neutrality) |
| `skills` | `list[str]` | Noeta-specific: declaratively activated skills |
| `budget` / `capabilities` | Advanced fields | Noeta-specific: budget spec, capability set. `Capabilities.skill_invocation: bool` controls whether the model can see the `skill` selection control tool, and is part of behavior-affecting agent identity |

**Explicitly removed**: the recursively nested `tools=`/`subagents=` fields (deprecated, replaced by the flat `agents` dict + top-level `allowed_tools`/`disallowed_tools`).
_Avoid_: Config, Settings, AgentConfig

**Step**:
The slice by which a task advances within one Engine main-loop pass: `compose_view → decide → dispatch`.
_Avoid_: Iteration, Turn, Cycle

**Attempt**:
One decide→act iteration within a Step. Its first durable record is `ContextPlanComposed` (the implicit attempt-start record), and it is the unit of crash recovery: a `StepAttemptAbandoned` marker seals an interrupted attempt as folded-over dead history.
_Avoid_: Iteration, Retry

**Decision**:
The return value of Policy.decide, and the input to Engine dispatch. A set of **neutral mechanism variants** (open-ended in number): 7 canonical ones — `tool_calls / spawn_subtask / yield_for_human / wait_timer / wait_external / finish / fail`; plus `spawn_subtasks` (fan out N sub-agents in one turn, an N-way join) and `state_patch` (a durable state write that continues the loop: emit one caller-constructed message + an optional `TaskStatePatch`, then keep looping; the Engine does not understand the payload at all). Product control tools do not get their own kernel variant: `todo_write` / `skill` are expressed by the runtime as `state_patch` (the `plan_mode` control tool has been removed), and `ask_user_question` is expressed by the runtime as `yield_for_human` (the kernel retains only neutral HITL auditing).
_Avoid_: Action, Command, Intent

**Policy**:
The function that "decides the next step given the current View." It can be a pure LLM (ReActPolicy), a pure FSM, or a hybrid.
_Avoid_: Pattern, Strategy, Brain

**Tool**:
An external action the agent can invoke. The structured-contract trio `name` / `input_schema` / `description` is **deliberately hand-written and LLM-facing** (not taken from the docstring — the docstring is developer documentation and would leak internal code names); `description` is the **single source of truth** for the model-visible tool semantics, rendered by the ContextComposer into the provider tool schema and then serialized by each adapter, and **never repeated in the system_prompt** (the prompt holds only role and cross-tool workflow policy). It also carries metadata such as `risk_level`. Tool is an **open** extension surface (an `Options` field); the `@tool` authoring decorator is the only tools component shipped with `noeta.sdk`, while the builtin tool implementations live in noeta-runtime.
_Avoid_: Function, Action, Skill (note that Skill is a separate, independent concept)

**Provider**:
A Noeta-shape adapter for an external service: each kind of service (LLM / storage / vector store, etc.) implements the corresponding internal Protocol (such as `LLMProvider`), and `noeta.providers` is the adapter layer (now runtime-internal — users cannot import it directly). The extension surface differs by service kind: `LLMProvider` is open via `Options.provider` and re-exported through `noeta.sdk`; storage backends are configured through **host config**, not through Options. **Not a context content source** — content enters context only via "event recording + assembly rendering"; the old meaning of a "dynamic-query context source" has been retired.
_Avoid_: Vendor, Backend, Connector

**Skill**:
A local, static LLM-workflow template at `.noeta/skills/<name>/SKILL.md`, optionally with resource files (reference docs / scripts). Three-layer merge (builtin < global `~/.noeta/skills` < workspace). Two-stage on-demand loading: the **menu** (name + one-line summary) is rendered into the model-visible `skill` control tool schema; once the model selects one, its body is rendered into the semi-stable segment (that segment is exempt from compaction, so the body survives naturally). `state_patch.activate_skills` is the recording channel; both the pre-loop forced preload (`--skill` / the `activate_skills` helper) and the model's selection feed into the same activation state and run through the same render pipeline, with state merge deduplicating automatically. It is now **absorbed as a content-channel tenant of `kind="skill"`**: activation recording emits a generic `ContextContentRecorded` (with drift policy `pinned`), `activate_skills` is kept as skill-specific syntactic sugar, and fold mirrors it into the generic `active_content`. **Its accompanying resources use the third tier of progressive disclosure**: the renderer reads no files and injects no content, only prepending a line with the **absolute base directory** before the body (`Base directory for this skill: <source_path.parent>`, rendered as-is with no resolution — deterministic); the model combines that line with the relative links in the body into absolute paths and reads them on demand via the **generic `read` tool** — the internal field `skill_roots` of `ReadFileTool` (not in the input_schema) widens the read-side fence to each skill root, while the containment check still works on realpath to prevent symlink escapes. The dedicated tool `read_skill_resource` (the old 0047 design) has been retired; activation no longer eagerly loads the accompanying files into context. **Not the same thing as a Tool.**
_Avoid_: Plugin, Module, Macro

### State and events

**EventLog**:
An append-only event stream, one stream per task. **The source of truth for causality and decisions.**
_Avoid_: Journal, Log, Audit Trail

**Event** / **EventEnvelope**:
One record in the EventLog. The envelope holds `seq / type / actor / trace_id / causation_id`; the payload is a typed dataclass.
_Avoid_: Message, Record

**ContentStore**:
Content-addressed, immutable large-object storage. **The source of truth for large objects.**
_Avoid_: BLOB Store, Asset Store, Object Store (ambiguous)

**ContentRef**:
A reference into the ContentStore: `hash + size + media_type`.
_Avoid_: URL, Path, Pointer

**Artifact**:
A large object produced by a Tool or Provider, referenced via a ContentRef.
_Avoid_: File, Attachment, Blob

**Snapshot**:
A special event in the EventLog whose body goes into the ContentStore. Written before each suspend; an acceleration point for fold.
_Avoid_: Checkpoint, State Dump

**Task State** (state slices):
Four typed slices, **each with exactly one writer**:
- `RuntimeState` — messages / usage (writer: Engine)
- `TaskState` — goal / phase / todos / decisions / active_content (writer: the Policy's state_patch; `active_content` is the exception, merged by fold from activation events such as `ContextContentRecorded`)
- `ContextState` — current plan ref (writer: Engine fold, from the `ContextPlanComposed` event)
- `GovernanceState` — cost / denied (writer: Engine, folded from events)

**TaskState** (narrow sense):
Of the four slices above, the one that holds "long-horizon task memory" maintained by the Policy. The core difference between a long-horizon agent and a short-task agent.
_Avoid_: Memory (too broad), Context (collides with ContextState)

### Execution model

**Engine**:
Advances a single Task by one step. ≤ 500 lines. Knows nothing of worker / dispatcher / workflow.
_Avoid_: Runtime (too broad; and don't confuse the Engine class with the `noeta-runtime` wheel — the latter is the pure-engine library, while the whole system is the app), Executor. The main loop is **locked**: it is not an extension point, and host config can only tune concurrency/lease.

**Worker**:
The process that leases a Task from the Dispatcher and calls the Engine to advance it. **One lease runs until the next suspend or terminal state, then releases.**
_Avoid_: Runner, Daemon

**Lease**:
A Worker's short-term exclusive hold on a Task, with `lease_id / expires_at`.
_Avoid_: Lock, Claim

**Dispatcher**:
The scheduling component; manages Task enqueue, Lease granting, Wake-event delivery, and Stale reclamation.
_Avoid_: Scheduler, Queue Manager

**Suspended**:
One of a Task's 4 states, waiting on a wake event. A **unified expression** of waiting on subtask / approval / timer / external event.
_Avoid_: Yielded, Paused, Blocked, Waiting

**WakeCondition** / **WakeEvent**:
Describes what a Task is waiting on. `SubtaskCompleted / HumanResponseReceived / TimerFired / ExternalEvent`.

**ExecEnv**:
The pluggable **execution backend** the fs/shell tools act through — a deep seam between the tools and their real IO (file read/write/create/unlink/mkdir/stat/glob + `run_argv`), operating on already-resolved absolute paths (the tool still owns containment via `WorkspaceRoot`). `LocalExecEnv` (default) is the host filesystem + subprocess, byte-identical to pre-seam behavior; `AioSandboxExecEnv` routes every side effect to an AIO Sandbox **container** over HTTP, so an untrusted agent's tools land in the container, not on the host. Injected as a per-tool construction field at wiring time — **never** part of a tool's schema, so the stable prefix is byte-identical whichever backend is bound. The v2 per-session evolution widened the seam's reach to **Tier 2** — beyond fs/shell, the skill indexer, `run_skill_script`, the workspace loaders (instructions / environment / shell-allowlist), and web fetch/search egress all route through the session's ExecEnv in sandbox mode (memory + MCP stay on the host). A session's container is welded durably (`TaskHostBound.exec_env_ref` = `"{base_url}#{sandbox_id}"`) so a resumed/reclaimed session reconnects to the same container; the API key rides only on the wire, never in the log. An optional `HostConfig.sandbox_exec_preamble` hook — the process twin of `SandboxAuth.connect_headers` — lets a product prepend a per-session shell preamble minted fresh each exec (for credentials that expire mid-session); `None` keeps the command byte-identical. See the execution-environment-seam ADR.
_Avoid_: Sandbox (that's one *backend* of this seam, not the seam — and "Workspace" is already the session path model + the `WorkspaceRoot` fence; don't overload it), Executor (that's the Engine's sense).

**SandboxProvider**:
The seam that **provisions and reaps** a per-session sandbox container — the "who runs `docker` / a K8s API" layer, distinct from `ExecEnv` (which *talks to* an already-running container). Defined in the SDK (`noeta.client`), implemented in the agent product (`LocalDockerSandboxProvider` — the Local family, one Docker container per root-task tree; a Distributed / TAE / K8s family is the reconnect-across-machines future). `allocate(session_root_id, spec)` builds a fresh container and returns a `SandboxHandle` (addressing + a live `SandboxAuth` strategy that is never serialized); `release` tears it down at the root-task terminal; `attach` reconnects to a recorded ref on resume/reclaim. The SDK's `SandboxExecEnvManager` drives the provider and turns handles into live `ExecEnv` backends. Provisioning + lifecycle belong to the **agent** layer, the mechanism (`ExecEnv`) to the **runtime**, the binding (durable `exec_env_ref`, reconnect) to the **SDK** — config carries addressing, never a secret. See the execution-environment-seam ADR (v2).
_Avoid_: calling the provider a "sandbox manager" (the manager is the SDK-side lifecycle over the provider) or conflating `allocate` with `ExecEnv` construction.

### Context

**View**:
The LLM input the ContextComposer assembles for the Policy. **Not equal to the Task** — it is a projection of the Task.
_Avoid_: Prompt (View is the structured form of a Prompt), Frame

**ContextComposer**:
The component that assembles a Task into a View. **The main path calls no LLM.** The concrete `ThreeSegmentComposer` lives in noeta-runtime and is a **closed** extension point on the user surface: replacing the composer wholesale is **not** open (a hard constraint: stable-prefix KV-cache reproducibility); internally it is still Protocol-injected (the Engine imports only the `ContextComposer` Protocol, the builder wires `ThreeSegmentComposer`, and `noeta.core` retains only the protocols-only `PassthroughComposer` fallback). The only open hook is registering a `ContentKindSpec` (see Content Channel).
_Avoid_: PromptBuilder, ContextAssembler

**ContextPlan**:
The View metadata for a given LLM call (which blocks were selected, what was compacted, what was dropped). Used for audit and debug.
_Avoid_: Prompt Trace

**Stable Prefix / Semi-stable / Dynamic Suffix**:
The fixed segment names in the View's three-part assembly. The cache-friendliness of the `Stable Prefix` is an **independent, protocol-level hard constraint** (unrelated to any verify/replay tooling, and still in force): perturbing the stable prefix between steps blows up the provider KV cache and sends cost soaring, so the stable prefix must serialize reproducibly across steps (sorted tool-schema keys, no timestamp in the persona, a fixed TaskState field order).
_Avoid_: Header / Body / Footer

**Content Channel**:
The generic mechanism by which resident content (the "semi-stable segment tenants" such as skills and the memory index) enters context, made of two load-bearing parts: **event recording** (`ContextContentRecorded`: kind / name / version / content_hash / policy; fold merges `name` into the generic `active_content[kind]`, gated on `content_hash` being non-empty) + **assembly rendering** (the runtime's `ContentChannelRegistry` renders each kind into the semi-stable segment, one `ContentKindSpec` per material kind (kind + renderer + hashes + policy); registering a `ContentKindSpec` is the open extension hook, exposed via `noeta.sdk`, while the registry and renderer themselves stay in noeta-runtime). `content_hash` / `policy` (`pinned` / `evolving`) hang on the recording as descriptive provenance; the drift-comparison consumers of the verify era were retired along with verify/replay, so they are still recorded but no longer enforced. Adding a kind = register a `ContentKindSpec` through the open ContentChannel extension surface (re-exported via `noeta.sdk`); the registry/renderer code in noeta-runtime needs no change. Current tenants: `skill` (pinned), `memory` (evolving). The red line: **providers may only record on the write side** — calling back to an external source at compose time is forbidden.
_Avoid_: Provider (that's the external-service adapter, above), ContentSource, Middleware

**origin**:
An optional author marker on a `Message`, one of `human / system / memory`, defaulting to `None` = the role's natural author (omitted on serialization, so old recordings drift zero). **Single-writer guard**: only the engine's recording path may write it; a marker forged in model/tool output is just text. The vendor-tag syntax does not enter the ledger: the Anthropic adapter wraps host injections (user messages with origin=system/memory) in `<system-reminder>` and merges them into the adjacent user turn; openai_compat renders them as system-role messages.
_Avoid_: Author, Sender, Role (role is a different dimension — don't conflate them)

**Memory**:
Cross-task long-term memory v1, a four-piece set that **does not impersonate a skill** (their drift policies are opposite): **write** = the ordinary tool `memory_write` (a file-based `MemoryStore`, one markdown per memory); **read the full text on demand** = the ordinary tool `memory_read`; **resident index** = the second tenant of the content channel (`memory_content_kind`, kind=`memory`, policy `evolving`, living in the semi-stable segment so compaction does not flush it); **auto-recall** = the host retrieves at the user-message recording seam (`append_user_message_with_recall`), recording hits with `origin="memory"`. Controlled by the `Capabilities.memory` flag (part of behavior-affecting agent identity); among the official presets only main enables it — only the top-level conversational agent receives user messages.
_Avoid_: using "Memory" to mean TaskState (that is in-task state; this is cross-task material)

### Governance

**Principal**:
The initiator of, or party responsible for, a Task; holds identity / capabilities / allowed_side_effects / delegation chain.
_Avoid_: User (a User is a kind of Principal), Actor (Actor means the event trigger, not the Principal)

**Contract**:
A Task's input, expected-output schema, rejection conditions, and side-effect declaration. Frozen into the TaskCreated event.
_Avoid_: Spec, Schema

**Budget**:
A Task's resource ceilings (iterations / cost_usd / wall_seconds / tool_calls).
_Avoid_: Quota, Limit

**Guard**:
A synchronous hook that runs at three points — `before_tool_call / before_spawn_subtask / before_finish` — returning `allow / deny / require_approval`.
_Avoid_: Middleware, Interceptor, Filter

**Observer**:
An asynchronous hook subscribed to the EventLog; its failure does not affect the Task.
_Avoid_: Listener (a synonym, but Observer is more precise), Subscriber

**Mutator**:
**Deprecated in Noeta v2.** Hooks may not modify ctx / payload. To modify, change the Policy or the Composer instead.

### Operations

**Inspect**:
Reads the EventLog + ContentStore and presents history to a human. No external IO.
_Avoid_: View Log, Dump

**Resume**:
Continues actual execution from a suspended state. An operational emergency-stop lever; the normal path is triggered by a wake event.
_Avoid_: Restart, Continue

## Relationships

- **Task → Subtask**: one-to-many; a subtask has its own EventLog stream, related through `parent_task_id`.
- **Agent → Task**: class to instance; one Agent can be instantiated by many Tasks.
- **EventLog ↔ ContentStore**: paired; the EventLog holds decisions and refs, the ContentStore holds large-object bodies.
- **Engine ↔ Worker**: one-to-many; the same Engine code is reused by many Worker processes.
- **Policy ↔ Tool**: the Policy **declares** a call via `Decision.tool_calls` and the Engine **executes** it; the Policy never calls the Tool directly.
- **Content Channel ↔ Skill / Memory**: mechanism to tenant; a skill moves in as `kind="skill"` (pinned), the memory index as `kind="memory"` (evolving), and adding a tenant only requires registering a `ContentKindSpec`.

## Flagged ambiguities

**"Workflow"**:
Not a first-class concept. Express fixed procedures with a deterministic Policy + spawn_subtask. **Do not** let `WorkflowSpec / WorkflowRunner / WorkflowPolicy` appear in documentation or code. An **orchestration script** the model improvises ("spawn a few assistants first, look at the results, then spawn the next batch") is likewise not a new primitive: it lands as **one Task + a Policy that interprets that script**, and the assistants it spawns are real Subtasks. At most, "workflow" is colloquial shorthand for some model-facing control tool; the class-name ban above still holds.

**"Session"**:
Not a first-class concept. Multi-turn conversation is simply one Task receiving user input repeatedly: **one interactive session = one Task**; each question in a session = one **turn** (a cycle of one wake → several Steps → suspend, with the Task resting at `suspended` + `HumanResponseReceived` between turns); each delegation in a session = one **Subtask**.
Exception: Session is allowed as the **runner name for L3 orchestration** (`AgentSessionRunner`). It **must not** serve as a persistent entity, a wire ID independent of `taskId`, or an event schema name. **Do not** let `SessionStore / ConversationManager` appear in documentation or code, and **do not** let `sessionId` or a hand-rolled `SessionEvent` appear in the wire protocol (the event payload is always the canonical `EventEnvelope`).

**"Run"**:
Not a first-class concept. Always use Task. When it appears in external docs or old code, treat it as a Task.

## Sample dialogue

> User: This task has been waiting on a subtask for a long time — can I cancel it?
>
> Answer: Yes. The task is currently **suspended**, with wake_on = `SubtaskCompleted(t-child-7)`. The web app's cancel (`POST /tasks/{id}/cancel`) cascades to cancel all in-flight subtasks.
>
> User: How did its earlier ContextPlan pick the files?
>
> Answer: Inspect the most recent `ContextPlanComposed` event (in the web timeline, or read the EventLog directly); its selected / dropped entries carry provenance, so you can trace back to the content source (which Skill, which message, and so on).
