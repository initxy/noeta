# Changelog

All notable changes to Noeta are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Noeta is pre-1.0: while on `0.x`, minor versions may carry breaking changes.

## [Unreleased]

## [0.1.16] - 2026-07-09

### Fixed

- **Sub-agents no longer fail to start under the resident worker pool.** With
  `background_drive` on (the served product's default), a spawned sub-agent
  could error at its first turn with a provider "no user message" rejection: a
  freshly created child task carries its goal in its genesis event, but only the
  delegation drain turns that goal into the child's opening message — and the
  resident worker could pick the child off the ready queue and drive it before
  the drain seeded it. Foreground sub-agents (the parent waits on the result)
  hit this every time; background sub-agents (fire-and-forget) hit it
  intermittently as a race against their executor. The resident worker now
  settles a delegation subtree through the same seeding drain the in-request
  path uses, and a background child is reserved for its executor so no worker
  can claim it first. Adds a dispatcher schema column (`reserved`); existing
  SQLite / Postgres databases migrate in place on open.

### Fixed

- **Large sandbox shell output is no longer lost.** A big `shell_run` in a
  sandboxed session could drop its output entirely — AIO truncates a large
  command's inline stream, and once the merged output crossed the 32 MB
  response cap the whole call failed, so the model got nothing back, not even
  the tail. The container backend now reads the full stream AIO spills to a
  file (`full_output_file_path`) via a bounded `tail`, so the recovered tail
  feeds the normal output cap and a big build log lands in the artifact instead
  of failing the run. Behaviour is unchanged against an AIO image that does not
  spill.

## [0.1.14] - 2026-07-08

### Added

- **Per-exec sandbox shell preamble (`HostConfig.sandbox_exec_preamble`).** A
  host-supplied `(exec_env_ref, argv) -> prefix` hook, minted fresh for every
  container `run_argv` and prepended ahead of the command — the process twin of
  `SandboxAuth.connect_headers` for HTTP. It lets a product inject per-session
  shell setup that must stay fresh across a long session (e.g. per-user
  credentials that expire mid-session, refetched each exec). `None` (default)
  leaves the command wire byte-identical. A host runtime injection, never
  LLM-controlled and never recorded. Recorded in the
  `execution-environment-seam` ADR.

## [0.1.13] - 2026-07-08

### Added

- **Per-session sandbox (opt-in).** With `NOETA_AGENT_SANDBOX=1` (needs a local
  Docker daemon + the AIO Sandbox image), each session runs in its own fresh
  container: file read/write/edit/patch, foreground `shell_run`, skill loading
  and skill scripts, the workspace config loaders, and `webfetch` /
  `web_search` all execute inside it, never on the host — `memory` and MCP stay
  on the host by design. Two concurrent sessions get separate containers, and a
  reclaimed session reconnects to the same container by its recorded
  `exec_env_ref` (now carrying the `sandbox_id`). A `SandboxProvider` seam
  (`LocalDockerSandboxProvider`) owns provisioning + lifecycle; the container
  key is passed to `docker` by name (never in the argv), and third-party tool
  keys reach in-container tools out-of-band. Extends the v0.1.11 `ExecEnv` seam
  from one shared container to per-session; recorded in the
  `execution-environment-seam` ADR.

### Fixed

- The container `webfetch` / `web_search` transports now run `curl --fail`, so
  an HTTP 4xx/5xx fails the tool call (parity with the host httpx path) instead
  of returning a server error page as a successful fetch or degrading a Tavily
  auth/quota error to a bland "no results".

## [0.1.12] - 2026-07-08

### Fixed

- A background `shell_run(run_in_background=True)` command that finishes while
  the session is mid-turn now delivers its completion notice at the next turn
  boundary (bounded retry-until-idle), matching background sub-agents.
  Previously the notice was dropped and only surfaced when the model next
  polled. The two background-completion paths now share one delivery seam.

## [0.1.11] - 2026-07-07

### Added

- **ExecEnv seam + sandboxed tool execution.** File-system and shell tools
  now run behind an `ExecEnv` interface with two backends: the host process
  (unchanged default) and an AIO Sandbox container (`exec_env="aio-sandbox"`
  in config or `HostConfig`). When sandboxed, the agent's `apply_patch` and
  shell commands execute inside an isolated container with a lexical
  workspace, so an untrusted agent can't touch the host. The session holds a
  durable `exec_env_ref` that survives reconnects across machines, and
  rewind restores file state through the same container. Recorded in the
  `exec-env` ADR.

### Fixed

- `shell_run` timeout is now honoured under the sandbox backend (previously
  the container-side exec ignored the host timeout).
- Background sub-agent completion notices now inline the result and deref
  content refs before anchoring, so the notice body is self-contained.

### Changed

- Docs: post-0.1.10 status sync and dead-link fixes (strict link-check).

## [0.1.10] - 2026-07-07

Supersedes the never-published 0.1.9 (its prompt-cache fix ships here).

### Added

- Step-attempt crash recovery: a step interrupted mid-flight (process death
  during a decide/tool round) is detected on the next lease, sealed with a
  `StepAttemptAbandoned` fold baseline, and either auto-re-driven or parked
  for re-approval — no double-executed tool calls and no lost turn. Bounded
  by an abandon cap so a crash loop parks instead of spinning. Recorded in
  the `step-attempt-recovery` ADR.
- Single-host multi-worker concurrency: the agent runs a resident
  `WorkerLoop` pool (size via `NOETA_AGENT_NUM_WORKERS`, default 1) instead
  of per-command daemon threads, so several tasks progress at once on one
  host. Adds the `release_yield` dispatcher verb (all three storage
  backends) for handing a seeded lease to the pool.
- Multi-host Postgres lease fencing: several host processes can now share one
  Postgres database safely. Emit appends are fenced in-transaction against
  the live lease (`SELECT ... FOR SHARE`), lease expiry runs on the database
  clock so per-host skew can't split-brain, and a `worker_id` audit column
  records the holder. Postgres-only; sqlite / in-memory stay single-host.
- `spawn_subagent` batch form: one tool call may carry `spawns: [{agent,
  goal}, …]` to fan out to several children at once — the fan-out path that
  was unreachable on models which never emit two spawn calls in a turn. The
  legacy single `{agent, goal}` form still works and old recordings replay
  unchanged.
- SDK `query()` returns a `QueryResult`: still the full event-envelope list
  (iteration / indexing unchanged), plus `messages()` and `answer()`
  projections folded against the live store **before** the temporary client
  tears down — so answers and message bodies carried by `ContentRef` no
  longer become unresolvable. `answer()` raises `QueryFailedError` on a
  failed or unterminated task instead of returning the failure reason.

### Fixed

- OpenAI Responses prompt-cache account stickiness: `HostConfig` accepts a
  per-request `provider_headers` factory that the agent lifecycle wires to
  emit `extra.session_id` (the task id) on the `openai-responses` provider.
  This pins every turn of a long task to one backend account on the ModelHub
  responses gateway, so its KV cache is actually reused and the long-session
  `invalid_encrypted_content` error is avoided.
- OpenAI Responses subagent prompt caching: `include:[reasoning.encrypted_content]`
  is now requested independent of the effort setting, a signature-less
  thinking block is never echoed back (it would break the cached prefix at
  its position), and a spawned subtask inherits the parent's per-turn effort.
  Subagent conversations now cache past the static head instead of stalling
  at the first assistant turn.

## [0.1.8] - 2026-07-06

### Added

- OTLP trace export: task / tool / LLM execution can now be shipped as
  real spans to any OTLP/HTTP collector (Jaeger, the OpenTelemetry
  Collector, …). A new `noeta.observers.otlp` module plugs an
  `OtlpSpanSink` behind the existing `TraceExportObserver` seam,
  pairing start/finish events by `call_id` into spans (deterministic
  sha256 ids; subtask spans join their parent's trace so a delegation
  tree renders as one waterfall). The export consumes the audit
  allowlist projection only — no goals, tool arguments, or message
  bodies leave the process — and hand-encodes the OTLP JSON wire
  format, so no OpenTelemetry SDK dependency is added (`httpx` was
  already a runtime dependency). Wired via
  `HostConfig(otlp_traces=OtlpTraceConfig(...))` (re-exported through
  `noeta.sdk`); the app enables it with `NOETA_AGENT_OTLP_ENDPOINT` /
  the `otlp_endpoint` config key (opt-in only — an ambient
  `OTEL_EXPORTER_OTLP_ENDPOINT` never silently enables export; the
  standard `OTEL_EXPORTER_OTLP_HEADERS` rides along once enabled).
  Resumed and rewound conversations keep tracing via segment spans;
  background sub-agents parent into the spawning task's trace. Export
  failures are logged and dropped — an unreachable collector never
  breaks a run.

## [0.1.7] - 2026-07-06

### Added

- Token streaming, end to end: all three provider adapters (Anthropic
  Messages, OpenAI Responses, OpenAI Chat) can stream text/thinking
  deltas while the LLM call is in flight, and the web UI renders a live
  assistant bubble that hands over to the durable message when it
  lands. Deltas are an ephemeral projection — named `event: delta` SSE
  frames without an id, never persisted and never replayed on
  reconnect; the EventLog and the recorded LLM round-trip stay
  identical to the non-streaming path, and the compaction summarize
  call never streams. Recorded in the `token-streaming-projection` ADR.
- `noeta.sdk` re-exports `StreamingProvider` / `StreamDelta`: a custom
  `Options.provider` opts into streaming by implementing the optional
  capability (`complete_streaming` keeps the blocking `complete`
  contract and still returns the complete response). Hosts wire the
  delta consumer through `HostConfig.delta_sink`; headless SDK use
  without a sink is byte-identical to before.

## [0.1.6] - 2026-07-05

### Added

- External-event delivery, end to end: `POST /tasks/{id}/events` and
  `Client.deliver_event(task_id, event_kind=..., payload=...)` (plus
  `seed_deliver_event`) wake a task suspended on the `wait_external`
  Decision branch. Matching is exact on `event_kind`; an optional JSON
  `payload` is recorded on the resumed turn as an `origin="system"`
  message (never on the wake event); a task not waiting on that
  `event_kind` answers the typed `not_resumable` error (409), same
  contract as a repeat `answer`.
- Workflow per-helper structured output on the SDK/backend path: a
  helper spawned via `agent(goal, schema=...)` now mounts the
  `structured_output` control schema and returns validated JSON (the
  feature previously existed only on the deleted runner path).
- Memory auto-recall on the SDK seed path: for memory-enabled agents,
  `start` / `send_goal` record the resident memory index
  (`ContextContentRecorded` kind=`memory`) and route the goal through
  the recall seam, so matching memories land as one `origin="memory"`
  turn. Memory-off agents' streams are byte-identical to before.
- `examples/crash_resume.py`: kill -9 a live worker mid-task, restart,
  fold the task back, and let the durable timer wake finish it — fully
  offline. Recorded as the README GIF (`scripts/demo/crash-resume.tape`).
- Docs: LangGraph section in the server-side comparison; `reclaim_max`
  poison-task backstop documented in the worker-lease-model ADR.

### Changed

- import-linter: the full `app-uses-only-sdk` seal is now in effect as a
  ratchet contract over the whole noeta-agent product namespace (legacy
  direct imports pinned in a shrink-only `ignore_imports` list);
  `backend-only-sdk` stays in force unchanged.
- Model catalog: all public pricing rows verified against the vendors'
  official pages (2026-07-05) with per-row source citations; the two
  internal-gateway models are plainly marked as unpriced ($0 cost
  accounting) instead of carrying pending-sign-off TODOs.

### Fixed

- `claude-sonnet-4-6` `max_output_tokens` corrected from 64k to 128k
  (raises the compaction output reservation for sonnet sessions).
- Docs said the web UI had no structured question/answer flow — it does;
  the real (and now documented) gap is out-of-band notification when a
  task starts waiting on a human. The zh README also claimed the
  packages were not yet on PyPI; they have been since 0.1.0.

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

[Unreleased]: https://github.com/initxy/noeta/compare/v0.1.16...HEAD
[0.1.16]: https://github.com/initxy/noeta/compare/v0.1.15...v0.1.16
[0.1.15]: https://github.com/initxy/noeta/compare/v0.1.14...v0.1.15
[0.1.14]: https://github.com/initxy/noeta/compare/v0.1.13...v0.1.14
[0.1.13]: https://github.com/initxy/noeta/compare/v0.1.12...v0.1.13
[0.1.12]: https://github.com/initxy/noeta/compare/v0.1.11...v0.1.12
[0.1.11]: https://github.com/initxy/noeta/compare/v0.1.10...v0.1.11
[0.1.10]: https://github.com/initxy/noeta/compare/v0.1.8...v0.1.10
[0.1.8]: https://github.com/initxy/noeta/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/initxy/noeta/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/initxy/noeta/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/initxy/noeta/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/initxy/noeta/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/initxy/noeta/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/initxy/noeta/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/initxy/noeta/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/initxy/noeta/releases/tag/v0.1.0
