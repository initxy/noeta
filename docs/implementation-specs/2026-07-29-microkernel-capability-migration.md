# Microkernel migration — official capability implementations move into the plugin band

> **Status: Active**

## Goal

Finish what the built-ins band started: `noeta-runtime` becomes a **pure
kernel**, and every official capability *implementation* lives inside its
plugin directory — `noeta/builtins/<name>/` holds the manifest **and** the
code, shipped in the **noeta-sdk** wheel. The default agent (`Options()` bare)
obtains its tools / guards / reminders exclusively **through the plugin
loader** at the client-build boundary; no kernel or SDK-core module statically
imports a capability implementation.

One sentence: *the engine hosts execution; everything an agent is made of is a
plugin — including ours.*

## Owner decisions (2026-07-29 interview)

- **D1 — wheel placement.** `noeta.builtins` (manifests + implementations)
  ships in **noeta-sdk**. `noeta-runtime` is kernel-only: installed alone it
  runs an agent only with host-injected `Tool` / `LLMProvider` / hook objects
  (protocols are its contract). `install_smoke` closures are re-pinned
  accordingly.
- **D2 — loader-resolved defaults.** The bare-`Options()` defaults (the 11
  tools, the default guards/observers, the three compose-time reminders) are
  obtained by resolving the built-in manifests through the ordinary plugin
  loader at client build — `BUILTIN_TOOL_CLASSES`-style static default tables
  are removed. Consequence: the `sdk-core-not-builtins` rule becomes
  **universal** — *nothing* statically imports `noeta.builtins`; the loader's
  dynamic `ref` resolution is the only doorway. `DEFAULT_PLUGINS = ("fs",
  "web")` keeps a bare `Options()` byte-identical (same classes resolve, same
  schemas render).
- **D3 — two phases.** Phase 1 (this spec) migrates the clean movers. Phase 2
  — the **skills subsystem** (entangled with the locked composer as a content
  tenant) and **ReActPolicy** (entangled with driver/resolver and the control
  tools) — each needs its own design and spec. Control tools (`todo_write` /
  `skill` / `ask_user_question`) stay kernel **permanently** (redesign
  non-goal: control tools are not contributions).

## Target layout

```
noeta/builtins/<name>/
  __init__.py     # MANIFEST only — imports nothing from impl (zero-execution listing)
  impl/ …         # the real implementation, refs point at siblings:
                  #   "noeta.builtins.fs.impl.read:ReadFileTool"
```

Phase-1 movers (source → destination):

| Built-in | Implementation moving in |
| --- | --- |
| `fs` | `noeta.tools.fs.*` (read/edit/patch/shell, `LocalExecEnv`, workspace fence helpers) |
| `web` | `noeta.tools.web.*` |
| `memory` | `noeta.tools.memory` + `noeta.execution.memory` (recall provider) |
| `browser` | `noeta.tools.browser.*` (pack + `AioBrowserBackend`) |
| `mcp` (new dir or under existing) | `noeta.tools.mcp.*` |
| `app` (new dir) | `noeta.tools.app.*` |
| `reminders` | `noeta.context.reminders` (three pure renderers) |
| `governance` | `noeta.guards.*` + `noeta.observers.hook` (decide `observers.otlp` in M2: telemetry may stay host wiring) |
| `providers` | `noeta.providers.*` (3 adapters + codecs + catalog) |
| `sandbox` | `noeta.tools.fs.exec_env:AioSandboxExecEnv` (with fs impl split) |

What stays kernel (locked, unchanged): `protocols`, `core` (Engine/fold),
`runtime` (Worker/Dispatcher/ToolRuntime/RuntimeLLMClient/compaction),
`storage`, `read_models`, the execution machine, `context` (the locked
`ThreeSegmentComposer` + registries), the `agent` identity layer, `policies`
(phase 2), the skills subsystem (phase 2), control tools (forever).

## The kernel inversion

Today `execution/builder.py` (and `environment` / `instructions` / `memory` /
`skills`) statically import `noeta.tools` etc. to assemble defaults. After the
migration the runtime builder is **injection-only**: the SDK client build
resolves the activated plugins' contributions (tools, guards, observers,
reminder renderers, memory pack, sandbox factories) and hands the kernel a
complete kit. The composer keeps its reminder *registry mechanism*; the three
default renderers arrive through the same injection path plugin reminders use.

Known static-import sites to sever (grep-verified 2026-07-29):
runtime side — `execution/{builder,driver,resolver,environment,instructions,
memory,skills}.py`, `context/composer.py`, `runtime/llm.py` (verify: the
kernel↛adapter contract is KEPT today, so this import must already be
lazy/`TYPE_CHECKING`), `testing/profile.py` (rehome or inject);
SDK side — `client/{parts,client,host,host_config,sandbox,capabilities}.py`,
`sdk/{__init__,authoring,providers}.py`.

Relocations that are *authoring machinery*, not capability impls (move into
SDK-core bands, **not** builtins): the `@tool` decorator
(`noeta.tools.decorator`), `path_within` / `WorkspaceRoot` types if any public
symbol currently lives in `noeta.tools` (public surface `noeta.sdk` must not
change). `ExecEnv` / `BrowserBackend` protocols sink to a kernel/protocols
band; only concrete backends move into builtins.

## Packaging / contracts

- `httpx` dependency moves noeta-runtime → noeta-sdk; `psycopg` stays
  (storage is kernel).
- Import-linter: re-draw the layer table (implementation bands `tools` /
  `providers` / guard-impl / observer-impl dissolve into `noeta.builtins`);
  keep — now universally — "nothing statically imports `noeta.builtins`";
  keep kernel↛adapter as "kernel bands import only protocols"; the
  `noeta.tools.mcp` narrow contract is redrawn inside builtins.
- Loader knobs keep working: `builtins=False` / `disabled_builtins` — with D2
  a bare `Options()` against a builtins-less PluginSet now **fails loudly**
  naming the missing activation (intended consequence; document it).

## Milestones

- [x] **M1 — inversion proof on fs/web.** Move fs/web impls; replace the
  static default tool table with loader resolution; parity goldens 5/5
  byte-identical. (Riskiest first: KV-cache byte-identity.)
- [x] **M2 — providers, governance, reminders, sandbox.** Builder/composer
  take injections; `observers.otlp` decided: host wiring (`noeta.client.otlp`).
- [x] **M3 — memory, browser, app, mcp.** Execution wiring
  (memory/instructions/environment) goes injection-side; runtime keeps seams
  only.
- [ ] **M4 — packaging.** Deps move; install smoke re-pinned; import-linter
  re-layered; runtime wheel verified impl-free.
- [ ] **M5 — docs.** CONTEXT.md distribution-boundary + Locked-vs-open
  rewrite; plugin-contribution-bundles ADR addendum; reference/how-to (en+zh);
  spec ticks.

Precondition: the 2026-07-28 extensibility redesign (currently uncommitted)
is **committed first** — this migration must not stack on an unreviewed tree.

## Acceptance criteria

1. No module outside `noeta.builtins` statically imports a moved
   implementation; `noeta.builtins` itself is statically imported by nothing
   (import-linter enforced).
2. Bare `Options()` parity goldens byte-identical (5/5 unchanged).
3. Importing `noeta.builtins` (manifest layer) imports **zero** impl modules
   (sys.modules test).
4. noeta-runtime installed alone: kernel imports work, no capability impls
   present, a hand-injected agent runs; noeta-sdk closure carries the impls
   (install smoke).
5. `httpx` absent from noeta-runtime dependencies.
6. The 6 example plugins + reference host behave unchanged.
7. `make check` green; all import-linter contracts KEPT after re-layering.
8. External authoring path (PluginBuilder / manifest / `@tool`) unchanged;
   `noeta.sdk` public surface unchanged (public-surface contract test).
9. `builtins=False` + bare `Options()` fails loudly naming the missing
   activation.

## Risks

- **Byte-identity across the assembly inversion** — mitigated by goldens-first
  M1 and `DEFAULT_PLUGINS` resolving the same classes.
- **Hidden static imports** (TYPE_CHECKING, testing/profile, doc code blocks)
  — sweep with a grep gate before each milestone closes.
- **Scope creep into phase 2** — skills/policy entanglement is explicitly out;
  any discovered coupling is recorded here and deferred, not solved inline.

## Progress log

- **2026-07-29 — M1 landed.** Kernel sinks: ``noeta.runtime.exec_env``
  (protocol + ``LocalExecEnv`` + the AIO backend until M2),
  ``noeta.runtime.workspace`` (``WorkspaceRoot`` / ``path_within`` /
  ``WriteRootsResolver`` / ``FsWriteMode``), ``noeta.runtime.subproc``,
  ``noeta.runtime._env``, and ``noeta.runtime.shell_policy`` (``ShellMode`` +
  the allowlist machinery + the project rules file — split out of the old
  ``shell.py``). Moves: the nine fs tool classes + pack factory into
  ``noeta/builtins/fs/impl/``, the web pair into ``noeta/builtins/web/impl/``;
  manifest refs now point at the sibling impl modules; the old
  ``noeta.tools.fs`` / ``noeta.tools.web`` packages are gone
  (``skill_script.py`` parked at ``noeta.tools.skill_script`` until phase 2).
  Inversion: ``client/parts.py`` static table replaced by
  ``builtin_tool_classes()`` (manifest-driven, ref-resolved, memoized;
  ``BUILTIN_TOOL_CLASSES`` kept as a module ``__getattr__``);
  ``build_session_inputs`` grew ``fs_tools_factory`` / ``web_tools_factory``
  (None fails loudly) and the SDK host injects
  ``parts.default_tool_factories()``. Import-linter: one pinned exemption
  (``noeta.tools.mcp._client -> noeta.runtime._env``). 81 test files swept.
  Gates: parity goldens 5/5 byte-identical, 3375 passed, coverage 87.6%,
  import-linter 13/13, mypy strict clean.
- **2026-07-29 — M2 landed.** Four movers, one decision:

  * **Reminders.** ``noeta.context.reminders`` is registry mechanism only
    (``ReminderView`` / ``ReminderSpec`` / ``ReminderRegistry``); the three
    renderers + ``BUILTIN_REMINDER_PRIORITIES`` moved to
    ``noeta/builtins/reminders/impl/`` (manifest refs re-pointed). A bare
    composer now has an EMPTY reminder registry; the builder takes
    ``base_reminders`` (None fails loudly) and the SDK resolves the specs
    from the manifest — ref + declared priority — via
    ``parts.default_reminder_specs()``. ``composer_reminders_*`` snapshots
    byte-identical.
  * **Governance.** Guard *config* vocabulary sank to
    ``noeta.runtime.governance`` (``Budget`` / ``PermissionPolicy`` /
    ``RepetitionPolicy`` + actions / ``PreToolUseRule`` + ``MatchArg`` /
    ``RiskLevel`` + ``KNOWN_RISK_LEVELS`` / ``SkillEnforcementMode`` — the
    M1 ``runtime.workspace``/``shell_policy`` precedent); the four guard
    classes + the live-only ``HookObserver`` (with its rule types) moved to
    ``noeta/builtins/governance/impl/``; ``noeta.guards`` deleted. The old
    ``_build_guards`` body is now the impl's ``build_default_guards``; the
    builder takes ``guards_factory`` (None fails loudly), pre-shaping the
    kernel-side facts (sdk-resolved skill grants, delegation-gated agent
    set) and passing operator fields through. SDK injects
    ``parts.default_guards_factory()``. ``testing/profile.py`` keeps type
    imports kernel-side and resolves guard classes via the dynamic doorway
    at call time.
  * **otlp decision.** ``observers/otlp.py`` → ``noeta/client/otlp.py``:
    telemetry is **host wiring** (a ``HostConfig`` opt-in), not agent
    governance — and the runtime wheel sheds its one httpx-wanting module
    (feeds M4). ``noeta.sdk.OtlpTraceConfig`` re-export unchanged.
  * **Providers.** ``noeta.providers.*`` (3 adapters + codecs + ``_sse`` +
    catalog) moved to ``noeta/builtins/providers/impl/``;
    ``derive_compaction_config`` (+ its two constants) moved out of the
    kernel builder into the catalog module (kernel keeps the
    ``CompactionConfig`` type + ``COMPACTION_OFF``). Kernel severances:
    ``select_provider_edit_tool`` now maps a *family* (not a model) and the
    builder takes ``provider_family`` pre-resolved (``None`` ⇒ drop
    neither — the documented no-catalog semantic); the ``InteractionDriver``
    takes ``alias_resolver`` (``None`` ⇒ identity — with no catalog there
    are no aliases, so identity IS the kernel-correct semantic, the one
    deliberate deviation from loud-fail). SDK-side accessors in ``parts``
    (``derive_compaction_config`` / ``provider_family`` / ``catalog_price``
    / ``resolve_model_alias``, memoized dynamic resolution);
    ``sdk/providers.py`` is now a PEP 562 lazy re-export;
    ``client/capabilities.py`` resolves the catalog dynamically.
  * **Sandbox.** ``AioSandboxExecEnv`` (+ ``AioHttpPost`` /
    ``AioSandboxError``) split out of ``runtime/exec_env.py`` (kernel keeps
    ``ExecEnv`` + ``LocalExecEnv`` + ``TreeSnapshot`` + the exclusive-create
    errors) into ``noeta/builtins/sandbox/impl/exec_env.py``;
    ``AioBrowserBackend`` (+ ``AioBrowserError`` + the ``/mcp`` wire
    constants) split out of ``tools/browser/_backend.py`` (kernel keeps the
    ``BrowserBackend`` Protocol) into ``…/impl/browser.py``; manifest refs
    re-pointed. ``client/sandbox.py``'s default factories resolve the
    adapters through the dynamic doorway on first use.

  Import-linter re-drawn minimally: the ``guards`` / ``providers`` bands
  dissolved; ``sdk-core-not-builtins`` widened to the **universal** "nothing
  statically imports ``noeta.builtins``" (all bands as sources, D2);
  10/10 KEPT. Discovered coupling for M3 (recorded, not solved):
  ``builtins/sandbox/impl/browser.py`` imports
  ``noeta.tools.mcp._http_client`` as its transport — when M3 moves the mcp
  pack, either the HTTP client sinks kernel-side or the edge is re-pointed.
- **2026-07-29 — M3 landed.** Four movers; the driver's memory seam inverted:

  * **Memory.** ``noeta.tools.memory`` → ``builtins/memory/impl/store.py``
    (store + 4 tool classes + pack builder + ``DEFAULT_GLOBAL_MEMORY_DIR`` /
    ``load_memory_store``, both formerly in ``execution.memory``); the
    store-touching recall glue (``recall_memories`` /
    ``memory_reminder_provider`` / ``append_user_message_with_recall``) →
    ``…/impl/recall.py``. ``noeta.execution.memory`` is **seams only**:
    ``record_memory_index`` (kernel-pure over ``context.memory``),
    ``intake_providers`` (now composes a bound ``ReminderProvider``, not a
    store), and ``RecallGoalPrelude`` (field ``store`` → ``recall``). The
    host seam contract changed: ``memory_recall_context`` returns
    ``(recall_provider, entries)`` — the host binds the impl's provider to
    the live store; the kernel driver never sees a store. Builder takes
    ``memory_factory`` (→ ``impl:build_memory_pack``; ``root=None`` reads
    the impl default LATE for test hermeticity — conftest now pins
    ``builtins.memory.impl.store.DEFAULT_GLOBAL_MEMORY_DIR``);
    ``SessionInputs.memory_store`` is an opaque handle.
    ``noeta.sdk.MemoryStore`` became a lazy module-``__getattr__``
    re-export. ``recall_intake_*`` goldens byte-identical.
  * **Browser.** The ``BrowserBackend`` Protocol sank to
    ``noeta.runtime.browser`` (M1 ``exec_env`` precedent); the pack
    (5 tools + ``build_browser_tools`` + ``BROWSER_TOOL_NAMES``) →
    ``builtins/browser/impl/``; ``noeta.tools.browser`` deleted. Builder
    takes ``browser_tools_factory`` (loud-fail only when backend +
    capability are both present); the host's approval roster resolves via
    ``parts.browser_tool_names()``. The manifest stays contribution-free ON
    PURPOSE — identity ``tool`` contributions would merge into an
    activating agent's AgentSpec, which the capability flag deliberately
    does not do (parity pinned).
  * **App.** ``AppPreviewGateway`` / ``AppMount`` sank to
    ``noeta.runtime.app_preview``; ``open_app`` + ``build_app_tools`` →
    ``builtins/app/impl/``; ``noeta.tools.app`` deleted. New ``app``
    built-in dir (declaration-free manifest, same rationale as browser);
    ``app`` joined ``_INERT_BUILTIN_ACTIVATIONS``. Builder takes
    ``app_tools_factory`` (loud-fail only when a gateway is present).
  * **MCP.** Vocabulary sank to ``noeta.runtime.mcp`` (``MCP_PREFIX``,
    ``McpConfigError`` / ``McpError``, ``HttpPostFn``, the server specs —
    the M2 governance-vocabulary precedent; ``noeta.sdk``'s six MCP names
    re-export from there statically); the connector impl (clients,
    ``McpTool``, discovery, prompts, resources) → ``builtins/mcp/impl/``;
    ``noeta.tools.mcp`` deleted (its import-linter contract + the pinned M1
    exemption retired with it). New ``mcp`` built-in dir (declaration-free
    manifest; the ``mcp`` activation name already existed as a capability
    flag). The host's live-MCP path resolves ``build_mcp_tools`` /
    ``mcp_provenance_from_specs`` via ``parts.mcp_impl()``. The M2-recorded
    sandbox coupling resolved as a **documented first-party cross-plugin
    edge**: ``builtins/sandbox/impl/browser.py`` imports
    ``noeta.builtins.mcp.impl._http_client`` as its private transport (both
    ship in the sdk wheel).

  ``noeta.tools`` now holds only authoring machinery + shared helpers
  (``decorator`` / ``fake`` / ``_invocation`` / ``_limits`` / ``_refs`` /
  ``descriptions`` / the phase-2-parked ``skill_script``). Catalogue: 12
  built-ins (app + mcp new). Gates: parity goldens 5/5 +
  ``recall_intake_*`` byte-identical, 3375 passed / 129 skipped, coverage
  87.60%, mypy strict clean, import-linter 9/9 KEPT.
