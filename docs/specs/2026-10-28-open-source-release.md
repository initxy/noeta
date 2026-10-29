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
- D10 A task tree has at most one holding worker (refines D6, ADR-011): a hold by another
  worker while one exists (expired but unreaped included) is refused in the store's
  transaction; `Runtime.hold` reaps once, then raises `HeldElsewhere`; a background `Bash` /
  `Monitor` is refused with a tool error, a foreground command past its timeout is killed
  instead of moved to the background, an MCP question is declined. Rejected: "any current
  holder may claim" (the root could run where its jobs are not, so ending it or `TaskStop`
  would miss the other worker's jobs).
- D9 Evaluation model: the `.env` live endpoint (gateway token gateway) or DeepSeek Flash;
  `make eval` must not drop below `evals/baseline.json`.

## Plan

### Phase 0 — security
- [x] F1a HTTP answers guarded against `bypassPermissions` (eed83d2).

### Phase 1 — structure (branch `os-structure`)
- [x] Structural series S1–S40 from the architecture audit, one commit each, check green after each (skips and partial items with reasons in the Progress log).
- [x] Behavior series F1b–F16 from the architecture audit, each with a test that fails before (F9, F13 not done; reasons in the Progress log).
- [x] CONTEXT.md / lint-layout updated for the moved kernel surface.

### Phase 2 — multi-machine (branch `os-cluster`)
- [x] Hold epochs, hold reaper, `hold_lost` notice; one process id; background processes die with their worker.
- [x] Idempotency key, `retry_safe`, `recover` (D5).
- [x] Fold snapshots (D7) with a both-ways test.
- [x] Tenants (D4).
- [x] ACP and stream-json reach a remote server through the client; live deltas across machines; `create_app` auth on by default.
- [x] Cluster test: Postgres, ≥3 worker processes, tool calls + background commands + sub-agents + timers + mid-run messages, random SIGKILL/SIGSTOP; every task terminal, no duplicate side effects, no lost messages. Runs from `make cluster` and in CI.

### Phase 3 — extensibility (branch `os-plugins`)
- [x] B1 plugin pinning, B2 `add_to`, B3 `/reload-plugins`, B4 channels, B5 Claude Code plugin directories, B6 `noeta plugin add`, B7 secret references, B8 `function_tool`, B9 extension-point table.
- [x] Three example plugins under `examples/` (tool, channel, prompt/reminder), each ≤ 1 page, tested by `make check`.
- [x] `docs/extending.md` with the comparison table and the do-not-build reasons.

### Phase 4 — parity leftovers (branch `os-parity`)
- [x] Close the open Plan items of `2026-10-28-claude-code-parity.md`; `make parity` 0; `make eval` ≥ baseline; `make e2e` green.
- [x] The three parity Plan items left open by the first pass: per-tool behavior pass, `SendMessage` `notify_when_idle` and its transcript file, ACP diffs / plan updates / model switching; an interrupted reply keeps a finished thinking block.

### Phase 5 — open-source face (branch `os-docs`)
- [x] README five-minute start with a real model; docs: architecture, extending, server/cluster.
- [x] CONTRIBUTING, SECURITY, templates, pyproject metadata, CHANGELOG match the code.
- [x] `docs/adr/` restored with the decisions that survived; `check-docs.sh` fails on a referenced missing directory.

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
- 2026-10-28 Behavior series on `os-structure-f` (one commit each, a test failing before every behavior fix, check green after each, parity 0): F1b (serve turns on the plugin's own bypass refusal; send/cancel through the guard), F2 (probed/compacted recorded; a front end's new `resumed` signal is SessionStart resume and Claude Code's new process), F3, F6 (TUI cost only; headless budget already matches Claude Code), F7, F8 (TaskView.cwd everywhere; task_env kept for env-less views), F10 (`inherit_model` / `permission_mode` / `isolation` fields), F11 (documented, store-wide is intended), F12 (UsageRank on a client), F14 (hooks caches `_decisions` / `_ran_once` gone, structural; `_rewrites` is per gate round), F15 (`Runtime.output_ceiling`, catalog default), F16, S14 (published `modes` record; hooks read permissions' `mode`), TUI core-record matching. Skipped: F9 (three caps and two filters are different policies; merging is a design call), F13 (memory already always records `dir`; the order check is what lets instructions show the index; `/plan` runs in the front end with no view to record on, and the text match can only enter plan mode), S11 (the overflow request would carry other plugins' betas into the summary call).
- 2026-10-28 Phase 2 holds on `os-cluster-holds` (from `os-structure` e584ae8): F5 (claim clears `waiting_on`; `append` returns `Appended(seq, at)`; `released` change kind only when a row changed); hold epochs (`tree_holds` table, renew fenced by epoch, several holders per tree), `Store.reap_holds` run before every claim posts `External(hold_lost)` to the tree's unended tasks atomically, a stopping worker abandons its holds the same way; one worker id `host:pid:random`; F4 (job end is `External(shell_job_ended)` posted before the hold is released; the runner settles each job once, in inbox order, via `TaskView.enqueue`; lost notice in the `<task-notification>` shape, status failed); jobs run beside a sh guard that kills their group when the worker pid is gone or its `.alive` file (kept only while the hold is fresh) stops being re-created; `noeta worker --lease`. Cluster harness starts `noeta worker`; the four xfails are gone; new scenario kills every worker repeatedly while roots wait on wake-ups and sub-agents. make check 1906 passed; full suite with Postgres DSN 1937 passed; `make cluster` 8/8 passed five runs in a row; parity 0. Left: a parked MCP elicitation only learns of a lost hold when answered ("outcome unknown"); a job of a sub-agent that already ended is not settled in that sub-agent's log on loss; the old `holds` table is left in existing stores unused (dropping it needs owner sign-off).
- 2026-10-28 Phase 4 on `os-parity` (from 682f246f; skills, agent files / hooks and headless done by helpers in their own worktrees and cherry-picked): the parity comparator counts message shapes and deferred-tool names; the leftovers listed in the parity spec's progress log are closed, the rest justified in its Decisions; `make check` green after each commit, `make parity` 0 (13 accepted deviations).
- 2026-10-28 Phase 2 interface and tenants on `os-tenants` (from `os-structure` 44083fe), one commit each, check green after each, parity 0: front ends run on `AppAPI` (`noeta/host/app.py`; `LocalApp` in process, `RemoteApp` over HTTP, new `POST /commands/run`), so ACP and stream-json no longer reach into a `Runtime` and `noeta acp --server URL` drives a served app; `create_app` raises without a token unless `auth=False`; live deltas published through Postgres (`<schema>_deltas`, 30 ms batches, LISTEN only once someone subscribes; sqlite keeps them in process); `TaskCreated.tenant` + ADD-only `tasks.tenant` column, `Client.scoped`, tenant-confined `ToolContext.post` / `tasks`; `create_app(tokens=)`, `noeta token add|list|remove` (`~/.noeta/tokens.json`, digests only); `~/.noeta/tenants/<name>/config.toml` with workspace (HTTP entry), `max_running` (`Store.claim(tenant_limits=)`, exact on Postgres via a per-tenant advisory lock and recount), monthly `budget_tokens` / `budget_usd` (`tenant_usage` table, turn fails with `budget_spent`), own provider; `docs/server.md`. make check 1963 passed; full suite with Postgres DSN 2001 passed; `make cluster` 8/8; parity 0. Left: the workspace bound is enforced where tasks come in (HTTP), not by the worker (host code with an unscoped client can still set any `cwd`); a budget is counted from when it is set and only for model calls, so a call that starts under the budget may end over it; ACP over `--server` cannot pass the editor's MCP servers (they hold credentials and stay in the editor's process); deltas published before a front end's LISTEN is up are not replayed (the recorded reply still arrives); tenant settings are per machine copies.
- 2026-10-28 Phase 2 continued on `os-cluster-runner` (from `os-structure` 44083fed), one commit each, check green after each: D5 (`ToolContext.idempotency_key`, also sent in MCP `tools/call` `_meta`; `Tool.recover` asked first, `retry_safe` re-runs up to `MAX_ATTEMPTS` starts; Read, ToolSearch, WebFetch, WebSearch, CronList, ListAgents and the MCP resource tools are retry-safe; MCP tools stay "outcome unknown"); D7 (`noeta/core/snapshots.py`, `snapshots` table added in both stores, claim = snapshot + tail, written every `Runtime.snapshot_every` = 100 events from stored events only; every log the suite loads is folded both ways; claim of a 20,002-event log: sqlite 120 -> 21 ms, Postgres 197 -> 119 ms; `noeta worker --snapshot-every`); a dead worker's parked MCP elicitation ends "outcome unknown" on the next pickup; the root is told of a lost job of an ended sub-agent; a graceful stop posts `hold_lost` without waking (`Post.wake`), the reaper still wakes; a timer due while parked fires once the turn ends (Claude Code fires only when idle; documented, tested). make check 1965 passed; full suite with Postgres DSN 1999 passed; `make cluster` 11/11 five runs in a row; parity 0. Cluster: retry-safe `Lookup` in the chaos mix plus a targeted kill mid-call, an ended sub-agent's lost job, a Cron scenario (70 s of faults). Left: a crash between taking `hold_lost` and enqueuing an ended sub-agent's loss notice loses that notice (no re-decide on pickup); MCP tools are never retry-safe (server `idempotentHint` untrusted); snapshots hold the whole conversation (a 20k-event log's is 12 MiB), so Postgres claims are bound by transfer.
- 2026-10-28 Phase 2 cluster CI on `os-cluster-ci` (from `os-structure` 275331a9), one commit each, check green after each: a root that dies after taking `hold_lost` re-decides the ended sub-agents' loss notices on its next pickup (its log's last take, deduped by the notice key; `TaskView.read_log` takes `after`); a crash right after a timer fires no longer drops a recurring Cron job, a fired one-shot job's deletion, or the `/loop` fallback (a pickup finishes a firing whose kernel timer is gone); cluster scenario `crash-after-take` (a test plugin kills the root's worker right after the take); CI `cluster` job runs `make cluster` against a `postgres:16` service (max_connections raised to 400, as the harness's own container). Checked without a further gap: kernel timers (post deduped, taken with the fold), sub-agent spawn/end/notice (same append; subtasks and workflow re-decide in `on_start`), job end vs loss (posted before the hold is released), a dead worker's job (hold reaped, `hold_lost` atomic with the release), a graceful stop (heard with the next message, as Claude Code). make check 2047 passed; `make cluster` 12/12 three runs (two with `NOETA_TEST_POSTGRES_DSN` on a throwaway service-like container, one on the harness's container); parity 0. Left: a sub-agent that takes its input, then gets `hold_lost` and ends before taking it, while the root took its own copy when the sub-agent was still live, is told by neither (a window between one inbox read and the end append).
- 2026-10-28 `os-holdrace` (from `os-structure` 633d24c2): the last gap above is closed. The store refuses an append that ends a task over unread inbox items (`InboxUnread`, a `Conflict`, checked under the row lock in both stores; contract test); the runner takes the input and decides again (a decided end re-runs the decision, so the sub-agent records the job lost and tells its model; a forced end takes the input, then ends). Lost vs ended is decided once: by the sub-agent's runner when `hold_lost` reached it unended, else by the root from the ended log. Deterministic test (`tests/plugins/shell/test_end_after_hold_lost.py`, fails before) injects the reap and the root's take right before the sub-agent's end append. No cluster scenario: making the reap land inside that window needs the sub-agent's own worker alive while its hold is reaped and no other worker claiming it, which faults cannot place reliably. make check 2049 passed; full suite with Postgres DSN 2090 passed; `make cluster` 12/12 three runs; parity 0. Left: a job's end posted to its sub-agent just after that sub-agent's end committed (before the worker marks it ended) lands in an ended inbox and reaches no one.
- 2026-10-28 Phase 5 on `os-docs` (from `os-structure` 633d24c2), one commit per piece, `make check` green after the docs commits, the pyproject commit and the ADR / gate commits: README rewritten (what noeta is, install from the repository since 1.0 is not on PyPI, Anthropic key or OpenAI-compatible endpoint with `NOETA_PROVIDER` / `NOETA_BASE_URL` and the per-provider `/v1` rule, the extras each command needs, embed, serve, extend, privacy; followed literally in a fresh tool environment: install from a checkout 11 s with a cold uv cache, `noeta -p` against the live gateway 3.4 s with `anthropic` and 3.1 s with `openai_compat`, both answered); `docs/architecture.md`; CONTRIBUTING, SECURITY (security model), issue/PR templates, `.env.example`; CHANGELOG Unreleased covers a244d2e..os-structure with one heading per type; pyproject PyPI metadata and a `keyring` extra (`uv build` + `twine check` pass; the sdist leaves out the local coding-tool settings directory); `docs/adr/` restored with ten records (001-010) and an index; `check-docs.sh` red on a live doc naming a missing `docs/` path or relative link (probe-tested, and red everywhere with `docs/adr` moved away); the CI ripgrep comment corrected. Left: the `git+https` install line is untested until the branch is pushed (the local-checkout form was tested); `noeta serve` / `acp` / a Postgres store without their extra still fail with a raw `ModuleNotFoundError` (code under `noeta/cli`, owned by the structure work); README's relative links will not resolve on a PyPI page.
- 2026-10-28 `os-holdrace2` (from `os-structure` 78302249): the job-end leftover above is closed. `Post.forward_if_ended` has the store put an item for a task that has ended into its root's inbox instead, decided under the task's row lock with the same dedupe key; background jobs' ends and monitor events ask for it (`ToolContext.post`). With the end refused over an unread inbox, one runner reads the item: the sub-agent while live, else the root (one notice per job by its key). Deterministic test `tests/plugins/shell/test_end_after_sub_agent_end.py` (fails before: the job finishes between the sub-agent's end commit and the worker marking it ended); store contract test. make check 2051 passed; full suite with Postgres DSN 2093 passed; `make cluster` 12/12 three runs; parity 0.
- 2026-10-28 Phase 4 open item on `os-parity2` (from `os-structure` d06c6cf6), one commit per behavior, a test failing before each, check green after each: the per-tool behavior pass, `SendMessage.notify_when_idle` and the sub-agent transcript `output_file`, ACP diffs / plan updates / model and mode switching, an interrupted reply keeping its finished thinking, `pip install 'noeta[<extra>]'` for extras-dependent commands (the os-docs leftover), and the TUI's "UI did not settle" flake (busy set after the send returned); details in the parity spec's progress log. make check 2157 passed (88.14%); `make parity` 0 (11 accepted deviations); make eval 1.000 vs 0.987; make e2e 7 passed; make cluster 12/12.
