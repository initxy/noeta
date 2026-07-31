# SDK-layer cleanup (2026-07-30)

Status: Implemented (2026-07-31) — `make check` green: 3423 passed / 129
skipped, coverage 85.55% (gate 85%), mypy --strict 24 files clean, ruff clean,
import-linter 10/10 contracts kept.

## Review follow-up (2026-07-31)

Acceptance review of the implementation commit found six things worth fixing;
all are done in the follow-up commit:

* **`process_hooks` mis-routed an unroutable surface.** Deriving the governance
  set from the registry was right, but the bucket split was `if observer /
  else guards`, so a host-registered process-scoped wiring surface had its
  value filed under `guards` — the engine would receive a non-`Guard` and
  crash on the first tool call. It is now refused loudly, and the docstring no
  longer claims a third process surface is "collected".
* **`SurfaceSpec` enum fields are validated in `__post_init__`.** Deleting
  `merge_rule` from the middle of the signature shifted every positional
  argument after it; three test call sites kept passing `"append"`, which
  silently became `ordering="append"` (an illegal `Ordering`) and behaved like
  `"sorted"` by luck. The call sites are fixed and the class now refuses the
  value that caused it.
* **Acceptance criterion 5 had no test.** Added: `query()` through
  `HostConfig(storage_path=...)` re-opened with a second stack (the durable
  round-trip), the `storage_path`-vs-explicit-triple refusal, and D10's
  unknown-URL-scheme rejection (asserting no file is created for the typo).
* **Doc drift from the removals.** `CONTEXT.md` (PluginSet / SurfaceSpec
  entries), `docs/reference/plugins.md` + its `zh` mirror (the `SurfaceSpec`
  block still taught `merge_rule` and never mentioned `activation_binding`;
  the `load_plugins` note had become self-referential), and an ADR addendum
  recording `merge_rule` → `activation_binding` + the two-process-channel rule.
* **`CHANGELOG.md` carried none of this commit's breaking removals** — only the
  carried session-rename. The full list is now under `[Unreleased]`.
* **`exec_plugin_file` / `find_builder` were typed `Any`** in a change whose
  theme is type discipline; they now name `ModuleType` / `str | Path`.

Judged and deliberately **not** changed: `OpenAICompatProvider(api_key="")`
now raises where it used to send an empty bearer token. Allowing an explicit
empty string back would re-open the silent-401 path D8 exists to close
(`api_key=os.environ.get("KEY", "")`), and a no-auth local endpoint can pass
any placeholder. The docstring says so explicitly instead.

AC4's exception list should also have named `examples/_internal/` — that
directory is contributor-facing by construction (`examples/_internal/README.md`
says so) and is not part of the "examples import only via `noeta.sdk`" rule.

Deviations from the plan as written, all deliberate:

* **D9 needed a module move.** `HostConfig.storage_path` cannot call
  `noeta.sdk.storage` (that module sits *above* `noeta.client`, and
  import-linter rejected the edge — verified, not assumed). The resolution
  implementation moved down to `noeta/client/storage_resolve.py`;
  `noeta.sdk.storage` re-exports it verbatim and stays the documented public
  doorway. Same shape as D2's `SdkMcpServer` move.
* **D11 replaced `merge_rule` rather than keeping it.** The field had no
  consumer and `collision_key` already determines append-vs-single, so it was
  decoration promising a mechanism the loader did not implement. Its slot on
  `SurfaceSpec` is now `activation_binding`, which *is* consumed — it drives
  the projection. The loud refusal moved from projection time to
  `SurfaceSpec.__post_init__` (earlier, before any plugin loads).
* **D16 delivered the drift-removal, not a full collaborator extraction.**
  The two engine-build paths now share one `_plugin_config` / one
  `_subagent_directory` (the real defect: two hand-assembled bags whose five
  differences were silent), the `object.__setattr__` ceremony is gone, and
  `Client` calls its own host's seams directly. Splitting `SdkHost` into
  `SandboxManager` / `McpResolver` / `EngineAssembler` objects was **not**
  done — see "Deferred" below.
* **Acceptance criterion 4 has two documented exceptions**, both symbols with
  no `noeta.sdk` home and both now carrying an in-file comment saying why:
  `SPAWN_SUBAGENT_TOOL` (control-tool wire name — runtime vocabulary, needed
  only to script a fake model) and `ToolCallApprovalResolvedPayload` (ledger
  payload type — `permission_gate.py` asserts on the recorded decision on
  purpose). `examples/crash_resume.py` is out of scope: it is a kernel
  durability demo, and its `Engine`/`fold` imports are its subject matter.

## Deferred (not done, deliberately)

* **`SdkHost` / `Client.__init__` collaborator extraction.** `SdkHost` is
  still ~81 fields and `Client.__init__` still runs its phases inline. The
  concrete hazards that motivated the split — the divergent `plugin_config`
  bags, the duplicated directory loop, the `getattr` probing of a
  just-constructed host — are fixed. What remains is structural churn across
  a 2200-line class whose byte-identity is pinned by goldens; doing it
  half-way would be worse than not starting, and it deserves its own spec.
* **An async API surface** (out of scope from the start — a product-direction
  call, not a defect).

## Note for review

`packages/noeta-sdk/noeta/client/host.py` carries ~27 lines of unrelated
whitespace churn: `ruff format` was run on it to fix continuation indents, and
this repo is not `ruff format`-managed (12 of 19 `client/` files would be
reformatted by it). `ruff check` — the actual gate — passes.

## Goal

Close out the full SDK-layer review of 2026-07-30: restore type discipline on
the public surface, make the identity-vs-wiring split real at the `Options`
layer, fulfil the "surface-agnostic loader" promise in the plugin projection
layer, fix the onboarding/DX gaps (docs teaching non-public imports, `query()`
excluding durability, missing ergonomics), fix the periphery inconsistencies,
and split the two god objects (`SdkHost`, `Client.__init__`) into internal
collaborators without changing any public surface.

## Scope

In scope: `packages/noeta-sdk/noeta/{client,sdk,builtins/providers}`, the SDK
README / `docs/tutorials` / `examples/`, and the tests pinned to the changed
surfaces.

Out of scope (deliberate):

* An **async API surface** — a product-direction decision, not a defect fix;
  the sync surface stays canonical for now.
* Changing the `write_mode="dry_run"` default — a safe default that stays;
  only its discoverability is improved (docs).
* The noeta-agent product sweep — tracked separately with the other pending
  breaking-change sweeps (deleted parts accessors, `load_plugins` rename
  below, etc.).

## Key decisions

* **D1 — `Options` equality = identity-relevant fields.** Every field the
  docstring already declares "excluded from identity" (`provider`, `cwd`,
  `can_use_tool`, `output_schema`, `thinking`, `effort`, `guards`,
  `observers`, `content_channels`, `metadata`, `model`) becomes
  `field(compare=False)` with its **real type** (`cwd: str | Path | None`,
  `can_use_tool: Callable[[str, dict[str, Any]], bool] | None`, …). The
  class docstring stops claiming hashability (`Options` holds mapping-valued
  fields and is not hashable; frozen is for immutability).
* **D2 — `SdkMcpServer` moves down.** `SdkMcpServer` +
  `create_sdk_mcp_server` relocate to `noeta/client/mcp_server.py` (they
  depend only on `noeta.tools.decorator`); `noeta.sdk.authoring` re-exports
  them (same pattern as `@tool`). `Options.mcp_servers` is then typed
  `tuple[SdkMcpServer, ...]` and the `.tools` duck-typing in
  `options.py` / `client.py` is replaced with the real attribute.
* **D3 — `PolicyFactory` Protocol.** Defined in `noeta.client.options`
  (`__call__(llm) -> Policy` + `ref: ComponentRef`); used by
  `Options.policy`, `PluginActivation.policy`, and the resolver helpers.
  Runtime validation in `_resolve_policy_ref` stays (a Protocol does not
  replace the loud compile-time check).
* **D4 — typed `Client` verbs.** The one-call verbs return `DriveOutcome`,
  the `seed_*` verbs return `SeededTurn`, `drive_seeded(seeded: SeededTurn)
  -> DriveOutcome` (both types are importable from `noeta.execution.driver`,
  a lower band). `delete_task` returns a `TypedDict` (runtime shape
  unchanged).
* **D5 — the mode enums are public vocabulary.** `_EFFORT_MODES` /
  `_PERMISSION_MODES` → `EFFORT_MODES` / `PERMISSION_MODES` (no
  back-compat aliases); `noeta.client.capabilities` imports the public
  names.
* **D6 — `query()` gains `host_config`**, so the sugar path can be durable.
* **D7 — client ergonomics.** `Client` becomes a context manager
  (`__exit__` → `shutdown()`); `workspace_dir` defaults to `Path.cwd()`
  (aligning with the `SdkHost` field default — the hard error goes);
  `QueryResult` gets a compact `__repr__`.
* **D8 — provider env-var fallback.** `AnthropicProvider` reads
  `ANTHROPIC_API_KEY`, `OpenAICompatProvider` reads `OPENAI_API_KEY` when
  `api_key` is not passed; a missing key still fails loudly at construction,
  naming both the parameter and the env var.
* **D9 — `HostConfig(storage_path=...)`.** One-string durable storage:
  `storage_triple()` resolves it through
  `noeta.sdk.storage.open_storage_stack`; mutually exclusive with the
  explicit triple (loud error).
* **D10 — `open_storage_stack` rejects unknown `://` schemes** instead of
  silently treating a typo'd DSN as a sqlite file path.
* **D11 — table-driven projection.** `PluginSet.identity_activations` /
  `process_hooks` dispatch off `SurfaceSpec` metadata (`plane` /
  `activation_scope`) instead of hardcoded surface-name chains, so a
  host-registered surface projects without editing the loader. The loud
  refusal for an unprojectable identity surface stays. If a declared field
  (`merge_rule`) still has no consumer after this, it is deleted rather than
  left as decoration.
* **D12 — loader hygiene.** One shared `split_ref` / `find_builder` /
  plugin-file exec helper in `plugin_manifest`; `_manifest_from_table`
  enforces `(surface, name)` uniqueness (parity with `PluginBuilder`);
  `plugin_check` normalizes default-valued ordering params (`priority == 0`,
  empty `seams`) before diffing; the public loader name is **`load_plugins`**
  (the `load_plugin_set` alias export is dropped — noeta-agent sweep notes
  it); stale milestone comments updated to past tense.
* **D13 — periphery.** `consolidation.py` types its client surface as a
  narrow Protocol (naming the two private members it deliberately uses);
  `wire.envelope_to_dict` iterates `dataclasses.fields`; `messages.py`
  drops the false `ARG001` noqa, merges the mirrored block-walk branches,
  converts payload-narrowing asserts to raises, and logs the swallowed
  content-store failure; `otlp.py` drops the dead `C901` noqa; `sandbox.py`
  reconciles the `BrowserBackend` docstring with its `Any` reality and uses
  `collections.abc.Callable`/`Mapping`.
* **D14 — compat removals.** `parts.BUILTIN_TOOL_CLASSES` module
  `__getattr__` deleted; callers (tests, examples, builtins docstrings) use
  `builtin_tool_classes()`. `options._BUILTIN_ACTIVATIONS` alias deleted.
* **D15 — preset registry injection.** `compile_options` (and
  `effective_root_policy` where needed) accept an optional
  `preset_prompts: Mapping[str, str]` that defaults to the module registry;
  `register_preset_prompt` keeps its documented last-writer-wins global
  behaviour.
* **D16 — god-object split, internal only.** `SdkHost` extracts
  collaborators (provider registry fold/lookup, MCP client staging +
  resolution, engine assembly with ONE shared `plugin_config`/directory
  builder for the session and orchestration paths) and drops the
  `object.__setattr__` ceremony (it is not frozen); `Client.__init__`
  extracts the activation-surface folding and construction phases into
  module-level helpers. **No public field, method, or behaviour changes**;
  the existing goldens pin byte-identity. `Client` calls its own host's
  seams directly (no `getattr` duck-typing against a concrete class it just
  constructed).

## Acceptance criteria

1. `make check` green (run unpiped), including import-linter — no new
   contract violations, mypy not worse than baseline.
2. `Options(system_prompt="x") == Options(system_prompt="x", cwd=...)` is
   `True`; no `object`-typed fields remain on `Options`.
3. No `type: ignore` at the former `client.py` can_use_tool assignment or
   `host_config.py` storage_triple return.
4. Every example and tutorial imports the SDK only via `noeta.sdk`
   (grep-clean for `from noeta.client`, `from noeta.protocols`,
   `from noeta.testing` under `examples/` + `docs/tutorials/`).
5. `query(..., host_config=HostConfig(storage_path=...))` round-trips a
   durable sqlite session.
6. A host-registered custom identity surface projects through
   `identity_activations` without editing `plugin_set.py` (new unit test).
7. `SdkHost` / `Client` public surfaces unchanged (existing tests pass
   unmodified except where they referenced deleted compat names).
