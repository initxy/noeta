# Changelog

All notable changes to Noeta are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
Noeta is pre-1.0: while on `0.x`, minor versions may carry breaking changes.

## [Unreleased]

## [0.6.17] - 2026-08-28

Covers `noeta-runtime` only: 0.6.16 → 0.6.17. `noeta-sdk` stays at 0.6.16
(the fix lives in the kernel's `create_task` / fold, not in a built-in), so
the sdk's `noeta-runtime>=0.6.16` floor is unchanged — a fresh install
resolves the dependency to 0.6.17.

### Fixed — the seeded environment resident described the host, not the container

`Engine.create_task` emitted `TaskHostBound` but returned a `Task` with an
unfolded `GovernanceState`, so the `host.resolve_engine(task)` that
`InteractionDriver.seed_start` runs a few lines later read neither the
session's `workspace` nor its `exec_env_ref` and silently fell back to the
host-fixed default dir with no `ExecEnv`. The Engine that build produced is
the one driving the pre-loop content init, so the `<workspace-environment>`
block was captured against the wrong root — and because that resident is
activate-once (`refresh=False`), the mis-captured block was the task's for
life. A sandboxed session therefore told its model `Working directory:
<host path>` for every turn while its tools were rooted at the container
workdir, and a model that trusted the block opened with a `cd` into a
directory that does not exist inside the container. Subtasks were unaffected
(their Engines resolve off a folded Task), which is why the divergence read as
a root-task-only quirk. `create_task` now lands the binding it just wrote on
the Task it returns, through the same `apply_host_binding` the fold handler
uses so the two cannot drift.

## [0.6.16] - 2026-08-27

Covers both packages, lockstep: `noeta-runtime` 0.6.15 → 0.6.16 (the driver
verb) and `noeta-sdk` 0.6.15 → 0.6.16 (the `Client` pass-through), with the
sdk's floor raised to `noeta-runtime>=0.6.16`.

### Added — `inject_goal(drive=False)` refuses to drive a parked task

`InteractionDriver.inject_goal` / `Client.inject_goal` take `drive` (default
`True`, the existing behaviour). With `drive=False` a task suspended on the
next-goal handle no longer falls through to `send_goal` on the caller's
thread: it raises the typed `NotResumableError` (`expected="a running turn"`)
and writes nothing, so a caller that must never drive a turn itself — a host's
wake pump handing batches to a resident worker pool — seeds the follow-up
through `seed_send_goal` / `dispatch_seeded` instead. Closes the window in
which a task that parked between the caller's status read and the injection
pulled the whole turn onto the caller. The running landing is unchanged.

## [0.6.15] - 2026-08-26

Covers both packages, lockstep: `noeta-runtime` 0.6.13 → 0.6.15 (the intake
seam + `Engine.record_content` half; 0.6.14 was an sdk-only tag) and
`noeta-sdk` 0.6.14 → 0.6.15 (the memory built-in half), with the sdk's floor
raised to `noeta-runtime>=0.6.15`.

### Changed — a recalled memory body enters a task once, as a resident

Auto-recall re-injected the same tier-1 bodies on every goal of a long-lived
task: a host that maps one chat channel to one task saw two memories recorded
eleven times over eleven wakeups — 58% of the transcript — and even machine
receipts triggered it, because a slug token appeared in every event envelope.
A tier-1 hit is now recorded as a `memory`-kind content-channel resident
(`ResidentActivation` → `Engine.record_content`, activate-once per task): the
body renders once — semi-stable on the opening goal, right after a later goal,
re-hung after a compaction summary — as one `origin="memory"` message, and
the name is silent in both tiers on every later goal of that task, at any
hash (the index resident and `memory_read` cover freshness). Pointer hits
(tier-2, recall judge, over-budget bodies) still ride one `origin="memory"`
follow-up turn per goal; a pointer for a resident name is dropped. The
follow-up turn therefore no longer carries bodies.

- `noeta.execution.reminders.ResidentActivation` (also on `noeta.sdk`): a
  `reminder_provider` may return it next to `Reminder`; the intake seam
  records activations after the goal and before the reminder turns.
- `Engine.record_content` — the in-turn twin of
  `SessionRecorder.record_content`, same gate.
- The `memory` renderer renders every non-index active name as a recalled
  body (`format_recalled_body`, `MEMORY_BODY_VERSION`); `recall_memories`
  takes `resident=` and `resident_memory_names` reads the activation map.

## [0.6.14] - 2026-08-25

Covers `noeta-sdk` only; `noeta-runtime` stays at 0.6.13 (the runtime is
untouched — the change lives in the memory built-in), so the sdk's
`noeta-runtime>=0.6.13` floor is unchanged.

### Fixed — `memory_write` no longer drops frontmatter fields a rewrite does not mention

The per-field merge used the *text's* fence as its base and read only
`created` back from disk, so any field the new write did not restate —
`description` / `type` / `keywords`, and every key the tool does not
recognize — vanished on rewrite. In practice that was every body-only
rewrite: the tool description tells the model not to write a fence, and a
consolidation curator's `keywords` live only on disk, so the very fields the
merge existed to protect were the ones it lost. The merge now layers disk
fence < text fence < parameters, each overriding only the fields it names;
naming a key with an empty value is the one way to remove it. The tool
description, `docs/reference/tools.md` and `CONTEXT.md` state the rule.

## [0.6.13] - 2026-08-21

Covers both packages, lockstep: `noeta-runtime` 0.6.11 → 0.6.13 (the
recorder/SPI half; 0.6.12 was an sdk-only tag) and `noeta-sdk` 0.6.12 →
0.6.13 (the workspace built-in half), with the sdk's floor raised to
`noeta-runtime>=0.6.13`.

### Fixed — the workspace-environment block no longer busts the prompt cache on resume

The `<workspace-environment>` resident carried a per-second `Captured at:`
wall-clock line (plus live git branch/status), and its snapshot is re-captured
whenever the engine is rebuilt — so driving the same task from a fresh process
(one process per turn, the common server deployment) re-recorded the resident
with new bytes on every `send_goal`. That moved message #0, invalidated every
cached prefix behind it, and dropped the provider's cache read back to the
system+tools breakpoint: the whole transcript re-primed each turn, at a cost
that grew with conversation length.

The resident now records **activate-once**: `SessionRecorder.record_content`
gained `refresh: bool = True` (runtime), first-write-wins when `False`, and the
workspace pack's environment init records with `refresh=False` (sdk). The
task's first capture is the one the composer keeps resolving — same process or
a resume in a fresh one — so message #0 stays byte-identical for the task's
whole life, matching Claude Code's memoized git-status/date semantics. The
rendered block now says so (`ENVIRONMENT_VERSION` 2 → 3): a snapshot note
after the git lines tells the model the branch/status are from task start and
to run git itself for live state. Deterministic residents (memory index,
instructions) keep the default refresh semantics — an unchanged source still
appends nothing, a real content change still records exactly one refresh.

## [0.6.12] - 2026-08-14

Covers `noeta-sdk` only; `noeta-runtime` stays at 0.6.11 (the runtime is
untouched — every change lives in the web built-in, the presets, and the
SDK client wiring), so the sdk's `noeta-runtime>=0.6.11` floor is unchanged.

### Changed — `WebFetch` aligned with Claude Code's surface

`WebFetch` now takes `url` **and `prompt`** (both required) and answers the
prompt against the fetched page with one auxiliary model call, returning the
answer instead of the raw rendering — a fetched page no longer floods the
calling model's context. The digest runs on the session's own provider; the
new wiring knob **`Options.webfetch_model`** (alias-resolved, excluded from
identity like `compaction_model` / `recall_model`) routes it to a cheaper
model, and `None` keeps it on the session's main model. With no provider wired
(direct tool construction) or on a digest failure, the tool degrades to the
previous raw-render behaviour, and the full rendering is still offloaded as
the ContentStore audit artifact either way.

Three more Claude Code parity behaviours landed with it: `http://` URLs are
upgraded to `https://` before fetching; a redirect to a **different host** is
returned to the model to re-issue explicitly instead of being silently
followed (same-host redirects still follow, bounded); and fetched pages are
cached per URL for 15 minutes (successes only), so a follow-up `prompt` about
the same page re-digests without re-fetching. The container (sandbox) egress
path keeps byte-parity on all of this: `curl` no longer follows redirects
itself — hops resolve in Python off `-w` status metadata (needs curl >= 7.63),
and an HTTP >= 400 fails with the status named exactly like the httpx path.

The `explore` / `plan` preset prompt also gained one rule: a web fetch that
fails or times out is reported and routed around — never re-hammered against
the same host.

## [0.6.11] - 2026-08-11

Covers both packages, lockstep at 0.6.11 (`noeta-sdk`'s `noeta-runtime>=` floor
rises with it — `Client.task_answer` reads the new `TaskSuspended.answer`).
Everything here is additive; no existing call changes shape.

### Fixed — a typo'd task id no longer corrupts the store

`cancel` / `interrupt` / `close` / `reopen` write their control-plane marker
through `system_emit`, which creates the stream as a side effect of appending.
Three of the four folded *after* that write, so one unknown `task_id` durably
minted a stream whose first event was `TaskCancelled` / `TurnInterrupted`. Every
later `fold` of that stream raised — and because `list_task_summaries` scans
every stream, one poisoned id took the whole store's task list down until the
task was deleted.

The guard now runs before any write, in one shared place rather than per verb,
and refuses with the new typed **`UnknownTaskError`** (code `unknown_task`,
carrying `task_id` / `verb` / `reason`) instead of the raw `ValueError` a fold
would give. It is a `RuntimeError` like its lifecycle siblings, so an
`except RuntimeError` contract is unaffected, and `reason` tells "never existed"
apart from "already poisoned" — a store written by an older version still
carries such streams.

### Added — a parked turn keeps its terminal answer

A multi-turn conversation never writes `TaskCompleted`: `MultiTurnReActPolicy`
rewrites the terminal `FinishDecision` into a next-goal suspend so the ledger
stays open. `FinishDecision.answer` had nowhere to go in that substitution, so a
host that wanted both "resumable conversation" and "structured terminal answer"
could have either, not both — with `Options.output_schema` set it had to discard
the kernel's deserialization and re-parse the assistant text.

- `TaskSuspendedPayload` gains `answer` / `answer_ref`, spilled exactly like
  `TaskCompletedPayload`'s, and `YieldForHumanDecision` gains `answer` to carry
  it there. `answer_from_payload` now reads both payloads.
- Byte-compatible with every older recording: `__canonical_omit_none__` keeps a
  `None` out of the stream, so a suspend that stands in for no finish is
  byte-equal to one recorded before the field existed. No `schema_version` bump.
- ADR: `terminal-answer-on-a-parked-turn`.

### Added — SDK surface for what the kernel already did

- **`Client.task_answer(task_id)`** — the latest turn's terminal answer as the
  *raw* value, off whichever lifecycle event that turn landed on. `messages()`
  renders the same value through `str()` for the transcript, which turns an
  `output_schema` dict into Python repr; take this one when you want the value.
- **`attachment_texts`** on `start` / `send_goal` / `seed_start` /
  `seed_send_goal` — host-composed reference snapshots (`@` mentions, a
  briefing, a workspace summary) recorded as their own `origin="system"`
  messages *before* the goal. It existed on the driver; `Client` did not forward
  it, so a host had to reach into `Client._host`.
- **`Reminder`, `RecallView`, `ReminderProvider`, `TURN_INTAKE`** exported from
  `noeta.sdk` — the other intake channel, for text that must be computed while
  the turn is recorded and lands *after* the goal. A plugin could declare a
  `reminder_provider` contribution but not write its value with public names.
- **`UnknownTaskError`** exported alongside its lifecycle siblings.

### Documentation

- The README now leads with what the runtime is for — server-ready deployment,
  every capability as a plugin, sixteen extension surfaces — instead of three
  diagrammed sections on fold, leases, and the package split. That prose and its
  diagrams moved to the concepts index, where a reader arrives wanting them.
- New **[Benchmarks](https://initxy.github.io/noeta/benchmarks/)** page:
  Terminal-Bench 2.1 (82.5% on a 40-task stratified sample) and SWE-bench
  Verified (86.7% on a 15-instance subset), run on the official harbor harness
  by `noeta-agent`'s `main` preset. Labelled as samples, with methodology,
  exclusions, and re-runnable commands.
- Corrected in the reference: `Options.output_schema` does **not** mount the
  `structured_output` control tool — it instructs the model natively through the
  provider. The control tool is gated on the per-helper schema a subtask or
  workflow helper is spawned with.

## [0.6.10] - 2026-08-08

Covers both packages, lockstep at 0.6.10 (`noeta-runtime` jumps from 0.6.4;
`noeta-sdk`'s `noeta-runtime>=` floor rises with it — the ReAct policy reads
the new `StepContext.steps_in_turn` field).

### Fixed — loop state no longer outlives its task or turn

The Engine cache deliberately shares one Engine (and its Policy) across every
task with equal bindings, but the ReAct policy carried task/turn-scoped
mutable state on the instance. Three consequences, all fixed:

- **The `max_steps` counter accumulated across turns — and across
  conversations.** A long multi-turn session eventually exhausted the cap and
  then EVERY later turn park-failed with `react_max_steps_exceeded` until
  something rebuilt the Engine (edit a role file, restart the process). The
  counter now rides `StepContext.steps_in_turn`, threaded by the Engine's
  step loop, so the budget is per driven turn and renews on every human
  message. (`ReActPolicy` no longer has `_step_count`; a custom Policy doing
  its own capping should read `ctx.steps_in_turn`.)
- **The compaction-trigger baselines bled across tasks.** One conversation's
  near-window real-usage baseline could make a fresh conversation's first
  turn fire the proactive trigger and die non-retryable on
  `compaction_no_progress`. The baselines (`last_estimate_at_call` /
  `last_input_tokens_at_call`) now live in a bounded per-`task_id` table
  inside the policy; semantics per task are unchanged. (Signature note:
  `_trigger_estimate` / `_observed_density` / `_summary_boundary` and friends
  now take the per-task state explicitly — private surface, but tests that
  poked the old attributes must switch to `_baseline_state(task_id)`.)
- **A schema-carrying workflow helper claimed by a resident worker lost its
  structured-output contract.** `resolve_engine` now reads the durable
  `TaskCreated.inputs.output_schema` for subtasks and builds the
  schema-shaped engine UNCACHED (same choice as the drain), so the
  `structured_output` mount + `StructuredOutputPolicy` receipt survive the
  untargeted-claim path and never leak into the shared cache.

### Changed

- `max_steps` defaults are now deliberately enormous (1,000,000 — SDK host,
  session-inputs builder, and `ReActPolicy` alike): the cap is a
  runaway-loop backstop, not a working budget, and with per-turn semantics a
  real turn can never reach it. Hosts that want a tight per-turn ceiling can
  still set one explicitly.
- `Budget` docs now spell out the dimension: its caps
  (`max_iterations` / `max_tool_calls` / `max_cost_usd`) accumulate over the
  task's WHOLE life — on the multi-turn path that means the whole
  conversation, durably — so they must be sized for the longest conversation
  you are willing to fund, not for one turn.

## [0.6.9] - 2026-08-08

Covers `noeta-sdk` only (`noeta-runtime` stays at 0.6.4). Patch bump per
default policy; note the new system requirement below.

### Changed — Grep/Glob now run on ripgrep

- `Grep` and `Glob` shell out to ripgrep through the `ExecEnv` seam instead
  of walking with Python `re` / `pathlib`. **rg is now a hard requirement of
  these two tools** — a missing binary fails with an install hint, and a
  sandbox container image must include it. One engine, one dialect: the
  pattern language is rg's (linear-time; lookaround and backreferences are
  rejected by rg itself, so the conservative ReDoS pre-screen and its false
  rejections are gone), and the walk carries rg's defaults — gitignore-aware
  inside a git repository, hidden and binary files skipped, symlinks not
  followed. `Glob` keeps pathlib pattern semantics (`*.py` stays top-level,
  `**/*.py` recurses) over the `rg --files` walk.
- `Grep`'s parameter surface now matches the reference agent's schema
  (verified byte-level against Claude Code 2.1.226): `context` with `-C` as
  its alias, `-n` defaulting to true in content mode, `-o` (only-matching),
  `type` passing through to rg's full file-type list, `head_limit` read as
  `head -N` over output lines/entries (default 250; 0 = unlimited), and a
  new `offset` that skips like `tail -n +N`.
- `-u: true` (`--no-ignore --hidden`) is the one extension beyond that
  surface: it searches gitignored and hidden files, and a zero-match answer
  names it instead of reading as "nowhere in the tree".

### Fixed

- A tree containing an unreadable file no longer fails the whole search: rg
  exits 2 in that case even after printing every reachable match, and that
  now counts as a clean per-file skip (a spoken stderr — bad regex, unknown
  type — still fails loudly). An rg stream that overflows the capture cap
  appends a "results are partial" note instead of truncating silently.

## [0.6.8] - 2026-08-08

Covers `noeta-sdk` only (`noeta-runtime` stays at 0.6.4). Patch bump: one
WebFetch bug fix.

### Fixed

- `WebFetch` on a page whose body renders to empty Markdown (blocked, empty,
  or script-only) now degrades to `success=False` with a "try another source"
  summary. It used to report success with zero bytes, which reads as "the
  page had nothing on it" and stopped the model from trying another source.

## [0.6.7] - 2026-08-08

Covers `noeta-sdk` only (`noeta-runtime` stays at 0.6.4). Patch bump:
additive catalog surface, no breaking change.

### Added — operator-extensible model catalog

- `HostConfig.extra_models` and `noeta.sdk.providers.register_models`: operator
  `ModelSpec` rows (internal gateway routing names, self-hosted models) join
  the shipped catalog through a registration overlay consulted by every
  lookup — spec resolution, pricing, compaction derivation, vision gating,
  family classification. Collisions with shipped rows refuse loudly at build;
  identical re-registration is a no-op; registration is all-or-nothing and
  thread-safe. Extension aliases may target shipped shorthand.
- `ModelSpec.provider_family`: an extension row whose id carries no vendor
  prefix can declare its wire family explicitly (validated at registration).
- `noeta.sdk.providers.find_spec` (merged point lookup) and `catalog_models()`
  (merged enumeration for model pickers). `CATALOG` remains the shipped table.
- Docs: the "register uncatalogued models" how-to now teaches registration
  instead of direct `CATALOG` mutation, which bypassed every collision rule.

## [0.6.6] - 2026-08-08

Covers `noeta-sdk` only (`noeta-runtime` stays at 0.6.4). Patch bump:
additive skill-discovery and instructions-discovery surface, no breaking
change.

### Added — skill directories across ecosystem conventions

- The vendor-neutral `<workspace>/.agents/skills` directory is indexed by
  default as a tier just below `<workspace>/.noeta/skills`, so a repo carrying
  skills in the shared convention works unchanged while the Noeta-native
  directory keeps sovereignty on name clashes.
- New opt-in tiers via `plugin_config["skills"]`: `extra_skill_dirs` (borrowed
  foreign directories such as `~/.claude/skills`, riding the lowest band after
  built-in and plugin packs) and `global_agents_skills_dir`
  (`~/.agents/skills`, just below the global `~/.noeta/skills`). Home-scoped
  and foreign directories are never scanned by default.
- `workspace_skills_trust` (`"open"` default / `"trust-store"`): gates both
  repo-derived workspace tiers on the plugin trust store — one `grant_trust`
  covers plugins and skills. The gate keys on the host-side workspace path
  (`trust_subject`), never a sandbox container path; an unknown value fails
  the session build instead of failing open. Skipped tiers produce an
  `UntrustedWorkspaceSkillsWarning` carrying the subject and directories as
  attributes.
- An operator `skills_dir` override now pins the workspace-scoped skill set:
  the `.agents/skills` tier does not mount beneath it.

### Added — CLAUDE.md as an instructions candidate

- The workspace-instructions search order is now `NOETA.md` → `AGENTS.md` →
  `CLAUDE.md` (first non-empty wins), for the root file and read-triggered
  subdirectory discovery alike, so existing Claude Code repos work without
  renaming anything.

## [0.6.5] - 2026-08-06

Covers `noeta-sdk` only (`noeta-runtime` stays at 0.6.4). Patch bump:
prompt-preset and tool-description text only, no API change.

### Changed — agent prompts aligned with Claude Code's communication guidance

- `main`/`main-web` presets: rule 7 now caps mid-run narration — one sentence
  before the first tool call, then quiet by default (at most a one-line status
  note on a load-bearing fact or direction change; never announcing the next
  tool call, never restating an explanation). Rule 2 restricts comments to
  constraints the code cannot show; rule 8 asks for brevity through
  selectivity (complete sentences, shape matched to the question); rule 9 adds
  "recommend, don't survey".
- `general-purpose` preset: dropped the ReAct-era "reason briefly before each
  tool call" rule — with thinking-capable models it double-spends reasoning
  and trains narration.
- `Read` tool: nudges partial reads (`offset`/`limit`) when the target region
  is known, and forbids re-reading a file just to verify your own edit.
- `Bash` tool: the avoid-list widens to `cat`/`head`/`tail`/`grep`/`find`/
  `sed`, and `Edit` joins the preferred dedicated tools.
- `Task` tool: once work is delegated, the caller must not redo it inline.
- `skill` tool: dropped the dead-weight Preconditions section (it described
  when the tool is absent — unobservable by any reader of the description).

## [0.6.4] - 2026-08-06

Covers both packages (lockstep). Patch bump: additive API
(`ControlTranslateContext.view`, `ReminderView.summary_boundary`, the
`RecallHistory` control tool + `collapsed-context` reminder) plus a
compaction-note template change.

### Added — compaction escape hatch: the collapsed originals stay reachable

- New `RecallHistory` control tool (react built-in, `recall_history` host
  flag, schema/routing band 550): renders a bounded, deterministic slice of
  the compaction-collapsed prefix (`rolling_history[:summary_boundary]`)
  straight off the composed View at translate time — no runtime handler, no
  new Decision or event vocabulary. Conversation-born content (error text,
  earlier model output) lives in no file, so `Read` could never recover it;
  this channel can. Recall output re-enters history as a normal tool result,
  so the tail prune clears it once it ages out.
- New `collapsed-context` compose-time reminder (react built-in, priority
  350): while `summary_boundary > 0`, points at the live collapsed range and
  names `RecallHistory`. Backed by the widened `ReminderView.summary_boundary`
  projection; `default_reminder_specs()` now collects `reminder`
  contributions from every built-in manifest, not just `reminders`.
- `ControlTranslateContext` gains a neutral optional `view` field (threaded
  through `translate_control_tool`), so a control tool can answer from folded
  state.

### Changed — the compaction note carries end-of-span continuity sections

- `summarize.md` gains sections `8. Current Work` and `9. Next Step`,
  describing the END of the span the note covers (the summarize input stops
  at the boundary), with an explicit defer-to-later-messages sentence and an
  anti-tangent guardrail adapted from Claude Code's compact prompt. Both are
  rewritten on every pass — the one exception to note carry-forward. This
  amends the context-compaction ADR (rejected alternative 8 partially
  adopted, reframed): model-derived in-flight work previously vanished at the
  note/tail seam once it aged past the protected tail.
- Section 5 (All user messages) is hardened against transcript-shaped
  injection: only user-role turns count as user messages, and quoted
  "user:"-style text inside assistant messages is attributed to the
  assistant.
- Section 6 (Pending Tasks) now includes follow-up work the assistant
  identified while working, not only work explicitly requested.

### Changed — main-agent prompt: exploration budget, delegation trigger, HITL respect

Trace-driven fixes to `main.md` / `main-web.md` (from a real task that spent
82.6% of wall time exploring before its first edit, burned 15 minutes in a
search rabbit hole, and overruled the user's answer in its final summary):

- Rule 1 scopes the search mandate: a named target or few-file change means
  locate-and-edit, not a repo survey, and an answered search is never
  repeated.
- Rule 9 gains a spin self-check: consecutive rounds with no edit and no new
  fact mean stop exploring — act on what you have or name the blocker.
- Rule 10 gains a delegation trigger: a hunt that takes more than a few
  rounds goes to an `explore` sub-agent.
- New rule (12 in `main.md`, 13 in `main-web.md`): a user's answer to a
  clarifying question settles the matter — conflicts get surfaced and
  re-asked, never silently overruled.

## [0.6.3] - 2026-08-05

Covers both packages (lockstep). Patch bump: additive API
(`StepContext.cancelled`, `AbortedError`, `StreamingProvider.should_abort`,
`interrupt(force=...)`) plus interrupt-latency bug fixes.

### Changed — interrupt lands in milliseconds, not after the round returns

- Pressing Stop no longer waits out the in-flight work. Interrupt still lands
  at the turn boundary, but the boundary is now reached promptly, because the
  cooperative-cancel mark reaches into the blocking waits themselves:
  - **The LLM round-trip is an abandonable wait.** With a cancel seam wired,
    `RuntimeLLMClient` runs the provider call on an I/O thread the step thread
    can stop waiting for — safe because `LLMProvider` is contractually pure —
    so a stop lands in milliseconds in any phase, including pre-first-byte
    silence. The abandoned round records the normal Started/Recorded/Finished
    trio with a new `category="aborted"` error response; streaming deltas are
    muted the moment the wait is abandoned.
  - **The transient-retry loop is cancel-aware.** Previously an interrupt
    during a rate-limit backoff sat through up to 8 further provider calls and
    ~2 minutes of sleep; now each attempt re-checks the mark and the backoff
    is sliced around it.
  - **Streaming adapters abort mid-stream.** `complete_streaming` grows an
    optional `should_abort` predicate (folded into the signature like
    `request_headers`); the three builtin adapters poll it per SSE event and
    close the connection, so an abandoned call stops burning tokens within a
    chunk interval. The runtime probes the adapter signature, so third-party
    adapters on the old signature keep working unchanged.
  - **Tool batches poll between calls.** A stop landing during call N of a
    parallel batch closes calls N+1… with paired `success=False` interrupted
    results (history stays balanced for the resumed request) and unwinds.
  - **Foreground shell commands are reaped.** Interrupt / cancel / close now
    kill a running foreground shell's process group exactly like background
    jobs (SIGTERM → grace → SIGKILL); the tool returns a failed
    "interrupted" result instead of blocking to its timeout.
  - The compaction summarize call inherits cancellability; the memory recall
    judge's provider call gets a bounded (default 10 s), abort-aware wait so a
    wedged judge can never stall turn intake.
- Recordings are untouched: resume/replay paths carry no cancel seam and stay
  byte-identical.

### Added — `interrupt(force=True)`: the double-Esc hard stop

- For a step wedged past every cooperative seam (a tool ignoring its
  timeout): force-clears the wedged step's lease (`dispatcher.enqueue`'s
  documented force-clear — the abandoned thread is lease-fenced, its late
  writes rejected), seals the dirty attempt via step-attempt recovery, and
  settles the task at the interrupted next-goal suspend. The conversation
  resumes by typing. `Client.interrupt` passes `force` through; map double-Esc
  to it in interactive frontends.
- `_force_terminal_on_lost_lease` now leaves a **suspended** task alone (a
  durable suspend is a resumable landing); only a genuinely stranded
  `running` fold is converged to terminal.

### Fixed — interrupts that were silently dropped

- **Yield-window drop:** an interrupt landing in the `release_yield` hand-off
  window (worker-pool path, between seeding a turn and a resident worker
  claiming it) armed nothing — Esc was a silent no-op and the worker drove
  the whole turn. The turn-in-flight gate now also recognises the folded
  `running` status.
- **Delegation drop:** an interrupt during a foreground delegation neither
  armed the mark (the root rests suspended on its member wake with no lease)
  nor settled the root — it was left stranded on a wake that could never
  fire, with a dangling spawn tool call and a stale cancel mark that
  pre-aborted the next turn. Both fixed: the gate treats a
  delegation-suspended root as in flight, and an interrupted drain now
  cascade-cancels the children, closes dangling spawn calls with failed
  interrupted results, parks the root at the interrupted next-goal suspend,
  and drops the mark.

See the new ADR `docs/adr/interrupt-responsiveness.md` for the design
(abandonment-over-cancellation, force-as-enqueue) and rejected alternatives.

## [0.6.2] - 2026-08-05

Covers both packages (lockstep). Patch bump: additive API (memory keywords /
recall judge / write-time stamps, interrupt-withdraw) plus internal breaking
changes tolerated pre-1.0.

### Added — interrupt withdraws a pending user question (Esc semantics)

- `interrupt` (Stop) now handles a task **suspended on a pending
  `ask_user_question`**. Previously that suspend had no turn in flight, so
  `interrupt` only wrote a `TurnInterrupted` marker and the question stayed
  stuck; the only escapes were `answer` (must supply a valid answer), `cancel`
  (terminal, not reopenable), or `rewind` (discards the turn's output). Now
  `interrupt` **withdraws the question**: it writes a new neutral
  `UserQuestionWithdrawn` audit event, closes the dangling ask tool call with a
  paired `success=False` tool result (so a later turn's transcript stays
  well-formed), and parks the conversation idle at the next-goal suspend —
  **without driving a model turn**. This is the "Esc" landing: the question
  clears, the prior turn's output stays in history, and the user resumes by
  typing. `Client.interrupt` is unchanged — the frontend Stop button needs no
  change. Approval suspends are out of scope (they keep `deny` as their
  graceful escape).
- New event `UserQuestionWithdrawn` (`UserQuestionWithdrawnPayload`) with its
  fold reducer (pops `pending_questions`, records no answer), payload restorer,
  and audit classification. `AskAnswerCodec` grows a `question_id_from_handle`
  field so the kernel recognises a question suspend without hardcoding the
  handle prefix or importing the built-in.

### Added — memory: cross-lingual keywords, date stamps, ledger receipts, write-time dedup

- Frontmatter `keywords` (comma-separated retrieval aliases; `，`/`、`/`;`
  separators accepted) joins the recall match surface as a tier-2 rule. Each
  item matches the text as a WHOLE phrase — word-prefix for ASCII items
  (`deploy` meets "deployment"/"deploys", never mid-word) and substring for
  CJK — so items neither decompose into noisy bigrams nor miss English
  inflections, and deliberate short terms (`ci`) floored out of name/summary
  matching stay reachable. This is the deterministic cross-lingual bridge — a
  Chinese question now reaches an English-named memory through its Chinese
  keywords (auto-recall was silently dead across languages: CJK bigrams and
  English word tokens never intersect). `memory_write` grows a `keywords`
  parameter; the memory policy and consolidation prompts instruct models and
  the curator to maintain the aliases. Keywords are matcher-only: never
  rendered into the index, so keyword maintenance moves no index bytes.
- `memory_write` always stamps `created` (sticky across rewrites) and
  `updated` (always today) into the frontmatter, plus a `source_task` ledger
  receipt when the runtime threads a task id — a doubted memory can be checked
  against the session that wrote it instead of trusted or discarded.
- `memory_write` under a NEW name reports similar existing memories
  (name/summary token overlap, advisory — the write still lands), so the model
  can merge while the context that caused the write is live. Param frontmatter
  now merges per-field with any fence the text carries instead of replacing it
  (the old rule silently destroyed unrecognized fields on every rewrite).
- The consolidation prompt gains explicit duties: resolve contradictions
  BETWEEN memories (keep the newer fact, archive the older, note the
  supersession) and maintain cross-lingual keywords.
- **Recall judge** (`Options.recall_model`, off by default): when set, a
  lexical auto-recall miss at turn intake is retried through one small-model
  call — the model reads the incoming message plus the memory index (aliases
  included) and picks the memories worth surfacing; picks ride in as tier-2
  pointers and are recorded like any recall, so replay never re-judges. A
  lexical hit never spends the call; any judge failure degrades to a plain
  miss. Same knob pattern as `compaction_model` — hosts typically point both
  at the same cheap model.
- **Breaking (SDK internal shape):** `MemoryStore.entries()` /
  `MemoryEntries` are now `(name, summary, type, keywords)` quadruples (was
  triples). Match primitives moved to
  `noeta.builtins.memory.impl.matching` (re-exported from `impl.index`).

## [0.6.1] - 2026-08-04

Covers both packages (lockstep). Patch bump: bug fixes plus additive API
(new plugin surfaces, `Options.compaction_model`, `requires-noeta`).

Claude Code mechanism alignment (see
`docs/implementation-specs/claude-code-mechanism-alignment.md`): compaction,
catalog/adapters, memory recall, skill frontmatter semantics, and the plugin
surface closure.

### Fixed — sandbox file reads were corrupt (adapter/API contract mismatch)

- `AioSandboxExecEnv.read_bytes` sent `encoding="base64"` to `/v1/file/read`
  and base64-decoded the response — but `FileReadRequest` has no `encoding`
  field (confirmed against the v1.11.0 OpenAPI; read is asymmetric with
  write, which does take it), so the server returned plain text and every
  sandbox read either failed loudly or silently returned corrupt bytes;
  binary reads were impossible. `read_bytes` now transfers via `base64 -w0`
  through `/v1/shell/exec` (byte-exact for any content, spill-aware, stat
  guards refine missing/unreadable into `FileNotFoundError` /
  `PermissionError`), and `read_text` reads `/v1/file/read` natively as the
  text endpoint it is. The guard runs in a subshell — the exec session's
  shell is persistent, and a bare `exit` wedges the request. Caught by the
  live container E2E (`TestLiveExecEnv`), which now pins text, binary
  (every byte value), and missing-file round-trips.

### Fixed — compaction survives real traffic

- The summarize request now carries the live tool schemas (was `tools=[]`
  against tool_use-bearing history — a provider 400 that killed the task on the
  first real proactive compaction — and it forfeited the cached prefix).
- Bounded summarize input: each compaction summarizes the previous note plus
  the delta since it (was: the full raw history from index 0, which grew
  without bound and overflowed the summarize call itself by the 2nd–3rd pass).
  Verbatim-preserved safety constraints survive re-summarization as a fixed
  point (no bullet accumulation).
- The Anthropic `model_context_window_exceeded` stop_reason now routes to the
  passive compaction path (was: an unmapped `"error"` → non-retryable task
  failure); `stop_details` is captured into `raw`.
- The observed token-density ratio is clamped to `[0.25, 8.0]` at both
  consumers, so a gateway reporting garbage usage can no longer inflate the
  protected tail ~250x and pin compaction on `compaction_no_progress`. (The
  ceiling sits above the ~4–7 genuine density of pure-CJK payloads, so the
  clamp never binds on a real measurement.)
- The summarize request's `max_tokens` is now the **compaction model's own**
  catalog ceiling when `Options.compaction_model` is set (was: the main
  model's cap, a provider 400 on a smaller summarizer that killed the task on
  every proactive compaction). New `compaction_max_output_tokens` rides the
  `compaction_model` path host → builder → react factory; third-party
  `PolicyFactoryBuilder` implementations must accept the new keyword.
- The summarize request sets `metadata["tool_choice"] = "none"` and the
  summarize prompt states the no-tools rule: a summarizer that saw the live
  tool schemas could answer with a tool call, and an only-`tool_use` response
  has no text — a non-retryable `compaction_summary_failed`. All three
  first-party adapters pass the metadata through (Anthropic wraps the neutral
  string as `{"type": ...}`; the OpenAI-shape adapters send it verbatim).
- An errored round-trip no longer pins the compaction trigger baseline: a
  200-shape overflow arrives as `stop_reason="error"` **with** real usage, and
  the baseline must only come from a successful turn.

### Changed — catalog and Anthropic adapter

- Catalog rows for `claude-opus-5` / `claude-sonnet-5`; the `opus` / `sonnet`
  aliases now resolve to them (4.x rows stay addressable by full id).
- An uncatalogued model warns once and falls back to conservative compaction
  knobs (128K window / 16K output) instead of silently disabling compaction;
  pricing warns once and charges $0 instead of silently; its images are
  admitted (the provider is the authority) instead of hard-refused —
  `model_capabilities` reports the same. Placeholder rows lost their fake
  $0.00 (explicit unpriced state). The OpenAI Responses adapter now carries
  the same fail-open vision contract (`_model_admits_images`): an uncatalogued
  model's images pass through — including tool-result images, which previously
  degraded silently to text — and only a catalogued `supports_vision=False`
  model refuses.
- `complete()` on the Anthropic adapter transports via SSE internally and
  accumulates (identical parsed response and recorded bytes), removing the
  non-streaming 60 s wall against 128K `max_tokens` turns. `delta_sink`
  remains the only external streaming surface.
- `thinking="disabled"` with `effort ∈ {xhigh, max}` (a documented provider
  400) is rejected at `Options` construction.

### Changed — prompts, allowlist, model-visible text

- `run_workflow`'s description no longer teaches the deleted `spawn_subagent`
  tool or its removed `spawns` array; `AskUserQuestion` ack/error strings use
  the reference name.
- The explore/plan preset prompts route reading through `Read`/`Glob`/`Grep`
  (no more `cat`/`head`/`tail`, which the shell allowlist would suspend on);
  `git log` (curated flags; `--ext-diff` excluded) joined the default shell
  allowlist; `main`/`main-web` gained a TodoWrite planning rule.

### Changed — memory freshness and recall budget

- The memory index resident re-records at every seed — task seed, subtask
  seeding, and each new goal on a resumed task — so a memory written
  mid-conversation appears in the index on the next turn (was: recorded once
  at task creation, stale for the task's life and across the warm Engine
  cache).
- Auto-recall matching gained an English stopword list and a 3-char minimum
  on ASCII tokens (CJK bigrams unchanged); recalled bodies are budgeted
  (4 KiB per body, 16 KiB per turn; over-budget hits degrade to index lines);
  the `__consolidation__` curator no longer receives recall against its own
  digest. `memory_write` is atomic (temp file + `os.replace`).

### Changed — skill frontmatter semantics

- `disable-model-invocation: true` now removes a skill from the model's menu
  (host preload still works); the flag accepts the YAML 1.1 boolean dialect
  (`yes`/`no`, `on`/`off`, `1`/`0`, case-insensitive) and one layer of
  surrounding quotes. `allowed-tools` recognizes the full mountable
  tool vocabulary; an unrecognized name is dropped per-name (was: the whole
  declaration degraded to an empty grant); `Bash(git:*)`-style specifiers
  grant the bare tool with a not-enforced warning. Roster descriptions are
  truncated at 1024 chars; a non-integer `priority` degrades to the default
  100 instead of deleting the skill; `$ARGUMENTS` placeholder lines are
  stripped at render, and the `$ARGUMENTS` token is excised from the
  frontmatter description on both model-visible surfaces (activation render
  and the skill tool's menu).

### Added — plugin closure

- The `skills` plugin surface is consumed: a plugin's skill-pack directory
  joins the lowest tier of the three-tier merge (below the user's global and
  workspace tiers). Paths must be absolute; a relative path is a loud
  `PluginError`.
- The `mcp_server` plugin surface is consumed: contributed in-process servers
  merge into `Options.mcp_servers` with loud alias collisions, entering agent
  identity through the existing path. `provider` / `sandbox_provider` are
  documented as host-resolved listings.
- `HostConfig.plugin_config` opens the config channel to third-party session
  packs (`ctx.config("<plugin>")`); for the four SDK-derived entries, host
  keys overlay per-key.
- `requires-noeta` is now evaluated (warn on unsatisfied; `load_plugins(...,
  strict=True)` refuses; unrecognized specifiers warn and never enforce — and
  the skip-enforcement warning names the actual unparseable side, so an
  installed pre-release like `1.0.0rc1` is no longer misreported as an
  "unrecognized specifier").
  `Options.compaction_model` routes the compaction summarize call to a cheaper
  model. A third-party non-int contribution `priority` is a loud `PluginError`
  (was: silent 0); an unnamed single-file plugin under an `enabled` allowlist
  is skipped, not executed.

## [0.6.0] - 2026-08-03

Covers both packages (lockstep). Minor bump: the model-facing tool surface is
a hard break (names, schemas, result rendering, host-side answer contract).

### Changed — Claude Code tool-surface alignment (BREAKING)

The whole model-facing tool surface now follows Claude Code (see
`docs/implementation-specs/claude-code-tool-alignment.md`); models hit their
strongest trained prior instead of a JSON-wrapped, ref-bearing dialect.

- **Plain-text tool results.** Every builtin tool renders a plain string —
  `Read` in `cat -n` form with true line numbers, `Bash` as raw streams,
  `Grep` in ripgrep-style lines, `Glob` as a path list, `WebFetch`/`WebSearch`
  as Markdown. The JSON envelopes and every model-visible
  `content_ref`/`stdout_ref`/`ref` hash are gone (the model has no deref tool;
  the audit layer keeps its refs). The wire fallback for structured host/MCP
  outputs uses `ensure_ascii=False`, ending the ~6x CJK escape expansion, and
  the OpenAI-shaped adapters now carry a failed call's error text (previously
  dropped silently).
- **Reference names and parameters.** read/glob/grep/edit/write/shell_run/
  shell_poll/shell_kill/webfetch/web_search/todo_write/ask_user_question/
  spawn_subagent → `Read`/`Glob`/`Grep`/`Edit`/`Write`/`Bash`/`BashOutput`/
  `KillShell`/`WebFetch`/`WebSearch`/`TodoWrite`/`AskUserQuestion`/`Task`,
  with the matching parameter names (`file_path`, `old_string`/`new_string`,
  `bash_id`, `shell_id`, …). Capability flags and plugin identifiers keep
  their snake_case names.
- **Capacity alignment.** `Write`'s 64 KB cap → 8 MiB guard; `Read` truncates
  by its 2000-line window only (1 MiB safety fence); `Bash` inlines up to
  30000 chars (was 2 KB/1 KB tails) with middle elision; `Grep`/`Glob`
  budgets 4 KB → 32 KB.
- **Logic parity.** `Edit` gains the read-first precondition (session
  `FileReadRegistry` — file `Read` this session and unchanged since; replaces
  the content-store probe and its cross-session false positives), rejects
  `old_string == new_string`, and returns a `cat -n` snippet. `Write` creates
  missing parent directories. `Glob` sorts by mtime (newest first). `Grep`
  gains `output_mode`/`-i`/`-n`/`-A`/`-B`/`-C`/`head_limit`/`multiline` and
  skips hidden + dependency/cache directories. `BashOutput` returns only the
  output since the last poll (plus optional `filter`); background-job output
  and exit notices now inline real text instead of an unreadable hash.
- **Control tools.** `TodoWrite` items are `{content, status, activeForm}`
  and may be batched with runtime tool calls in one turn (no extra round
  trip). `AskUserQuestion` takes 1–4 `{question, header, options, multiSelect}`
  questions with label-based answers (`{selected, other}` — a breaking
  host-side answer contract). `Task` is one sub-agent per call
  (`{description, prompt, subagent_type}`); parallelism is several `Task`
  calls in one assistant turn; the `spawns` array form is removed.
- **Removed:** `apply_patch` (no reference counterpart; `Edit` is the single
  precise-edit tool) and the per-provider edit-tool mutex.
- **Replay note:** outbound request bytes changed, so byte-equivalent
  replay-verify against pre-0.6 recordings reports drift. New recordings are
  self-consistent.

### Fixed

- **`as_messages` no longer renders host-injected turns as `UserMessage`**
  (noeta-sdk). A user-channel turn the host authored (`origin` `"system"` /
  `"memory"` — reminders, memory recall, `inject_goal(goal_origin=...)`) now
  projects as the new `InjectedMessage(text, origin)` view type in the
  `ViewItem` union. Consumers matching `UserMessage` stop seeing recall and
  reminder text as the human speaking without any code change; showing
  injected context is the opt-in of handling the new type. An exhaustive
  match over `ViewItem` needs a new branch.

### Added

- **`noeta.protocols.is_host_injected(message)`** (noeta-runtime): the shared
  predicate for "user-channel turn authored by the host rather than the
  human", replacing four hand-rolled copies (the three provider adapters and
  the consolidation digest). Wire behavior is unchanged.

## [0.5.5] - 2026-08-02

Covers both packages.

### Fixed

- **`InjectionRequestedPayload` drops its dead `consumes_injection` field**
  (noeta-runtime). The field was copied in from `MessagesAppendedPayload`
  (where it is the exactly-once consume key) but was never written on the
  request side, and `__canonical_omit_none__` kept it out of every recorded
  byte stream — so removing it changes no recording and no fold behavior.

### Changed

- **Model-facing prompts deduplicated and tightened** (noeta-sdk; prompt bytes
  change, so stable-prefix hashes and `AgentSpec` identities shift). The
  `main` / `main-web` preset prompts drop the "Your strengths" section — its
  delegation guidance folds into rule 10, and `main-web` now carries the
  browser-delegation instruction as a dedicated rule, locked to "`main.md`
  plus exactly one extra line" by the prompt/roster lockstep test. The
  `spawn_subagent` and `run_workflow` descriptions lose their intra-document
  restatements of the batch-for-parallel rule (one clear statement per
  surface remains; the cross-surface repetition — rule 10, tool description,
  `spawns` property, delegation reminder — is deliberate and kept). `plan`'s
  read-only command list gains `git status` (matching `explore`), rule 3 now
  reads "run the relevant tests", and the consolidation goal preamble defers
  the curation contract to the preset's system prompt instead of restating it.

## [0.5.4] - 2026-08-01

Covers both packages.

### Added

- **Mid-turn goal injection** (ADR `mid-turn-goal-injection`). A new verb
  `Client.inject_goal` / `InteractionDriver.inject_goal` delivers a user message
  to a task **while its turn is running**, instead of requiring it to be
  suspended first. It is status-dispatched: a **running** task takes the message
  mid-turn — a durable `InjectionRequested` is written lease-free (the same
  control-plane seam `cancel` uses) and the running Engine drains it at its next
  turn boundary, so the injected message is delivered without tearing down the
  turn; a task **suspended on the next-goal handle** falls through to
  `send_goal`; any other state raises the typed `NotResumableError`. Delivery is
  exactly-once and crash-safe (the injection folds into
  `GovernanceState.pending_injections` and is popped only by its consuming
  `MessagesAppended`), and an injected message can never split a
  `tool_use`/`tool_result` pair. Additive: a turn with nothing to inject is
  byte-identical to a pre-injection recording.


## [0.5.3] - 2026-08-01

Covers both packages.

### Changed

- **Worker queue routing — named queues on the Dispatcher** (ADR
  `worker-queue-routing`). Every dispatcher row now carries a `queue` name,
  assigned once at row birth (explicit / inherited from the parent row /
  `DEFAULT_QUEUE`) and immutable afterwards; an untargeted `lease` claims FIFO
  **within one queue** — there is no wildcard claim. Roots are born on their
  seeding client's queue (`HostConfig.queue`), children inherit it, and a
  resident worker pool claims only its own — so differently-configured clients
  sharing one storage triple (same process; SQLite included) can no longer
  drive each other's work. Targeted leases and the maintenance sweeps are
  queue-agnostic. **Breaking** for third-party `Dispatcher` adapters:
  `enqueue` gains `queue=` / `parent_task_id=`, `lease` gains `queue=`
  (sqlite migration 11, postgres migration 6).
- **The parent↔child handoff is now derived from the log, not process
  memory.** `ChildLifecycleObserver` finds the parent by reading the child
  stream's `TaskCreated` and durably dedupes against the parent stream, so the
  handoff fires correctly in whichever process commits the terminal;
  construction is a recovery pass that emits any handoff a crashed process
  left missing (previously that crash window stranded the parent forever).
  `wire_default_observers` is idempotent per event log — N clients over one
  shared triple get exactly one default observer, owned by the store's
  lifetime (`Client.close()` no longer stops it).

### Documentation

- `noeta-runtime`: corrected the `ShellMode` enum docstring — `ALLOWLIST`
  runs a metachar-free command as a direct argv with no shell, and
  `ARBITRARY` runs it through a real `bash -c` (which is why pipes and
  redirects are permitted). The prior text wrongly described `ARBITRARY` as
  metachar-free.

## [0.5.2] - 2026-07-31

Covers both packages.

### Changed

- **`noeta-sdk`: OpenAI adapters now preface host-injected turns with a
  self-describing preamble.** Host injections (`origin="system"` reminders,
  `origin="memory"` recall) still render as mid-history system-role wire
  messages, but the content now opens with `HOST_INJECTED_PREAMBLE` — one line
  telling the model the turn is automated host context, not the user speaking,
  and is background only. The system role alone does not carry that semantic
  to an arbitrary model behind an OpenAI-shaped endpoint: models answered
  reminders as if addressed, and worse, memorized them as the user's words.
  The Anthropic adapter is untouched — Claude is trained on the bare
  `<system-reminder>` envelope. Wire bytes change for `openai_compat` and
  `openai_responses` consumers; the ledger recording stays byte-identical.

### Fixed

- **`noeta-runtime`: a background shell's terminal status now flips only
  after the durable `BackgroundShellExited` event.** Previously a fast
  `shell_poll` could observe `status="exited"` while the ledger still lacked
  the event; a terminal poll answer now implies the event is durable.

## [0.5.1] - 2026-07-31

Closing the public-surface holes a product host fell through. A host may import
only `noeta.sdk` / `noeta.presets`, and building one on 0.5.0 surfaced eight
places where the honest move was an internal import or a `# noqa: SLF001`.
Additive throughout, except the one ordering fix noted below.

### Added

- **`Client.dispatch_seeded(seeded)` — the public non-blocking turn handoff.**
  The twin of `drive_seeded`: one runs the seeded turn on the calling thread and
  returns its `DriveOutcome`, the other yields the seed's lease back to the
  ready queue for a resident worker and returns at once. This is the shape an
  HTTP host needs — a request thread cannot block for the minutes a turn takes,
  and the durable seed already made the ack crash-safe. It existed as the
  private `_yield_seeded_lease`, which the SDK's own `run_consolidation` and
  every real host had to reach for behind a `noqa`.
- **`Client.task_status(task_id) -> TaskStatus | None`** — where one task rests
  right now, without driving anything. `status` + `wake_handle` are the same
  pair a `DriveOutcome` carries, so "I just drove it" and "I am only asking"
  answer in one vocabulary; `closed` is orthogonal (a closed conversation is
  still `suspended`). `None` for an unknown task, which a bare fold cannot
  express — it answers `pending` for both an unstarted task and a stream that
  does not exist. Snapshot-accelerated.
- **`Client.suspend_reason(task_id) -> SuspendReason | None`** — the `reason`
  on the most recent `TaskSuspended`, parsed into a structured
  **`SuspendReason(kind, detail)`**, plus the three kinds to compare `.kind`
  against with plain `==`: **`SUSPEND_REASON_WAITING_HUMAN`**,
  **`SUSPEND_REASON_INTERRUPTED`**, **`SUSPEND_REASON_TURN_FAILED`** (whose
  `detail` carries the policy's own failure reason). `status="suspended"`
  collapses three rests a host renders differently — waiting on you, stopped by
  you, failed and parked — and the tags were previously spelled as literals in
  two runtime modules with nothing binding them together. Both producers now
  import the protocol's constants, pinned by an identity test. The ledger
  still records the single-string tag; **`parse_suspend_reason`** is exported
  for hosts reading raw `TaskSuspendedPayload`s off `subscribe`.

  Deliberately **not** a field on `DriveOutcome`: `interrupt` returns as soon as
  it has marked the cancel registry, while the matching `TaskSuspended` is
  written later by the worker that settles the turn, so an outcome field would
  read stale on the one verb the tag exists for.
- **`Client.task_summaries()`** — every task stream folded into a lifecycle row
  (`status`, `closed`, `parent_task_id`, `agent_name`, `workspace_dir`,
  `background_jobs`, …), previously reachable only through the forbidden
  `noeta.read_models`. Documented as a reconciliation/repair path, **not** a
  list-render one: it folds every task *and* reads each stream in full to reach
  the genesis event, so it costs one pass over the whole log and batch content
  reads cannot help.
- **The types the verbs take and return are exported**, so a host can annotate
  its own signatures: `DriveOutcome`, `SeededTurn`, `DeleteTaskResult`,
  `TaskStatus`, `EventEnvelope`, `TaskStreamSummary`, `TaskSuspendedPayload`,
  `ViewItem`, plus `NotForkableError` (which `fork` raises and nothing else
  named) and `DEFAULT_MODEL_ALLOWLIST` (what `allowed_models=None` authorizes).
- **`FakeStreamingLLMProvider` in `noeta.sdk.testing`** — its batch twin was
  already there, but a host testing a token-streaming wire needs the streaming
  one.
- `Client.task_streams()` is typed `list[TaskStreamSummary]` instead of
  `list[Any]`.

### Fixed

- **`permission_modes()` and `effort_modes()` return picker order, not
  alphabetical order.** Both projections exist to fill a host's dropdown, and
  both were `tuple(sorted(...))` over a `frozenset` — so `effort_modes()`
  returned `('high', 'low', 'max', 'medium', 'xhigh')`, which is nonsense in a
  picker, and `permission_modes()` offered `bypassPermissions` first. The
  sources are now ordered tuples: permission modes in widening-trust order
  (`default`, `acceptEdits`, `bypassPermissions`) and effort modes in
  increasing-intensity order (`low` … `max`) — the order
  `docs/reference/sdk-options.md` documented all along. **This changes a
  returned tuple's order**: a host that renders them in order is fixed, one
  that pinned the alphabetical tuple must update.
- `docs/reference/sdk-options.md` (both languages) documented
  `model_capabilities` as returning `{'vision': True, ...}`. It returns exactly
  one key, `supports_vision` — the name the provider's own vision guard uses —
  and the example named a model that is not in the catalog, so it silently
  demonstrated the fail-closed path instead of a vision model. Doc fixed, code
  unchanged; the key set is now pinned by a test.

## [0.5.0] - 2026-07-31

### Added

- **`Client.fork(task_id, message_seq=…)` — branch a conversation, keep both.**
  The fork twin of rewind: same anchor (the seq of a user-goal
  `MessagesAppended`), same fold-through boundary, but the resulting baseline
  lands on a **new** task's stream instead of re-basing the source. So a rewind
  is "undo this and everything after"; a fork is "edit that message and try
  again, keeping the original". The returned `DriveOutcome.task_id` is the
  branch's, resting at a next-goal suspend, so `send_goal` drives it straight
  away. The source stream is never written to — branching cannot perturb what
  it branched from. The branch inherits the whole folded state (messages,
  TaskState, context plan, governance including accumulated cost) plus the
  source's agent, policy and host binding; it does **not** get its own
  workspace, so both branches act on the same disk. Only a root task can be
  forked, and only at a message that has a prior turn to branch from.
- **`TaskForked` — a new fold-baseline event.** The branch's inherited history
  is not derivable from its own genesis, so the marker is a real re-base
  baseline (like `TaskRewound`), not inert provenance. Both durable backends
  widen the `ix_events_snapshot` partial index for it (sqlite migration 10,
  Postgres migration 5).
- **`Client.interrupt(task_id)` — stop a turn, keep the conversation.** The
  member the human-stop family was missing: `cancel` kills the conversation and
  `close` archives it, while this halts only the in-flight turn and leaves the
  task resting at its next-goal suspend, resumable by simply typing again — what
  pressing Esc in an interactive client should do. Records a `TurnInterrupted`
  marker and tags the resulting `TaskSuspended.reason` `"interrupted"`, so a
  park reached by a human stop is distinguishable from one the model asked for.
  The interrupted turn's events stay on the stream as real history (interrupt is
  not a rewind; the two compose), and the stop lands at a turn boundary — it
  cannot abort a tool call already executing. Safe to call from another thread
  while a turn is being driven.
- **`Client.rewind(task_id, message_seq=…)` is now exposed.** It has existed on
  the driver all along; shipping fork without its twin was an arbitrary hole.
- **Batch content reads — a traversal costs one query, not N.** `fold` and
  `as_messages` know every `ContentRef` they will dereference before they
  process an event (the refs sit in the payloads), so both now scan first and
  fetch their bodies in a single `ContentStore.get_many`. The per-event
  handlers are untouched — they read through a `PrefetchedContentStore` view
  that answers from the batch and falls through to the store for anything the
  scan did not predict. `as_messages` benefits most: it walks the full task
  history with no snapshot to shorten it.
- **`CachedContentStore` — a byte-bounded LRU over the durable backends.**
  `noeta.sdk.storage.build_storage_stack` now wraps sqlite / Postgres content
  stores in it (`memory` is left bare — it is already a dict). This collapses
  the reads batching cannot: the same immutable hash re-read across composes
  and folds, which is what the compose-time resident deref does on every model
  turn. Bounded by bytes rather than entries, and bodies over 1 MiB are served
  straight through so one large body cannot evict the working set.

- **`HostConfig(storage_path=...)` — durable storage from one string.** A
  sqlite file path, a `postgresql://` DSN or `":memory:"` resolves through
  `noeta.sdk.storage.open_storage_stack`, including the ordering invariant
  (the event log takes the dispatcher as its `lease_validator`). Mutually
  exclusive with the explicit `event_log` / `content_store` / `dispatcher`
  triple, which stays supported.
- **`query()` accepts `host_config`**, so the one-shot sugar path is no longer
  limited to in-memory storage — `query(..., host_config=HostConfig(
  storage_path="run.sqlite"))` records durably.
- **`Client` is a context manager** (`__exit__` → `shutdown()`), so a `with`
  block can no longer leak a worker pool or a sandbox container.
- **Provider env-var fallback**: `AnthropicProvider` reads `ANTHROPIC_API_KEY`
  and `OpenAICompatProvider` reads `OPENAI_API_KEY` when `api_key` is omitted.
  A key that is neither passed nor in the environment still fails loudly at
  construction, naming both the parameter and the variable.
- `QueryResult.__repr__` — a compact summary instead of the inherited
  dump of every envelope.

### Changed

- **BREAKING — `ContentStore` gains a required `get_many(refs)`.** Third-party
  adapters must implement it; the three shipped backends do (`WHERE hash IN`
  chunked for sqlite, `hash = ANY` for Postgres, dict lookups for InMemory).
  It is deliberately a required member rather than a Protocol default: an
  adapter that answered it with a loop over `get` would silently cost N
  round-trips at exactly the call sites that chose the batch API to avoid
  them. Unlike `get`, a missing hash is **omitted** from the result rather
  than raising, so one reclaimed body cannot abort a batch.
- **BREAKING — SDK-layer cleanup: typing, extensibility, DX.** Removals, with
  no compatibility aliases:
  - `noeta.sdk.load_plugin_set` → **`load_plugins`** (one name; the alias
    existed only while the 0.4.0 bundle loader held it).
  - `noeta.client.parts.BUILTIN_TOOL_CLASSES` → **`builtin_tool_classes()`**.
  - `options._EFFORT_MODES` / `_PERMISSION_MODES` → public **`EFFORT_MODES`** /
    **`PERMISSION_MODES`**; the `options._BUILTIN_ACTIVATIONS` alias is gone
    (`BUILTIN_ACTIVATIONS` is the only spelling).
  - **`SurfaceSpec.merge_rule` is deleted** — nothing read it; `collision_key`
    already determines append-vs-single. Its slot now holds
    **`activation_binding`**, which the identity projection *does* read:
    an identity-plane surface names the `PluginActivation` channel it feeds,
    so a host-registered identity surface reaches `compile_options` with no
    loader edit. It is required for `plane="identity"`, rejected elsewhere,
    and every enum field is now validated at construction — a positional
    argument left in the old `merge_rule` slot raises instead of landing in
    `ordering`.
  - **`Options` field order changed** (the wiring fields moved into one
    block). Keyword construction is unaffected; positional construction past
    `system_prompt` breaks.
- **`Options` equality now matches the identity story it documents.** Every
  field the docstring already called "excluded from identity" (`provider`,
  `cwd`, `can_use_tool`, `model`, `metadata`, `output_schema`, `thinking`,
  `effort`, `guards`, `observers`, `content_channels`) is `compare=False` and
  carries its **real type** — `cwd` / `can_use_tool` were annotated `object`,
  which only disabled type checking while equality still compared them. The
  class never was hashable (it holds mapping-valued fields) and no longer
  claims to be.
- **Public typing**: all 16 `Client` verbs return `DriveOutcome` / `SeededTurn`
  instead of `Any`; `delete_task` returns a `TypedDict`; new `PolicyFactory` /
  `ToolLike` Protocols replace `.ref` duck-typing; `SdkMcpServer` moved down to
  `noeta.client` (re-exported from `noeta.sdk.authoring`) so
  `Options.mcp_servers` can name its element type.
- **`open_storage_stack` rejects an unrecognised URL scheme.** A typo'd DSN
  (`postgesql://…`) used to fall through to the sqlite branch and create a
  database file *named after the DSN*.
- **A process-scoped wiring surface beyond `guard` / `observer` is refused.**
  `Client` wires exactly two process-wide seams; a third has nowhere to go, and
  filing it under `guards` handed the engine a value that is not a `Guard`.
- The TOML manifest form enforces `(surface, name)` uniqueness like
  `PluginBuilder` always did, and `plugin_check` no longer reports an omitted
  default (`priority = 0`, empty `seams`) as drift.
- `Client(workspace_dir=...)` falls back to `Options.cwd` and then the process
  working directory instead of raising — matching the `SdkHost.workspace_dir`
  field default it used to contradict.

- **BREAKING — "session" is no longer an identity in engine/SDK names.**
  `CONTEXT.md` bans naming a thing after a session (the engine knows only
  Tasks), but the ban had drifted: a `session_id` that was really a task id, a
  session-keyed cap, a sessions list. Every such name is renamed to the task
  vocabulary it always meant, with **no compatibility aliases**. On the public
  surface:
  - `noeta.read_models.list_session_summaries` → `list_task_summaries` (the
    module `noeta.read_models.sessions` → `noeta.read_models.tasks`); it always
    returned one row per **task** stream.
  - `HostConfig.max_background_jobs_per_session` →
    `max_background_jobs_per_root_task`;
    `HostConfig.max_background_subagents_per_session` →
    `max_background_subagents_per_root_task`;
    `HostConfig.sandbox_session_policy` → `sandbox_policy`.
  - `SandboxProvider.allocate(session_root_id, …)` / `release(session_root_id)`
    → `root_task_id` (a Protocol third-party hosts implement).
  - `SdkHost.kill_background_session` / `purge_background_session` →
    `kill_background_shells` / `purge_background_shells`.

  The **durable wire is untouched** — no recorded event or state schema ever
  carried a session field, so there is no migration: existing event logs fold
  unchanged.

  The construction-scope vocabulary is unaffected and now explicitly sanctioned:
  `session_pack` / `SessionBuildContext` / `SessionInputs` /
  `build_session_inputs` keep their names (a session pack builds one task's tool
  set — a scope, not an identity).

- `scripts/lint-naming.py` now enforces the rule instead of only banning
  `class Session` / `SessionStore`: inside `packages/` and `examples/`, any
  compound identifier containing "session" fails unless allow-listed. This is
  what let the drift accumulate unnoticed.

### Fixed

- **A failed turn no longer ends a multi-turn conversation.** `TaskFailed` is a
  terminal event, so any turn that failed — a transient provider 5xx, a tool
  crash escalated to a fail, a spent structured-output nudge budget — sealed the
  task's ledger and voided every bit of context the person had built up, leaving
  "start a new task" as the only move. `MultiTurnReActPolicy` now translates a
  `FailDecision` on a non-final turn into the same next-goal suspend an ordinary
  turn rests on, recorded as `TaskSuspended.reason = "turn_failed: <reason>"`;
  the human's next message resumes the same task. No new event type and no
  Engine / fold change — it reuses the wake-resume primitive the wrapper already
  drives between turns. A `final=True` turn still terminates.

  `retryable` is deliberately **not** the gate: it answers "would re-driving
  this step help?", and it is `False` for exactly the faults a human can clear
  (a transient provider error arrives as non-retryable `llm_error`), so gating
  on it would have kept killing the motivating case.

  New protocol field: **`YieldForHumanDecision.suspend_reason`** (optional, last
  position → byte-safe for existing recordings) overrides the recorded
  `TaskSuspended.reason` tag, which defaults to `waiting_human` as before.

- **`structured_output` payloads are checked against the schema before they
  become an answer.** A provider's tool `parameters` only steer the model, so a
  call that missed a required field or got a type wrong used to be accepted
  verbatim and completed the helper — the `agent(goal, schema=...)` caller then
  parsed a shape it never declared, raising far from the cause against a ledger
  that recorded success. A mismatch is now answered with a failed `tool_result`
  naming each violation by JSON path, and the assistant retries. Both failure
  modes (never calling the tool, and calling it with a payload that missed the
  schema) share the one `MAX_STRUCTURED_OUTPUT_NUDGES` budget; exhausting it
  fails the helper with the violations in the reason.

  The check is deliberately conservative and dependency-free: it reports only
  what it is certain of (`type`, `required`, `properties`, `items`, `enum`,
  `additionalProperties: false`) and stays silent on what it does not model
  (`$ref`, `anyOf` / `oneOf` / `allOf` / `not`, numeric bounds, `format`), so it
  can never reject a payload that was valid.

## [0.4.0] - 2026-07-26

### Added

- **Plugin mechanism** (`noeta.sdk`): a plugin is a module exporting
  `noeta_plugin(api)`; `PluginAPI` accumulates typed contributions (tools,
  guards, observers, a provider, content kinds, agents, MCP specs, skill
  dirs); `load_plugins` discovers from the `noeta.plugins` entry-point group,
  explicit specs, or trust-gated directories (`~/.noeta/trust.json`);
  `merge_plugins` folds contributions into `Options` deterministically —
  collisions raise `PluginError` naming both sources, and load order never
  changes the compiled `AgentSpec`. The `enabled` allow-list is keyed on the
  plugin's name (a file's `noeta_plugin_name` literal is read statically, so an
  unapproved plugin is never imported), and trust grants are matched on a
  canonical path. See `docs/how-to/write-a-plugin.md`.
- **First-party example plugins** under `examples/plugins/`:
  `approval-modes` (graduated chat / approve / smart_approve / auto modes
  with per-tool always/ask/never overrides), `protected-paths` (path
  allow/deny guard), `git-checkpoint` (workspace checkpoint observer with a
  restore path).
- **Host contract surface**: `noeta.sdk.storage` re-exports the sqlite
  storage triple so a product host never imports runtime internals; the
  `ProposedAction` members (`ProposedToolCall` / `ProposedSpawnSubtask` /
  `ProposedFinish`) are re-exported too, so a Guard can dispatch on them
  without reaching into `noeta.protocols.hooks`;
  `tests/test_public_surface.py` pins the symbols a host binds to (and proves
  the reference host and every first-party plugin live on the public surface);
  `examples/reference-host/` is a minimal complete host (durable storage,
  streaming, plugins, stub provider) that doubles as the contract's
  integration bed.

### Fixed

- **`noeta.presets` shipped in the wrong wheel.** It imports `noeta.client.*`
  (noeta-sdk) but was packaged into the **noeta-runtime** wheel, so
  `pip install noeta-runtime` followed by `import noeta.presets` raised
  `ModuleNotFoundError` — including on the published 0.3.0–0.3.2. The package
  moved to noeta-sdk (import paths are unchanged, PEP 420), and
  `test_no_distribution_imports_outside_its_dependency_closure` now pins every
  distribution's imports to its own dependency closure so it cannot recur.
- **Background jobs and foreground tool commands inherited the host's stdin.** A
  detached job has no console to read from, so a spawned command could block on
  a read (burning its whole timeout) or consume bytes meant for whatever drives
  the host process. Both spawn paths now pass `stdin=DEVNULL`.
- **`Client.start` / `send_goal` / `seed_send_goal` now accept `activations`.**
  Only `seed_start` forwarded it, so a product implementing `/skill-name` could
  pin a skill on the opening turn but had no public path for any later turn.
- **A failed Engine build no longer leaks connected MCP clients.** Live clients
  were staged for the engine cache to adopt on the put that follows a successful
  build; when a later step raised, nothing ever adopted — and therefore never
  reaped — them, leaking an `McpStdioClient` subprocess and its fds per failed
  build. The connect is now failure-atomic.
- **A dead OTLP endpoint no longer skips sandbox teardown.**
  `Client.shutdown` ran `trace_export.stop()` unguarded, so an exporter flush
  failure aborted shutdown before the step that releases a remote container.
- **`allowed_models=[]` is honored.** An explicitly empty sequence means "no
  per-turn model selector is authorized"; it previously fell back to the stub
  allowlist, silently widening a deliberate lockdown.
- **`stop_workers` keeps the pool tracked on timeout.** It used to clear its
  state even when a worker had not exited, letting the next `start_workers`
  stack a second pool on top of the still-running stragglers.
- **Sandbox `teardown()` fires the `on_release` listeners** for every root it
  reaps, as `release()` already did — so product-side cleanup (preview gateway
  mounts and similar container-tracked side effects) also runs on the shutdown
  path.
- `Options.cwd` type validation is a real check rather than an `assert` that
  `python -O` strips into a confusing `Path()` TypeError.

### Added

- `HostConfig.instructions_enabled` / `instructions_file` — the workspace-root
  `NOETA.md` → `AGENTS.md` switch was reachable only by constructing an
  `SdkHost` directly, leaving `instructions_discovery` as a half-exposed
  feature.
- `HostConfig.max_background_jobs_per_session` /
  `max_background_subagents_per_session` — three comments already described
  these caps as "configurable via HostConfig"; now they are.

### Removed

- `SandboxExecEnvConfig.provision`. Nothing ever read it, so `"eager"` silently
  attached to the shared container instead of provisioning. Per-session
  provisioning is the `SandboxProvider` seam; passing `provision=` now fails
  loudly rather than quietly doing the other thing.

## [0.3.2] - 2026-07-24

### Added

- **Read-triggered instruction discovery** (`HostConfig.instructions_discovery`,
  off by default). When enabled, a successful `read` inside the session
  workspace activates the subdirectory `NOETA.md` / `AGENTS.md` files between
  the read file's directory and the workspace root, each rendered *anchored* at
  its point of discovery — so a mid-task activation appends instead of
  rewriting the stable prompt head. `Client.seed_start` and the anchored
  content-placement seams are forwarded through the SDK. Decision record:
  `docs/adr/anchored-content-placement.md`.
- **Per-session sandbox opt-out** (`HostConfig.sandbox_session_policy`). A
  policy returning `False` keeps a session on the `local` execution tier — no
  container, the host `WorkspaceRoot` fence — even while a sandbox provider is
  configured for other sessions. Default (`None`) provisions every session as
  before.
- **Host-authorized out-of-workspace writes** (`HostConfig.write_roots`, plus
  the exported `path_within` containment predicate). A host may open specific
  directories outside the workspace for a task's `edit` / `write` /
  `apply_patch`, consulted per call so a grant made while a task is paused takes
  effect on the resumed call. The default keeps the single-root wall.
- **`Client.seed_start(activations=…)`** — pin built-in skills pre-loop for a
  seeded task, the same forced-preload channel a `/skill-name` slash command
  uses. `()` keeps the seed byte-identical to the no-skill path.
- **Memory recall now matches CJK text.** A message written without spaces
  (Chinese / Japanese / Korean) is segmented into character bigrams, so it
  recalls at all — previously such a message produced no recall. Still pure and
  deterministic: no dictionary, no service.

### Changed

- **Filesystem reads are no longer fenced to the workspace.** An absolute
  `read` / `glob` / `grep` path resolves where it points (a neighbouring
  checkout, a skill pack's bundled reference); `glob` gains an optional `path`
  argument to choose the tree. Writes stay fenced and widen only by host
  authorization (`write_roots`). The rationale: a read is observation, not
  mutation, and `shell_run` already reaches the same bytes (PRD §B19), so a path
  check in the read tool was never the disclosure boundary — the wall now stands
  where the irreversible act is.
- **Memory recall is now two-tier.** A name match injects the full memory body
  (unchanged); a weaker summary-overlap match injects only a one-line pointer
  the model expands with `memory_read` if it wants the text — so a chatty match
  no longer spends whole memories of context on a maybe.

### Fixed

- **The compaction summarize round-trip ignored the model's output ceiling,
  so proactive compaction reliably killed reasoning-model tasks with
  `compaction_summary_failed`.** `_summary_prompt_request` built its
  `LLMRequest` without `max_tokens` (unlike every normal turn, which forwards
  `max_output_tokens`), so a gateway that caps output when the client sends
  none — e.g. the aidp Responses gateway's 1000-token default — let a reasoning
  model spend the entire default budget on hidden reasoning and return
  `stop_reason="max_tokens"` with an empty text body. The empty-summary guard
  then (correctly) refused to record the empty summary and failed the step,
  taking down the whole task. The summarize request now forwards
  `max_output_tokens` the same way a normal turn does.

## [0.3.1] - 2026-07-18

### Fixed

- **A pip-installed `noeta-agent` 0.3.0 served no web frontend.** Two
  halves: the wheel force-include (`apps/web/dist` → `noeta/agent/static`)
  was lost in the platform port's pyproject rewrite, so the published
  wheel shipped API-only; and the server's SPA lookup resolved the wheel
  candidate against `APP_DIR`, which degenerates to `site-packages` in an
  installed venv, so even a bundled SPA would not have been found. The
  include is restored, the lookup is package-relative, and the opt-in
  install smoke now asserts the root path serves the SPA index, not just
  `/api/v1/health`.

## [0.3.0] - 2026-07-17

### Changed

- **The official product is now a multi-user server platform.** The
  single-user, local, no-auth coding agent app (and its raw-envelope wire
  protocol) is retired and replaced by a deployable multi-user agent
  service: a FastAPI backend with app-layer sessions and collaboration
  spaces, pluggable authentication (dev-login reference implementation,
  signed session cookie), an admin console, and a React 19/TypeScript SPA.
  The dist name (`noeta-agent`), directory (`apps/noeta-agent`) and
  entrypoint (`python -m noeta.agent`) are unchanged. The wire protocol is
  a versioned REST surface plus one SSE stream per session carrying
  translated flat UI events re-derived from the EventLog on replay
  (`since_seq`); raw envelopes remain available on the admin trace
  surface. Agent shell execution is sandbox-only (one container per
  session); per-call approval no longer exists. Decision record:
  `docs/adr/server-platform-product.md`.
- Space-scoped app content: skills, knowledge sources (`git_repo` /
  `local_dir` sync), agent memory, prompt/workflow templates, a feedback
  loop (per-message ratings feeding an owner-gated analysis agent), MCP
  connectors, and per-space agent configuration (persona prompt, default
  model/effort, knowledge selection).

### Added

- Per-space MCP connector management: CRUD + enable/disable + per-connector
  tool subsets over `/api/v1/spaces/{space_id}/mcp/servers`, with live
  tool/prompt/resource discovery for HTTP connectors and per-turn resolver
  wiring into the engine. Credentials are stored server-side and never
  echoed.
- Composer image input: attach/paste/drop PNG/JPEG/GIF/WebP (≤ 5 MB each);
  images reach the agent as `ImageBlock`s and render back through
  `GET /api/v1/content/{hash}`.
- Opt-in OTLP trace export for the platform (`OTLP_ENDPOINT` /
  `OTLP_HEADERS`).
- `noeta.sdk` surface: `LLMRequest` / `LLMResponse` / `Message` /
  `TextBlock` / `ToolUseBlock` / `ToolResultBlock` / `Usage`,
  `MemoryStore`, `noeta.sdk.testing.FakeLLMProvider`, and
  `CATALOG` / `ModelSpec` on `noeta.sdk.providers`. The app-layer
  import ratchet is burned down to its two ADR-documented exemptions.
- Playwright e2e suite for the platform SPA (`make e2e-web`, opt-in).

### Fixed

- The builtin `read` tool no longer fails with "is not utf-8 text" on text
  files containing stray invalid bytes: it decodes them with U+FFFD
  replacements (noted in the tool summary). A NUL byte still marks real
  binary and keeps the hard error.

## [0.2.11] - 2026-07-16

### Fixed

- **A codeless mid-stream error from the Responses gateway killed the task
  instead of retrying.** The gateway occasionally emits an in-stream `error` /
  `response.failed` frame carrying neither a `code` nor a `message` (surfacing
  as `Responses stream error: code=unknown;`, seen under load alongside 429s).
  `_translate_stream_error` (OpenAI Responses provider) bucketed every code
  outside the transient allowlist — including an *absent* one — as `FatalError`,
  so `RuntimeLLMClient` skipped its retry budget and the task failed at once with
  a non-retryable `llm_error`; long-running tasks died on a single transient
  gateway hiccup. A codeless error frame is not a classifiable semantic
  rejection — in practice it is a truncated / dropped stream, the same retryable
  failure the loop already reissues for a mid-stream disconnect or a stream that
  ends without a terminal event. An empty code now maps to `TransientError`;
  only a genuine coded rejection (e.g. `invalid_prompt`) stays `FatalError`.

## [0.2.10] - 2026-07-16

### Fixed

- **A resident worker could steal a task out of a resume's wake window and
  re-drive the turn without the user's new message, dropping it.** The sibling
  of the 0.2.9 seed-window fix, on the resume path.
  `InteractionDriver._seed_wake_common` (the `send_goal` / `answer` /
  `deliver_event` / approve / deny resume) wakes the suspended task — flipping it
  to `ready` — then targeted-leases it, and only then appends the command's
  message. Between the wake and the claim the task is `ready` but the new message
  is not yet durable, so a resident-worker pool's untargeted `lease(task_id=None)`
  poll could land there, lease the task, and re-drive the turn on the *old*
  context — silently dropping the user's input. The resume's own targeted lease
  then found the task already leased and raised `NotResumableError`, which the
  served product misreads as "task not resumable" and handles by restarting the
  session fresh (new event stream from seq 0), so the conversation's history
  appears lost. 0.1.16 added `Dispatcher.enqueue(reserved=True)` for exactly this
  hazard but only the enqueue path carried it; the wake path had no such guard.
  `Dispatcher.wake` now takes `reserved` (threaded through the sqlite / postgres
  / in-memory adapters; no schema change — the `reserved` column already exists),
  and `_seed_wake_common` sets it. One-shot as before: the resume's targeted
  lease clears it, so the worker drives the turn normally once the message is
  durable. `reserved=False` (the default, every other `wake` caller) is
  byte-identical to the historical wake.

## [0.2.9] - 2026-07-16

### Fixed

- **A resident worker could steal a root task out of its own seed window and
  drive it with no user message, failing the turn on a provider 400.**
  `InteractionDriver.seed_start` enqueues the new task, targeted-leases it, and
  only then writes `ModelBound` + the opening goal message — so between the
  enqueue and the claim the task sits `ready` but unseeded. Under the served
  product's resident `WorkerLoop` pool an untargeted `lease(task_id=None)` poll
  could land in that window (~5ms), steal the task, and drive it with an empty
  message history; the seed's own targeted lease then found nothing and raised
  `dispatcher gave no lease for freshly enqueued task`. Observed once in ~150
  production tasks: the stolen task's stream jumps `TaskHostBound` →
  `TaskStarted`, with no `ModelBound` / `MessagesAppended`, and the request goes
  out with `messages: []`. 0.1.16 fixed exactly this hazard for subtask children
  by adding `Dispatcher.enqueue(reserved=True)`, but scoped the guard to
  `BackgroundSubagentRegistry._submit`; the root seed path has the same shape and
  was left unguarded. `seed_start` now reserves too. The guard stays one-shot —
  the seed's claim clears it, so `_yield_seeded_lease` hands the seeded task back
  to the pool unchanged.

## [0.2.8] - 2026-07-15

### Fixed

- **Micro-compaction (`_prune_tail`) was dead on any payload denser than the
  chars/4 heuristic, so the tail relief valve never opened and the only relief
  left was a full summarize.** 0.2.7 corrected the compaction trigger to mix in
  the provider's recorded real usage and closed by scoping the density
  conversion to the Policy. Compaction has two layers, and that scope left the
  other one reading the same real-token knobs in chars/4:
  `ThreeSegmentComposer._prune_tail` gated on
  `estimate_messages_tokens(messages) < available_window` and accumulated
  chars/4 against a real-token `tail_token_budget`. Measured on a production
  session: 99 composes, `cleared_outputs` empty in all 99 — the estimate read
  ~42.5k while the real request sat at 181,870 against a 181,616 window. The
  session went straight to a summarize that collapsed a specification file it
  then re-read verbatim. The gate now takes `max(estimated, real_baseline)` and
  the budget is converted by the same observed density the Policy applies to
  `_summary_boundary`. `composer_version` is unchanged: this moves when the
  valve opens, not the composed structure.
- **`fold(events) → state` now holds inside a tool loop, not just across one.**
  `RuntimeLLMClient` appends its events straight to the EventLog while `Engine`
  folds only the events it emits itself, so mid-loop the in-memory task diverged
  from `fold(events)` and `RuntimeState.last_input_tokens` sat at the entry
  fold's value (`0` on a first turn) however many round-trips the loop made.
  0.2.7 worked around this inside `ReActPolicy`; a second consumer could not
  reuse a private workaround. `StepContext` — already the Engine → Policy →
  client channel, transient and never serialized — now carries an `apply_event`
  callback bound to the task being stepped, and the client invokes it after each
  emit. The Engine stays the sole physical writer of `RuntimeState`.
- **A compaction invalidates the real-usage baseline in `fold`, not in a
  Policy-private sentinel.** After the prefix collapses every recorded input
  count over-reads a history that no longer exists — a property of the history,
  so every reader needs it and a resume must re-derive it. `_on_compacted` zeroes
  `RuntimeState.last_input_tokens`; `0` is the same value a fresh task carries,
  so each consumer's existing no-baseline fallback covers the case unchanged.

## [0.2.7] - 2026-07-15

### Fixed

- **Context compaction now actually counts real tokens, so a long single turn
  compacts instead of silently overflowing.** `context-compaction.md` decided
  that the trigger mixes the provider's recorded real usage with a chars/4
  estimate of the increment, explicitly rejecting pure chars/4. The real
  baseline reached the policy only through `StepContext.last_input_tokens`,
  which `Engine` rebuilds from `task.runtime.last_input_tokens` — a field only
  `fold` writes, and the mid-loop `LLMRequestFinished` is never applied to the
  in-memory task. So within one `Engine.run_one_step` the baseline stayed
  frozen at `0` and the trigger degraded to the rejected pure estimate for the
  whole turn. On a measured production session — 40 round-trips in one turn —
  real input climbed to 215,836 against a 200,000 window while the trigger read
  54,426, and nothing ever compacted. `ReActPolicy` now pins the real count off
  the response it just received, and invalidates it when a compaction collapses
  the history (a stale pre-compaction high would re-fire on a just-shrunk
  history and die on `compaction_no_progress`).
- **The protected tail is converted into the unit it is compared in.**
  `tail_token_budget` counts real provider tokens; `_summary_boundary`
  accumulates chars/4. The two only coincide while the heuristic is accurate —
  a CJK + JSON + base64-signature payload measured ~1.2 chars/token against the
  assumed 4. While the baseline was dead both sides were consistently wrong and
  still agreed; correcting the trigger alone breaks that symmetry and turns a
  working session into `TaskFailed(compaction_no_progress)`, because the whole
  history fits inside a tail budget four times larger than intended. The budget
  is now converted with the density observed on the last recorded round-trip
  (`1.0`, i.e. today's exact arithmetic, until one exists). No
  `composer_version` bump: this changes the tail size, not the composed
  structure.

## [0.2.6] - 2026-07-13

### Fixed

- **Subtasks now inherit the parent's sandbox binding in `resolve_engine`
  (#59).** Subtasks spawned via `spawn_subagent` carry no `TaskHostBound` of
  their own, so their `governance.exec_env_ref` / `workspace` / `provider`
  folded to `None`. The foreground drain path (`_build_drain_host`) already
  inherited these from the root parent, but `resolve_engine` (the
  resident-worker path where an idle worker's untargeted `tick()` claims a
  child) read the child's own binding only — leaving the subtask on the local
  host with no `browser_*` tools and container-isolated fs visibility.
  `resolve_engine` now inherits the parent's bound values when the task is a
  subtask and its own binding is `None`, so both code paths resolve the same
  sandbox backend.

## [0.2.5] - 2026-07-13

### Fixed

- **ChildLifecycleObserver no longer loses lineage after a process restart
  (#57).** The observer rebuilt its `child_id → parent_id` mapping only from
  live `TaskCreated` events, so a child created before a restart that reached
  its terminal *after* the restart was a no-op: the parent stream never got
  `SubtaskCompleted` and a parent suspended on `SubtaskCompleted` /
  `SubtaskGroupCompleted` waited forever. The observer now replays the
  persisted EventLog at construction to seed lineage for any not-yet-terminal,
  non-background child; already-terminal children are skipped so they are not
  double-notified.

## [0.2.4] - 2026-07-10

### Added

- **Multi-tenant memory — per-task store resolution and scoped
  consolidation.** A product backend serving many end users from one resident
  `Client` can now give each tenant its own memory store (#53):
  - **`HostConfig` reaches the memory roots.** `memory_dir` /
    `global_memory_dir` are now forwarded through the public facade
    (previously host-internal), and the new `memory_root_resolver`
    (`task_id → Path | None`) resolves a store root per task. Recall, the
    memory tool pack, the resident index, and `Client.memory_root(task_id)`
    all follow one resolution chain, falling back to
    `memory_dir > global_memory_dir > ~/.noeta/memories` on `None`. The
    Engine cache partitions by resolved root, so two tenants never share a
    cached engine's baked-in store.
  - **Scoped consolidation.** `build_consolidation_digest` /
    `run_consolidation` take `include_task` to digest only one tenant's root
    sessions (the digest header states the scoping; the per-root debounce
    marker makes tenants debounce independently), and
    `run_consolidation(on_seeded=…)` hands the curation task id to the host
    before any worker can claim it, so the run curates the same tenant store
    it was scoped to.
  - Defaults unchanged: without a resolver or filter, single-tenant hosts are
    byte-identical. See the new how-to `docs/how-to/multi-tenant-memory.md`.

## [0.2.3] - 2026-07-10

### Added

- **Memory v2 — the store maintains itself.** Memory v1's file-per-memory
  base gains the pieces that keep it healthy over time:
  - **Memory-policy prompt.** Memory-enabled presets (`main` / `main-web`)
    carry a policy fragment (exported as `MEMORY_POLICY_PROMPT`) telling the
    model what earns a memory, what never does, and the write hygiene
    (dedupe before writing, archive the stale).
  - **Frontmatter + richer recall.** `memory_write` takes optional
    `description` (one-line index summary) and `type`
    (`user` / `project` / `procedural` / `reference`), stored as frontmatter
    the tool composes itself; recall upgrades to two deterministic tiers
    (name tokens first, then summary tokens). Files without frontmatter keep
    the v1 behavior byte-for-byte.
  - **`memory_search` and `memory_archive` tools.** Case-insensitive
    substring search with grep-style excerpts (a `truncated` flag reports
    when more matched), and reversible retirement into an `archive/`
    subdirectory — memories are never deleted.
  - **Background consolidation.** After a session stops (debounced, default
    24h, marker file in the memory root), a hidden `__consolidation__` agent
    reads a digest of recent session activity and merges duplicates, archives
    superseded memories, and backfills missed facts — through the same memory
    tools, memory pack only. On by default in the served backend
    (`NOETA_AGENT_MEMORY_CONSOLIDATION=0` disables;
    `NOETA_AGENT_MEMORY_CONSOLIDATION_DEBOUNCE_HOURS` tunes); SDK hosts
    orchestrate their own runs via `noeta.sdk.run_consolidation`. See
    `docs/adr/memory-consolidation.md`.

## [0.2.2] - 2026-07-10

### Added

- **`noeta.sdk` exports `Capabilities` and `BudgetSpec`.** Both were documented
  as part of the SDK surface but were unreachable imports; they are now
  importable from `noeta.sdk`.
- **`Options.skills` is honored.** Skills passed through `Options.skills` are now
  wired into pre-loop activation — previously a silent no-op.
- **The slash-command catalog is served from `/capabilities`.** The command menu
  was permanently empty; it is now populated from the same endpoint.

### Fixed

- **`apply_patch` surfaces its file changes**, so a conversation rewind restores
  the files it wrote.
- **Delegated subtask drain no longer leaks its lease on fault.** Child-descent
  and parent-resume are wrapped so any fault releases the lease instead of
  leaking it and crashing the drive; a `num_workers>=2` race now degrades
  gracefully.
- **`react` guards the empty-content `max_tokens` branch**, avoiding an
  Anthropic 400 followed by a retried poisoned history.
- **Workflow orchestration AST-splices subtask scripts** instead of
  `textwrap.indent`, which could corrupt triple-quoted strings.
- **`background_subagent.recover()` is guarded** so one bad record can no longer
  crash startup.
- **`openai_compat` maps `prompt_tokens_details.cached_tokens`** into
  `Usage.cache_read`.
- The right dock falls back to another tab when the live preview disappears, and
  the composer keeps per-session draft text and images.

### Performance

- **Sandbox skill indexing folds into one container round-trip.** Sandbox-mode
  indexing previously cost one HTTP round-trip per file (~200 calls for a couple
  dozen skills), stalling `seed_start` for ~160s; a single
  `ExecEnv.tree_snapshot` walk now completes it in ~1s. (#46)
- The trace inspector reuses the incremental multiplex store (removing an O(N²)
  re-fold); the hot-path `_recent_tool_calls` scan stops at the window; `/tasks`
  skips subtask folds and `/stream` caches immutable parent links.

## [0.2.1] - 2026-07-09

### Fixed

- **Delegated subtasks no longer deadlock their parent under the resident
  worker.** `resolve_engine` wrapped every claimed task with the multi-turn
  (interactive `Client`) wrapper unconditionally. A child task claimed directly
  by a resident worker then turned its `FinishDecision` into a next-goal
  *suspend* instead of a genuine `TaskCompleted`, so the `ChildLifecycleObserver`
  never woke the parent and the parent's `SubtaskGroupCompleted` barrier
  deadlocked. A child (its `parent_task_id` set) is now built unwrapped.

## [0.2.0] - 2026-07-09

### Added

- **Sandbox browser subsystem (opt-in).** With `NOETA_AGENT_SANDBOX=1`, the
  agent gains a noeta-owned browser tool pack (`browser_navigate` / `click` /
  `type` / `extract` / `screenshot`) driving the per-session container's
  browser, plus a `web` delegation specialist that owns it. The tool
  names/schemas are pinned by noeta (stable across AIO image changes), and
  `main` stays browser-free — it delegates every page interaction to `web`, so
  a browsing task's token churn is isolated in a child context that returns a
  distilled result. Off by default: non-sandbox deployments keep a
  byte-identical agent roster and stable prefix. The screenshot lands as a
  workspace artifact (viewable in the file panel), not model vision, in this
  increment.
- **Live-preview panels.** When the sandbox is on, the web UI's right dock
  gains three live tabs — **Browser** (noVNC), **Terminal** (container PTY),
  and **Code** (code-server) — reverse-proxied (HTTP + a stdlib WebSocket
  pump) to the session's container. They are served from a **dedicated preview
  port** that holds no noeta state (origin isolation, since the iframes run
  `allow-same-origin`); discover it via `GET /tasks/{id}/preview`. Pin the port
  with `NOETA_AGENT_SANDBOX_PREVIEW_PORT` behind a firewall/tunnel. The
  browser→noeta leg is unguessable-token-only (demo boundary); container
  credentials ride only the noeta→container leg. See
  [known limitations](docs/operations/limitations.md).
- **Inline image artifacts in the transcript.** Image artifacts a tool
  produces (e.g. `browser_screenshot`) now render inline beneath their tool
  call in the web UI, opening in the existing lightbox.

### Fixed

- **Foreground sub-agents no longer fail under multi-worker contention.** The
  0.1.16 fix (`settle_subtasks_after_step`) drives foreground children through
  the delegation drain after the parent step completes, but with multiple
  workers a sibling worker can claim the child from the FIFO queue before the
  drain's targeted lease succeeds. When that happens the child runs with empty
  `runtime.messages` → provider 400. `run_leased_task` now defensively seeds
  the child's goal as its opening user message (mirroring `_descend_to_child`),
  so the child is well-formed regardless of which worker picks it up. Idempotent
  with the drain path — the drain's own "empty messages" guard skips re-seeding.

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

[Unreleased]: https://github.com/initxy/noeta/compare/v0.6.17...HEAD
[0.6.17]: https://github.com/initxy/noeta/compare/v0.6.16...v0.6.17
[0.6.16]: https://github.com/initxy/noeta/compare/v0.6.15...v0.6.16
[0.6.15]: https://github.com/initxy/noeta/compare/v0.6.14...v0.6.15
[0.6.14]: https://github.com/initxy/noeta/compare/v0.6.13...v0.6.14
[0.6.13]: https://github.com/initxy/noeta/compare/v0.6.12...v0.6.13
[0.6.12]: https://github.com/initxy/noeta/compare/v0.6.11...v0.6.12
[0.6.11]: https://github.com/initxy/noeta/compare/v0.6.10...v0.6.11
[0.6.10]: https://github.com/initxy/noeta/compare/v0.6.9...v0.6.10
[0.6.9]: https://github.com/initxy/noeta/compare/v0.6.8...v0.6.9
[0.6.8]: https://github.com/initxy/noeta/compare/v0.6.7...v0.6.8
[0.6.7]: https://github.com/initxy/noeta/compare/v0.6.6...v0.6.7
[0.6.6]: https://github.com/initxy/noeta/compare/v0.6.5...v0.6.6
[0.6.5]: https://github.com/initxy/noeta/compare/v0.6.4...v0.6.5
[0.6.4]: https://github.com/initxy/noeta/compare/v0.6.3...v0.6.4
[0.6.3]: https://github.com/initxy/noeta/compare/v0.6.2...v0.6.3
[0.6.2]: https://github.com/initxy/noeta/compare/v0.6.1...v0.6.2
[0.6.1]: https://github.com/initxy/noeta/compare/v0.6.0...v0.6.1
[0.6.0]: https://github.com/initxy/noeta/compare/v0.5.5...v0.6.0
[0.5.5]: https://github.com/initxy/noeta/compare/v0.5.4...v0.5.5
[0.5.4]: https://github.com/initxy/noeta/compare/v0.5.3...v0.5.4
[0.5.3]: https://github.com/initxy/noeta/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/initxy/noeta/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/initxy/noeta/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/initxy/noeta/compare/v0.3.2...v0.5.0
[0.2.11]: https://github.com/initxy/noeta/compare/v0.2.10...v0.2.11
[0.2.10]: https://github.com/initxy/noeta/compare/v0.2.9...v0.2.10
[0.2.9]: https://github.com/initxy/noeta/compare/v0.2.8...v0.2.9
[0.2.8]: https://github.com/initxy/noeta/compare/v0.2.7...v0.2.8
[0.2.7]: https://github.com/initxy/noeta/compare/v0.2.6...v0.2.7
[0.2.6]: https://github.com/initxy/noeta/compare/v0.2.5...v0.2.6
[0.2.5]: https://github.com/initxy/noeta/compare/v0.2.4...v0.2.5
[0.2.4]: https://github.com/initxy/noeta/compare/v0.2.3...v0.2.4
[0.2.3]: https://github.com/initxy/noeta/compare/v0.2.2...v0.2.3
[0.3.2]: https://github.com/initxy/noeta/compare/v0.3.1...v0.3.2
[0.3.1]: https://github.com/initxy/noeta/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/initxy/noeta/compare/v0.2.11...v0.3.0
[0.2.2]: https://github.com/initxy/noeta/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/initxy/noeta/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/initxy/noeta/compare/v0.1.17...v0.2.0
[0.1.17]: https://github.com/initxy/noeta/compare/v0.1.16...v0.1.17
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
