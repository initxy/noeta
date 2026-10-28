---
status: active
created: 2026-10-28
owner: main agent (integrates); phase work by subagents in worktrees
branch: os-structure (phase 1; each phase gets its own branch)
---

# Open-source release: Claude Code parity, extensible, multi-machine

## Goal

noeta becomes a project worth publishing: an open-source Claude Code that is friendly to
servers and runs on many machines.

1. Architecture: the kernel owns only the task log, scheduling and recovery; every capability
   (tools, prompts, memory, channels, front ends) is a plugin through one extension interface.
   One source per fact: task state is only a fold of the log; one writer per log; every outside
   write goes through the inbox. Deep modules, few interfaces, names that say what they do.
2. Behavior: everything the model or the person sees matches the latest Claude Code default
   configuration (differences are bugs; prompt text is our own words). Implementation may differ
   where ours is better. Extensibility is at least as good as pi-mono, DeepSeek Harness (dsh) and
   Claude Code's plugin system on every row of a written comparison table.
3. Server and multi-machine: many workers on one store; a dead worker's tasks finish elsewhere
   without repeating tool side effects; background commands, timers and sub-agents never belong
   to one process; sqlite for one machine, Postgres (`SKIP LOCKED`) for many; claiming a task
   does not fold its whole log; HTTP/WS and ACP are exits of one client with auth on by default;
   tenants are isolated.

## Non-goals

- Pushing to the remote, releasing, or touching any host's data (owner sign-off).
- Reading Claude Code's places (`~/.claude`, `.claude`, `CLAUDE.md`, `.mcp.json`).
- Uploading anything by default (dsh's default session-log upload is never built).
- Plugin code sandboxing in-process, module hot-swap, raw-request rewrite hooks, plugin-drawn UI,
  a hosted marketplace, agent self-installing plugins (reasons in `docs/extending.md` once
  written).

## Context

- Start: local main @a244d2e, `make check` 1895 passed / 26 skipped, coverage 87%, `make parity`
  0. `docs/adr/` is referenced by AGENTS.md but absent since eff3ec4.
- Audit reports (outside the repo, kept for the run): `~/cc-recordings/open-source-2026-10-28/`
  — `audit-architecture.md` (commit plan S1–S40, F1–F16), `audit-multimachine.md`,
  `research-extensibility.md` (table, B1–B9, three example plugins), `audit-facade-parity.md`.
- Claude Code baseline: 2.1.312 (recordings and the installed CLI agree).
- The parity spec `2026-10-28-claude-code-parity.md` stays the home of the per-feature parity
  checklist; phase 4 here closes its open Plan items.

## Decisions

- D1 Phases run on their own branches, each merged to local main `--no-ff` only after an
  independent review per `docs/reviewing.md` says mergeable.
- D2 Structural and behavior commits never mix (AGENTS.md); phase 1 does the structural series
  before the behavior series.
- D3 Workspace on many machines: the exec env stays replaceable. Supported deployments are a
  shared filesystem mounted at the same path on every worker, or a remote exec env (container
  host). The cluster test uses a shared directory. Recorded as a proposal for the owner.
- D4 Multi-tenant isolation is in scope (the owner's tentative reading of the open TODO): a
  task carries a `tenant`; a server token maps to one tenant; reads, lists, watches and
  `SendMessage` are confined to it; per-tenant config, secrets, workspace root and quotas.
  Revisit if the owner meant something else.
- D5 Side effects: a tool call carries a stable idempotency key (task id + call id) in its
  context; tools declare `retry_safe` (re-run on recovery) or offer `recover` (look up the
  outcome); the rest keep "outcome unknown". Exactly-once for retry-safe and recoverable tools,
  at-most-once for the rest.
- D6 Process-bound work: holds carry the holder's epoch; a reaper releases holds of dead
  workers and posts a `hold_lost` notice to the task's inbox so a parked task wakes and the
  model is told; local background processes die with their worker.
- D7 Snapshots: a versioned fold snapshot per task, written by the runner after N events;
  claim = snapshot + tail; a test folds both ways and compares.
- D8 Extensibility per `research-extensibility.md` B1–B9 with its recommended answers: a worker
  missing a task's pinned plugin fails the task with `plugin_missing` (no silent run); `add_to`
  defaults to `()`; channel replies relay from inside the task; Claude Code plugin directories
  load only from noeta's places, fetched at a pinned commit by `noeta plugin add`.
- D9 Evaluation model: the `.env` live endpoint (gateway token gateway) or DeepSeek Flash;
  `make eval` must not drop below `evals/baseline.json`.

## Plan

### Phase 0 — security
- [x] F1a HTTP answers guarded against `bypassPermissions` (eed83d2).

### Phase 1 — structure (branch `os-structure`)
- [ ] Structural series S1–S40 from the architecture audit, one commit each, check green after each.
- [ ] Behavior series F1b–F16 from the architecture audit, each with a test that fails before.
- [ ] CONTEXT.md / lint-layout updated for the moved kernel surface.

### Phase 2 — multi-machine (branch `os-cluster`)
- [ ] Hold epochs, hold reaper, `hold_lost` notice; one process id; background processes die with their worker.
- [ ] Idempotency key, `retry_safe`, `recover` (D5).
- [ ] Fold snapshots (D7) with a both-ways test.
- [ ] Tenants (D4).
- [ ] ACP and stream-json reach a remote server through the client; live deltas across machines; `create_app` auth on by default.
- [ ] Cluster test: Postgres, ≥3 worker processes, tool calls + background commands + sub-agents + timers + mid-run messages, random SIGKILL/SIGSTOP; every task terminal, no duplicate side effects, no lost messages. Runs from `make cluster` and in CI.

### Phase 3 — extensibility (branch `os-plugins`)
- [x] B1 plugin pinning, B2 `add_to`, B3 `/reload-plugins`, B4 channels, B5 Claude Code plugin directories, B6 `noeta plugin add`, B7 secret references, B8 `function_tool`, B9 extension-point table.
- [x] Three example plugins under `examples/` (tool, channel, prompt/reminder), each ≤ 1 page, tested by `make check`.
- [x] `docs/extending.md` with the comparison table and the do-not-build reasons.

### Phase 4 — parity leftovers (branch `os-parity`)
- [ ] Close the open Plan items of `2026-10-28-claude-code-parity.md`; `make parity` 0; `make eval` ≥ baseline; `make e2e` green.

### Phase 5 — open-source face (branch `os-docs`)
- [ ] README five-minute start with a real model; docs: architecture, extending, server/cluster.
- [ ] CONTRIBUTING, SECURITY, templates, pyproject metadata, CHANGELOG match the code.
- [ ] `docs/adr/` restored with the decisions that survived; `check-docs.sh` fails on a referenced missing directory.

### Phase 6 — final gate
- [ ] Postgres contract tests and the 3.11 / 3.12 / 3.13 matrix run locally green.
- [ ] Final independent review on code only: no P0/P1.

## Acceptance criteria

1. `make check` green; Postgres contract tests and Python 3.11/3.12/3.13 run locally green.
2. `make parity` 0 against the latest recordings (intended differences justified); `make eval`
   ≥ baseline; `make e2e` all pass.
3. The cluster test passes automatically.
4. ≥ 3 example plugins (tool, channel, prompt/reminder), each ≤ 1 page; comparison table in
   docs, noeta not worse on any row or the reason written.
5. README five-minute start with a real model; docs for architecture and extending;
   CONTRIBUTING / SECURITY / LICENSE / CHANGELOG consistent with code.
6. Final independent review: no P0/P1.

## Risks

- Phase 1 touches most files; later phases branch from its merge to avoid conflicts.
- The cluster test can be flaky; it uses a scripted provider and bounded timeouts.
- Tenant isolation changes HTTP surfaces; the single-tenant default must stay simple.

## Progress log

- 2026-10-28 make check green at a244d2e (1895/26 skipped). Four read-only audits done; F1a fixed.
- 2026-10-28 Structural S1–S21 on `os-structure-w1` (one commit per item, S13/S18 split; check green after each, parity 0, Postgres contract green). Landed: dead and tests-only code (S1–S3); TaskRun.model (S4); optional Tool/Provider/ExecEnv members documented, Resumable merged (S5); row patch derived from events, shared row rule and codec (S6–S7); typed core records `core/records.py` + per-event fold (S8); shared notify/timers/child staging (S9, partial); TaskView.cwd (S10, identical-fallback sites only); TaskView.folded (S12); pending/questions to host, mode vocabulary to `toolkit/permission_modes.py` (S13–S14, structural part); ClientAPI (S15); `host/feed.py` tool_title/failure_text, ACP and stream-json on records.decode (S16, partial); env scrubbing and command tags to toolkit (S17, partial); shell hardening to the shell plugin, toolkit/context split (S18, partial); small duplicates (S19); one task-notification builder (S20); renames (S21). Skipped as functional or not worth it: S11 (compaction remembers its mid-chain request, the final one differs), full ToolContext/TaskView merge and workflow `_ToolView` (third-party tool API), AppAPI, full feed classify and TUI core-kind matching (TUI shows the hooks plugin's own `retry` records as model retries), models.py and prompt discovery moves (need F15 / a loader default), one shell splitter / wrapper / git-options table, frontmatter and brace-expander merges (their outputs differ), hooks' mode copy and the published mode record (change the log / what front ends show).
- 2026-10-28 Phase 3 on `os-plugins` (from e584ae8), one commit per item, check green after each, parity 0: `function_tool` (B8); per-task plugin pins in a core `plugins` record, `plugin_missing` on a worker without one, re-pinned when changed (B1; written in the same append as the prompt files, so no model-visible change); `Extension.add_to` for plugin files (B2); `{env:}` / `{keyring:}` secret references (B7); `/reload-plugins` through `LocalApp.reload_plugins`, unchanged plugins reused (B3; structural prep split out); store contract test for create-by-id under a race (it was already idempotent; a losing create's input is dropped, so channels create, then send with a dedupe key); `Channel`, `noeta.channels`, `deliver`, channels run by worker/serve (B4); Claude Code plugin directories through the existing plugins plus a `markdown_commands` plugin (B5); `noeta plugin add/list/remove`, pinned, marketplaces read only (B6); `/etc/noeta` `[policy]` allow/block; examples run-tests, chat-bridge, house-style; `docs/extending.md` (B9). Left out: `<plugin>:<name>` namespacing of a Claude Code plugin's skills and agents (agent names may not hold `:`; built-in agent names are protected instead), `${CLAUDE_PLUGIN_ROOT}` inside skill bodies (the skills plugin reads them through the exec env), a plugin's MCP servers next to ACP's editor servers (the front end's `servers` replaces them), pip installs in `noeta plugin add`.
