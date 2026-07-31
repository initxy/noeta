# Noeta

A runtime for long-horizon, task-oriented agents. It hosts, records, schedules, and replays agent execution, without prescribing how an agent is written.

## Distribution boundary: two layers — pure engine / thin client

Physically, distribution is **two libraries**, split along the **outward wheel boundary plus public surface**. The model is **in-process**, like LangChain or the Claude Agent SDK: runtime and sdk are pure libraries with no HTTP.

- **noeta-runtime** — the **pure kernel** (microkernel, 2026-07-29): everything needed to *host* one agent in-process, with **no capability implementation of its own**. `protocols` (the only typed boundary) + `core` (Engine / fold / snapshot) + kernel services (`runtime`'s Worker/Dispatcher/ToolRuntime/RuntimeLLMClient/compaction, plus the kernel vocabulary sinks `runtime.{workspace,shell_policy,exec_env,governance,browser,app_preview,mcp}`, `storage`, `observers`, `read_models`) + the materials band (`context` — the locked ThreeSegmentComposer + the generic content-kind / reminder registries; the residents' kind vocabulary, snapshot types and renderer prose all live in the `memory`/`workspace` built-ins since the kernel final form — the kernel names no kind); `policies` — the **control band**, now the **neutral translate mechanism only** (`control_semantics`: the control-tool spec/mount types + `ControlTranslateContext` + the `translate_control_tool` dispatcher) + a few reserved recorded-wire constants (`SPAWN_SUBAGENT_TOOL` / `RUN_WORKFLOW_TOOL` / `WORKFLOW_AGENT_NAME`, `concurrent_fanout_enabled`) + the `stub` test doubles — the control-tool schemas / descriptions / translate bodies and the workflow-script validation sandbox all moved into their built-ins (the sixteenth `control_tool` surface, S2/S4), and ReActPolicy lives in the `react` built-in since phase 2b; `tools` — **authoring machinery only**: the `@tool` decorator, FakeTool — each built-in tool's `.md` description ships beside its impl in its built-in plugin since phase 2c; the whole skill subsystem — kit, activation helpers, material — lives in the `skills` built-in since the kernel final form) + the `execution` machine (an **injection-only** session builder: packs / control tools / guards / reminders / provider facts all arrive as injections that fail loudly when absent) + the `agent` identity layer (AgentSpec/registry). It is transport-free (no httpx) and ships no HTTP/SSE server. Installed alone it runs an agent only with host-injected `Tool` / `LLMProvider` / hook objects — the protocols are its contract (`tests/test_install_smoke.py` pins this closure).
- **noeta-sdk** — the **thin client** on top of runtime, and the only thing users import. It exposes the library public surface `client` (query / Client / Options / `compile_options` / messages / parts), the authoring API (`@tool`, `create_sdk_mcp_server`), the re-exported open extension interfaces (Tool / LLMProvider / Policy / Guard / Observer / ContentChannel `ContentKindSpec`, plus the advanced `View` / `Decision`; along with `AgentDefinition` / `SystemPromptPreset` / `as_messages`), the four official agents in `noeta.presets` (main/explore/plan/general-purpose, plus the sandbox-gated `web` and the internal `__consolidation__` curator), and — since the microkernel migration — **`noeta.builtins`, the built-in plugin catalogue that carries every official capability implementation** (`noeta/builtins/<name>/impl/`: the fs/web tool packs, the provider adapters + model catalog, the guards + HookObserver, the reminder renderers, memory, browser, app, mcp, the skill subsystem, the ReAct policy + workflow orchestration, the AIO sandbox backends). It **contains no engine**; it forwards into runtime (an in-process, legitimate dependency) and owns the `httpx` dependency its HTTP-speaking impls need. `noeta.presets` and `noeta.builtins` ship in **this** wheel, not runtime's (`tests/test_install_smoke.py` pins every distribution's imports to its own dependency closure — the runtime wheel is verified impl-free).

The `noeta.agent` **identity layer** (`AgentSpec` / `AgentRegistry`) is published by noeta-runtime and stays a runtime-internal module (an agent is a "class" of task, not a network surface); it is not to be confused with any server/host built on top of these libraries.

Repository shape: `packages/{noeta-runtime,noeta-sdk}` (two libraries) + a runnable `examples/` tree (including a reference host assembled from the public surface) + a repo-root `tests/`. Distribution mapping: `noeta-sdk` ↔ claude-agent-sdk; the dist names `noeta-runtime` / `noeta-sdk` are unchanged.

**The only public surface is `noeta.sdk`.** Users install noeta-sdk and import only `noeta.sdk`; noeta-runtime is a transitive dependency they never touch. The internals a user never imports directly are `noeta.core` / `noeta.protocols` / `noeta.runtime` / `noeta.policies` / `noeta.tools` (the whole package — the `@tool` / `create_sdk_mcp_server` authoring API lives in `noeta.sdk`, not `noeta.tools`) / `noeta.context` / `noeta.execution` / the `noeta.agent` identity layer / **`noeta.builtins`** (the capability impls — reached only through the plugin loader; the supported adapter import path is `noeta.sdk.providers`). A host that embeds these libraries reaches everything it needs through `noeta.sdk`, with two wiring-only escape hatches also on the public surface: `noeta.storage` (wire a concrete backend — wiring only, never a second writer) and `noeta.read_models` (the peripheral file-tree/preview projection). The concrete AIO sandbox adapters (`noeta.builtins.sandbox.impl.{exec_env,browser}`) are kept **off** the `noeta.sdk` public surface, which exposes only the `ExecEnv` / `BrowserBackend` protocols + the `BackendFactory` / `BrowserBackendFactory` / `BoundPreamble` factory types (see the execution-environment-seam ADR). The "import only `noeta.sdk`" guarantee is enforced by wheel packaging (runtime ships as a transitive wheel). noeta-sdk itself may import runtime (legitimate in-process). Internally, import-linter still enforces the topology — most importantly the **universal microkernel contract**: *nothing* statically imports `noeta.builtins` (every band is a source; the loader's dynamic `ref` resolution is the only doorway), which is also what carries the provider-neutral kernel↛adapter rule now that every adapter lives in a built-in plugin — and re-layering moves the **distribution, not the import path** (PEP 420 keeps every `noeta.<module>` path stable while the physical wheel moves), so the contracts rerun as-is.

**Locked vs. open.** The **open** extension surfaces are described by a **surface registry** (see Surface / SurfaceSpec) — sixteen standard surfaces across three planes: **identity** (Tool / agent / `ContentKindSpec` / `prompt_fragment` / Policy / `control_tool`), **wiring** (Guard / Observer / LLMProvider / `reminder_provider` / `reminder` / `tool_result_transform` / `session_pack`), and **host** (`mcp_server` / skills / `sandbox_provider`). Each can be hand-wired through an `Options` field (or `HostConfig`) exactly as before, or populated by a **Plugin** (the packaging + discovery unit) and selected per agent by **activation**. **Noeta's own capabilities ride the same path**: the bare-`Options()` defaults (the 11 fs/web tools, the default guards, the three compose-time reminders, the provider facts) are obtained by resolving the built-in plugin manifests at client build — the SDK's `noeta.client.parts` accessors are the sanctioned doorway, there is no static default table — and `DEFAULT_PLUGINS = ("fs", "web")` keeps a bare `Options()` byte-identical. The kernel builder is injection-only and fails loudly when a required part is missing. Storage backends (EventLog/ContentStore/Dispatcher) — and the host's other non-identity runtime injections — are configured through **host config** (`noeta.sdk.HostConfig`, passed as `Client(..., host_config=...)`), not through Options and **never a plugin surface**; `HostConfig` never enters `AgentSpec` identity. **Locked**: the Engine main loop and Dispatcher/Worker/Lease (host config can only tune concurrency/lease), and `ContextComposer` — replacing the composer wholesale is **not** open on the user surface (stable-prefix KV-cache reproducibility is a hard constraint; the internal composer is still Protocol-injected, the Engine imports only the protocol, and the builder wires `ThreeSegmentComposer`); the composer's open hooks are **registry-only and append-only** — registering a `ContentKindSpec` (a resident, reminder track C) or a compose-time `reminder` (track B), both of which touch only the volatile dynamic suffix / semi-stable segment, never the stable prefix.

**There is no operator CLI.** `run / inspect / resume` are the library core of the runtime's capabilities (inspect / state reconstruction go through `noeta.core.fold`; drain / resume go through `noeta.runtime.worker`), with no argparse wrapper and no `noeta` console script. The resident drain loop ships as the library primitive `noeta.runtime.worker.WorkerLoop`; nothing launches it for you — an embedding host constructs and runs it. HTTP/SSE, a UI, and any session model are **not** part of these libraries: a host that wants them builds them on top of `noeta.sdk` (`examples/reference-host` is the smallest such host — durable storage, token streaming, and plugins wired from the public surface alone).

Installation: **`uv pip install noeta-sdk`**, then `import noeta.sdk` (noeta-runtime comes along as an untouchable transitive dependency). On PyPI the project is published under the dist names `noeta-runtime` / `noeta-sdk`; the bare `noeta` name is held by an unrelated package, so the two-wheel split doubles as the naming workaround.

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
| `agents` | `dict[str, AgentDefinition]` | A **flat dict**, not nested; `AgentDefinition` fields: `description` (required), `prompt`, `tools`, `model`, `plugins` (its own activation list). The description is rendered into the spawn_subagent dispatch tool schema |
| `allowed_tools` | `list[str \| Tool]` | A **replacement** tool allowlist: setting it means *only* these tools (not additions to the default set); **omitting it = the full built-in tool set (loader-resolved from the `fs` / `web` built-in plugin manifests via `parts.builtin_tool_classes()`, 11 tools: read/glob/grep/edit/write/apply_patch/shell_run/shell_poll/shell_kill/webfetch/web_search)**. The memory/control tools are not in this set; they mount conditionally on **activation** (the built-in feature-bundle names an agent lists in `plugins`) |
| `disallowed_tools` | `list[str]` | A subtractive tool denylist (removed from the full set or from allowed_tools) |
| `permission_mode` | `default \| acceptEdits \| bypassPermissions` | Three approval modes, mapped to the existing guard config (`plan` mode has been removed) |
| `can_use_tool` | `Callable[[str, dict], bool] \| None` | A programmatic approval callback (args: tool_name + arguments, True = allow); its ruling **is recorded as an ordinary approval event** (resolver="can_use_tool") |
| `max_turns` | `int \| None` | Upper bound on ReAct loop iterations, compiled into `BudgetSpec.max_iterations` |
| `cwd` | `str \| Path \| None` | Working directory; a **wiring** field, not part of behavior-affecting agent identity (in the same column as `provider`) |
| `provider` | `LLMProvider \| None` | Noeta-specific: the provider adapter (the basis of provider neutrality) |
| `skills` | `list[str]` | Noeta-specific: declaratively activated skills |
| `plugins` | `tuple[str, ...]` | **Activation** (see the term): the loaded plugins this agent uses. Recognized activation names fold into behavior-affecting identity: `memory` / `browser` / `mcp` and the three control-tool plugins `todo_write` / `ask_user_question` / `delegation` are **real built-in plugins** (the last three contribute on the `control_tool` surface), while `skill_invocation` stays a recognized **non-plugin** activation (it gates the `skill` control tool inside the `skills` built-in); third-party plugin names pull in that plugin's identity-plane contributions. Defaults to `DEFAULT_PLUGINS` (`fs` / `web` — identity-inert, so a bare `Options()` is byte-identical). Whether the model sees the `skill` selection control tool is `skill_invocation` activation |
| `budget` | Advanced field | Noeta-specific: budget spec. (The old `capabilities` authoring field is **removed** — feature gating is `plugins` activation; `Capabilities` was deleted wholesale, so identity is now `AgentSpec.plugins` + `spawnable` directly) |

**Explicitly removed**: the recursively nested `tools=`/`subagents=` fields (deprecated, replaced by the flat `agents` dict + top-level `allowed_tools`/`disallowed_tools`).
_Avoid_: Config, Settings, AgentConfig

**Step**:
The slice by which a task advances within one Engine main-loop pass: `compose_view → decide → dispatch`.
_Avoid_: Iteration, Turn, Cycle

**Attempt**:
One decide→act iteration within a Step. Its first durable record is `ContextPlanComposed` (the implicit attempt-start record), and it is the unit of crash recovery: a `StepAttemptAbandoned` marker seals an interrupted attempt as folded-over dead history.
_Avoid_: Iteration, Retry

**Decision**:
The return value of Policy.decide, and the input to Engine dispatch. A set of **neutral mechanism variants** (open-ended in number): 7 canonical ones — `tool_calls / spawn_subtask / yield_for_human / wait_timer / wait_external / finish / fail`; plus `spawn_subtasks` (fan out N sub-agents in one turn, an N-way join) and `state_patch` (a durable state write that continues the loop: emit one caller-constructed message + an optional `TaskStatePatch`, then keep looping; the Engine does not understand the payload at all). Product control tools do not get their own kernel variant: `todo_write` / `skill` are expressed as `state_patch` by the contributing control-tool built-ins' translate (the `plan_mode` control tool has been removed), and `ask_user_question` is expressed as `yield_for_human` (the kernel retains only neutral HITL auditing); since the `control_tool` surface these translate bodies live in their built-in plugins, not the runtime.
_Avoid_: Action, Command, Intent

**Policy**:
The function that "decides the next step given the current View." It can be a pure LLM (ReActPolicy), a pure FSM, or a hybrid.
_Avoid_: Pattern, Strategy, Brain

**Tool**:
An external action the agent can invoke. The structured-contract trio `name` / `input_schema` / `description` is **deliberately hand-written and LLM-facing** (not taken from the docstring — the docstring is developer documentation and would leak internal code names); `description` is the **single source of truth** for the model-visible tool semantics, rendered by the ContextComposer into the provider tool schema and then serialized by each adapter, and **never repeated in the system_prompt** (the prompt holds only role and cross-tool workflow policy). It also carries metadata such as `risk_level`. Tool is an **open** extension surface (an `Options` field); the `@tool` authoring decorator is the only tools component shipped on the `noeta.sdk` surface, while the built-in tool implementations live in their built-in plugins (`noeta.builtins.<name>.impl`, the noeta-sdk wheel) and are resolved through the plugin loader at client build.
_Avoid_: Function, Action, Skill (note that Skill is a separate, independent concept)

**Provider**:
A Noeta-shape adapter for an external service: each kind of service (LLM / storage / vector store, etc.) implements the corresponding internal Protocol (such as `LLMProvider`), and the `providers` built-in plugin (`noeta.builtins.providers.impl` — adapters + codecs + model catalog) is the adapter layer; the supported import path is `noeta.sdk.providers` (a lazy re-export). The extension surface differs by service kind: `LLMProvider` is open via `Options.provider` and re-exported through `noeta.sdk`; storage backends are configured through **host config**, not through Options. **Not a context content source** — content enters context only via "event recording + assembly rendering"; the old meaning of a "dynamic-query context source" has been retired.
_Avoid_: Vendor, Backend, Connector

**Skill**:
A local, static LLM-workflow template at `.noeta/skills/<name>/SKILL.md`, optionally with resource files (reference docs / scripts). Three-layer merge (builtin < global `~/.noeta/skills` < workspace). Two-stage on-demand loading: the **menu** (name + one-line summary) is rendered into the model-visible `skill` control tool schema; once the model selects one, its body is rendered into the semi-stable segment (that segment is exempt from compaction, so the body survives naturally). `state_patch.activate_skills` is the recording channel; both the pre-loop forced preload (`--skill` / the `activate_skills` helper) and the model's selection feed into the same activation state and run through the same render pipeline, with state merge deduplicating automatically. It is now **absorbed as a content-channel tenant of `kind="skill"`**: activation recording emits a generic `ContextContentRecorded` (with drift policy `pinned`), `activate_skills` is kept as skill-specific syntactic sugar, and fold mirrors it into the generic `active_content`. **Its accompanying resources use the third tier of progressive disclosure**: the renderer reads no files and injects no content, only prepending a line with the **absolute base directory** before the body (`Base directory for this skill: <source_path.parent>`, rendered as-is with no resolution — deterministic); the model combines that line with the relative links in the body into absolute paths and reads them on demand via the **generic `read` tool** — reads are unfenced, so the absolute path just works — the `skill_roots` allow-list this once needed was deleted when the special case dissolved into the general rule (see Write fence). The dedicated tool `read_skill_resource` (the old 0047 design) has been retired; activation no longer eagerly loads the accompanying files into context. **Not the same thing as a Tool.**
_Avoid_: Plugin, Module, Macro

**Plugin**:
A **manifest-declared contribution package** — a pip package (or a single local `.py` file) carrying a **static manifest** (`[tool.noeta]` in `pyproject.toml`, mirrored to `noeta-plugin.toml` package data; or a `PluginBuilder` with decorator sugar) that declares a name, a `requires-noeta` range, and a list of contributions to the **Surfaces**. The manifest is inert data read **without importing plugin code** (a contribution's `ref` is a string resolved only at the client-build boundary), so a plugin's contributions are listable and collision-checkable before any of it runs. Contributions merge deterministically (`(plugin, name)` order); a collision names both sides and there is no override. noeta itself is its first plugin author (see Built-in plugin). See the plugin-contribution-bundles ADR.
_Avoid_: Extension (that is the seam vocabulary), MCP connector (configuration, not code), Bundle (the 0.4.0 `PluginAPI` factory form — retired).

**PluginSet**:
The **loaded, host-level** set of Plugins — which plugin code is in the process, returned by `load_plugin_set(...)` (the public name; the internal function is `load_plugins`). **Listable and auditable without executing plugin code** (`.contributions()` / `.merged()` read only the static manifests; `.resolve()` is the single import boundary, called at the client build, never on a turn). Passed as `Client(options, plugins=<PluginSet>)`.
_Avoid_: Registry (that is the Surface registry), plugin list (imprecise about the no-execution guarantee).

**Activation**:
The **per-agent selection** of which loaded plugins an agent uses — `Options.plugins` and `AgentDefinition.plugins` (tuples of names). Enters `AgentSpec` identity. A name is either a recognised **built-in feature-bundle** name (`memory` / `browser` / `skill_invocation` / `mcp` / `todo_write` / `ask_user_question` / `delegation`) or the name of a plugin in the loaded PluginSet; an unknown name **fails compilation loudly**. Activation *is* identity: every recognised name folds into the single `AgentSpec.plugins` tuple (the retired `Capabilities` flag set is gone), and gating reads that tuple through `agent_activates(agent, plugin)` — membership is the flag. `delegation` is the one activation that overlaps a *structural* capability: it is normally derived from the `agents` dict and activating it only turns it on (the way a flat child agent is granted the right to spawn); `spawnable` stays derived and is not activatable. `DEFAULT_PLUGINS = ("fs", "web")` is the identity-inert default that keeps a bare `Options()` byte-identical. Effect scope differs by surface (see Surface / SurfaceSpec): identity surfaces follow activation; **guards / observers do not** — once loaded they are in force process-wide (governance is operator authority).
_Avoid_: Capability set (retired as the user-facing vocabulary), enablement (that is the `enabled` load-time allow-list, a different gate).

**Surface / SurfaceSpec**:
A **Surface** is one named extension point (`tool`, `agent`, `content_kind`, `prompt_fragment`, `policy`, `guard`, `observer`, `provider`, `reminder_provider`, `reminder`, `tool_result_transform`, `mcp_server`, `skills`, `sandbox_provider`, `session_pack`, `control_tool` — sixteen standard). A **SurfaceSpec** fully describes one: its `plane` (identity / wiring / host), `activation_scope` (per-agent / process / host-wired), `validator`, `collision_key`, `merge_rule`, and `ordering`. The loader is **surface-agnostic** — it consults one `SurfaceRegistry` (name → SurfaceSpec), so adding a surface (including a host's own app-plane surface, registered on a `copy()` before load) is registering one SurfaceSpec, not editing the loader.
_Avoid_: Hook / seam (Surface is the plugin-facing registry entry; a seam is the code-level substitution point), Slot.

**Built-in plugin**:
One of noeta's own capabilities expressed as a Plugin, living in the top-of-stack `noeta.builtins/` band beside `noeta.presets` — **one directory per built-in, holding the manifest AND the implementation** (`noeta/builtins/<name>/__init__.py` is the zero-execution `MANIFEST`; `noeta/builtins/<name>/impl/` is the code, with manifest `ref`s pointing at the sibling impl modules), so adding a first-party capability is adding a directory there. It rides the identical loader / validation / merge path as any external plugin, and the band is reached only by **dynamic import** (no static edge — `.importlinter`'s universal `sdk-core-not-builtins` contract: every band is a source). Importing `noeta.builtins` (the manifest layer) imports zero impl modules. Current set (17): `fs`, `web`, `memory`, `browser`, `app`, `mcp`, `skills`, `react`, `reminders`, `governance`, `providers`, `sandbox`, `presets`, `workspace` (the environment/instructions resident material — phase 2c), `todo_write`, `ask_user_question`, `delegation` (the last three are the control-tool built-ins — the `control_tool` surface, S4). A built-in whose capability is host-wired rather than per-agent-contributed cannot express its own removal through a manifest, so `disabled_builtins` is **recorded on the `PluginSet`** and the host reads it (`SdkHost.skills_enabled`); absence from a set is never a disable (`builtins=False` scopes the loaded set, not the SDK's capabilities). `react` is the one built-in that **refuses** to be disabled: it supplies the default decision policy, whose identity every compiled `AgentSpec` pins as `POLICY_REF ("react", "1")` — the default brain is *replaceable* through the `policy` surface, not removable.
_Avoid_: Core plugin, internal plugin (use "built-in plugin").

**App Plugin**:
A contribution on a host's own **host-plane** Surface (routers / channels / schedules / commands), registered into the surface registry by the host before load; validated and collision-checked by the same pipeline but handed to the host, **never part of `AgentSpec` identity**. Defined by each host in its own repository.

**Session pack**:
The **session-construction half** of a capability — a plugin's `session_pack` contribution (Surface, plane wiring, per-agent, collision key `name`, ordering `priority`). A factory `(SessionBuildContext) -> PackContribution` the kernel builder (`noeta.execution.session_pack` / `execution.builder`) runs in **one** priority-ordered loop; the builder enumerates no capability by name (microkernel phase 3). Built-in bands reproduce the pre-migration byte order — `fs`=100, `web`=200, `memory`=300, `instructions`=400, `environment`=500, `skills`=600, `browser`=700, `app`=1000 — with the two kernel-owned injections `mcp`=800 / `custom`=900 riding the same loop as fixed-priority entries; the bands are **locked by byte-equality goldens** (tool dict insertion order feeds the stable-prefix hash). A pack **self-gates** on its `SessionBuildContext` (backend absent, flag off, no config) and returns the empty `PackContribution` when inapplicable — never a kernel `if`.
_Avoid_: tool factory / builder stage (the retired per-feature `*_tools_factory` + `_stage_*` seams), Capability (retired — identity is the `AgentSpec.plugins` activation tuple).

**SessionBuildContext**:
The generic **frozen** context every session pack reads — the kernel-built containment `WorkspaceRoot`, `workspace_dir`, content store, exec env, model / provider family, the **backend bag**, `capability_flags`, `plugin_config`, and the shared write/shell safety inputs. Built by the kernel before the pack loop (so a pack can never perturb a later pack's inputs); a pack self-gates on it, the kernel never gates for a pack. Carries only generic slots — no feature-named field.
_Avoid_: BuildSpec (that is the builder's frozen operator inputs), ToolContext (a per-call runtime object, not a construction one).

**PackContribution**:
What a session pack hands back — `tools` (a `name → Tool` mapping merged in loop order, **later-wins**, preserving the custom-shadows contract) + `content_kinds` (each a `ContentKindContribution` with its **own** registration priority, because the semi-stable layout order differs from the tool order) + `exports` (named side-state consumed by existing kernel seams, keyed by the closed `EXPORT_*` vocabulary; a key is admitted only when a seam consumes it). All fields optional; the empty contribution is the universal "not applicable" answer.
_Avoid_: BuildResult, tool bundle (imprecise about the content-kind / export halves).

**Backend bag**:
The host-populated `backends` mapping on `SessionBuildContext` — live backing objects keyed by the **contributing plugins' own names** (`"browser"` from the sandbox provider, `"app_preview"` from the product gateway), never the kernel's vocabulary. An absent name means the capability has no live backing, so the pack returns the empty contribution. Replaced the typed `browser_backend` / `app_gateway` builder parameters when the kernel shed its capability-seam Protocols (microkernel phase 3).
_Avoid_: registry (that is the Surface registry), backend registry (imprecise — it is a plain per-session mapping, not a merge-checked catalogue).

**Control tool mount**:
The **control-tool-construction half** of a capability — a plugin's `control_tool` contribution (Surface, plane identity, per-agent, collision key `name`, ordering `priority`). A factory `(ControlToolBuildContext) -> ControlToolMount | None` the kernel builder (`noeta.execution.control_tool` / `execution.builder`) runs in the builder's **post-tools phase** in one priority-ordered loop, replacing the old hardcoded schema if-chain (the sixteenth surface). A `ControlToolMount` carries `name`, `schema` (the materialized provider-facing dict), `translate` (a closure over its **own** build inputs — so the runtime `ControlTranslateContext` stays feature-free), and **two** priorities: `schema_priority` (render order — spawn=100, todo=200, ask=300, skill=400, workflow=500, structured_output=600) and `routing_priority` (translate dispatch order — ask=100, todo=200, spawn=300, skill=400, workflow=500), both **locked by byte goldens**. A mount **self-gates** on its `ControlToolBuildContext` (returns `None` when inapplicable) — the kernel never gates for it; the context carries the **same generic `capability_flags` bag session packs read** (`ctx.flag("<activation name>")` — the five feature-named builder kwargs were folded away, 2026-07-30 addendum), plus the packs' `exports` (the skills mount derives its menu from its own `EXPORT_SKILLS_KIT` there). `structured_output` mounts `translate=None` (schema-only; react's `StructuredOutputPolicy` intercepts). A mount's `exports` (closed vocabulary, admitted only when a kernel seam consumes it; sole tenant `CONTROL_EXPORT_ASK_ANSWER_CODEC`, the ask answer codec) reach the kernel the same way `PackContribution.exports` do. See the control-tool-contributions-and-activation-identity ADR.
_Avoid_: control-tool spec (the internal mechanism type, not the plugin-facing contribution), `ControlToggles` (the retired per-tool enable bundle), Capability (retired — identity is the `AgentSpec.plugins` activation tuple).

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
- `TaskState` — goal / phase / todos / decisions / active_content (writer: the Policy's state_patch; `active_content` is the exception, merged by fold from activation events such as `ContextContentRecorded` — shape `kind → {name → content_hash}`, hash last-write-wins, see Content Channel)
- `ContextState` — current plan ref (writer: Engine fold, from the `ContextPlanComposed` event)
- `GovernanceState` — cost / denied (writer: Engine, folded from events)

**TaskState** (narrow sense):
Of the four slices above, the one that holds "long-horizon task memory" maintained by the Policy. The core difference between a long-horizon agent and a short-task agent.
_Avoid_: Memory (too broad), Context (collides with ContextState)

### Execution model

**Engine**:
Advances a single Task by one step — where a "step" is a turn boundary: `run_one_step` loops internally over `tool_calls` decisions and returns at the next suspend or terminal. Knows nothing of worker / dispatcher / workflow. (An earlier "≤ 500 lines" budget is retired: `core/engine.py` is ~1.5k lines. The constraint that still binds is the dependency one above, not a line count.)
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
The pluggable **execution backend** the fs/shell tools act through — a deep seam between the tools and their real IO (file read/write/create/unlink/mkdir/stat/glob + `run_argv`), operating on already-resolved absolute paths (the tool still owns containment via `WorkspaceRoot`). `LocalExecEnv` (default) is the host filesystem + subprocess, byte-identical to pre-seam behavior; `AioSandboxExecEnv` routes every side effect to an AIO Sandbox **container** over HTTP, so an untrusted agent's tools land in the container, not on the host. Injected as a per-tool construction field at wiring time — **never** part of a tool's schema, so the stable prefix is byte-identical whichever backend is bound. The v2 per-session evolution widened the seam's reach to **Tier 2** — beyond fs/shell, the skill indexer, `run_skill_script`, the workspace loaders (instructions / environment / shell-allowlist), and web fetch/search egress all route through the session's ExecEnv in sandbox mode (memory + MCP stay on the host). A session's container is welded durably (`TaskHostBound.exec_env_ref` = `"{base_url}#{sandbox_id}"`) so a resumed/reclaimed session reconnects to the same container; the API key rides only on the wire, never in the log. An optional `HostConfig.sandbox_exec_preamble` hook — the process twin of `SandboxAuth.connect_headers` — lets a product prepend a per-session shell preamble minted fresh each exec (for credentials that expire mid-session); `None` keeps the command byte-identical. The per-session backends themselves are injectable through `HostConfig.sandbox_backend_factory` / `sandbox_browser_factory` (typed `BackendFactory` / `BrowserBackendFactory`, exported via `noeta.sdk`); `None` keeps the SDK's hand-written AIO defaults, and a host may inject its own `agent-sandbox`-SDK-backed adapters. See the execution-environment-seam ADR.
_Avoid_: Sandbox (that's one *backend* of this seam, not the seam — and "Workspace" is already the session path model + the `WorkspaceRoot` fence; don't overload it), Executor (that's the Engine's sense).

**SandboxProvider**:
The seam that **provisions and reaps** a per-session sandbox container — the "who runs `docker` / a K8s API" layer, distinct from `ExecEnv` (which *talks to* an already-running container). Defined in the SDK (`noeta.client`); a host implements it (e.g. a Local family with one Docker container per root-task tree; a Distributed / TAE / K8s family is the reconnect-across-machines future). `allocate(session_root_id, spec)` builds a fresh container and returns a `SandboxHandle` (addressing + a live `SandboxAuth` strategy that is never serialized); `release` tears it down at the root-task terminal; `attach` reconnects to a recorded ref on resume/reclaim. The SDK's `SandboxExecEnvManager` drives the provider and turns handles into live `ExecEnv` backends. Provisioning + lifecycle belong to the **host**, the mechanism (`ExecEnv`) to the **runtime**, the binding (durable `exec_env_ref`, reconnect) to the **SDK** — config carries addressing, never a secret. See the execution-environment-seam ADR (v2).
_Avoid_: calling the provider a "sandbox manager" (the manager is the SDK-side lifecycle over the provider) or conflating `allocate` with `ExecEnv` construction.

**Browser tool pack**:
The **noeta-owned** browser tools (`browser_navigate` / `browser_click` / `browser_type` / `browser_extract` / `browser_screenshot`) a sandbox session's agent drives the container's headless browser with. Like the fs pack it is a **per-session tool pack** injected by construction field (never `ToolContext`), gated on **both** a sandbox container being present **and** the agent activating `browser` (`plugins=("browser", …)`) — **not** an MCP connector (it never enters `mcp_registry` / takes an alias). The model-facing name/schema are noeta's (stable prefix owned by noeta); the implementation delegates through a narrow `BrowserBackend` seam whose one impl `AioBrowserBackend` pins the container `/mcp` browser wire in a single adapter (element-by-numeric-`index`; `browser_type` → `form_input_fill` + `press_key`; `browser_extract` → `get_markdown` + `get_clickable_elements`), reusing an `McpHttpClient` purely as an **internal transport**. Perception is text/element-level in v1 — `browser_screenshot` is a workspace artifact, not vision. The **`web` subagent** (an official `AgentDefinition`, `plugins=("browser", …)`) is the layer-4 identity the main agent delegates page work to, so browsing token bloat stays isolated in a child context. See the execution-environment-seam ADR (browser subsystem) + the sandbox-browser-subsystem spec.
_Avoid_: calling it an "MCP browser server" or a connector (the container's MCP is an internal transport here, not a model-facing connector); "the browser tool" (singular) when you mean the whole pack.

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
The generic mechanism by which resident content (the "semi-stable segment tenants" such as skills and the memory index) enters context, made of two load-bearing parts: **event recording** (`ContextContentRecorded`: kind / name / version / content_hash / policy; fold records `active_content[kind][name] = content_hash` — the map is `kind → {name → hash}`, and the **hash is last-write-wins**: a re-record with a new hash is a *refresh*, an identical hash a no-op; gated on `content_hash` being non-empty. The activation *anchor* (`ContextState.content_anchors`, placement) is first-write-wins — refresh moves bytes, never placement) + **assembly rendering** (the runtime's `ContentChannelRegistry` renders each kind into the semi-stable segment, one `ContentKindSpec` per material kind (kind + renderer + hashes + policy); registering a `ContentKindSpec` is the open extension hook, exposed via `noeta.sdk`, while the registry and renderer themselves stay in noeta-runtime). The recorded `content_hash` is **load-bearing** (kernel final form, spec §6): a renderer receives a `resolve(kind, name) -> bytes` that derefs the resident's *active* hash through the `ContentStore`, so the composed bytes are a pure function of `(folded state, content store)` (law 2) — a refresh yields new bytes, a mutated backing store on disk changes nothing until a re-record. (Skills are the exception: they are `pinned` and their renderer composes from the preloaded `SkillRegistry`, ignoring `resolve` — their base-dir line is not content-addressed.) The write seam is the generic `init` hook (`PackContribution.init`) run through the scoped `SessionRecorder` at seed time — the feature-named seed recorders (`record_memory_index` / `record_instructions` / `record_environment`) are gone; an `init` hook `put()`s its rendered bytes and records the ref, reruns every drive, and the recorder's hash gate makes it idempotent. `policy` (`pinned` / `evolving`) stays descriptive provenance (the verify-era drift-comparison consumers were retired). Adding a kind = register a `ContentKindSpec` through the open ContentChannel extension surface (re-exported via `noeta.sdk`); the registry/renderer code in noeta-runtime needs no change. Current tenants: `skill` (pinned), `memory` (evolving), `instructions` (evolving), `environment` (evolving). The red line: **providers may only record on the write side** — resolving recorded bytes from the durable content-addressed `ContentStore` is not an external callback; reaching out to a *live external source* at compose time is forbidden.
_Avoid_: Provider (that's the external-service adapter, above), ContentSource, Middleware

**Reminder tracks (A / B / C)**:
The three distinct ways authored context text reaches the View, each a plugin Surface, distinguished by *when* it runs and *whether it is recorded*:
- **Track A — `reminder_provider` (recorded injection).** An **impure** provider at a named intake seam (`turn_intake`, `task_seed`) that, given a narrow read-only `RecallView`, returns zero or more `Reminder(text, origin ∈ {system, memory})`. May query a vector DB or external system **because its output is recorded** through the Engine's sole origin-writer seam (`append_user_message`); resume/replay folds the reminder back from the ledger and **never re-invokes the provider**. Multiple providers on one seam run `(plugin, name)` order; a raise **fails the turn loudly** (no silent skip). The built-in memory auto-recall is the first tenant, and this is the seam RAG-backed memory plugins use.
- **Track B — `reminder` (compose-time, pure).** A `(name, priority, render)` spec where `render` is a **pure** function of a narrow folded-state projection (`ReminderView`) returning `str | None`, rendered at the **tail of the dynamic suffix** (the adapter wraps it in `<system-reminder>`), **never recorded** and re-derived on every compose — so the **stable prefix is untouched by construction**. Ordered by integer `priority`, ties broken by `(plugin, name)`. The three composer built-ins (`unfinished-todos` / `delegation-nudge` / `read-suggestion`) are the first tenants, priorities chosen to keep the composed tail byte-identical to the pre-migration order.
- **Track C — the resident Content Channel.** The pre-existing `content_kind` / `ContentKindSpec` mechanism (above): a **pure** renderer for resident content in the semi-stable segment, its activation recorded as a `ContextContentRecorded` event.

Determinism of a third-party `render` (tracks B and C) is a **documented contract**, not enforced — the same trust class as existing `ContentKindSpec` renderers.
_Avoid_: mixing the tracks — "reminder" alone is ambiguous; say track A (recorded / impure) vs track B (compose-time / pure) vs track C (resident).

**Anchored Placement**:
Where a content-channel resident renders, decided by its **activation anchor** — the rolling-history length fold records (`ContextState.content_anchors`, first-write-wins) when the activation folds (docs/adr/anchored-content-placement.md). One rule, no per-kind flag: an anchor at/before the first assistant message ⇒ the resident renders in the semi-stable segment (pre-loop activations, byte-identical to the pre-anchor layout); a later anchor ⇒ it renders **inside the dynamic suffix at that anchor** — one message at the point of activation, so a mid-task skill/instructions activation appends instead of rewriting the head (KV-cache re-prime avoided). The insertion index slides past `role="tool"` messages (never splits a tool round-trip); an anchor covered by a compaction summary **re-hangs right after the summary message** — automatic, because resident content is state-rendered, never stored in history. Companion feature: **instructions discovery** (`instructions_discovery`, default off) — after a successful `read` INSIDE the workspace, the runtime activates the not-yet-active `NOETA.md`/`AGENTS.md` between the read file's directory and the workspace root (resident name = workspace-relative path); scope is deliberately fenced to the workspace (write authorization ≠ instruction authority).
_Avoid_: Lazy loading (that's the *when*; this term is the *where*), Inline injection

**origin**:
An optional author marker on a `Message`, one of `human / system / memory`, defaulting to `None` = the role's natural author (omitted on serialization, so old recordings drift zero). **Single-writer guard**: only the engine's recording path may write it; a marker forged in model/tool output is just text. The vendor-tag syntax does not enter the ledger: the Anthropic adapter wraps host injections (user messages with origin=system/memory) in `<system-reminder>` and merges them into the adjacent user turn; openai_compat renders them as system-role messages.
_Avoid_: Author, Sender, Role (role is a different dimension — don't conflate them)

**Memory**:
Cross-task long-term memory (v2), file-based and model-managed, that **does not impersonate a skill** (their drift policies are opposite). Mutation = the ordinary tools `memory_write` (one markdown per memory, optional frontmatter `description`/`type`) and `memory_archive` (move into `archive/` — retire, never delete); on-demand reading = `memory_read` (full text by name) and `memory_search` (case-insensitive substring over names + bodies, excerpt-only output); **resident index** = the second tenant of the content channel (`memory_content_kind`, kind=`memory`, policy `evolving`, living in the semi-stable segment so compaction does not flush it), rendering `(name, type, summary)` entries; **auto-recall** = re-expressed as a **reminder track A** `reminder_provider` (`memory_reminder_provider`) on the `turn_intake` seam: it reads the store now (impure — legal because the output is recorded), two-tier matching (name tokens, then summary tokens), and records hits as one `origin="memory"` follow-up through the Engine's origin-writer seam; `append_user_message_with_recall` is now a thin wrapper over the generic `record_intake_reminders` (byte-identical ledger); **policy** = the `MEMORY_POLICY_PROMPT`, migrated onto the `prompt_fragment` surface as the `memory` built-in plugin's `memory-policy` contribution (what to save / what not / update-before-create), appended after the prompt because an empty store renders zero resident bytes. Activated by `memory` (`plugins=["memory"]`, part of behavior-affecting agent identity); among the official presets main and main-web enable it — only a top-level conversational agent receives user messages. The store root resolves through ONE chain for every consumer (engine build, recall, `memory_root()`): the per-task `HostConfig.memory_root_resolver` (`task_id → Path | None`, the multi-tenant seam — the Engine cache partitions by the resolved root) when it resolves, else `memory_dir` > `global_memory_dir` > `~/.noeta/memories`.
_Avoid_: using "Memory" to mean TaskState (that is in-task state; this is cross-task material)

**Memory consolidation**:
The asynchronous curation pass over the memory store: a reserved-name agent (`__consolidation__`, tool surface = the memory pack only) runs as an ordinary root task on the resident worker pool, fed a digest of recent session activity, and merges duplicates / archives superseded memories / fills clear gaps. Triggered at the session-stop seams (explicit close + turn boundary) behind a debounce marker (`.consolidation-state.json` in the memory root, written at enqueue time); the toggle is **host configuration** (`memory_consolidation`), not agent identity. It never injects into live sessions and can only archive, never delete. A multi-tenant host runs one pass per tenant: `run_consolidation(include_task=…)` scopes the digest to that tenant's root sessions, the per-root marker debounces tenants independently, and `on_seeded` hands the curation task id over before any worker can claim it. See `docs/adr/memory-consolidation.md`.
_Avoid_: "dreaming" (colloquial; use consolidation), calling it a scheduler (it has none — the debounce marker over existing seams is the whole mechanism)

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

**Write fence** (`WorkspaceRoot`):
The path-containment seam the **write** fs tools (`edit` / `write` / `apply_patch`) resolve through: a target is canonicalised (`realpath`, or `normpath` under a container root) and must land under the session workspace **or** under one of the `extra_roots` the host has authorized. Containment is **component-wise** (`path_within`, published on `noeta.sdk`), never string-prefix — `/srv/app-old` is not inside `/srv/app`. **Reads are not fenced**: `read` / `grep` anchor a *relative* path to the workspace and take an absolute one wherever it points (a neighbouring checkout, a skill pack's bundled reference). The widening is a **resolver**, not a fixed tuple — `HostConfig.write_roots` is `task_id -> extra writable directories`, consulted per call, so an authorization granted while a task sits paused takes effect on the resumed call without rebuilding the tool set (which would move the stable prefix). `None` (the CLI, a bare SDK embedding) keeps the single-root wall. Never a sandbox: `shell_run` reaches the whole filesystem through a subprocess (§B19) — the container is the isolation boundary, this is the *deliberate-mutation* boundary. See the workspace-write-authorization ADR.
_Avoid_: Sandbox, jail (this is path resolution, not process confinement); "the fs fence" unqualified now that reads are outside it

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
Not a first-class concept in the engine. Express fixed procedures with a deterministic Policy + spawn_subtask. **Do not** let `WorkflowSpec / WorkflowRunner / WorkflowPolicy` appear in library documentation or code. An **orchestration script** the model improvises ("spawn a few assistants first, look at the results, then spawn the next batch") is likewise not a new primitive: it lands as **one Task + a Policy that interprets that script**, and the assistants it spawns are real Subtasks. Multi-node "workflow" sequencing — one root task per node, advanced by handoffs — is something a host builds on top of the libraries, not an engine primitive; the class-name ban above protects the engine libraries.

**"Session"**:
**Not a concept in these libraries.** The engine knows only Tasks, and multi-turn conversation is simply one Task receiving user input repeatedly — each question = one **turn** (a cycle of one wake → several Steps → suspend, with the Task resting at `suspended` + `HumanResponseReceived` between turns); each delegation = one **Subtask**. A host that groups turns into a user-visible "session" owns that concept itself. **Do not** let session ids or session event schemas appear in engine/SDK code or below-app identifiers.

**"Run"**:
Not a first-class concept. Always use Task. When it appears in external docs or old code, treat it as a Task.

## Sample dialogue

> User: This task has been waiting on a subtask for a long time — can I cancel it?
>
> Answer: Yes. The root task is currently **suspended**, with wake_on = `SubtaskCompleted(t-child-7)`. Cancelling it (a `Client` cancel verb) cascades to cancel all in-flight subtasks.
>
> User: How did its earlier ContextPlan pick the files?
>
> Answer: Inspect the most recent `ContextPlanComposed` event (read the EventLog directly); its selected / dropped entries carry provenance, so you can trace back to the content source (which Skill, which message, and so on).
