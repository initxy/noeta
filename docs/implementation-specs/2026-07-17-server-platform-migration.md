# Server-platform migration — adopt the downstream multi-user app as the official product

## Goal

Replace the current single-user application layer (`apps/noeta-agent` backend +
`apps/web` frontend) with the generic core of the downstream multi-user agent
service, stripped of every vendor-internal integration. The governing decision
is `docs/adr/server-platform-product.md`; this spec holds the sequencing, the
per-file port/adapt/drop inventory, and the open-source hygiene rules.

The source tree is the maintainer's local checkout of the downstream repo,
referred to below as `dot-agent/`. It depends on `noeta-sdk==0.2.11` from PyPI
(no fork, no vendored runtime), so the import is a workspace-dependency switch,
not a version migration.

## Non-goals

- **No runtime/sdk behavior changes** beyond the boundary-compliance items in
  D8 (each of which is a promotion or a bug fix, not a redesign).
- **No horizontal scaling work.** v1 is single-process single-instance;
  Postgres-backed multi-instance is future work and only gets a documented
  limitation.
- **No i18n framework.** UI copy is written in English directly; a translation
  layer is a later product decision.
- **No new features** during the migration. Parity with the stripped source
  plus the back-ported items in D9 is the finish line.

## Context

- The downstream backend is FastAPI + SQLite (application DB with 24 tables) +
  an embedded `noeta.sdk.Client` host (worker pool, envelope→UI-event
  translator, `since_seq` SSE replay, per-session workspace + sandbox). The
  frontend is ~22k LOC React 19 / TypeScript / Tailwind 4 with no router and
  no state library.
- Internal coupling is concentrated: one auth-provider module holds every
  vendor endpoint; sync adapters, an internal skill market, an internal
  code-hosting client, and a CLI-credential exec preamble make up the rest.
  The generic core (host, translator, stores, APIs, workflow, admin, tests) is
  cleanly separable.
- The downstream test suite (57 files) runs against a real uvicorn on a random
  port with a deterministic fake LLM, no sandbox, and no external credentials —
  it is the migration's primary safety net and ports along with the code.
- The downstream repo reaches past `noeta.sdk` into runtime internals in a
  handful of places and monkey-patches one builtin tool; in-repo these become
  either public-surface promotions or pinned ratchet exemptions (D8).

## Decisions

### D1 — Layout and naming: same dist, same entrypoint, new contents

- Dist name `noeta-agent`, directory `apps/noeta-agent`, and entrypoint
  `python -m noeta.agent` are all retained. Version target: **0.3.0** (minor —
  the product form changes).
- The downstream Python package root `app.*` is renamed to `noeta.agent.*`:

  | Source (`dot-agent/backend/`) | Target (`apps/noeta-agent/`) |
  |---|---|
  | `app/agent/` (host, providers, sandbox, translator) | `noeta/agent/host/` |
  | `app/api/` | `noeta/agent/api/` |
  | `app/auth/` | `noeta/agent/auth/` |
  | `app/store/` | `noeta/agent/store/` |
  | `app/services/` | `noeta/agent/services/` |
  | `app/workflow/` | `noeta/agent/workflow/` |
  | `app/main.py`, `app/config.py`, `app/config_registry.py`, `app/models_config.py` | `noeta/agent/` package root (+ `__main__.py`) |
  | `sync/knowledge/` (kept parts) | `noeta/agent/services/knowledge/` |

  PEP 420 constraint: `noeta.agent.spec` and `noeta.agent.registry` are
  published by noeta-runtime (the identity layer) — the mapping above has no
  collisions and must stay collision-free.
- The downstream `frontend/` replaces `apps/web/` in place (same directory
  name, wheel force-include mechanism unchanged).

### D2 — Fresh import, scrubbed

- Code is imported as new files; **no git history migration** (the downstream
  history contains internal hostnames and data).
- Never copy: `dot-agent/backend/data/` (live workspaces, token directories),
  `dot-agent/vendor/` (internal CLI binary), any `.env`, `dot-agent/docs/`
  (internal ADRs/specs stay downstream; noeta writes its own).
- Scrub gate (CI-able): over the imported tree,
  `git grep -iE 'byted|bytedance|feishu|larkoffice|larksuite|titan|agentbuddy|bytecloud|modelhub|super-relay|lark-cli|bytedcli'`
  returns **zero hits**.

### D3 — Auth: a provider seam with a dev-login reference implementation

- Keep: the request-auth dependency chain and signed session cookie
  (`app/auth/deps.py`, `app/auth/cookies.py`), the dev-login endpoints, the
  admin allowlist.
- Replace: `app/auth/provider.py` (the vendor-endpoint chokepoint) with an
  `AuthProvider` protocol owned by the app; ship `DevLoginProvider` only.
  Document the seam so real deployments can plug in OIDC/SSO.
- Drop: `app/auth/titan.py`, `app/api/external_auth.py`, external-token
  storage (`app/store/lark_tokens.py`).

### D4 — LLM providers: generic gateways only

- Keep: the provider-build seam (`app/agent/providers.py`) targeting
  `noeta.sdk.providers` adapters (openai-compatible / openai-responses /
  anthropic) configured by `base_url` + `api_key`; the `RoutingProvider`
  per-model multi-gateway mechanism; the fake-LLM offline mode.
- Replace: internal gateway defaults and hostnames with neutral config;
  `models.json` with OpenAI/Anthropic example entries; the vendor
  session-header transform with a configurable header hook.
- Adapt: the fake LLM's canned demo script from the tracking domain to a
  generic product demo.

### D5 — Knowledge: pluggable sync sources, no domain-derived layer

- Keep: the knowledge-source model, store, API, sync manager scaffolding, the
  citation resolver (footnote refs + existence/anchor verification), and the
  read-only sandbox mount.
- Replace source types: `git_repo` (clone URL + token) and `local_dir`
  (uploaded/managed directory) instead of the vendor wiki and internal
  code-hosting adapters (`sync/knowledge/wiki_export.py`,
  `sync/knowledge/repo_sync.py` rewritten to plain `git`).
- Drop: the tracking-domain derived retrieval layer
  (`sync/knowledge/event_catalog.py`, `point_library.py`, `point_table.py`,
  `doc_summary.py`, `derived.py`, `scripts/rebuild_derived_layer.py`) and its
  API/UI surface (`rebuild-derived`, wiki browse).

### D6 — Sandbox: stock image, no credential preamble

- Keep: the sandbox provider (knowledge/skills read-only mounts), the docker
  lifecycle module, the `agent-sandbox` SDK adapters, the live-preview reverse
  proxy, and the `BoundPreamble` seam itself.
- Replace: `sandbox.Dockerfile` with the stock AIO sandbox image (no
  preinstalled internal CLIs).
- Drop: the exec-preamble credential injection block (vendor CLI env exports
  and token bootstrap in `app/agent/service.py`) and the related
  command-detection helpers.
- The no-sandbox degraded mode (shell disabled) stays, as the zero-dependency
  evaluation and test path.

### D7 — Language and open-source style

- Everything committed is **English**: code comments, docstrings, identifiers,
  log/error messages, UI copy, and docs. A file is not "ported" until its
  comments and strings are translated — translation happens at import time,
  not as a later pass.
- Follow this repo's gates and conventions: `make check` (CONTRIBUTING.md),
  import-linter contracts, existing naming per `CONTEXT.md`. Frontend keeps
  its oxlint + Vitest setup; enabling TypeScript `strict` is an explicit task
  in M2 (the code is written strict-style; the compiler flag is not yet on).
- No internal platform names anywhere, including in docs and examples (D2
  scrub gate applies to the whole imported tree, tests and fixtures included).

### D8 — Boundary compliance: promote or pin every runtime-internal import

The downstream code imports past `noeta.sdk`. Disposition, item by item:

| Downstream usage | Disposition |
|---|---|
| UTF-8 lenient read monkey-patch of the builtin `read` tool | Fix in noeta-runtime properly (+ regression test); delete the patch |
| `noeta.testing.fake_llm.FakeLLMProvider` | Promote: export via `noeta.sdk.testing` |
| `noeta.protocols.messages` / `tool` / `wake` / `canonical` (translator, mock) | Promote the needed types onto `noeta.sdk` re-exports where they are already public-shaped; otherwise pin |
| `noeta.storage.sqlite` | Already an allowed host-wiring exemption; pin |
| `noeta.tools.fs.exec_env`, `noeta.tools.browser` (sandbox adapters) | Same regime as the retired app: pinned per-module ratchet exemptions |
| `noeta.tools.fs.read`, `fs._workspace`, `memory.MemoryStore`, `_invocation`, `_limits` | Case-by-case: promote a public equivalent or refactor the call site; pin only as a last resort |
| `noeta.providers.catalog` (model specs for custom gateways) | Promote a read-only catalog accessor onto `noeta.sdk` |
| `noeta.presets.with_consolidation_agent` | Promote via `noeta.sdk` presets surface |

The `app-uses-only-sdk` ratchet is re-pinned to the new module layout with the
final exemption list; the list may only shrink. `backend-only-sdk` is
re-scoped to the API/transport modules (`noeta.agent.api`), which must have
zero exemptions.

### D9 — Back-ports from the retired app

Before deleting the old `apps/noeta-agent` + `apps/web`:

- **Diff-merge shared host modules.** The downstream copies of
  `docker_sandbox.py`, `sdk_sandbox_exec_env.py`, `sdk_browser_backend.py`,
  and `preview_ws.py` originated in the old app; diff against the old-app
  versions and fold in any fixes that landed there after the copy.
- **Port onto the platform** (as M4 tasks, after the swap):
  1. MCP connector management — re-scoped from the global `~/.noeta` registry
     to per-space configuration (store + API + UI page).
  2. Composer image input (`put_content` → `ContentRef` → `ImageBlock`,
     read-back via the content endpoint).
  3. App preview gateway (single-port, token-prefixed) — optional; the
     sandbox live-preview panels already cover browser/terminal/editor.
- **Rebuild e2e** on the new protocol reusing the old scenario list (approval,
  question/answer, scrolling, session management, toasts); the old Playwright
  specs themselves do not port.
- Delete with the old app: its backend tests (`tests/test_backend_*`,
  preview/MCP/sandbox host tests that are superseded by ported equivalents)
  and the old web unit/e2e suites.

### D10 — Collaboration layer stays gated off

The channels + task-board code (backend services/stores/APIs and frontend
pages) is generic and ports, but remains behind its existing feature gate
(default off). Turning it on is a product decision outside this migration.

### D11 — Storage and deployment story

- Application DB: SQLite file, as downstream. Engine storage: the sdk host
  storage seam (SQLite by default). Postgres remains a documented future
  option, not wired in v1.
- Ship a `docker-compose` example (app + sandbox image + data volume) and a
  zero-credential quickstart (mock provider, dev-login, sandbox off).

## Milestones

- **M1 — Backend import + strip.** Apply D1–D6 and D10 to the backend and its
  test suite; the ported suite is green offline (`mock` provider, sandbox
  disabled); D2 scrub gate passes on the imported tree. The old app still
  exists and still passes its tests (no interleaving).
- **M2 — Frontend import + old-layer deletion.** Import the SPA per the
  frontend inventory; English copy; enable `strict`; wire the wheel
  force-include; then execute D9's diff-merge and delete the old backend,
  frontend, and their tests. `make check` green at the end of M2.
- **M3 — Boundary compliance.** Execute D8; import-linter green with the
  re-pinned ratchet; the runtime UTF-8 fix lands with its own test.
- **M4 — Back-ports.** D9 items 1–3 plus the e2e rebuild.
- **M5 — Docs + release.** Rewrite `CONTEXT.md`'s product section and the
  README narrative; deployment story per D11; release 0.3.0 per
  `docs/releasing.md`.

Each milestone is a separate PR (or small PR series) ending in a green
`make check`; M1 and M2 are the only window where two app layers coexist in
the tree, and no release is cut inside that window.

## Inventory — backend (`dot-agent/backend/`)

**Port as-is** (rename + comment translation only):
`app/main.py` (minus dropped routers), `app/config_registry.py`,
`app/models_config.py`, `app/agent/{service.py*, translator.py,
routing_provider.py, title.py, dot_sandbox_provider.py, docker_sandbox.py,
sdk_sandbox_exec_env.py, sdk_browser_backend.py, sandbox_preview.py,
preview_ws.py, board_tools.py, channel_tools.py, workspace_files.py}`,
`app/api/{auth.py, sessions.py, spaces.py, skills.py, space_skills.py,
templates.py, channels.py, board.py, memories.py, feedback.py, admin.py,
misc.py}`, `app/auth/{deps.py, cookies.py}`, `app/store/{sessions.py,
spaces.py, users.py, skills.py, templates.py, knowledge.py, channels.py,
board.py, feedback.py, agent_config.py, app_config.py}`,
`app/services/{knowledge_resolve.py, channels.py}`, `app/workflow/*`,
`tests/*` (minus tests of dropped integrations).
(*`service.py` minus the exec-preamble block per D6.)

**Adapt**: `app/config.py` (drop internal knobs/hosts), `app/agent/providers.py`
and `models.json` (D4), `app/agent/mock_llm.py` (generic demo script),
`app/services/knowledge_sync.py` + `sync/knowledge/repo_sync.py` (D5),
`app/services/{feedback_reference.py, feedback_report.py}` (report export as
markdown instead of a vendor doc), `sandbox.Dockerfile` (D6),
`app/agent/feedback_analysis.py` (strip vendor references).

**Drop**: `app/auth/{titan.py, provider.py→replaced}`,
`app/api/{external_auth.py, codebase.py, agentbuddy_skills.py, lark.py,
lark_chat.py}`, `app/services/{codebase.py, agentbuddy.py, lark_doc_meta.py,
lark_group.py, lark_events.py, user_search.py}`,
`app/store/{lark_tokens.py, lark_chat.py, agentbuddy_skills.py}`,
`app/tools/lark_users.py`, `sync/knowledge/{wiki_export.py, event_catalog.py,
point_library.py, point_table.py, doc_summary.py, derived.py}`,
`scripts/rebuild_derived_layer.py`, `app/flow/` (dead; stale bytecode only),
`vendor/`, `sandbox.Dockerfile` internal preinstalls.

## Inventory — frontend (`dot-agent/frontend/`)

**Port as-is**: the chat/streaming engine (`chat/`, `api/{client,sse}.ts`),
conversation rendering, composer + model selector, question cards, todo strip,
side panel (files + sandbox preview), trace viewer (`components/trace/`),
spaces UI, skills page (minus market tab), templates, memories, feedback,
admin console, theming/toasts, all `lib/` helpers except the vendor-doc ones,
the Vitest suites.

**Adapt**: `api/endpoints.ts` + `api/types.ts` (remove dropped API groups),
`KnowledgePage.tsx` (rebase on `git_repo`/`local_dir` sources),
`chat/useCitations.ts` + `Citations.tsx` (generic path resolution),
`LoginPage.tsx` (dev-login only), `UserSettingsPage.tsx` (drop external-auth
connect), `SkillsPage.tsx` (drop market tab), all UI copy → English,
`index.html` (`lang="en"`, product title).

**Drop**: `lib/feishu.ts`, `lib/docTitle.ts`, `components/{FeishuCard.tsx,
LarkAuthBanner.tsx, SourceDocsSlideOver.tsx, DocTree.tsx}`,
`state/externalAuth.ts`, the `agentBuddyApi`/`codebaseApi`/`larkApi`/
`larkChatApi` endpoint groups and their call sites.

## Acceptance criteria

1. `make check` green; the ported backend suite green fully offline (mock
   provider, no sandbox, no credentials).
2. D2 scrub grep returns zero hits over `apps/`.
3. import-linter green: `app-uses-only-sdk` re-pinned (shrink-only list),
   `backend-only-sdk` zero-exemption on the API/transport modules.
4. `python -m noeta.agent` boots the platform, serves the SPA; dev-login → new
   session → agent turn with skill activation and file output works end-to-end
   in mock mode.
5. SSE reconnect with `since_seq` replays without duplication or loss; token
   deltas are never replayed.
6. The repo contains no Chinese (or other non-English) comments, strings, or
   docs in the imported tree.
7. `CONTEXT.md` and README describe the platform; release 0.3.0 cut per
   `docs/releasing.md`.

## Risks

- **Translation volume.** ~22k LOC of frontend with Chinese copy and dense
  Chinese comments; budget it per-file inside M1/M2 rather than as a cleanup
  pass (D7 makes it a port gate).
- **Silent behavior drift in diff-merged host modules** (D9): mitigated by
  porting the old app's corresponding tests where they still apply.
- **Docker as the default execution path** may be a first-run hurdle for
  open-source users; mitigated by the zero-credential mock quickstart (D11).
- **Ratchet pressure** (D8): promotions onto `noeta.sdk` widen the public
  surface and deserve individual review; do not batch them mechanically.
