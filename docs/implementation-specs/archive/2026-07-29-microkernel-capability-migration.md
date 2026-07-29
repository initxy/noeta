# Microkernel migration — official capability implementations move into the plugin band

> **Status: Shipped** — landed across `9d81206` (M1 fs/web), `24c99bb` (M2
> providers/governance/reminders/sandbox), `99b4034` (M3
> memory/browser/app/mcp), `f179904` (M4 packaging) and the M5 docs commit;
> the durable decisions live in the
> [plugin-contribution-bundles.md](../../adr/plugin-contribution-bundles.md)
> 2026-07-29 addendum. Phase 2 (skills subsystem + ReActPolicy) is
> deliberately out — each needs its own spec.

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
- Loader knobs keep working: `builtins=False` / `disabled_builtins` — **amended
  in M4**: the knobs scope the *loaded set* (the external audit / resolution
  surface), never the SDK's own defaults. A bare `Options()` against a
  builtins-less PluginSet still resolves its default fs/web roster through the
  `noeta.client.parts` doorway (the M1-landed architecture reads the catalogue
  directly, not the session set) — the reference host documents and relies on
  exactly this semantic (`builtins=False` + preset recipe). The loud-failure
  guarantee for a truly builtins-less *environment* is the runtime-alone
  closure: the kernel builder's loud-fail `None` injections name the missing
  part (install smoke, acceptance 4).

## Milestones

- [x] **M1 — inversion proof on fs/web.** Move fs/web impls; replace the
  static default tool table with loader resolution; parity goldens 5/5
  byte-identical. (Riskiest first: KV-cache byte-identity.)
- [x] **M2 — providers, governance, reminders, sandbox.** Builder/composer
  take injections; `observers.otlp` decided: host wiring (`noeta.client.otlp`).
- [x] **M3 — memory, browser, app, mcp.** Execution wiring
  (memory/instructions/environment) goes injection-side; runtime keeps seams
  only.
- [x] **M4 — packaging.** Deps move; install smoke re-pinned; import-linter
  re-layered; runtime wheel verified impl-free.
- [x] **M5 — docs.** CONTEXT.md distribution-boundary + Locked-vs-open
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
9. *(amended in M4 — the original wording contradicted criterion 6)*
   `builtins=False` scopes the loaded set only: a bare `Options()` against a
   builtins-less PluginSet compiles byte-identically to the no-set build
   (pinned by `test_builtinless_set_keeps_the_default_capability_surface`);
   the reference host's `builtins=False` + preset recipe behaves unchanged.
   The loud failure for a genuinely builtins-less environment lives in the
   runtime-alone closure (criterion 4): the kernel builder's loud-fail
   injections name the missing part.

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
- **2026-07-29 — M4 landed.** Packaging only — no code moved:

  * **Dependency move (acceptance 5).** ``httpx`` left noeta-runtime's
    ``pyproject.toml`` and arrived in noeta-sdk's (runtime code no longer
    imports it anywhere — grep-verified, only two comments mention it);
    ``uv lock`` re-pinned. A new always-on static test
    (``test_httpx_is_an_sdk_dependency_not_a_runtime_one``) pins the
    declaration so a stray re-add fails in every dev run, not just CI.
  * **Install smoke re-pinned to the two closures (acceptance 4).**
    ``tests/test_install_smoke.py`` now proves BOTH halves: the sdk-closure
    test additionally asserts the impls arrived (the parts doorway resolves
    the 11-tool default roster) and inspects the wheels (``noeta/builtins/``
    ships in the sdk wheel ONLY); a new runtime-alone test installs the
    kernel wheel by itself into a fresh venv — kernel imports work
    (one module per band, incl. ``noeta.testing.profile``, which imports
    runtime-alone but whose ``build_runtime`` needs the sdk at call time),
    ``noeta.builtins`` / ``noeta.client`` / ``noeta.sdk`` /
    ``noeta.presets`` / ``httpx`` are absent, and a **hand-injected agent**
    (FakeTool + FakeLLMProvider + ReActPolicy over in-memory storage —
    protocol objects only) runs a scripted turn to ``TaskCompleted``. The
    wheel-content inspection doubles as the "runtime wheel impl-free"
    verification.
  * **Import-linter redraw.** The header narrative now tells the
    microkernel story (materials band = locked composer + phase-2 policies
    + authoring-only tools, all kernel-side; every capability impl in
    ``noeta.builtins.*.impl`` behind the universal
    ``sdk-core-not-builtins`` ban — which is also what keeps
    kernel↛adapter, since every adapter now lives in builtins). One new
    contract: **kernel-vocabulary-diet** — the M2/M3 vocabulary sinks
    (``noeta.runtime.{governance,browser,app_preview,mcp}``) may import
    nothing in-project beyond ``noeta.protocols``, so config vocabulary can
    never fatten into impl-reaching modules. 10/10 KEPT.
  * **Acceptance 9 amended** (the M1-carried nuance, resolved as
    "amend the criterion"): the original wording — ``builtins=False`` +
    bare ``Options()`` fails loudly — contradicted acceptance 6, because
    the reference host *documents and relies on* loading its example
    plugins with ``builtins=False`` while the preset recipe keeps noeta's
    own capabilities (its ``builtins=False`` comment says exactly this).
    Under the landed architecture the loader knob scopes the **loaded set**
    (the external audit / resolution surface); the SDK's defaults arrive
    through the ``noeta.client.parts`` doorway, which reads the catalogue
    directly. Pinned by
    ``test_builtinless_set_keeps_the_default_capability_surface`` (bare
    ``Options()`` against a builtins-less set compiles byte-identically to
    the no-set build). The loud-failure guarantee for a genuinely
    builtins-less *environment* is the runtime-alone closure: the kernel
    builder's loud-fail ``None`` injections name the missing part.

  Gates: parity goldens 5/5 + reminder/recall snapshots untouched
  (packaging moved no code), install smoke 2/2 + 4 static, 3377 passed /
  129 skipped, coverage ≥ 85 gate green, mypy strict clean, naming lint
  clean, import-linter 10/10 KEPT.
- **2026-07-29 — M5 landed.** Docs only:

  * **CONTEXT.md** — the Distribution boundary section rewritten for the
    microkernel (noeta-runtime = pure kernel, transport-free, injection-only
    builder; noeta-sdk carries ``noeta.builtins`` with every capability
    impl + the httpx dep); Locked-vs-open gained the D2 statement (noeta's
    own defaults ride the plugin path through the ``parts`` accessors, no
    static default tables); the **Tool** / **Provider** / **Built-in
    plugin** vocabulary entries updated (12-dir catalogue, manifest + impl
    co-located, ``noeta.sdk.providers`` as the supported adapter path).
  * **ADRs** — ``plugin-contribution-bundles`` gained the microkernel
    addendum (co-location, loader-resolved defaults, injection-only
    builder, vocabulary sinks, the universal contract, phase-2 scope) and
    its built-ins section updated; ``provider-neutral`` re-expresses
    kernel↛adapter via the universal ``sdk-core-not-builtins`` contract
    (the retired ``runtime-no-providers`` / ``providers-only-protocols``
    pair is named as history); 13 further ADRs swept for moved paths
    (landing-point references updated, decision narratives untouched).
  * **Reference / how-to (en+zh)** — ``swap-providers`` imports fixed to
    ``noeta.sdk.providers`` (the old ``noeta.providers.*`` imports were a
    live API break); ``reference/tools`` source paths re-pointed at
    ``noeta/builtins/*/impl/``; ``reference/sdk`` re-export provenance
    fixed to the kernel vocabulary modules (``noeta.runtime.app_preview`` /
    ``noeta.runtime.mcp``); ``reference/plugins`` built-ins section
    updated to the manifest+impl co-location and the 12-name catalogue.
  * **Wheel READMEs** — both package READMEs (the wheel long descriptions)
    updated to the kernel / catalogue split.
  * **Wrap-up nit** (carried from the plugin redesign): the three newer
    example plugins (``checklist-reminder`` / ``memory-recall`` /
    ``redaction``) gained the illustrative ``pyproject.toml`` the older
    three ship (entry point + ``[tool.noeta]`` mirror; ``plugin_check``
    passes 3/3).

  Spec archived as Shipped. Phase 2 (skills subsystem + ReActPolicy) needs
  its own specs — deliberately not folded in here.
