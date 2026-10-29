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
- The parity spec `archive/2026-10-28-claude-code-parity.md` held the per-feature parity
  checklist; phase 4 here closed its open Plan items, and what it left open is listed under
  "Not yet matched" in `parity/deviations.md`.

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
- D10 A task tree has at most one holding worker (refines D6, ADR-011): a hold by another
  worker while one exists (expired but unreaped included) is refused in the store's
  transaction; `Runtime.hold` reaps once, then raises `HeldElsewhere`; a background `Bash` /
  `Monitor` is refused with a tool error, a foreground command past its timeout is killed
  instead of moved to the background, an MCP question is declined. Rejected: "any current
  holder may claim" (the root could run where its jobs are not, so ending it or `TaskStop`
  would miss the other worker's jobs).
- D11 Snapshot writes follow the state's size (refines D7, ADR-012): due after
  `snapshot_every` events whose bytes are at least `SNAPSHOT_GROWTH` = 0.1 of the newest
  snapshot's; the ending append deletes the task's snapshot.
- D12 What a request may set is an allow list, not a deny list: a new task's `config` takes
  `model`, `effort`, `max_turns`, `cwd`, `permission_mode`, `output_schema`; a `config` signal
  the kernel's `CHANGEABLE_CONFIG`; signals only `permission_mode`, `config`, `resumed`,
  `restore_files`. Everything else (plugin keys such as `permission_rules`, plugin signals such
  as `shell_job_ended`) is 403 naming it. A host plugin that wants a new request-settable key
  or signal must be added to the list (`noeta/host/_http_guard.py`).
- D13 Tenant queues: `queues = [...]` per tenant; without it `default` and the queue of the
  tenant's own name; the default tenant (server token) may use any queue.
- D14 (owner may revisit) Tenants and the OS user: `noeta serve` (with its worker) and
  `noeta worker` refuse to start when tenants besides the default one are known (tokens file or
  tenant directories), unless the sandbox confines every command (`enabled`,
  `failIfUnavailable`, no `allowUnsandboxedCommands`) or `[server] shared_os_user_tenants =
  true` accepts API-only separation; a tenant token added later is ignored by such a server.
  Known limits: the sandbox confines `Bash` only (file tools run in-process, bounded by
  permission rules the tenant answers itself; docs recommend `Read`/`Edit` deny rules for
  `~/.noeta`); a remote exec env is reachable only from host code; `serve --no-worker` is not
  checked and a worker without tenant directories or tokens file does not know of tenants.
- D15 The server token is kept as `sha256:<hex>` in `~/.noeta/server-token` and printed only
  when made (or when an older plain-text file is migrated, one last time); clients take it from
  `NOETA_SERVER_TOKEN`. This changes ADR-009's "read by `noeta --server`" consequence: a
  refining ADR is due at wrap-up.
- D16 File tools of a task whose tenant is not `default` are fenced off from noeta's home,
  the sqlite store's files, `/etc/noeta`, `/proc` and other tenants' workspaces
  (`noeta/toolkit/tenant_fence.py`), whatever the deployment (this closes D14's file-tool
  limit). Checked twice: a permissions-gate `Deny` ahead of every rule, mode and answer on
  the path as written, tidied and resolved; and `LocalExecEnv(fence=)` on the file actually
  opened (a read's fd path, a write's opened directory), so a symlink swapped in between
  does not help. Inside the fenced places a task keeps its own spill directories (its id
  and its root's), `plans/<tenant>/` (a tenant's plan files move there), `projects/<slug>`
  for its cwd and the directories above it within its workspace, and read access to
  `skills/`. The memory plugin, which reads in-process, takes a git main checkout only when
  it holds the cwd and keeps `user`-scope agent memory under the cwd for such tasks.
- D17 The tenant decision is also made per task: a worker without a strict sandbox or
  `[server] shared_os_user_tenants = true` (whether or not it knows of tenants) fails a
  non-default tenant's turn with `tenant_not_isolated` and the reason before any model
  call, and denies its pending calls (`Permissions(tenant_refusal=)`). Rejected: skipping
  such tasks at the claim (needs a tenant filter in every store's claim; a pool with no set
  up worker would leave them pending with nobody told). Multi-machine: workers with the
  same settings, or tenants on their own queues served by set-up workers (D13 queues).
- D18 (`os-fix-tools`) Read-before-write follows Claude Code 2.1.312: skipped only for a write
  the permission layer allowed outright by its mode or an allow rule (`Allow.outright` ->
  `ToolContext.allowed_outright`), never on older Claude models (`ToolContext.model`); with no
  permission layer the check always holds.
- D19 (`os-fix-tools`) Own words, checked: `scripts/parity/own_words.py` fails on any run of
  45+ characters shared with Claude Code's recorded texts. It hashes 43-character windows at
  stride 3 in memory from the recordings outside the repository (`NOETA_PARITY_RECORDINGS`,
  default `~/cc-recordings`) and skips with a notice where there are none (CI). No index is
  committed: unkeyed hashes of overlapping windows can be reversed from a known opening
  (`os-fix-r2docs` removed the one `os-fix-tools` committed; it stays in branch history). What
  derived parity data is committed, and why: `scripts/parity/README.md`. Protocol literals go
  in `parity/own-words-allowed.txt`. Texts the recordings never show (deferred tools, MCP
  sign-in) stay a review matter.
- D20 (`os-fix-tools`) Background and foreground job logs: the temp directory on disk, one
  directory per worker process (`w<pid>`, a dead one's removed on this machine a day after its last write), each stream kept
  to its first 64 MB; a foreground command's guard kills it 30 s after its worker stops.
- D21 (`os-fix-tenant2`) MCP for tenants: a task whose tenant is not `default` gets no server
  that runs a process on the worker (a `command`, a `headers_helper`), whoever configured it
  (host code, user or workspace settings, an agent file); they show as down with the reason,
  in the task and in its tenant's command listing (`CommandContext.tenant`). Remote servers
  run, always in a connection partition of the tenant's (`tenant:<name>/<scope>`), so no
  connection or OAuth sign-in is shared across tenants. Rejected: running stdio servers in
  the task's sandbox profile (the pool shares one process per server and scope across tasks,
  and a long-lived process would outlive the per-task profile it was started under).
- D22 (`os-fix-tenant2`) Commands of tenants: under the sandbox, a non-default tenant's task
  gets `TenantFence.confine` of the settings: D16's places unreadable but its own corners,
  no `excludedCommands`, no way out, the filesystem confined, a fresh `/proc`. Every program a
  tool runs that can run the tenant's code (a skill's script, an inline skill command without
  `Bash`, `git` with its hooks, a rewind's `rm`) goes through `run_in_tenant_sandbox`; helpers
  that only list names (`find -L` of instructions and skills, the gate's glob expansion) and
  the PDF converters (given a fenced copy) do not. The operator's saved workflows join
  `skills/` as readable.
- D23 (`os-fix-r2host`) git that noeta itself runs in a non-default tenant's workspace (the
  environment's snapshot, memory's repository lookup) goes through `run_in_tenant_sandbox`
  too, extending D22 to every place operator code runs git on a tenant's checkout. Hardened
  flags alone are not sound: the repository's config is the tenant's, git has no switch for
  every config key that runs a program (`gpg.program` under `log.showSignature`, a
  submodule's filter drivers, keys added by later git versions), and repository
  `include.path` cannot be turned off with `-c`. The capture keeps its flags for every task
  (the default tenant's checkout may be cloned code). Without a sandbox (the operator set
  `shared_os_user_tenants`) git runs as the OS user, as that tenant's `Bash` already does.
  `sandbox_plan`'s `git rev-parse --git-path hooks` stays a host call: it reads config but
  runs nothing from it.
- D24 (`os-fix-r2kernel`) A held tree's tasks run only on its holder (refines D10): a hold
  is also refused while another worker has an unexpired lease on a task of the tree, in the
  hold's transaction; on Postgres a claim takes the per-tree hold lock shared after picking
  its row and looks at `tree_holds` again, a hold takes it exclusive. A log no claim can read
  or fold ends the task `failed` (`unreadable_log`) after `LOAD_ATTEMPTS` = 3 such claims in
  a row on one worker, written from the row. Upgrading a version 1 store posts `hold_lost`
  (epoch 0) for its old `holds` rows and keeps the table. Rejected: counting failed claims in
  the store (a schema change for a rare case), dropping `holds` (data deletion: maintainer).
- D25 (`os-fix-r3kernel`) A hold takes back other workers' lapsed claims of the tree (refines
  D24): in the hold's transaction, before the live-lease check, every task of the tree
  `running` on another worker with its lease run out gets `owner_epoch + 1` and goes back to
  `ready` (announced `released`), so the stalled worker's next `renew` or `append` fails on
  the epoch. The lease only says a worker may be dead; the epoch decides who writes. On
  Postgres the hold locks those rows `NOWAIT` and, if a claim or renewal in flight has one,
  rolls back and tries again, so it never waits on a row while holding the tree's lock a
  claim then waits for. Rejected: `renew` refusing while another worker holds the tree (the
  stalled worker could still `append` until its next renewal, and every renewal would read
  `tree_holds`).
- D26 (`os-fix-r3tenant`) A tenant's files on disk outside noeta's home: each non-default
  tenant has its own temporary directory, `<temp>/noeta-temp-<uid>/tenants/<tenant>/` (0700,
  a long or unusual name cut and hashed so proxy socket paths stay short), holding its
  background job logs (`jobs/`), its sandboxed commands' `TMPDIR` (`tmp/`) and proxy
  sockets, and a PDF read's scratch copies (`LocalExecEnv(temp_dir=)`, set by
  `local_env_for`). The fence refuses, and `confine` hides, all of `noeta-temp-<uid>` but
  that directory and `noeta-background-<uid>` in every temp directory the shell plugin may
  pick; those places are made before each tenant command, so one made while it runs is
  hidden too. A tenant's denied directory stays hidden with a writable directory inside it,
  which is mounted again (the operator's `denyRead` keeps dropping such a directory).
  Worker sandbox directories move to `noeta-temp-<uid>/sandbox/`. A started workflow's
  script goes to its task's own spill directory. Rejected: per task (nothing cleans per-task
  directories, and a tenant's tasks already share its plans), under the spill directory
  (kept forever, and hidden), flat `/tmp` names matched by a glob (one made while a command
  runs would be visible).
- D27 (`os-fix-r3tenant`) A non-default tenant must have a workspace: `noeta serve` refuses
  to start without one for every known tenant, `noeta token add` refuses such a tenant, and
  the HTTP guard answers 403 to its task creation and any `cwd` (tokens added while serving,
  host code). A `cwd` whose closest workspace is another tenant's is refused (nested
  workspaces included). Rejected: deriving a default workspace (a directory the operator
  never chose, and a new place to keep on every worker).
- D28 (`os-fix-r3tenant`) Project directories by tenant: a non-default tenant's memory and
  compaction transcripts live in `projects/<tenant>/<slug>/` (a slug starts with `-`, a
  tenant name never does); the default tenant's stay `projects/<slug>/`. In it a task's own
  corners are `memory/` and, to read, `<task id>.jsonl` and `<root id>.jsonl`; a root does
  not read its sub-agents' compaction transcripts (the fence does not know the tree's ids).
- D29 (`os-kernel-struct`) Extensions change a task's tool list through
  `Extension.tool_changes(view, offered) -> ToolChanges` (ADR-017): asked after every
  extension's `tools` with the tools they offer, never another extension's changes, so a tool
  computed from the others' tools does not recurse; it adds tools, withholds offered ones from
  the request (a call to one is answered with the given error before any gate) and adds
  `ToolEntry`s (request entries that are no tool, placed before a named tool, else after the
  tools that are not `listed_last`). Every extension gets the same `offered`; withheld names
  are a union and entries go in by name, so the request does not depend on plugin order.
  Rejected: filtering in `before_model` (order-dependent), one extension owning the tool list
  (a single-owner slot), the placeholder as a callable tool (a call to it would run).
- D30 (`os-kernel-struct`) The loaded deferred tools are read from the fold, not recorded:
  every `ToolReference` in a tool result of `TaskState.history` (never compacted, carried into
  a continued task) and of the open batch. `TaskState.loaded_tools` is gone. Rejected: a
  `tool_search` record folded in `reduce` (a second copy of a logged fact; logs written before
  it would lose their loaded tools).
- D31 (`os-kernel-struct`) Every agent built from agent files runs `tool_search`
  (`ALWAYS_PLUGINS` in `noeta/agents/agent_files.py`, inserted before `compaction`; a host's
  `enabled = false` turns it off): any plugin's tool may be deferred, and an agent file that
  lists its plugins without it would announce tools nothing can load. An `Agent` built in code
  adds `ToolSearch()` itself (CHANGELOG). Rejected: listing it in each built-in agent file
  (third-party agent files with their own plugin lists would break).
- D32 (`os-fix-r4docs`) A front end attached to a server (`noeta --server`,
  `noeta acp --server`) needs no local model and sends neither this machine's directory nor
  its model: `--cwd PATH` names a directory on the server, `--model` a model. A task created
  over HTTP without a `cwd` gets the server's directory (a tenant's: its workspace) written
  into its config, so every worker runs it in the same place. `noeta -p --server` is refused
  rather than implemented (headless drives a local app). Rejected: sending the local path
  when it happens to exist on the server (a coincidence, and still another machine's path).
- D33 (`os-fix-r4docs`) Tenant running limits, budgets and providers stay in the kernel as
  scheduling (ADR-019, refines ADR-007): the limit is part of the claim, and every model call,
  the turn's and each extension's `TaskRun.complete`, passes through the kernel, which picks
  the tenant's provider, refuses past the budget and counts usage. `tenant_usage` is a
  cross-task total beside the logs (no fold reads it; a refusal is logged), not task state.
  Rejected: a budget plugin on `before_model` (misses extension calls, cannot choose the key).
- D34 (`os-fix-r4kernel`) The agent's allow and deny lists apply to `tool_changes`' added
  tools too (deny wins), and an extension whose added tool they refuse has its whole change
  set dropped: its withholds and entries were computed alongside that tool. The deferred
  tools a dropped set withheld are sent plain (`ToolOffer.plain`: no `defer_loading`, not in
  `deferred_tool_names`), since nothing can load them; Claude Code turns tool search off when
  its search tool is not available. Kernel-generic, no tool named. Rejected: dropping only
  the refused tool (its deferred tools would stay withheld, uncallable); a `loads_deferred`
  tool flag (needs the plugin to set it, and every deferred tool goes plain until it does).
- D35 (`os-fix-r4kernel`) Snapshots are kept per task and fold version, at most
  `SNAPSHOT_VERSIONS` (2) per task; a write replaces only its own version's at a lower seq,
  and past the cap the one at the lowest seq goes. `snapshot(task_id, version=None)` reads one
  version's, or the newest; the runner reads the newest and asks for its own version only
  when that differs (one read in the common case; the task row names the agent when the
  newest is not decodable here). Schema version 3 (sqlite rebuilds the table, Postgres swaps
  the primary key; rows kept); the end still deletes every version in its transaction.
  Rejected: one row per task with a version-aware overwrite rule (the two versions still
  replace each other each claim); a written-at column to evict the least recently written
  (lowest seq is close enough, no column).
- D36 (`os-fix-r4tenant`; superseded by D38) A non-default tenant's file tools reach an allow
  list (refines D16):
  its workspace, its own corners (D16, D26, D28), its temporary directory and, to read only,
  `/usr`, `/bin`, `/sbin`, `/lib`, `/lib32`, `/lib64`, `/libx32`, `/etc`, `/private/etc`,
  `/nix/store`, never `/etc/noeta`, a home directory (`/home`, `/root`, `/Users`,
  `/var/root`, the worker's `~`, each account's of uid 0 or 500-65533) or a server
  directory (the worker's cwd, `TenantFence(server_dirs=)`: serve's cwd and `--allow-root`)
  inside them. Matched on the resolved path against the resolved places. A tenant whose
  workspace the worker does not know reaches none (fail closed). The operator's
  `additionalDirectories` are not its scope. Rejected: `/opt`, `/srv`, `/var`, `/mnt`
  (other software's data, data disks: `/opt` is a link to one on the dev box); using the
  task's cwd when the workspace is unknown (host code can set any cwd).
- D37 (`os-fix-r4tenant`; superseded by D38) A non-default tenant's sandboxed commands also cannot read any home
  directory or server directory (D36's), but the tenant's workspace and own corners; the
  operator's `allowWrite` / `allowRead` / `additionalDirectories` do not apply to them. The
  real Python running the bridge (`sys.base_prefix`) is shown again, since a virtual
  environment's link to it may pass through a home. Directories that hold the machine's
  programs, `/tmp`, `/var/tmp`, `/opt` or `/run` are never hidden (an account whose home is
  `/`). bwrap and Seatbelt read rules are applied outermost first. Rejected: hiding every
  account's home including service accounts (uid < 500: `/var/lib/...`, `/bin`).
- D38 (`os-tenant-relax`; supersedes D36, D37: too strict, the goal is reasonable
  protection) A non-default tenant's file tools and sandboxed commands may read what the OS
  user can but a protected set: noeta's home but its own corners (D16, D26, D28), the
  store's files, `/etc/noeta`, `/proc` (a command has its own), noeta's temp places but
  its tenant's own, other tenants' workspaces, the server's own directories (worker cwd,
  `TenantFence(server_dirs=)`: serve's cwd and `--allow-root`; one holding the machine's
  programs or temp directories is not hidden; a workspace inside one stays the tenant's),
  the sandbox's credential stores (`DEFAULT_DENY_READ`, now in `noeta/toolkit/sandbox.py`)
  under every home directory (the worker's `~`, each `pwd` account's, each directory in
  `/home` and `/Users`) and the operator's sandbox `denyRead` (with `Read(...)` deny rules
  and `credentials` files, `TenantFence(deny_read=)`, whether or not the sandbox is on).
  Inside the tenant's workspace only protected places inside it count. Writes: only its
  workspace, own corners and temp directory; the operator's `additionalDirectories`,
  `allowWrite` and `allowRead` are not its. Ahead of every rule, mode and approval, and
  on the opened file, as before. Kept from D37: the shown-again bridge Python and
  outermost-first bwrap / Seatbelt read rules. Rejected: the system-path allow list
  (`/opt`, `/srv`, `/var`, data disks, `/tmp` are where tenants' inputs live); hiding
  whole home directories (credential stores are what matters there); matching credential
  names anywhere on disk (a project's own `.npmrc`).
- D39 (`os-fix-r4tools`, owner) Supersedes D19's "fails on any shared run": copying Claude
  Code's prompt wording is acceptable where it serves parity; what matters is the behavior
  (ADR-020, refining ADR-003). `scripts/parity/own_words.py` is an advisory report: it
  prints the shared runs, always exits 0, and no test fails on them. The rest of D19 holds
  (recordings read from outside the repository, nothing derived from their wording
  committed). Rejected: a word-run rule (8 consecutive words) on top of the character
  windows, and rewording `Agent`, `ToolSearch` and the background-command result to pass it.
- D50 (`os-fix-r5kernel`) A child task goes to its parent's queue unless one is named
  (`spawn(queue=)`, `Subtasks(queue=)`, a roster entry's `queue`). The parent's queue
  reaches the runner as `Claim.queue` (a defaulted field, filled by both stores), not a
  row read per spawn. Rejected: recording the queue in `TaskCreated`.
- D51 (`os-fix-r5kernel`) `Store.hold(queues=)`: taking a hold is refused while an unended
  task of the tree is on a queue the holder does not serve; renewal does not check (a
  child named onto another queue meanwhile is the host's explicit route). Rejected:
  checking on renewal, which would stop a running background job.
- D52 (`os-fix-r5kernel`) A load error msgspec words "Invalid value '...'" or "Invalid enum
  value '...'" (an unknown tag or literal) is a newer version's log: the worker lets the
  lease run out without counting it toward `unreadable_log`, so it backs off by the lease
  instead of releasing into a busy loop. Malformed JSON and wrong shapes still count.
- D53 (`os-fix-r5kernel`) A model call is charged to its tenant as soon as it returns,
  empty replies included; the kernel's `spawn(history_from=)` is refused like
  `Client.create`'s, an unknown source included.

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

### Follow-ups (after this release; each moves to a spec of its own before this one is `done`)
- [x] Deferred tool loading out of the kernel (ADR-016): an `Extension` way to offer tools computed from the task's other tools, then `ToolSearchTool`, its description and the placeholder into a `tool_search` plugin; request bytes unchanged (`make parity` 0), existing snapshots still valid.
- [x] The file splits S22–S40 left undone in the kernel: `core/runner/task.py` (timers; stopping and ending a turn), `core/runner/model.py` `call_model` (reminders and hooks; overflow), and `backends/stores/postgres.py`'s connection handling; structure only.
- [x] The last S22–S40 split: `plugins/shell/bash.py` (`ShellState`, the sandbox helpers, `BashTool.run`); structure only, once no other branch is changing it.

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
- 2026-10-28 `os-fix-tui` (from `os-structure` abcee613), the two TUI "UI did not settle" hangs under load, one commit each, a test failing before each, check green after each. Shift+Tab pressed before the follower showed the permissions plugin's records stepped from the fallback `auto` into `bypassPermissions` (the task really switched); it now reads the mode and offered modes from the log (waiting up to 2 s for a new task's start) and does nothing without a recorded mode. Textual's `MarkdownStream.stop()` raises the cancel into its caller when the stream's task was cancelled before it first ran, which ended the TUI's follower and delta pump after a fast failed attempt (35/300 runs hung at `-n 8` with CPU load); `_end_stream` absorbs it and writes the undelivered text. After: 300/300 of that test and 30 loops of the four TUI test files (870 runs) green at `-n 8` with 40 busy processes. make check 2161 passed.
- 2026-10-28 Review fixes on `os-fix-docs` (from `os-structure` b8f21252), one commit each, make check green after each (2161 passed, 88.16% at the end): CHANGELOG Unreleased caught up to b8f21252 (per-tool pass, notify_when_idle, transcripts, ACP, thinking on interrupt, job-end forwarding; a Store contract heading for `InboxUnread` and `Post.forward_if_ended`); chat-bridge keeps the reply URL out of the task log and requires an `X-Chat-Secret` shared secret (hmac); house-style keeps a horizontal rule in the body; `noeta -p` announces an unknown model once; WebFetch's user agent names github.com/initxy/noeta; extending/architecture docs (notify signature, marketplace `--commit`, `--no-token`); the parity spec archived `done`, its open differences under "Not yet matched" in `parity/deviations.md`. Left: the missing-extra message in `noeta/cli/main.py` still says only `pip install 'noeta[<extra>]'` (wrong for a `uv tool install`); the skills plugin logs its untrusted-workspace notice once per agent (three times in `noeta -p`).
- 2026-10-28 Host security review fixes on `os-fix-host` (from `os-structure` b8f21252), one commit each, a test failing before every behavior fix, check green after each: request `config` and `config` signals take only the allow-listed keys and signals only the front ends' four (D12; the repro `permission_rules: ["Bash"]` now 403); `/commands` and `/commands/run` check `cwd` and use a task's recorded one; stream-json resume/fork read the task through the scoped client before `adopt`; tenant queues (D13); serve/worker refuse tenants without an OS-level boundary unless opted in (D14); server token kept as a digest, printed once, plain-text file migrated (D15); `?token=` only on the WebSocket; `noeta plugin add` takes only git URLs, after `--`; one `token_digest`, `LOOPBACK_HOSTS` reused; SECURITY.md and docs/server.md say what is and is not a boundary; also the missing-extra message's `uv tool install --reinstall` form and the untrusted-skills warning once per process. make check 2173 passed. Not done: ADR-009 refinement (D15, at wrap-up); the sandbox does not confine file tools (D14).
- 2026-10-28 Phase 6 review fixes on `os-fix-kernel` (from `os-structure` b8f21252), one commit per fix, a test failing before each, check green after each, structural commits apart: a tree has at most one holding worker (D10, ADR-011; store contract on sqlite and Postgres, cluster scenario with a root and its sub-agent each starting a long job on two workers); concurrent `Runtime.hold` of one tree shares one `TreeHold` and waits for a give-back in flight; the snapshot version hashes every in-package module a fold module imports (reducers in `fs/state`, `shell/bash`, `subtasks/idle`); ending a task deletes its snapshot; snapshot writes follow the state's size (D11, ADR-012; 21,627-event log: 699 -> 75 MiB written, claim 19 -> 27 ms on sqlite); both stores at schema version 2, a newer one refused (the old `holds` table is kept, unused: dropping it needs owner sign-off); the lease keeper gives a lease up by the local monotonic clock; a Postgres claim that finds a tenant full tries the others; structure: `postgres_feed.py` / `postgres_snapshots.py` out of `postgres.py` (945 -> 804 lines), `runner/snapshotting.py` out of `task.py` (754 -> 673). make check 2179 passed (88.09%); full suite with Postgres DSN 2227 passed; `make cluster` 13/13.
- 2026-10-28 `os-fix-tenantfs` (from `os-structure` b58174c1), one commit each, a test failing before each behavior fix, check green after each: the tenant file fence at the permissions gate (every path class, every mode including `bypassPermissions`, an allow rule and an approval tested; D16); the same fence on the opened file in `LocalExecEnv` for tenants' tasks (D16); memory never resolves to another tenant's repository or the shared `agent-memory` (D16); per-task `tenant_not_isolated` on a worker not set up for tenants (D17); ADR-013 refines ADR-009 (server token kept as a digest, clients use `NOETA_SERVER_TOKEN`). make check 2198 passed, 88.24%. Left: `Bash` under the strict sandbox still reads other tenants' workspaces, other tasks' spill directories and a store outside noeta's home (the sandbox hides only noeta's home, minus `tool-results` and `projects`); Read of a PDF hands the path to a converter command run by the worker after its header was read through the fence; stdio MCP servers run as the OS user and are not fenced; a worker without tenant directories knows no workspaces to fence (the home, store, `/etc/noeta` and `/proc` still are).
- 2026-10-28 `os-fix-tools` (from `os-structure` abcee613): the independent review's tool findings, one commit each (a test failing before each behavior commit), `make check` green after each, structural commits apart: EnterWorktree from a subdirectory makes the worktree under the task's root and starts the shell in its copy of that subdirectory; ExitWorktree keeps a worktree on any git error; one toolkit module for noeta's worktrees and one function for the Bash script (structural); WebFetch answers a binary body with the saved-file line only and caches it; read-before-write per D18; job logs per D20 (capped, on disk, per-worker directories swept, foreground guard in containers); sub-agent transcripts appended, not rewritten, progress map bounded; copied texts reworded and the own-words check per D19 (122 runs reworded, 71 of them in the classifier prompt); hardening's dead rules removed (structural); FsState keeps 1000 files; AskUserQuestion refuses empty labels and runaway texts (deviation recorded). make check 2160 passed; `make parity` 0 (11 accepted deviations), own-words green; make eval 0.949 on the first run (one delegation trial did not spawn, one delete trial did not ask; within tolerance), 1.000 on a second, vs 0.987. Left: texts the recordings never show (deferred tools, MCP sign-in) are outside the own-words check; dead workers' logs in a container are not swept.
- 2026-10-28 `os-fix-tenant2` (from `os-structure` 7a4985a8), one commit per fix, a test failing before each, make check green after each (2232 passed, 88.27% at the end): the tenant sandbox profile (D22; tested on the generated bwrap and Seatbelt profiles and with a real bwrap denied read of the tokens file, another tenant's workspace, plans, memory and spill files, the store and `/proc/<worker>/environ`); skill scripts, inline skill commands, `git` (a post-checkout hook) and a rewind's delete in the tenant's sandbox; PDF converters on a fenced copy; no local MCP servers for tenants and per-tenant MCP partitions (D21), with a fix on the way (an empty pool given to the MCP prompt and resource helpers was passed over); saved workflows readable like skills. Grep and Glob no longer exist (removed with 1.0's file tools) and noeta runs no `rg`, so there was no search root to fence. Left, documented in docs/server.md "Not a boundary": operator-run hooks and plugins, remote MCP servers' configured headers and `WebFetch` reaching any address the worker can, names listed by `find -L` and glob expansion, macOS processes (Seatbelt has no PID namespace), a trusted tenant workspace; `EnterWorktree` and isolated agents fail for tenants (`.noeta/` is read-only in the sandbox).
- 2026-10-28 `os-fix-r2docs` (from `os-structure` 1c1cd896), the second review's docs and parity findings, one commit each, make check green after each (2236 passed at the end): the own-words index (`parity/2.1.312/cc-windows.bin`, reversible into Claude Code's text from a known opening) removed and ignored; the check hashes the recordings in memory from `NOETA_PARITY_RECORDINGS` (default `~/cc-recordings`) and skips with a notice without them; D19 corrected (the file stays in branch history, unpushed; purging it is the owner's call); `scripts/parity/README.md` lists the committed derived parity data and why, for the owner to sign off; each allowed literal says why. ADR-014 refines ADR-007 (tenants separated at the API; tools need an OS boundary: D14, D16, D17, D21, D22). An unknown model is announced once, its $0.00 price in the same notice. chat-bridge: 400 for a malformed delivery, replies at least once with an `Idempotency-Key`. D9 moved into order; CHANGELOG no longer names Grep. Left: chat-bridge is still about 95 lines (one page would mean dropping approvals through the chat or the listener); internal names in committed specs (D9's gateway, hosts in the 1.0 redesign spec, the archived parity spec's upstream) are for the owner to decide.
- 2026-10-28 `os-fix-r2host` (from `os-structure` 1c1cd896), one commit per fix, a test failing before each, make check green after each (2239 passed, 88.28% at the end). The suspected P1 reproduced, not by `core.fsmonitor` (the capture's `-c core.fsmonitor=` already stops it) but by `gpg.program` with `log.showSignature` and a signed HEAD: the environment's `git log --oneline` ran the tenant's script as the OS user, which read the tokens file; a submodule's clean filter also ran (the filter check looked only at the top repository). Fixed: environment and memory git through `run_in_tenant_sandbox` (D23); every task's capture with `-c log.showSignature=false` and `--ignore-submodules=dirty`. Also: `?token=` redacted in uvicorn's access and error logs (`redact_token_logs`, wired in `noeta serve`); `noeta serve --allow-host` (Host is checked on a non-loopback bind only when named; SECURITY.md said always); `--no-token` refused once tenant tokens or directories exist; the missing-extra message installs from the git URL (noeta 1.0 is not on PyPI); SECURITY.md's `.mcp.json` wording; docs/server.md audit of what a tenant's workspace feeds operator code. Left: operator hooks run as the OS user in the tenant's directory, and the sandbox profile's `git rev-parse --git-path hooks` reads the tenant's config as the OS user (runs nothing); both documented under "Not a boundary".
- 2026-10-28 `os-fix-r2tools` (from `os-structure` 1c1cd896), the second review's tool findings, one commit per fix, a test failing before each behavior commit, make check green after each (2243 passed at the end): a resumed worktree agent whose worktree was removed runs in its caller's directory, and closing a gone worktree claims nothing kept (Claude Code also falls back to the parent's directory); a backgrounded foreground command's liveness file is re-created at its guard's own period (`Job.guard_period`); a dead worker's job logs stay 24 h after their last write so a lost job's notice names a file that exists; switching worktrees twice keeps the made one for `ExitWorktree remove`; `AgentTool.recover` removes the worktree a crashed call made (records land with the result, so no intent record was possible); a cancelled command hook is killed with its group and its output capped while read; compaction and the MCP budget use `estimate_tokens` (structural move of the prefix search first); finished MCP sign-ins let go after `SIGN_IN_REUSE_S`; the untrusted-skills warning set bounded and given only for directories that exist; ExitWorktree's scope sentence reworded; stale deviations rows removed (transcript rewrite, MCP idle timeout, crash re-run). `make parity` 0 (11 accepted), own-words green; make eval 1.000 vs 0.987 (delegate_parallel_review hit its 240 s timeout in all three trials but scored 1.00). Left: a short PDF stays page images (reason now in `parity/deviations.md`: a logged document block would be lost after a `/model` switch to a provider without one); deferred tools no recording loads (EnterWorktree, ExitWorktree, Cron, SendMessage, ...) have no Claude Code text for the own-words check; loaded ones are already indexed; dead workers' logs in a container are still not swept.
- 2026-10-28 `os-fix-r2kernel` (from `os-structure` 1c1cd896), one commit per fix, a test failing before each, make check green after each (2239 passed, 52 skipped at the end; with Postgres 2291 passed; `make cluster` 13 passed): a hold is refused while another worker runs a task of the tree, and claim and hold serialize per tree on Postgres (D24; store contract on both stores, two Postgres interleaving tests); the snapshot version is hashed when the `Runtime` is made (`snapshots.pin`), so code upgraded on disk does not change a running worker's; local lease and hold clocks start before the request; `TaskView.read_log` / `child_tasks` stay in the tenant; a version 1 store's old holds are told `hold_lost` on upgrade; an unreadable log ends the task failed after three claims. The cluster's sub-agent now loads subtasks and hands back its NO-JOB report (the refusal made that path common, and with `SubagentHandback` offered the root's notice carried no last reply). Left: the shell plugin's `HELD_ELSEWHERE` text still names only "a background job" as the cause, and its `hold_lost` handling ignores epoch 0 (version 1 jobs carry no hold epoch); a child agent without the subtasks plugin gets `RESULT_NO_REPORT` instead of its last reply.
- 2026-10-28 `os-fix-r2rest` (from `os-structure` 81b5a24e), the three leftovers of `os-fix-r2kernel`, one commit per fix, a test failing before each, make check green after each (2266 passed, 52 skipped at the end; `make cluster` 13 passed; `make parity` 0 mismatches, own-words green; `make eval` 1.000 vs baseline 0.987, `delegate_parallel_review` timing out at 240 s in all three trials as in earlier runs): `HELD_ELSEWHERE` names both causes (another worker's background command or another task of the tree) and offers the foreground or a later try; an epoch 0 `hold_lost` marks lost the running jobs recorded without a hold epoch, and only those, in the task and, for an ended sub-agent, in the root; the `Agent` tool no longer expects `SubagentHandback` from an agent this worker knows is not offered it (no subtasks plugin, or the tool filtered out), so such a sub-agent's final message is its report, as in Claude Code; an agent this worker does not know (served on another queue) was still expected to hand back and lost its last reply without one (corrected in `os-fix-r3tools`: any sub-agent ending without a hand-back reports its last reply).
- 2026-10-28 `os-fix-r3kernel` (from `os-structure` 1f93291b), one commit per fix, a test failing before each, make check green after each (2268 passed, 54 skipped at the end; with Postgres 2322 passed; `make cluster` 13 passed): a hold takes back other workers' lapsed claims of the tree, so a worker that stalled past its lease cannot renew or append once another worker holds the tree (D25; store contract on both stores, a Postgres test that a claim of a lapsed task in flight is neither deadlocked nor waits out `deadlock_timeout`); only a failure to load a log counts toward `unreadable_log`, a later one (failing the turn failed too) is logged and the task claimed again; CHANGELOG and docs/server.md say an old sqlite binary does not refuse an upgraded store, so every old sqlite worker stops first.
- 2026-10-28 `os-cluster-test` (from `os-structure` 130518b7, merged e584ae84), one commit (ca357d89): `make cluster`, 3-5 worker processes started like `noeta worker` with a short lease on one Postgres (a throwaway `postgres:16` container or `NOETA_TEST_POSTGRES_DSN`, one database per test), driven only through the client; a test package (`tests/cluster/scripted`) registers a scripted provider whose reply is a function of the conversation, a journalled `Effect` tool and a fast-clock schedule; roots call `Effect`, start background `Bash` jobs, sub-agents and `ScheduleWakeup` timers and get messages mid-run (some twice under one dedupe key); checked: every root finishes, no side-effect token twice, every message taken once, log invariants (`tests/cluster/invariants.py`) and sampled rows; `NOETA_CLUSTER_SEED` replays a run. Four hold gaps were `xfail(strict=True)` at the merge (a waiting root's job lost silently when its holder dies; a dead worker's job writing on; a frozen holder's late job end; a finished job recorded lost when another worker took its end notice); `os-cluster-holds` closed them (88afbcba).
- 2026-10-28 `os-split-plugins` (from `os-structure` 275331a9, merged 459cc8bf), structure only, one commit per file: permissions extension into state, rule, shell and auto-mode modules (a23bab08); read-only program tables out of `permissions/scope.py` (b8bdeddd); hook firing and tool-call gates out of the hooks extension (e4272421); agent roster and worktree handling out of the subtasks extension (e2f523b1); the Skill tool and skill gates out of the skills extension (fb4ae14f); the Workflow tool out of `workflow/__init__.py` (65c806f8); the fs long `run`s: `ReadTool.run` (d0526f05), `NotebookEditTool.run` (3b093768), `read_pdf` (d83b7666). `plugins/shell/bash.py` was not split (no reason recorded).
- 2026-10-28 `os-split-host` (from `os-structure` 633d24c2, merged 538f7e45, an `mcp/extension.py` conflict resolved), structure only, one commit per file: `host/http.py` into guard, request handlers, streaming handlers and the OpenAPI document (141cacbb); `host/stream_json.py` control requests and message shapes (b7c5c336); `host/acp.py` session updates and live-output reconciliation (50636e3c); `cli/tui/app.py`, the transcript view as the app's base class (c61345fc); `cli/config.py` `from_settings` helpers (9588b5ba); the providers' wire halves (`_anthropic_wire` 55597c88, `_openai_compat_stream` 55801ae4, `_openai_responses_stream` c34ea11a); `execenvs/sandbox.py` path plan, bwrap arguments and Seatbelt profile (50043374); `execenvs/container.py` provisioning (2b550ab1); `mcp/sign_ins.py` (e0f4f822) and `mcp/oauth_discovery.py` (b10579fb). Not split: `core/runner/task.py` (`os-holdrace`, from the same base, was changing it), `core/runner/model.py` `call_model` and `backends/stores/postgres.py` (no reason recorded).
- 2026-10-28 S22–S40 against the audit's list (twenty files under nineteen numbers; the twentieth, `cli/config.py`, is counted with S40): S22 http 141cacbb; S23 permissions extension a23bab08; S24 scope b8bdeddd; S25 stream_json b7c5c336; S26 tui c61345fc; S27 acp 50636e3c; S28 hooks e4272421; S29 `core/runner/task.py` partial: only snapshot writing moved (d6d1aa57, `os-fix-r2kernel`), the audit's timers / closing split not done; S30 `call_model` not done; S31 providers 55597c88, 55801ae4, c34ea11a; S32 postgres partial: change feed and snapshots moved (e651bce6, `os-fix-r2kernel`) instead of the audit's connection module; S33 sandbox 50043374; S34 container 2b550ab1; S35 mcp e0f4f822, b10579fb; S36 `shell/bash.py` not done; S37 subtasks e2f523b1; S38 skills fb4ae14f; S39 workflow 65c806f8; S40 fs d0526f05, 3b093768, d83b7666 and `cli/config.py` 9588b5ba. The three undone splits are in Follow-ups: other agents are changing the runner, the stores and the shell plugin now, and a structural move this late would collide with them.
- 2026-10-28 `os-fix-r3docs` (from `os-structure` 1f93291b), the final review's docs findings, one commit each, make check green after each (2266 passed, 52 skipped, 88.26%; `make example` 13 passed): Progress entries for `os-cluster-test`, `os-split-plugins`, `os-split-host` and the S22–S40 mapping above; chat-bridge cut to about 60 lines of code, approving tool calls from the chat moved to `examples/chat-approvals` (its own listener on `POST /approve`, the thread naming the task); `docs/extending.md` gives reasons for LSP servers, output styles and themes; ADR-016 (deferred tool loading stays in the kernel for now, refines ADR-002) with its follow-up; ADR-015 (D24, refines ADR-011); CHANGELOG notes schema version 2; `scripts/parity/README.md` lists every committed parity file; `noeta trust` wording; run-tests tests for a failing run and a timeout. Left: the Follow-ups above.
- 2026-10-28 `os-fix-r3tenant` (from `os-structure` 1f93291b, rebased on 03570c0c), the final security review's two tenant P1s and the docs P3, one commit each with a test failing before it, make check green after each (at the end 2278 passed, 54 skipped, 88.29%): tenants' job logs, sandbox `TMPDIR` and temp places (D26); a tenant needs a workspace and never reaches into another's (D27); projects by tenant and only the task's own transcripts (D28); workflow scripts per task; PDF scratch in the tenant's temp directory; docs. Audit of the other places noeta writes for tasks in the branch report. WebFetch stays unrestricted for tenants (owner), listed under "Not a boundary".
- 2026-10-28 `os-fix-r3tools` (from `os-structure` 1f93291b), the final review's tool findings, one commit per fix, a test failing before each, make check green after each (2277 passed, 52 skipped at the end): an isolated agent's worktree is named from a hash of task id and call id, and crash recovery keeps one holding work; a sandboxed command in a linked worktree may write its own git directory and the shared `objects`, `refs`, `logs` (hooks, config, `packed-refs`, `commondir`, `gitdir`, `config.worktree` stay read-only; real bwrap test; `parity/deviations.md`); any sub-agent ending without a hand-back reports its last reply (the `os-fix-r2rest` line above corrected); failure-path tool texts reworded (Edit's several matches, read-before-write, Read's size and token ceilings, PDF page limits, missing `pdftoppm`, locked or unrenderable PDF, notebook outputs, the changed-file note, Bash's long-sleep refusal); unsaved work counts commits on the worktree branch as well as `HEAD`; ended background jobs kept per root bounded (64) and the ended-task list drops its oldest first; the MCP token file written through a unique temporary file, updates under an flock; the sandboxed Bash tool lists and resolved rosters bounded (64, least recently used out). `make parity` 0 (11 accepted), own-words green (one run showed the m1/web WebFetch digest request missing, the rerun 0); `make eval` 1.000 vs 0.987 (`delegate_parallel_review` timing out at 240 s as before). Left: `EnterWorktree`'s worktree is still read-only in the sandbox (`.noeta`); a `PytestUnraisableExceptionWarning` from a garbage-collected subprocess transport in `tests/plugins/test_hooks_events.py` appeared in 2 of about 14 full runs (not on the one baseline run; not reproduced in isolation).
- 2026-10-28 `os-kernel-struct` (from `os-structure` 03570c0c), one commit each, structural commits apart, make check green after each (2273 passed, 54 skipped, 88.23% at the end): S29 `core/runner/timers.py` and `core/runner/closing.py` out of `task.py` (733 -> 614 lines) 7f77dbf8; S30 `call_model`'s `_run_hooks` / `_on_overflow` 2afb4a0a; S32 `backends/stores/postgres_conn.py` (pool, opening, transaction retry; `postgres.py` 884 -> 798) 92523059; deferred tool loading out of the kernel into the `tool_search` plugin through the new `Extension.tool_changes` (D29–D31, ADR-017 supersedes ADR-016) 3181ce34. Full suite with Postgres DSN 2327 passed; `make cluster` 13/13; `make parity` 0 (11 accepted deviations), own-words green; `make e2e` 7 passed; `make eval` 1.000 vs 0.987 (`delegate_parallel_review` scored 1.00 with its three trials hitting the 240 s timeout). Left: `plugins/mcp/tools.py` and `plugins/mcp/config.py` docstrings still name `noeta.core.deferred` (the mcp plugin was being changed elsewhere); the `shell/bash.py` split.
- 2026-10-28 `os-struct-shell` (from `os-structure` 81878191), structure only, one commit each, make check green after each (2294 passed, 54 skipped, 88.32% at the end): S36 `plugins/shell/bash.py` 647 -> 299 lines into `state.py`, `output.py`, `git_prelude.py`, `cwd.py` and `sandbox.py`, `command_script` into `hardening.py`, `BashTool.run`'s rendering into `_result` 9dd4f24e; `plugins/shell/jobs.py` 794 -> 698, the log directory into `job_logs.py` 1911eebb; stale `noeta.core.deferred` references in the mcp docstrings and two parity rules be122964. Left as they are (each one class or one concern, already split once): `backends/stores/postgres.py`, `backends/stores/sqlite.py`, `core/runner/task.py`, `plugins/mcp/extension.py`, `cli/config.py` (its `HOME` is what tests patch). `make parity` 0 (11 accepted deviations).
- 2026-10-28 `os-fix-r4docs` (from `os-structure` 8a847cc2), the final review's round-4 findings, one commit each, behavior fixes with a test failing before, make check green after each (2305 passed, 54 skipped, 88.35% at the end): front ends on a server need no local model and send neither this machine's directory nor its model, `--cwd PATH` names one on the server, a cwd-less HTTP task gets the server's directory recorded, `-p --server` refused (D32, 2e7762fd); ADR-018 for D26–D28, refining ADR-014 (0ab7a68b); ADR-019, tenant limits and providers as kernel scheduling, refining ADR-007, with the reason in `docs/architecture.md` (D33, 14ebdadd); `docs/extending.md` counts a skill as one step (97d1312c); `~/.noeta` made 0700 and a new sqlite store 0600 (side files follow), existing ones kept with a notice (9af315a3); `cli/config.py` split into `home_dir`, `server_token`, `trust` and `plugin_policy`, `HOME` patched only in `home_dir` (676b1930).
- 2026-10-28 `os-fix-r4kernel` (from `os-structure` 8a847cc2), the final review's kernel findings, one commit per fix, a test failing before each, make check green after each (2307 passed, 56 skipped, 88.29% at the end; with Postgres 2363 passed; `make cluster` 13/13; `make parity` 0, 11 accepted, own-words green): the agent's allow and deny lists cover tools added through `tool_changes`, a refused added tool drops its extension's whole change set and the deferred tools it withheld are sent plain (D34) bee646f3; `Runtime.hold` asks again when a hold's commit is unknown (taking a hold extends the worker's own) f1a80eb9; snapshots kept per task and fold version, schema version 3 (D35) a28c97aa. Left: a hold whose last of three attempts commits and still raises `CommitUnknown` is reaped as before; `_unhold` and lease renewal do not retry on `CommitUnknown` (the hold runs out).
- 2026-10-28 `os-fix-r4tenant` (from `os-structure` 8a847cc2, rebased on 22c46102), one commit per fix, a test failing before each, make check green after each (2314 passed, 54 skipped at the end): tenant file tools on an allow list and tenant commands seeing no home or server directory (D36, D37; gate tests in bypass mode and after approval, an opened-file test, a real bwrap test with a temp `HOME`'s `~/.ssh` and the server directory's `.env`); MCP prompt commands kept per connection partition (two tenants interleaved); HTTP rewind through the guard with only request config and the task's queue. `noeta/cli/config.py`: one line (the sandbox fence's `server_dirs`). Left: names listed by `find -L` and glob expansion still cover home directories (as before, "Not a boundary"); a worker's `servers()` reads its cwd at check time.
- 2026-10-29 `os-tenant-relax` (from `os-structure` 3af861a2), owner feedback that D36/D37 were too strict: D38 replaces them; two commits (move `DEFAULT_DENY_READ` into `noeta/toolkit/sandbox.py`; the protected-set rule with gate tests in bypass mode and after approval, an opened-file test, real bwrap tests for the credential stores of two homes, the server directory, the operator's `denyRead`, `/opt`- and `/var`-like reads and a workspace inside the server directory), make check green after each (2328 passed, 56 skipped). ADR-014's file-fence line is to be refined at the review wrap-up. Left: credentials kept under other names or outside home directories are readable unless `denyRead` names them.
- 2026-10-29 `os-fix-r4tools` (from `os-structure` 3af861a2), the final review's tool findings, one commit per fix, a test failing before each behavior commit, structural commits apart, make check green after each (2328 passed, 56 skipped, 88.36% at the end): per the owner's change of scope (D39, ADR-020) the own-words check is an advisory report that always exits 0 and no test fails on it, and the `Agent`, `ToolSearch` and background-start texts are not reworded (a word-run rule was measured, 7 words flagging 157 runs, 8 flagging 58, 9 flagging 25, and dropped); the report no longer misses a text copied from its first character; a background job let go of removes its log files, so a long root keeps at most 64 ended jobs' logs on disk (a gone worker's `w<pid>` in an env that is not this machine is left, documented in `job_logs.py`); ExitWorktree says when `git branch -D` kept the branch; ToolSearch's `+word` matches names only and an infinite `max_results` means the default; `Registry.get` removed and one file-removal path (structural); every tool description loads through `toolkit.tools.load_description`, the monitor and web texts moved into `descriptions/` (structural); `make parity`'s scratch home is 0700 (an open home's notice was its one mismatch). `make parity` 0 (11 accepted deviations), own-words none. No prompt text changed (only ExitWorktree's new branch-kept result, shown when git refuses), so `make eval` and `make e2e` were not run.
- 2026-10-29 `os-fix-r5kernel` (from `os-structure` 49c277e3), the final review's round-5 kernel findings, one commit per fix, a test failing before each, make check green after each (2343 passed, 58 skipped, 88% at the end; with Postgres 2401 passed; `make cluster` 13/13): children go to the parent's queue via `Claim.queue` (D50) 523928aa; a hold is refused unless the holder serves every unended task's queue (D51) 9df478f0; a newer version's log is left to a newer worker (D52) f4958c63; usage charged as a call returns and `spawn(history_from=)` tenant-checked (D53) f85ff3d8, b1859d47; unhold asked twice 20ee8882; `Runtime._unreadable` bounded at 1,024 9eed3be5; a test-isolation fix (`test_missing_extras` left `noeta.cli.tui` rebound, failing `test_server_attach` by xdist scheduling) 5cb13505. Outside the kernel: `noeta/plugins/subtasks/extension.py` two lines (`queue` defaults to `None`, its docstring). Left: a hold is not re-checked on renewal; an inbox item of a kind this version does not know is not covered by D52; a second failed unhold still ends in a false `hold_lost`.
