# Repo split: this repo becomes SDK-only; the agent product moves out

> **Status: Active**

Companion to
[2026-07-25-plugin-architecture.md](2026-07-25-plugin-architecture.md) (the
mechanism this split depends on). Durable plugin decisions live in
[plugin-contribution-bundles.md](../adr/plugin-contribution-bundles.md).

## Goal

Move the agent product (`apps/noeta-agent` + `apps/web`) into its own
repository at `/data00/home/xiyang.dai/Documents/noeta-agent`, leaving this
repo as the SDK-only platform (noeta-runtime + noeta-sdk wheels) — executing
the move only after the SDK contract (plugin mechanism, public-surface audit,
reference host) has shipped, so the new repo consumes the published contract
from day one.

## Non-goals

- No merging of the runtime and sdk wheels (package-layout stands; a separate
  decision if ever).
- No renaming: distribution `noeta-agent` and import path `noeta.agent` are
  kept (the `noeta.*` namespace already spans separately-installed wheels).
- No app rearchitecture during the move — move first, evolve there (the
  presets/plugins consumption work is the new repo's own spec).
- No deployment-data migration; storage schemas are owned by runtime/sdk and
  unchanged.

## Context

Decided 2026-07-26, superseding the 2026-07-25 layout decision to keep the
product in-repo. The plugin design clarified the relationship: the SDK is the
platform contract; the agent product is one host among several (noeta-agent,
noeta-workspace, third parties). Splitting makes the contract honest — the
product may only use what pip installs. The cost (cross-repo coordination) is
mitigated by contract-first sequencing, a reference host in this repo, and an
editable-install dev workflow.

## Decisions

- **D1 — Destination & history.** New git repo at
  `/data00/home/xiyang.dai/Documents/noeta-agent`, seeded from a fresh clone
  via `git filter-repo` restricted to `apps/noeta-agent` + `apps/web` (+ their
  doc paths), so file history follows. This repo then deletes `apps/` going
  forward; its own history stays intact.
- **D2 — Naming & versioning.** Distribution `noeta-agent`, import
  `noeta.agent`, unchanged. First post-split release is 0.4.0 from the new
  repo's CI, pinned `noeta-sdk>=0.4,<0.5`. The sdk follows semver with a
  one-minor deprecation window.
- **D3 — Contract-first gate (hard).** Surgery starts only after noeta-sdk
  0.4.0 (plugin mechanism M1 + public-surface audit + reference host) is on
  PyPI. Rationale: splitting first would pin internal paths and force a second
  migration.
- **D4 — Import discipline.** The new repo imports only the `noeta.sdk` public
  surface — enforced there by import-linter; enforced here by a
  public-surface completeness test. Gaps found during the audit (sqlite
  storage triple, `EventEnvelope` wire contract, streaming/sandbox types) are
  closed by re-export through `noeta.sdk`, never by blessing internal paths.
  The one standing exception predates this split and keeps its own ADR: the
  two SDK sandbox-adapter modules (`sdk_sandbox_exec_env` /
  `sdk_browser_backend`) extend the **concrete** AIO adapters, which are held
  off the public surface on purpose (execution-environment-seam ADR, "SDK-adapter
  export surface"). They stay pinned `ignore_imports` entries in the new repo's
  contract — scoped to exactly those two importer→module pairs — rather than
  turning a retirement-slated implementation into user-facing API. Any *other*
  gap is closed by re-export.
- **D5 — Reference host.** A minimal host (`examples/reference-host/`) stays
  in this repo: sqlite triple + streaming sink + plugin loading. It is the
  contract-test executor, the host-builder tutorial, and the app's stand-in as
  integration bed.
- **D6 — Docs split.** App-layer ADRs (server-platform-product,
  token-streaming-projection, web-task-creation, web-file-panel-and-app-preview,
  web-image-attach) are copied to the new repo; the originals get
  `> **Status:**` pointer blockquotes and are never deleted. CONTEXT.md's
  app-layer terms (Space, UI event, Skill registry, Knowledge source, MCP
  connector, Agent-config, Feedback loop) move to the new repo's CONTEXT.md;
  library terms stay. Product docs pages and their `zh/` mirrors move;
  each repo keeps its own `releasing.md`; the new repo gets its own
  AGENTS.md / CLAUDE.md seeded from this repo's working agreement.
- **D7 — CI & release.** The agent publish job leaves this repo's
  release.yml; the new repo gets its own workflow (web build + wheel publish,
  reusing the tag-version gating pattern).
- **D8 — Dev workflow.** Cross-repo development uses an editable install of
  this repo (`uv pip install -e ../noeta` style), documented in the new repo's
  CONTRIBUTING; seam changes are batched to limit coordinated releases.
- **D9 — Local secrets.** The real gateway `.env` is copied by hand to the new
  project and stays uncommitted in both.
- **D10 — Follow-on ownership.** Plugin-architecture M3 (presets + runtime
  plugins consumption) and M4 (app-plugin plane: routers, channels, scheduled
  triggers, sandbox-provider selection) execute in the new repo under its own
  spec; `feishu-channel` remains the app plane's external validation target.

## Plan

- [x] **Phase 0 — close the in-flight tree.** Land the uncommitted work
      (presets→sdk move, doc revisions); patch-release if warranted.
- [x] **Phase 1 — contract (the gate).** In this repo: plugin M1 + M2 (per the
      plugin spec) + public-surface audit + reference host + host-builder
      docs; 0.4.0 bumped locally (owner directive: no tag / no push /
      no PyPI yet — the new repo consumes an editable local install instead).
- [x] **Phase 2 — surgery.**
  - [x] Extract `apps/` history into the new repo (filter-repo on a fresh
        clone; paths rehomed apps/noeta-agent→/, apps/web→web/; merged into
        the owner's pre-seeded harness repo, no push).
  - [x] New repo bring-up: standalone pyproject with editable path sources to
        this repo, import-linter (`noeta.sdk` only), CI + release workflow,
        AGENTS.md verbs / CONTEXT.md / docs / `.env`; internal imports
        rewritten to the public surface.
  - [x] This repo: remove `apps/`, workspace refs, make targets, agent publish
        job; migrate CONTEXT.md terms; prune docs and `zh/` mirrors; sweep
        dangling links. **D6 deviation (owner directive):** app ADRs were
        *moved*, not stub-pointered — the five files live in the new repo's
        `docs/adr/` and the surviving ADRs' inline references were annotated
        "(now in the noeta-agent repository)".
  - [x] Verify both sides: this repo `make check` green; new repo `make
        check` green against the **locally installed** sdk (PyPI consumption
        deferred until the owner publishes 0.4.0).
- [ ] **Phase 3 — new repo's own spec** for M3/M4 (its
      `docs/specs/2026-07-26-migrate-app-from-monorepo.md` records the
      migration; M3/M4 remain queued there).

## Acceptance criteria

- [ ] New repo builds, tests, and runs from PyPI noeta-sdk only; a full
      session with streaming works on a dev instance. **Open** — it consumes
      editable path sources until 0.4.0 is published.
- [x] `git log --follow` shows pre-split history for moved files in the new
      repo (verified 2026-07-28 on `noeta/agent/main.py`).
- [x] This repo contains no `apps/`, no agent publish job, no dangling doc
      links; CONTEXT.md term migration in place (ADRs were *moved*, not
      stub-pointered — the D6 deviation recorded below).
- [x] import-linter in the new repo proves `noeta.sdk`-only imports (with the
      two D4 sandbox-adapter exemptions); the public-surface completeness test
      here pins what a host binds to and keeps the reference host + first-party
      plugins on public paths.
- [ ] noeta-agent 0.4.0 published from the new repo; this repo's release.yml
      publishes runtime+sdk only. **Open** — publishing is the maintainer's
      call; release.yml is already runtime+sdk only.
- [x] `make check` green in both repos (re-verified 2026-07-28: this repo
      3,2xx tests + 12 contracts; new repo 333 pytest + 101 vitest + 1
      contract).

## Risks

- **Cross-repo friction**: seam changes now need coordinated releases —
  mitigated by the editable-install workflow and batching; accepted cost of
  the split.
- **Doc link rot**, doubled by the `zh/` mirrors — mitigated by a link sweep
  in Phase 2 and the vitepress config split.
- **filter-repo mistakes** — operate on a fresh clone only; verify file counts
  and `--follow` history before pushing.
- **Namespace packaging**: `noeta.agent` must keep working beside pip-installed
  `noeta.runtime`/`noeta.sdk` — already proven by the existing two-wheel
  layout, but re-verified in Phase 2 bring-up.
- **Contract gaps discovered late**: anything the app needs that the audit
  missed shows up as an import-linter failure in Phase 2 — fix by re-export
  and a sdk patch release, never by blessing an internal path.

## Progress log

- 2026-07-26 — Spec created; direction confirmed by the owner (destination
  path fixed). Plugin spec revised the same day to hand M3/M4 to the new
  repo; ADR amended (app plane owned by each host). No surgery started.
- 2026-07-26 (later) — Phases 0–2 executed. Notable deltas vs. the plan:
  the owner had pre-seeded the destination with an init-harness skeleton, so
  the extraction was **merged** into it (`--allow-unrelated-histories`)
  rather than seeding a fresh repo; 0.4.0 is bumped locally only (no tag /
  push / publish per owner directive), so the new repo consumes editable
  path sources (`[tool.uv.sources]`) until a real release; app ADRs moved
  without stub pointers (owner: "as if the agent never existed"), inline
  references annotated instead. Friction log: two opus subagents finished
  their work but failed to emit structured reports (result junk / retry-cap
  errors) — disk state + tests were treated as the source of truth and
  verified independently; the app-coupled tests left in this repo
  (docker-sandbox/browser-backend/exec-env, public-surface scanner,
  server-boot install smoke) were removed or rewritten for the two-wheel
  world. Both gates green: this repo `make check` exit 0 (3,20x root tests +
  mypy + import-linter + naming lint), new repo `make check` green (333
  pytest + 101 vitest + lint-imports).
- 2026-07-28 — Review pass; two split-side gaps closed. (a) The public-surface
  scanner had been deleted with the app it scanned, leaving D4's "enforced here"
  half unimplemented (and the 0.4.0 changelog claiming a file that no longer
  existed). `tests/test_public_surface.py` is back in a split-independent
  form: it pins the paths and symbols a host binds to, asserts
  `noeta.sdk.storage` is a zero-logic re-export, and scans the host-contract
  examples (reference host + first-party plugins) for any non-public `noeta.*`
  import. That scan immediately found a real gap — the `ProposedAction` members
  were not public, so both example guards imported `noeta.protocols.hooks`;
  closed by re-export per D4. (b) The two sandbox-adapter `ignore_imports`
  entries in the new repo were a silent deviation from D4's absolute wording;
  D4 now records them as the standing, ADR-scoped exception they are.
