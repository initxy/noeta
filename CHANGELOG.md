# Changelog

All notable changes to Noeta are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Noeta is pre-1.0: while on `0.x`, minor versions may carry breaking changes.

## [Unreleased]

## [0.1.5] - 2026-07-05

### Changed

- `psycopg[binary]` is now a regular dependency of `noeta-runtime` (the
  `postgres` extra is gone): the Postgres storage backend works out of the
  box, with no system libpq required. Installs that used
  `noeta-runtime[postgres]` keep working — the extra name is simply ignored.

## [0.1.4] - 2026-07-05

### Added

- PostgreSQL storage backend: `noeta.storage.postgres` ships psycopg-backed
  `PostgresEventLog` / `PostgresContentStore` / `PostgresDispatcher` (plus the
  inspect-only `PostgresReadOnlyStore`), behaviour-pinned by the same
  storage-backend-neutral contract suites as the sqlite adapters. Install the
  optional extra `noeta-runtime[postgres]`; the core wheel stays psycopg-free.
- Durable storage is now configured by a **storage URL**: a sqlite file path
  or a `postgresql://` DSN, via `NOETA_AGENT_STORAGE` / config key
  `storage_url` (`noeta.agent.host.storage.open_durable_storage` dispatches;
  `noeta.storage.stacks.open_storage_stack` accepts the same shapes in-process).

### Changed

- Config spelling: `storage_url` / `NOETA_AGENT_STORAGE` replaces
  `sqlite_path` / `NOETA_AGENT_SQLITE` as the documented storage setting; the
  legacy spellings remain accepted with unchanged semantics.

## [0.1.3] - 2026-07-02

### Added

- New observational `LLMRetryScheduled` event: the runtime records each
  scheduled transient-retry backoff (call_id, attempt, delay, category,
  truncated error) so the web chat shows "Provider error — retrying (n/m)"
  in the composing indicator, status text, and a per-call timeline marker
  instead of stalling silently. Fold-inert (no state slice changes); the
  request/response event trio still fires exactly once per logical request.
- `spawn_subagent` batch form: `spawns: [{agent, goal}, ...]` fans out N
  subtasks from ONE tool call (SR2 parallel execution). Models that never
  emit two spawn calls in a single turn can now actually run delegations
  in parallel; a single-entry batch stays on the sequential SR1 path.
- `AnthropicProvider` implements `complete_with_headers`, so the runtime
  can attach request-scoped HTTP headers (e.g. a per-task trace id)
  without rebuilding the shared client. Transport-only — headers do not
  affect prompt-cache hits.

### Changed

- Transient LLM retry budget raised from 5 to 8 attempts (max backoff wait
  ~31s → ~2min), so a sustained 429 rate limit gets a real recovery window.

### Fixed

- Subtasks now inherit the parent session's model binding: a child agent
  without its own declared default model runs on the root parent's bound
  model (recorded as the child's opening `ModelBound`, identity
  `"inherited"`) instead of silently dropping to the host default model.
- OpenAI Responses subagents no longer lose the provider prompt cache:
  `include: [reasoning.encrypted_content]` is requested independent of the
  reasoning-effort gate, an empty reasoning echo is skipped, and children
  inherit the parent's per-turn effort.
- Web trace page: clicking a subagent in the TaskTree switches the
  inspected task without reconnecting the SSE stream; only navigating
  outside the current subtree re-roots it.

## [0.1.2] - 2026-07-02

### Fixed

- Cross-package dependencies now carry lockstep `>=` lower bounds
  (`noeta-sdk` → `noeta-runtime>=X.Y.Z`; `noeta-agent` → both), so a
  resolver can no longer pair a new `noeta-sdk` with an older
  `noeta-runtime` that lacks the symbols it imports (previously
  `noeta-sdk` 0.1.1 + `noeta-runtime` 0.1.0 → `ImportError` at
  `import noeta.sdk`).

## [0.1.1] - 2026-07-02

### Added

- `query()` now returns a `QueryResult`: still the full event-envelope
  list, plus projections materialized before the temporary client shuts
  down — `messages()` (the pre-dereferenced human-readable view) and the
  strict `answer()` accessor, which raises the new coded
  `QueryFailedError` on a failed (or missing) terminal instead of handing
  back the failure reason as an answer.
- Typed/coded public error surface: `CodedError` base plus coded engine
  errors, re-exported through `noeta.sdk` for structural matching on
  `exc.code`.

### Changed

- Runtime architecture/contract optimizations: absolute timer `fire_at`
  (EventLog migration 7), wake-reclaim dedup, and merged kill paths.

### Fixed

- Large answers from one-shot `query()` are no longer lost: previously
  the terminal answer spilled to the ContentStore (`answer_ref`) became
  unresolvable once `query()` tore the temporary client down (#5).
- Web: bypass-permissions chip simplified — single icon, concise label.

## [0.1.0] - 2026-07-01

Initial preview release.

### Added

- Three-distribution layout: `noeta-runtime` (engine + agent materials),
  `noeta-sdk` (thin in-process client surface), and `noeta-agent` (the official
  coding-agent app shell with HTTP/SSE backend and bundled web app).
- Event-sourced engine: every step lands in an append-only EventLog, the single
  source of truth a task's state is folded from.
- Offline `stub` provider — a deterministic two-turn LLM double that needs no API
  key and no network, for proving install + storage + Engine wiring on a fresh
  checkout.
- Single-host, single-worker durable execution with exactly-once wake recovery.

[Unreleased]: https://github.com/initxy/noeta/compare/v0.1.5...HEAD
[0.1.5]: https://github.com/initxy/noeta/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/initxy/noeta/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/initxy/noeta/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/initxy/noeta/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/initxy/noeta/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/initxy/noeta/releases/tag/v0.1.0
