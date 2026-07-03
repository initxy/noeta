# Changelog

All notable changes to Noeta are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Noeta is pre-1.0: while on `0.x`, minor versions may carry breaking changes.

## [Unreleased]

### Added

- New observational `LLMRetryScheduled` event: the runtime records each
  scheduled transient-retry backoff (call_id, attempt, delay, category,
  truncated error) so the web chat shows "Provider error — retrying (n/m)"
  in the composing indicator, status text, and a per-call timeline marker
  instead of stalling silently. Fold-inert (no state slice changes); the
  request/response event trio still fires exactly once per logical request.

### Changed

- Transient LLM retry budget raised from 5 to 8 attempts (max backoff wait
  ~31s → ~2min), so a sustained 429 rate limit gets a real recovery window.

### Fixed

- Subtasks now inherit the parent session's model binding: a child agent
  without its own declared default model runs on the root parent's bound
  model (recorded as the child's opening `ModelBound`, identity
  `"inherited"`) instead of silently dropping to the host default model.

## [0.1.0] - YYYY-MM-DD

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

[Unreleased]: https://github.com/initxy/noeta/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/initxy/noeta/releases/tag/v0.1.0
