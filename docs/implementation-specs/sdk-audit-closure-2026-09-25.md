# SDK audit closure — 2026-09-25

Status: IMPLEMENTED 2026-09-25 on branch `fix/sdk-audit-closure-2026-09-25` (base 5f75aca, runtime 0.6.30 / sdk 0.6.30) — uncommitted, awaiting the maintainer's commit and release call. `make check` green: ruff, 4400 passed / 141 skipped, coverage 88.49 %, `mypy --strict` on both packages (231 files), naming and import lints. See "Outcome" at the end for what shipped, what changed shape, and what was left out.

## Goal

Close every reproduced defect from the 2026-09-25 seven-track audit of the SDK
(shell policy, providers, MCP, built-in tools, client/host surface, token
economy, packaging/CI/docs) and land the small token-economy fixes it measured.
When this ships, `make check` is green, every finding below has a regression
test or a documented reason it has none, and the docs say what the code does.

## Scope

Work packages, each with exclusive file ownership so they can be built in
parallel. A package edits only its files; anything it needs elsewhere it
reports to the integrator (main session) instead of editing.

### WP-B — providers + ReAct policy

Files: `packages/noeta-sdk/noeta/builtins/providers/impl/*`,
`packages/noeta-sdk/noeta/builtins/react/impl/*`, their tests.

- B1 **Dangling `tool_use` after `max_tokens`.** When a response stops on
  `max_tokens` and carries parseable `ToolUseBlock`s, the `llm_truncated`
  `FailDecision.assistant_message` must not carry those blocks into history (a
  following user turn is then rejected with 400 by Anthropic and OpenAI). Keep
  the text, drop the calls; a message left empty is not attached.
- B2 **Truncated arguments are `max_tokens`, not a transient error.**
  `openai_compat` / `openai_responses`: on `finish_reason == "length"` do not
  decode partial tool arguments as `MalformedToolArgumentsError` (which retries
  the whole generation up to 8 times); return `stop_reason="max_tokens"` with
  the complete calls only. `anthropic`: the same case currently raises a bare
  `ValueError` ("streamed tool_use input was not valid JSON") — same mapping.
- B3 **A refusal that interrupts a tool call** (`stop_reason="refusal"` with
  `tool_use` content) maps to `end_turn` with the tool blocks dropped instead of
  the "inconsistent Anthropic response" error.
- B4 **HTTP error text keeps the body.** All three `_translate_http_error`
  helpers build `FatalError` / `TransientError` from `str(exc)`, which for
  `httpx.HTTPStatusError` has no body. Append the first 500 characters of the
  response body (JSON `error.message` when present).
- B5 **A clean mid-stream disconnect is transient.** Anthropic: stream ends
  without `message_delta` → `TransientError`, not `stop_reason="error"`.
  OpenAI compat: stream ends without `[DONE]` after partial content →
  `TransientError` (today only the empty-content case is).
- B6 **OpenAI compat parallel `tool_calls` without `index`** start a new call
  when a delta carries a new `id`, instead of concatenating arguments.
- B7 **OpenAI compat tool-result images** render an `[image omitted: …]` line
  the way `openai_responses` already does, instead of vanishing.
- B8 **Catalog:** `gpt-4o` / `gpt-4o-mini` are `supports_vision=True`.
- B9 **`structured_output` with neighbouring calls** in the same response is
  rejected with a tool error telling the model to send it alone, instead of
  silently ending the turn and dropping the neighbours
  (`react/impl/orchestration.py`).
- B10 The `_response_to_decision` fatal arm passes the provider's error text
  through as `FailDecision.detail` (field added by WP-C).

Non-goals: adding catalog rows for models whose pricing is unknown;
`max_completion_tokens` for OpenAI reasoning models (unverified; gateways
differ).

### WP-C — failure diagnostics + observability projections

Files: `packages/noeta-runtime/noeta/protocols/{decisions,events}.py`,
`packages/noeta-runtime/noeta/core/_decision_handlers.py`,
`packages/noeta-runtime/noeta/execution/multi_turn.py`,
`packages/noeta-runtime/noeta/observers/audit.py`,
`packages/noeta-runtime/noeta/storage/_payload_restore.py`,
`packages/noeta-sdk/noeta/client/otlp.py`, their tests.

- C1 **`FailDecision.detail: Optional[str] = None`** and
  **`TaskFailedPayload.detail: str = ""`**, additive with defaults, byte-safe
  for existing recordings (follow the `LLMRequestFinishedPayload` precedent;
  verify the canonical encoder and `_payload_restore`). The handler copies
  `detail` from decision to payload. `multi_turn`'s `SuspendReason.detail`
  carries `"<reason>: <detail>"` when detail is present.
- C2 **Audit + OTLP carry `usage` and `latency_ms`** from
  `LLMRequestFinished` (today stripped to `cost_usd`).
- C3 **OTLP parent span across machines**: derive `parentSpanId`
  deterministically from the parent task id (same function as
  `_task_span_id`) so a child exported on another host still links.
- C4 **`ToolSchemaRecorded`** is documented as legacy/not emitted; stale
  comments in `session_pack.py` / `builder.py` corrected (report to WP-D for
  those files if not in scope).

`mypy --strict` on `noeta.protocols` must stay clean.

### WP-D — client / host / options / runtime execution

Files: `packages/noeta-sdk/noeta/client/*` (except `otlp.py`, and except the
shell-rule composition block in `host.py` that WP-E owns),
`packages/noeta-sdk/noeta/sdk/__init__.py`,
`packages/noeta-sdk/noeta/builtins/memory/*`,
`packages/noeta-sdk/noeta/builtins/delegation/*`,
`packages/noeta-sdk/noeta/presets/*` (prompt text: keep any change to one line;
report the byte delta),
`packages/noeta-runtime/noeta/execution/{driver,resolver,background_subagent,background_delivery,subtask_drain,host}.py`,
their tests.

- D1 **Recovery does not re-drive a live sub-agent.**
  `recover_background_subagents` / `recover()` skip a sub-task another Client
  currently holds (live lease or queue row leased) — a second `Client` on the
  same store must not double-run it or double-deliver. The
  `_ChildNotReady` path must not deliver a false "did not complete".
- D2 **Sub-agent approvals surface.** A foreground child suspended on a tool
  approval reaches `can_use_tool` (with the child's task id) and the root's
  `DriveOutcome` names the child's wake handle; `query()` reports the pending
  approval instead of "no terminal event". Docs claim
  `UnsupportedSubtaskSuspend` — align code or docs (report the doc change).
- D3 **`memory_read_only` prompt.** The compiled memory-policy fragment
  offers only read/search when the pack is read-only (a shorter variant, not an
  extra rule). Default prompt bytes unchanged; goldens untouched.
- D4 **Model aliases resolve everywhere.** `AgentDefinition.model` resolves
  through the catalog when the engine is built; a recorded `ModelBound` alias
  from a pre-0.6.30 store resolves on resume; `_inherited_model_of` compares
  resolved ids.
- D5 **Seed prelude is compensated.** The post-`ModelBound` engine rebuild
  moves inside the compensation `try`; a failure re-suspends and releases the
  lease. `send_goal(permission_mode=…)` / `effort=…` validate at entry and
  raise a `CodedError` before any event is written (same rules as `Options`).
- D6 **`shutdown()` owns background work.** Kill the tree's background shells
  (reuse the orphan machinery), stop driving background sub-agents after
  shutdown, close storage adapters the Client opened from `storage_path`.
- D7 **Constructor cost.** `recover()` does not read every task stream; use the
  task summaries / status read model to find candidates.
- D8 **Delivery after the window.** A background result deferred past the
  delivery window is re-scanned for that root at the end of each turn, not only
  at Client construction.
- D9 **HostConfig validation**: `recall_exclude` must be a collection of
  strings (a bare `str` is rejected); non-positive `memory_max_bytes`,
  negative `memory_index_budget_tokens`, non-positive `max_background_*`,
  negative `mcp_idle_ttl` rejected; `instructions_file` without
  `instructions_enabled` rejected or documented; `storage_path=""` rejected;
  unknown `plugin_config` keys warned. New `HostConfig.reliability_sink`
  forwarded by `start_workers`; `lease_backoff_max_s` reachable.
- D10 **Errors are typed and exported.** `AnswerValidationError` and
  `WorkspaceEscape` become `CodedError`s exported from `noeta.sdk`;
  `resolve_tool_call_arguments` and `WorkerLoop` exported.
- D11 **`messages()` views**: `ToolResultView.tool_name` populated,
  `ToolResultView.error` added.
- D12 **Task tool**: `background` accepts only booleans (a string is a tool
  error); an empty / whitespace `prompt` is a tool error.
- D13 **Memory**: recall judge index capped by `memory_index_budget_tokens`;
  `memory_write` `max_bytes` counts frontmatter too; a body starting with
  `---` is treated as frontmatter only when every line before the closing
  `---` is a known field. WebFetch digest and recall judge receive
  `HostConfig.provider_headers` (call `complete_with_headers` when the
  provider has it) — the judge side is D's, the digest side is E's.
- D14 **Sub-agent tool set**: an agent whose spec declares no `spawnable`
  agents gets no `Task` tool (today `delegation` is inherited regardless).

### WP-E — built-in tools, ExecEnv, shell policy

Files: `packages/noeta-runtime/noeta/runtime/{shell_policy,subproc}.py`,
`packages/noeta-sdk/noeta/builtins/fs/*`, `packages/noeta-sdk/noeta/builtins/web/*`,
`packages/noeta-sdk/noeta/builtins/sandbox/impl/exec_env.py`, the shell
allowlist composition block in `packages/noeta-sdk/noeta/client/host.py`
(only that block), their tests.

- E1 **Allowlist bypass (P0).** A command containing an unquoted `{`, `*`,
  `?`, `[`, `~` (bash expands these; `shlex` does not) is never allowlisted;
  quoted or backslash-escaped occurrences stay allowed so
  `find . -name '*.py'` is unchanged.
- E2 **Curated rules that execute repo code** (`pytest`, `uv run pytest`,
  `npm test`, `pnpm test`; `git` rules without `-c core.fsmonitor=
  --no-ext-diff --no-textconv`) are gated behind the workspace trust already
  used for `.noeta/shell-allowlist.json`, or hardened with those flags.
- E3 **Bash output is capped while streaming**, not after `communicate()`:
  bounded head+tail buffers per pipe, same rendered result as today's
  `cap_stream`.
- E4 **Read** reads only what it serves: `offset`/`limit` stream the window;
  a hard cap of 100 KB (≈25K tokens) on the served text with an actionable
  message; the ContentStore receives at most the served window (check what
  provenance / drift needs before changing what is stored).
- E5 **CRLF**: `Edit` matches and writes with the file's own line ending.
- E6 **Glob** supports `{a,b}` brace alternatives.
- E7 **WebFetch honours Content-Type**: HTML → today's pipeline; text/*,
  JSON, XML → text as-is (size-capped); binary (image/*, PDF,
  octet-stream) → a tool error naming the type; charset from the header or
  `<meta charset>`. Same-host redirect compares scheme and port. Sandbox curl
  gets `-g`. Digest receives `provider_headers`.
- E8 **Line numbering** in Read/Edit uses `\n` only (matches `rg`), not
  `str.splitlines()`.
- E9 **Write `allowed_path_globs`**: `*` does not cross `/` (`**` does).
- E10 **Sandbox `run_argv`**: a missing `exit_code` is a failed run (-1),
  never success.
- E11 **Sandbox `_read_spill`** truncates on a UTF-8 boundary and handles a
  second overflow.

Non-goals: a foreground `&` background-job policy; sandbox interrupt (needs a
container to verify).

### WP-F — MCP client

Files: `packages/noeta-sdk/noeta/builtins/mcp/*`,
`packages/noeta-runtime/noeta/runtime/mcp.py` (additive field only), tests.

- F1 `tools/list`, `prompts/list`, `resources/list` follow `nextCursor`.
- F2 A tool name over 64 characters is truncated with a short hash suffix; an
  intra-server sanitised-name collision gets the same suffix; the server is
  never skipped for one name. A cross-server alias collision raises
  `McpConfigError` at build time.
- F3 A call timeout or EOF retires the pooled connection.
- F4 An SSE response returns as soon as the matching JSON-RPC id arrives.
- F5 `structuredContent` is included in the tool output; image content
  becomes `ToolResult.images`; resource links are listed as text.
- F6 stdio: a message with `method` is a server request/notification, never a
  response; `ping` is answered.
- F7 `McpServerSpec.call_timeout_s: Optional[float] = None` (additive)
  overrides the 30 s default for tool calls.

### WP-G — token economy (phase 2, after B/D land)

Files: `packages/noeta-runtime/noeta/runtime/llm.py`,
`packages/noeta-runtime/noeta/context/*`, the Anthropic adapter's
`cache_control` placement, `react.py`'s summary request, the skill-menu and
memory-index budget defaults.

- G1 Tool-call arguments are key-sorted (recursively) on the live path so
  the live prefix equals the replayed one.
- G2 The fourth Anthropic cache breakpoint sits on the previous step's last
  recorded block.
- G3 The summarize request shares the main system+tools prefix (instructions
  only in the trailing user message; `tool_choice` none where the adapter
  supports it). Ship only if the compaction tests and the note-shape gate stay
  green; otherwise report.
- G4 Memory index is byte-stable within a task (first-write-wins like the
  environment block); a page written by this task shows in the
  `memory_write` result. Ship only if clean; otherwise report.
- G5 Skill-menu and memory-index budgets get an absolute ceiling
  (`min(1 % of window, 4096 tokens)`), configurable.
- G6 `ReminderView.already_spawned` and the dead "delegation fan-out nudge"
  comment removed.
- G7 The `composer_view_*` golden tests build the real default tool set.

Non-goals: microcompaction of old tool outputs; MCP deferred schemas
(ToolSearch); both need an ADR.

### WP-H — packaging, CI, docs

Files: `packages/*/pyproject.toml`, `packages/*/LICENSE`, `py.typed`
markers, `packages/noeta-runtime/README.md`, `.github/workflows/*`,
`Makefile`, `docs/**`, `README*.md`, `CONTEXT.md`, the five ruff errors in
`tests/`.

- H1 LICENSE in every wheel and sdist (`license-files`); `py.typed` in each
  regular sub-package of both distributions; a test asserts both.
- H2 `packages/noeta-runtime/README.md` rewritten for PyPI (current, short).
- H3 Benchmarks: record the 2026-09-19 run at sdk 0.6.28 (TB2.1 sample-40,
  first pass 24/40 with 9 infrastructure errors — 3 deterministic `llm_error`
  on image tasks, 6 harness timeouts; 33/40 after re-running the errored
  tasks twice; disclose the reruns), state that the headline is a single
  sample, and soften "Proven on" wording on the site and READMEs.
- H4 CI: ruff step; Python 3.11/3.12/3.13 matrix; docs build on
  `pull_request`; `timeout-minutes`; release depends on the test job;
  `setup-uv` versions aligned; `permissions` / `concurrency` set.
- H5 ripgrep named in install docs; a one-time warning when `rg` is missing
  is WP-E's (fs plugin factory).
- H6 Docs drift: `sdk.md` approval sample (find the *unresolved* request),
  error table, `WorkerLoop` import path, `CONTEXT.md` default tool list,
  `subagents.md` approvals, `limitations.md` multi-client caveat, async
  blocking note, `ToolResultView` fields, MCP tool naming, memory read-only
  prompt behaviour, CHANGELOG `[Unreleased]` (integrator writes it).

### WP-M — type gate (phase 2)

Declare `ResidentHost.note_turn_permission`; clear the 81 `mypy --strict`
errors across both packages; extend the `make check` / CI mypy target to both
packages if it passes.

## Key decisions

- Shell policy: reject *unquoted* expansion characters rather than every
  occurrence, so quoted patterns keep working; execution stays `bash -c`.
- Failure detail is an additive field, never a change to `reason` strings
  (hosts branch on them).
- Prompt text changes are one line each and the byte delta is reported.
- Anything that needs a container, a Postgres server or real tokens to verify
  (sandbox interrupt, Postgres reconnect, benchmark rerun) is out of scope and
  listed in the handoff, not half-done.

## Acceptance

- `make check` green (pytest ≥ 85 % coverage, mypy --strict on the gated
  scope, naming + import lints).
- Every WP item above has a regression test, or the report names why not.
- Goldens re-locked only by the integrator, with the byte delta stated.
- CHANGELOG `[Unreleased]` lists every behaviour change, defaults first.

## Outcome (2026-09-25)

Every WP landed; CHANGELOG `[Unreleased]` is the authoritative list of
host-visible changes. Items whose shape differs from the plan above:

- **C1** `TaskFailedPayload.detail` is `Optional[str] = None` with
  `__canonical_omit_none__`, not `str = ""`: the canonical encoder omits only
  `None`, so `""` would have changed every recorded `TaskFailed`'s bytes.
- **D13** frontmatter parsing: a line is a field when it is a tool field, a
  field already on disk, or a lower-case `key: value` name; the spec's "every
  line a known field" would have broken the documented custom-field promise.
- **E2** git rules are hardened (`-c core.fsmonitor=`, `--no-ext-diff
  --no-textconv`) rather than trust-gated — the ADR says a prompt on every
  `git status` is noise. Residual: a repository `filter.<driver>.clean` still
  runs on a changed file.
- **F7** `call_timeout_s` was added to `McpHttpServerSpec` as well as
  `McpServerSpec` (both additive).
- **G3** ships, but on Anthropic the `tool_choice` change invalidates the
  message-layer cache entry, so only the tools and system layers are served
  from cache there; the full saving appears on OpenAI-style prefix caching.
  Getting it on Anthropic needs a decision: first summarize call without
  `tool_choice`, retry once with `none` only when the model answers with a
  tool call.
- **G4 not shipped.** Freezing the memory index per task contradicts the
  earlier D9 decision (a page written mid-task must reach the resident on the
  next turn; `recall.py` relies on the same refresh so a rewritten page stops
  contradicting the resident). The cost stays: one prefix rewrite per turn
  that follows a write. A maintainer call.
- **G5** ceiling implemented in `host.py` (`SKILL_MENU_BUDGET_CEILING_TOKENS`
  = 4096) for the derived defaults only.
- **D5 residual**: when the post-switch engine rebuild fails, the goal message
  and `ModelBound` are already recorded; re-issuing the command records the
  goal twice. The task is no longer stranded.
- **D7 residual**: half of the remaining construction reads come from
  `ChildLifecycleObserver._recover_pending_handoffs`, which still reads whole
  streams.
- **D8** covers turns driven through `Client` methods; a turn finished by a
  resident worker or `dispatch_seeded` does not re-scan.
- **E4** sandbox backend still reads the whole file (a windowed read needs an
  `ExecEnv` Protocol method, a breaking change for third-party backends).

Left out on purpose (need a container, a database, real tokens, or an ADR):
sandbox foreground interrupt; Postgres reconnect / pooling; benchmark rerun
(the 2026-09-19 run is now documented instead); `TodoWrite` + `Task` in one
response (K2); hooks / `Principal` on the public surface; a cost read model;
microcompaction of old tool outputs; MCP deferred schemas (ToolSearch);
`max_completion_tokens` for OpenAI reasoning models via the compat adapter;
catalog rows for models whose pricing is unknown.

The benchmark headline on the site now shows 33/40 best-of-three with the
24/40 first pass beside it; whether the first pass should lead is the
maintainer's call.

## Phase 2 — the left-over list (2026-09-25, same day)

Status: IMPLEMENTED 2026-09-25 on the same branch, uncommitted. `make check`
green after integration: ruff, 4537 passed / 149 skipped, coverage 88.85 %,
`mypy --strict` on both packages (232 files), naming and import lints; the
docs site builds. See "Phase 2 outcome" at the end. The maintainer asked for
the rest. Everything the Outcome above listed as "left out on purpose" that
can be built and verified on this machine is built here (docker is available
with `postgres:16` and the AIO sandbox image), plus the D5 / D7 / D8 / E4
residuals. Items that would
reverse a recorded ADR decision, or that need the maintainer's product call,
are written up under "Decisions" and not built.

Rules for every WP: English artifacts, `scripts/lint-naming.py` and the
import-linter contracts hold (the kernel never imports `noeta.builtins`; the
client reaches built-ins only through the loader), no `ExecEnv` / storage
Protocol gains a required member, prompt text changes on one line with the
byte delta reported, goldens re-locked only for the snapshot a WP actually
changes. Shared files (`client.py`, `host.py`) are edited in disjoint regions.

### WP-S — sandbox: foreground interrupt + windowed read (E4)

Files: `packages/noeta-sdk/noeta/builtins/sandbox/impl/exec_env.py`,
`packages/noeta-sdk/noeta/builtins/fs/impl/{shell,read}.py`,
`packages/noeta-runtime/noeta/runtime/{background_shell,exec_env}.py`
(additive only), `docs/adr/interrupt-responsiveness.md`,
`docs/adr/execution-environment-seam.md`, `docs/guides/sandbox.md` (+zh),
`docs/operations/limitations.md` (+zh), tests.

- S1 Every foreground `run_argv` on the AIO backend runs in its own shell
  session: the request carries `id=<fresh uuid>` and `hard_timeout=timeout_s`
  (the image's `/v1/openapi.json` lists both on `ShellExecRequest`;
  `/v1/shell/kill {id}` terminates that session's process). A timed-out
  command is therefore killed in the container, not left running.
- S2 The interrupt cascade reaches the container: the registry's foreground
  table accepts a kill callable as well as a `Popen`, `_kill_foreground`
  invokes it (POST `/v1/shell/kill`) off the control-plane thread, and
  `unregister_foreground` still answers `killed`. The shell tool registers the
  callable when the backend exposes the optional capability; `ExecEnv` itself
  gains no required member.
- S3 The interrupted run reports *interrupted*, not a timeout, exactly as the
  local backend does.
- S4 (E4) Sandbox `Read` no longer pulls the whole file: an optional backend
  capability (`read_range(path, offset, length) -> bytes`, byte-exact through
  `dd` + base64 in a subshell) lets `Read` iterate chunks lazily; the
  ContentStore rule is unchanged (whole file up to 1 MiB, the served window
  above). `read_bytes` stays for every other caller.
- Tests: fake transport (exec body carries `id` + `hard_timeout`; the kill
  POST carries the same id; interrupted result), registry with a kill
  callable, windowed Read over a fake backend; a `-m live` test against the
  local image for kill-while-sleeping and a windowed read.

### WP-P — Postgres reconnect

Files: `packages/noeta-sdk/noeta/builtins/storage/impl/postgres/*.py`,
`docs/operations/limitations.md` (+zh), the storage ADR (amend), `tests/_pg.py`,
tests.

- P1 A dropped connection (server restart, idle kill, network reset —
  `psycopg.OperationalError` / `InterfaceError` on a broken connection) is
  reopened once, transparently. A statement outside a transaction retries
  once on the fresh connection. A transaction that fails before its `COMMIT`
  is issued is re-run from `BEGIN` (advisory locks are per transaction, so
  nothing leaks). A failure raised by `COMMIT` itself is ambiguous and
  propagates — the connection is still reopened for the next call — except
  an `_append` carrying an idempotency key, which the key makes safe to
  retry.
- P2 One small wrapper in `_connection.py` exposing `execute` / `close`;
  the adapters keep their shape and the tests that reach `_conn.execute`
  keep working.
- P3 No pool: one connection per adapter behind its lock stays (pooling is a
  separate design).
- Tests: unit with a fake connection (retry / no-retry per rule, including
  the `COMMIT` rule); integration against a real server started with
  `docker run postgres:16` and `NOETA_TEST_POSTGRES_DSN` — kill the adapter's
  backend with `pg_terminate_backend`, then emit / read / lease succeed. The
  tests gate on the DSN like the contract suites (CI has the service).

### WP-K — K2: `TodoWrite` + `Task` in one response

Files: `packages/noeta-runtime/noeta/protocols/decisions.py`,
`packages/noeta-runtime/noeta/core/_decision_handlers.py`,
`packages/noeta-runtime/noeta/execution/subtask_drain.py` (if the result
pairing lives there), `packages/noeta-sdk/noeta/builtins/todo_write/impl/*`,
`packages/noeta-sdk/noeta/builtins/delegation/impl/__init__.py`,
`packages/noeta-sdk/noeta/builtins/react/impl/{orchestration,control_tool}.py`
(only if the translate chain needs a companion hook),
`docs/adr/control-tools-neutral-mechanism.md` (amend), tool docs that state
the restriction (+zh), tests.

- K1 One `TodoWrite` plus one or more `Task` calls in a response saves the
  checklist AND spawns: the todos patch and the TodoWrite ack ride the spawn
  decision (`SpawnSubtaskDecision` / `SpawnSubtasksDecision` gain
  `preacked_results: tuple[ToolResultBlock, ...] = ()`).
- K2 Every tool_use gets exactly one tool_result and the provider sees ONE
  tool-role message per assistant turn: for `background=True` the ack joins
  the "started" result; for a foreground single / fan-out spawn the ack
  reaches the resume-time result message. If the durable resume path needs
  the ack, it is an additive Optional payload field with
  `__canonical_omit_none__` (the `TaskFailedPayload.detail` pattern).
- K3 `AskUserQuestion` + TodoWrite and a second TodoWrite stay refused.
- K4 `todo_write.md` changes on its one batching line only; byte delta
  reported; only the affected golden is re-locked.

### WP-H — hooks and `Principal` on the public surface, plus D5

Files: `packages/noeta-sdk/noeta/client/{host_config,client,host}.py`,
`packages/noeta-sdk/noeta/sdk/__init__.py`,
`packages/noeta-sdk/noeta/builtins/governance/impl/__init__.py`,
`packages/noeta-runtime/noeta/execution/driver.py`,
`docs/reference/{options,sdk,types}.md` (+zh),
`docs/adr/guard-observer-hooks.md` (amend), `CONTEXT.md`, tests.

- H1 `HostConfig.hooks: Optional[HooksConfig]` with `pre_tool_use`
  (`PreToolUseRule`, feeds the existing HookGuard input), `post_tool_use` and
  `notification` (`PostToolUseRule` / `NotificationRule`, build one
  `HookObserver` subscribed at Client construction and stopped by
  `shutdown()` / `close()`), plus the command timeout and queue bound.
  Validated at construction. Exported from `noeta.sdk`: `HooksConfig`,
  `PreToolUseRule`, `MatchArg`, `PostToolUseRule`, `NotificationRule`.
- H2 `Client(principal=...)` (default `LOCAL_PRINCIPAL`) reaches the driver;
  `start` / `send_goal` and their `seed_` twins accept `principal=` as a
  per-turn override (a multi-user host serves many principals from one
  Client), gated by the same `allowed_models ∩ allowlist` rule and stamped on
  `ModelBound.principal_identity`. Exported: `Principal`, `LOCAL_PRINCIPAL`.
- D5 Re-issuing `send_goal` after a failed post-switch rebuild does not
  record the goal a second time.

### WP-C — cost read model

Files: new `packages/noeta-sdk/noeta/client/usage.py`,
`packages/noeta-sdk/noeta/client/client.py` (`usage()`, `QueryResult.usage()`),
`packages/noeta-sdk/noeta/sdk/__init__.py`, `docs/reference/{sdk,types}.md`
(+zh), `docs/guides/models.md` (+zh), tests.

- C1 `Client.usage(task_id, *, include_children=True) -> UsageReport`: totals
  and per-model rows (requests, input / output / cache-read / cache-write /
  reasoning tokens, `cost_usd`, latency total and max) built from the
  `LLMRequestStarted` (model) + `LLMRequestFinished` (usage, cost, latency)
  pairs of the root and, through `TaskCreated.parent_task_id`, its children;
  `unpriced_models` names every model whose calls were charged $0 because the
  catalog has no rates. Frozen dataclasses, stdlib types.
- C2 `QueryResult.usage()` for the one-shot path.
- C3 No new catalog rows: the ids the maintainer's benchmarks ran are
  gateway-internal and belong in `HostConfig.extra_models`; `unpriced_models`
  makes the gap visible instead.

### WP-O — OpenAI-compat `max_completion_tokens` + provider leftovers

Files: `packages/noeta-sdk/noeta/builtins/providers/impl/{openai_compat,anthropic}.py`,
`docs/guides/models.md` (+zh), tests.

- O1 `OpenAICompatProvider(max_tokens_param="auto")`: `auto` sends
  `max_completion_tokens` when the catalog row for the request's model is a
  reasoning row of the OpenAI family (or the id is an `o1` / `o3` / `o4` /
  `gpt-5` id), `max_tokens` otherwise; the two literal values force one
  (gateways differ). Nothing changes for non-reasoning or uncatalogued ids.
- O2 Anthropic streaming: an empty text block is dropped from the assembled
  message so the next request is not rejected.

### WP-D — residuals D7 / D8

Files: `packages/noeta-runtime/noeta/core/observers.py` (D7),
`packages/noeta-runtime/noeta/runtime/worker.py`,
`packages/noeta-sdk/noeta/client/host.py` (D8, its own region), tests.

- D7 `_recover_pending_handoffs` reads full streams only for candidates found
  through the catalog (`list_task_streams` + latest snapshot / tail), with a
  test counting `read` calls on a large in-memory store.
- D8 The deferred-delivery re-scan also runs when a turn finishes on a
  `WorkerLoop` worker or via `dispatch_seeded`, through the existing resident
  host seam (no new required Protocol member).

### Decisions (written up, not built)

- **Microcompaction** of old tool outputs before the window fills reverses
  the compaction ADR's "relief valve, not an always-on clamp" decision (the
  re-read thrash it names). Proposal for the maintainer: a second, count-based
  valve — keep the newest N tool outputs verbatim and clear older ones above
  the size floor once the request passes a fraction of the window — with the
  ADR amended and the thrash risk accepted.
- **MCP deferred schemas** need the per-task tool set to grow mid-task (tool
  schemas are part of the stable prefix and the agent identity), a
  model-facing `ToolSearch` tool and one prompt line; an ADR-level change to
  `mcp-connectors`.
- **G3 on Anthropic**, **G4 vs D9**, **the benchmark headline**: unchanged.
- **Postgres pooling**: not a reliability gap once reconnect lands.
- **Per-model `effort` / `thinking` validation** needs a capability column the
  catalog does not carry.
- **Benchmark rerun**: real tokens and hours on the sibling repo.

### Phase 2 outcome (2026-09-25)

Every WP landed; CHANGELOG `[Unreleased]` lists the host-visible changes.
Shape divergences from the plan above:

- **S1** the image does not create a shell session from an unknown `id`
  (it answers "Session not found"), so each foreground command first calls
  `POST /v1/shell/sessions/create {id}`, then `exec`. `hard_timeout` returns
  the request on time but does not kill the process, so every timeout path
  (`hard_timeout`, `no_change_timeout`, the HTTP read timeout) also posts
  `/v1/shell/kill`. `no_change_timeout` cannot be left unset (the image
  defaults it to 120 s and returns a quiet command as `exit_code -1`); it is
  `timeout_s + 10`. A killed session's `exec` request only returns at
  `hard_timeout`, so the `exec` runs on a helper thread and the stopped
  caller abandons the wait (interrupt ADR, rule 1); the stopped command's
  partial output is therefore not returned. Sessions are not deleted after a
  normal exit (a `server &` must survive, and the delete verb is `DELETE`,
  which the transport does not speak); the image evicts beyond ten.
- **S2** seam: `supports_foreground_kill` + `run_argv(on_start=)`, the
  `supports_background` pattern; `register_foreground(popen=None, kill=None)`.
- **P2** the wrapper takes `query: str` (psycopg's `Query` type failed
  strict mypy on template strings); `apply_migrations` takes the wrapper; no
  `commit()` on the wrapper (only SQLite tests call it).
- **K** the companion mechanism is `ControlTranslateContext.translate_rest`
  (re-run the translate chain without given call ids), so the kernel knows
  only "pre-answered result blocks". A foreground spawn persists the ack as
  `SubtaskSpawnedPayload.preacked_ref` (additive, omitted when `None`) and
  the Engine's two sub-agent result methods prepend it at resume; a
  background spawn merges it into the "started" message. Two files outside
  the WP's list were edited for that: `core/engine.py` (the result methods)
  and `policies/control_semantics.py` (the translate dispatcher lives there).
  The `TodoWrite` description grew by 15 bytes; `composer_view_main` and the
  embedded `_SCHEMA_TODO_WRITE` golden were re-locked for that line only.
- **H1** `HooksConfig` compiles a string `value` for a regex match into
  `MatchArg.pattern` at construction; the observer is built through
  `noeta.client.parts.build_hook_observer` (dynamic `ref`, no static built-in
  import); `PostToolUseRule` / `NotificationRule` moved to
  `noeta.runtime.governance` with re-exports left in the built-in.
- **H2** the per-turn principal is consumed at seed time, so `SeededTurn`
  needs no field; `inject_goal` uses the Client's principal.
- **D5** the compensation now records suspend reason `seed_failed`; a retry
  reuses the goal only when the transcript ends with that exact goal AND the
  newest suspend is `seed_failed` (a plain re-sent goal after an interrupt
  stays a new turn).
- **D7** reads the last 32 envelopes per stream and considers only parents
  whose newest lifecycle event is a suspend on a subtask barrier; a child
  finishing after its parent stopped waiting no longer gets a phantom handoff
  (ADR `worker-queue-routing`, amended).
- **D8** the kernel calls the optional `note_root_turn_settled(root_task_id)`
  on the resident host after a worker-driven turn that parks on next-goal;
  `dispatch_seeded` is covered by the worker path.
- **C1** the report's token fields use the `task.governance` names
  (`input_tokens` = uncached + cache read + cache write; reasoning inside
  output); a start without a finish counts under `unfinished_requests`;
  children are walked through the spawn events (not a store-wide scan); an
  unknown task returns an empty report, like `task_answer`.
- **O2** whitespace-only text blocks are dropped as well as empty ones.

Residuals: a `TodoWrite` whose companion spawn is interrupted while awaiting
approval gets the generic "interrupted" result although the checklist was
saved; sandbox sessions are never deleted; the D5 retry check reads the
whole stream, but only when the transcript already ends with the same goal.

Decisions unchanged from the "Decisions" list above: microcompaction, MCP
deferred schemas, G3 on Anthropic, G4 vs D9, the benchmark headline,
Postgres pooling, per-model effort validation, a benchmark rerun.

## Phase 3 — the decisions, decided (2026-09-25, same day)

Status: IMPLEMENTED 2026-09-25 on the same branch. The maintainer's reply to
the Phase 2 report was "do them all", which resolves the items listed under
"Decisions": build them, amending the ADRs they touch, and lead the benchmark
headline with the first pass. Same rules as Phase 2. See "Phase 3 outcome".

### WP-MC — microcompaction: a count-based second valve

Files: `packages/noeta-runtime/noeta/context/composer.py` (the prune region),
`packages/noeta-runtime/noeta/execution/builder.py` (`CompactionConfig`),
`packages/noeta-sdk/noeta/builtins/providers/impl/catalog.py`
(`derive_compaction_config`), `packages/noeta-sdk/noeta/builtins/react/impl/recall_history.py`,
`packages/noeta-runtime/noeta/context/reminders.py` (only if the
collapsed-context reminder must widen), `docs/adr/context-compaction.md`
(amend), option docs (+zh) if a knob is exposed, tests.

- MC1 Once the request estimate reaches `microcompact_fraction` (0.5) of the
  usable window, tool outputs older than the newest `microcompact_keep_recent`
  (5) tool results are cleared with the existing lean marker, size floor and
  cleared-output provenance; the relief valve at the water mark stays as the
  backstop. `keep_recent=None` disables. Pure and deterministic: live and
  resume clear the same blocks.
- MC2 `RecallHistory` can page back a cleared-but-not-summarized output by
  message index (the bodies are in `ContextPlan.cleared_outputs`), and the
  reminder that advertises it renders when cleared outputs exist even before
  a summary — so the accepted thrash is a recall, not a re-run.

### WP-MD — MCP deferred schemas without a growing tool set

Files: `packages/noeta-runtime/noeta/runtime/mcp.py`,
`packages/noeta-sdk/noeta/builtins/mcp/impl/*`,
`packages/noeta-runtime/noeta/protocols/tool.py` (docstring only),
`packages/noeta-runtime/noeta/context/composer.py`
(`_render_provider_tool_schemas` only), the react translate chain,
`docs/guides/mcp.md` (+zh), `docs/reference/options.md` (+zh),
`docs/adr/mcp-connectors.md` (amend), tests.

- MD1 `McpServerSpec(deferred=True)` / `McpHttpServerSpec(deferred=True)`:
  the server's tools are registered (invocable, guarded, audited under their
  real `mcp__alias__tool` names) but not advertised. Two small tools are
  advertised instead, once, for every deferred server together: `ToolSearch`
  (query → matching deferred tools with name, description and full input
  schema; an empty query lists names with one-line descriptions) and
  `McpCall(tool, arguments)`.
- MD2 A `McpCall` tool_use is rewritten at translate time into a `ToolCall`
  on the real tool name with the same `call_id`, so the Guard, `can_use_tool`,
  audit and the ToolRuntime see the real tool; an unknown or non-deferred
  name is a tool error. A tool may ask not to be advertised through an
  optional attribute the composer honours; the kernel learns nothing about
  MCP. The stable prefix therefore carries two schemas instead of N.

### WP-G3 — summarize without `tool_choice`

Files: `packages/noeta-sdk/noeta/builtins/react/impl/react.py` (the summarize
request region), `docs/adr/context-compaction.md` (amend), tests.

- The summarize round-trip is sent without `tool_choice` so the Anthropic
  message-layer cache entry survives; if the model answers with a tool call,
  the request is retried exactly once with `tool_choice: none`. The retry is
  an ordinary recorded round-trip.

### WP-G4 — the memory index is frozen per task; changes ride a reminder

Files: `packages/noeta-sdk/noeta/builtins/memory/impl/*`, the reminder
contribution it needs, `tests/test_memory_wiring.py` (the D9 test),
`CONTEXT.md` (one sentence), the ADR or archived spec that recorded D9
(amend), tests.

- G4a The index resident is first-write-wins for the life of the task, like
  the environment block: a `memory_write` no longer rewrites the cached
  prefix.
- G4b D9's guarantee is kept in the volatile suffix: a one-line compose-time
  reminder lists the pages created or re-described since the task's index
  snapshot, so the model still learns about them on the next turn. Tier-1
  resident bodies keep their refresh (a rewritten page must stop
  contradicting its resident); only the index freezes.

### Benchmark headline

The landing pages lead with the first pass (24/40) and show best-of-three
beside it; the tables already did.

### Phase 3 outcome (2026-09-25)

- **MC** the second valve lives in `_prune_tail` next to the relief valve and
  reads the same size (`max(prefix + dynamic estimate, last_input_tokens)`);
  it protects the newest N `ToolResultBlock`s counted by block. The knobs are
  on `CompactionConfig` and derived in the catalog (5, 0.5), not on
  `HostConfig` (no natural seam). `View.cleared_boundary` /
  `ReminderView.cleared_boundary` were added so `RecallHistory` can page back
  a cleared-but-unsummarized output and its reminder renders before any
  summary (+192 bytes when both apply). Once open, each new tool result
  clears one older one, which rewrites a few messages near the end of the
  cached prompt per request — accepted in the ADR.
- **MD** `McpCall` is a plain tool with a `route_call` attribute, not a
  control tool; `react/impl/call_routing.py` rewrites it at the two return
  points of `_response_to_decision`; `McpCall.invoke` refuses to run so an
  unrouted call can never bypass the Guard. `ToolSearch` returns at most 5
  matches. A 40-tool server: 25,867 → 1,200 bytes of MCP schema per request.
  The two tools are advertised whenever an enabled spec is deferred, even if
  that server was skipped this turn, so an outage never moves the prefix.
- **G3** the retry also fires on an empty text answer; `stop_reason ==
  "error"` and a non-summary narration do not retry. Anthropic's
  message-layer hit for the summarize request still depends on a cached
  prefix existing within ~20 blocks of the summary boundary and inside its
  TTL, which the main loop's moving breakpoint does not guarantee — the ADR
  says "may be served", not "is".
- **G4** the delta is recorded at turn intake (`memory_index_delta_provider`
  on the `turn_intake` seam, the skills `roster_note` pattern) rather than
  computed at every compose: a compose-time reminder cannot see
  `active_content` or the ContentStore without widening `ReminderView` (a
  kernel change). It also lists removed pages (`-name`), since a frozen index
  keeps listing an archived page. Two blind spots: a page whose index line
  was name-only in the snapshot cannot show a description change, and pages
  the snapshot dropped for budget are not reported as new. Measured: a write
  on turn two invalidated 730 bytes of the turn-three prefix before (growing
  with the conversation), 0 after; the note is 62 bytes.
- The landing pages lead with the first pass.

Residuals: message-layer cache reuse for the summarize request on Anthropic
(breakpoint placement); the per-compose delta variant of G4.
