# Claude Code mechanism alignment (post-audit fixes)

Status: spec drafted 2026-08-04 from the full-mechanism audit; S1–S6 implemented.
D12's open question is closed — **the owner approved the WIRE option on
2026-08-04**, so S6 consumes the two live host surfaces and documents the other
two as host-resolved listings; the "shrink" alternative is not taken.
Owner: initxy

## Goal

The 2026-08-03 effort aligned the tool *surface* (names, schemas, result
rendering). This effort aligns the *mechanisms* behind it — compaction, the
model catalog and adapters, memory freshness and recall precision, skill
frontmatter semantics, and the plugin-surface closure — with how Claude Code
and current SOTA agents actually behave, and fixes the defects the 2026-08-04
audit confirmed. Once done:

1. **Compaction survives real traffic.** The summarize call succeeds against a
   history that contains `tool_use`/`tool_result` blocks, its input stays
   bounded across arbitrarily many compactions, and a context overflow that
   arrives as an HTTP-200 `stop_reason` triggers compaction instead of killing
   the task.
2. **Unknown models fail loud.** A model missing from the catalog produces a
   logged warning and conservative defaults — never silent `$0` pricing, a
   silent vision refusal, or silently disabled compaction.
3. **The model never reads stale guidance.** No model-visible text names a
   deleted tool (`spawn_subagent`, the `spawns` array) or an old snake_case
   name; the preset prompts never instruct a command the shell allowlist will
   suspend on.
4. **Memory stays fresh and recall stays bounded.** The memory index resident
   refreshes on every drive (as `CONTEXT.md` already claims); auto-recall stops
   firing on stopwords and stops injecting unbounded bodies.
5. **Claude Code skill frontmatter is honored where it gates behavior.**
   `disable-model-invocation` removes a skill from the model's menu;
   `allowed-tools` understands the current tool vocabulary; the menu has a
   size budget; a malformed `priority` degrades instead of deleting the skill.
6. **No declared-but-dead extension surface.** Every registered plugin surface
   is either consumed by code or explicitly documented as host-resolved
   listing-only; the third-party `plugin_config` channel works or is removed
   from the how-to.

## Non-goals

- **Parallel tool execution.** The wire shape (all results in one `role="tool"`
  message) is already correct; concurrent dispatch touches engine ordering,
  cancellation, and the event log, and is its own effort. Deferred.
- **A manual compaction verb** (`/compact` equivalent on `Client`). Deferred —
  needs a driver→policy signalling design that this effort should not rush.
- **Streaming as a public default.** `HostConfig.delta_sink` stays the only
  external streaming surface; S2 changes the Anthropic adapter's *transport*
  only.
- **Native mid-conversation `role:"system"` injection** (GA on Opus 5 /
  Fable 5). Worth doing once the catalog carries a capability bit; deferred so
  this effort does not couple adapter behavior to catalog schema changes.
- **Plan permission mode, Grep `type`, WebSearch domain filters, WebFetch
  `prompt`** — previous non-goals stand.
- **Embedding / semantic recall.** The substring matcher stays the documented
  adapter-swap point; S4 only sharpens the existing lexical matcher.
- **Consolidation scheduling.** Triggering `run_consolidation` remains host
  wiring by design (ADR memory-consolidation); no scheduler is added.
- The multi-breakpoint rolling cache scheme (more than one moving
  `cache_control` marker). Noted as follow-up; the single-tail marker is
  correct, just suboptimal for >20-block turns.

## Why (evidence, verified 2026-08-04)

Compaction (all in `builtins/react/impl/react.py` unless noted):

- The summarize request sends `tools=[]` while `messages` still carries
  `ToolUseBlock`/`ToolResultBlock` (`react.py:~420`; the Anthropic adapter
  omits `tools` when falsy). Anthropic rejects tool blocks without tool
  definitions → 400 → `compaction_summary_failed` → `FailDecision(retryable=False)`.
  **The first real proactive compaction on Anthropic kills the task**, and
  `tools=[]` also forfeits the cached prompt prefix.
- Each compaction re-summarizes `raw_history[:boundary]` from index 0;
  `runtime.messages` is never trimmed, while the trigger compares the
  *composed* (post-summary) size. The Nth summarize input grows ~N × available
  window; by the second or third compaction the summarize call itself
  overflows → same non-retryable death. The multistep regression test uses a
  fake LLM, so size never bites.
- `_STOP_REASON_MAP` (`builtins/providers/impl/anthropic.py:66-80`) has no
  `model_context_window_exceeded`; an overflow arriving as a 200 stop_reason
  becomes `"error"` → `llm_error`, never reaching the compaction path (which
  today only triggers on a 400 sniffed as `category="overflow"`).

Catalog / adapters (`builtins/providers/impl/catalog.py`, `anthropic.py`):

- Aliases resolve one generation back (`opus→claude-opus-4-8`,
  `sonnet→claude-sonnet-4-6`); no `claude-opus-5` / `claude-sonnet-5` rows.
  An uncatalogued model silently gets `price()→$0`, `supports_vision=False`
  (hard image refusal), and `COMPACTION_OFF` — compaction *and* tail-prune
  disabled with no signal. Two gpt-5.x rows are `# placeholder` at $0.00.
- ReAct forwards the catalog's `max_output_tokens` (128 000 on the 4.x rows)
  as `max_tokens` every turn, against a non-streaming transport with a 60 s
  client timeout — long answers time out and burn the 8-attempt retry budget.
- `thinking="disabled"` combined with `effort ∈ {xhigh, max}` is a documented
  hard 400 on Opus 5; both fields are settable via `Options` with no
  compile-time validation. `_apply_cache_control`'s docstring inverts the
  render order (tools render before system).

Model-visible staleness / prompt-allowlist mismatch:

- `builtins/react/impl/run_workflow.md:33,36` still instructs
  "`use spawn_subagent` instead" and "batch the goals into one
  `spawn_subagent` call's `spawns` array" — the tool is `Task` and the array
  was deleted (D8 of the tool-alignment spec). `ask_user_question` ack/error
  strings still use the snake_case name (`impl/__init__.py:180,184,433`).
- `explore.md:7` / `plan.md:7` tell the model to use `cat`, `head`, `tail`
  (and both prompts name `git log`) — none are in `DEFAULT_SHELL_RULES`, so
  under `default`/`acceptEdits` every such call suspends for approval.
- `main.md` contains zero todo guidance; the `unfinished-todos` reminder only
  renders once a list exists, so nothing ever prompts the model to *start*
  one. Claude Code's system prompt carries explicit TodoWrite pressure.

Memory (`builtins/memory/impl/`, `execution/`):

- `run_content_init` has exactly two call sites — `driver.seed_start` and the
  subtask drain (verified by grep). `send_goal` never runs it, and the pack's
  `_init`/renderer close over an `entries()` snapshot taken at Engine build,
  so the index resident never refreshes within a task and can be stale across
  tasks through the warm Engine cache (LRU 256, no memory dimension in the
  key). `recorder.py:70-72` and `CONTEXT.md` §Content Channel both claim
  "init reruns every drive" — currently false.
- Recall tier-1 fires on any single shared name token, with no stopword list
  and no minimum-length filter beyond 2 chars, and injects the **entire file
  body verbatim with no byte cap** (contrast `memory_read`'s 1 MiB cap), up
  to 5 bodies per turn. `__consolidation__` activates `memory`, so recall
  runs against the whole 160 KB digest and near-certainly maxes out.
- Every turn intake reads the full store from disk twice for tier-1 hits;
  `MemoryStore.write` is truncate-then-write (torn reads possible, not just
  the lost-update the ADR accepts).

Skills (`builtins/skills/impl/`):

- Only `name`/`description`/`version`/`priority` are semantic; Claude Code's
  `disable-model-invocation`, `user-invocable`, `model`, `context`, `agent`
  land in opaque metadata — a skill declaring `disable-model-invocation: true`
  still enters the model's menu, a semantic violation, not just a missing
  feature.
- `CLAUDE_TO_NOETA_TOOL` is a six-entry identity map; any other name
  (`WebFetch`, `TodoWrite`) or a `Bash(git:*)` specifier degrades the whole
  declaration to an **empty grant** — fail-safe direction, silently hostile
  to any real Claude Code skill.
- The menu has no budget: every skill's full description concatenates into
  one property description inside the stable-prefix hash. A non-integer
  `priority:` silently deletes the skill from the index — harsher than any
  other field's failure mode.
- `$ARGUMENTS` is never substituted anywhere, while every built-in skill
  fixture uses it. `skill_tool_enforcement`'s comment says `off/warn/enforce`;
  the real values are `off/approval/deny`.

Plugins (`client/plugins.py`, `plugin_set.py`, `surfaces.py`):

- Four of sixteen surfaces (`mcp_server`, `skills`, `sandbox_provider`,
  `provider`) have zero consumers; `PluginSet.resolve()` has zero production
  callers. `docs/reference/plugin-surfaces.md` claims a `skills` path
  contribution is "merged into the skill catalogue" — no code does this.
- `SdkHost._plugin_config` hardcodes four keys (fs/skills/workspace/memory);
  a third-party pack's `ctx.config("my-plugin")` is always `{}`, while
  `docs/how-to/write-a-plugin.md` documents it as the config route.
- `requires-noeta` is parsed, stored, and never evaluated; README's sample
  range disagrees with what every built-in declares. A third-party `priority`
  that is not an int coerces silently to 0 where the built-in path raises.
- Net capability gap vs Claude Code plugins: a Noeta plugin can ship
  tools/agents/policy/packs/control tools/prompt fragments but **not** skills,
  MCP servers, hooks, or slash commands — the exact inverse of what a Claude
  Code plugin carries.

## Key decisions

- **D1 — Summarize with the live tool schemas.** The summarize `LLMRequest`
  carries the same `tools` (and system prefix position) as a normal turn, so
  the request is valid against tool-bearing history *and* reuses the cached
  prefix. Rejected: stripping tool blocks from the summarized history —
  mutates what the model actually said and loses grounding for the note.
- **D2 — Bounded summarize input: previous summary + delta.** When
  `summary_ref` exists, the summarize input is the previous summary message
  plus `raw_history[prev_boundary:new_boundary]` — i.e. summary-of-summary,
  which is what Claude Code does. The "always re-summarize from zero" design
  is dropped; its unbounded input is the bug. The verbatim-constraint
  enforcement (`enforce_verbatim_constraints`) now also scans the previous
  summary so preserved constraints survive re-summarization.
- **D3 — Map `model_context_window_exceeded` to overflow.** The Anthropic
  adapter translates that stop_reason into the same `category="overflow"`
  shape the 400 sniffer produces, so the policy's passive-compaction path
  handles both. `stop_details` is captured into `raw` while we are there.
- **D4 — Unknown model: warn + conservative defaults, never silent off.**
  `derive_compaction_config` on a miss logs one warning and uses a
  conservative default window (128 000 / 16 384) instead of `COMPACTION_OFF`;
  `_catalog_pricing` warns once per model instead of silently charging $0;
  the vision guard lets images through on an *unknown* model (the provider is
  the authority; catalogued non-vision models still refuse cleanly).
  Placeholder rows lose their fake $0.00 (absent price = the same warn path).
- **D5 — Catalog refresh.** Add `claude-opus-5` / `claude-sonnet-5` rows and
  repoint `opus` / `sonnet` aliases; numbers (context window, max output,
  price, cache tiers) are taken from the current Anthropic docs at
  implementation time, not guessed in this spec. Keep the 4.x rows (still
  addressable by full id).
- **D6 — Anthropic transport goes streaming internally.** `complete()`
  delegates to the SSE path and accumulates — same parsed response, same
  events, no API change — so a 128 K `max_tokens` request cannot hit the 60 s
  non-streaming wall. `delta_sink` remains the only external streaming
  surface. Also: validate `thinking="disabled"` × `effort ∈ {xhigh,max}` at
  `compile_options` (fail loud before the wire), and fix the inverted
  `_apply_cache_control` docstring.
- **D7 — Optional cheap summarizer.** `Options.compaction_model: str | None`
  (default `None` = same model). When set, the summarize request's `model` is
  that alias. One knob, no behavior change unless opted in. (Claude Code uses
  a small model for compaction.)
- **D8 — Prompts follow Claude Code's read-tool guidance instead of widening
  the allowlist.** `explore.md` / `plan.md` stop naming `cat`/`head`/`tail`
  and instead say "read files with `Read`, search with `Grep`/`Glob`; use
  `Bash` only for what those cannot do". `git log` (bounded flags) joins
  `DEFAULT_SHELL_RULES` since both prompts legitimately need it. `main.md`
  gains one rule: use `TodoWrite` to plan multi-step work and keep it updated.
  Rejected: an empty-todo-list reminder every turn — noise on trivial asks.
- **D9 — Memory init re-reads at drive time.** `build_memory_pack`'s `_init`
  hook re-scans the store when invoked (no build-time snapshot closure), and
  `run_content_init` is called on the `send_goal` / `IntakeGoalPrelude` path
  as well as seed. The recorder's no-op-on-unchanged-hash rule makes this
  idempotent, and it fixes both within-task and warm-engine-cache staleness
  without touching cache keys. The renderer stays pure (recorded bytes only).
- **D10 — Recall precision and budget.** Matching gains a small English
  stopword set and a minimum token length of 3; tier-1 (name) keeps
  threshold 1 but only counts non-stop tokens of length ≥ 3; tier-2 stays
  at 2. Injection is budgeted: per-body inline cap 4 096 bytes (an over-cap
  hit degrades to its index line + "call `memory_read`"), total recall budget
  16 384 bytes. The recall provider is not wired for `__consolidation__`
  (the digest is not a user message). Defaults are module constants, not new
  Options surface. `MemoryStore.write` becomes write-temp-then-`os.replace`.
- **D11 — Skill frontmatter semantics.** `disable-model-invocation: true`
  excludes the skill from the model menu (host preload still works — same
  split Claude Code has). `allowed-tools` parsing: the recognition set
  becomes "every model-visible tool name the session can mount" (fs/web +
  memory/browser/control names), and a `Name(spec)` form parses to `Name`
  with a logged warning that argument-level specs are not enforced (rejected:
  degrading the whole declaration to an empty grant — punishes valid Claude
  Code skills; rejected: enforcing arg-level specs — that is the shell
  allowlist's job). Menu budget: per-skill description truncated at 1 024
  chars in the roster; `priority` parse failure degrades to the default 100
  with a warning instead of deleting the skill. Fix the
  `off/warn/enforce` comment. `$ARGUMENTS`: strip the placeholder line when
  no argument channel exists (fixtures updated) — argument passing itself
  stays out of scope.
- **D12 — Plugin closure (owner call required).** Recommendation:
  (a) consume the `skills` surface — path contributions merge as an extra
  built-in-tier skills dir into the skills pack's config; (b) consume
  `mcp_server` — contributions merge into the effective `Options.mcp_servers`
  with the existing alias collision rule; (c) open the config channel —
  `HostConfig.plugin_config: Mapping[str, Mapping]` merged under each
  plugin's name (host keys win over the built-in four); (d) `requires-noeta`
  becomes enforced-as-warning at load (`strict=True` refuses); (e)
  `provider` / `sandbox_provider` stay declarable, and the reference docs are
  corrected to say "host-resolved listing, not auto-wired". Alternative — if
  the owner prefers shrinking: delete the four dead surfaces from
  `standard_registry()` and the docs. Either way, the current
  "declared but silently ignored" state ends. Also: third-party `priority`
  coercion becomes a loud `PluginError` (same strictness as built-ins), and
  a single-file plugin whose name cannot be statically read is *skipped*
  under an `enabled` allow-list instead of executed.

## Slices

Each slice lands green (`make check`) and is independently releasable.

- **S1 — compaction survival** (D1, D2, D3). Files: `react/impl/react.py`,
  `providers/impl/anthropic.py`, `core/_decision_handlers.py` (spiral guard
  interaction), tests. Includes a regression test that drives ≥3 consecutive
  compactions over a tool_use-bearing history with a size-asserting fake:
  summarize `request.tools == live tools`; summarize input estimate ≤
  available window; task alive at the end. Plus a stop-reason translation
  test for `model_context_window_exceeded` → policy reaches
  `CompactionRequestedDecision`.
- **S2 — catalog + adapter hardening** (D4, D5, D6, D7). Files:
  `providers/impl/catalog.py`, `anthropic.py`, `client/options.py`,
  `execution/builder.py`. Goldens that pin the stable prefix must not move
  except where a schema deliberately changed (none expected here).
- **S3 — model-visible text sweep + prompt/allowlist alignment** (D8 + the
  staleness list): `run_workflow.md`, `ask_user_question` strings,
  `summarize.md` ("read tool" → `Read`), `explore.md` / `plan.md` /
  `main.md`, `shell_rules.py` (`git log`), stale docstrings
  (`shell_policy.py:61`, `host.py:250-252`, `context/reminders.py`), zh
  how-to mirror, delete the orphan `patch.*.pyc`. Prompt goldens re-pinned.
- **S4 — memory freshness + recall budget** (D9, D10): `memory/impl/`
  (`__init__.py`, `index.py`, `recall.py`, `store.py`), `execution/driver.py`
  (init on goal path), doc drift (`recorder.py:70-72`, `CONTEXT.md` §Content
  Channel note stays true once D9 lands). Tests: index refresh visible on a
  follow-up turn; stopword non-match; body-cap degradation; consolidation
  seed carries no recall reminder.
- **S5 — skill semantics** (D11): `skills/impl/` (`indexer.py`,
  `control_tool.py`, `allowed_tools.py`, `wiring.py`), fixtures, `host.py`
  comment. Tests: `disable-model-invocation` skill absent from menu but
  preloadable; `allowed-tools: [Read, WebFetch]` grants both; `Bash(git:*)`
  grants `Bash` with a warning; oversized description truncated in roster;
  `priority: high` keeps the skill at default priority.
- **S6 — plugin closure** (D12) — **owner approved WIRE, 2026-08-04**. All
  sixteen surfaces stay registered (the count pin is unchanged). Landed:
  (a) `PluginSet.host_skills_dirs()` → `SdkHost.plugin_skills_dirs` → the
  lowest tier of `plugin_config["skills"]["builtin_skills_dirs"]`, with an
  **absolute-path-only** rule (a relative path raises `PluginError` naming the
  plugin — a manifest read from a wheel / `.toml` / `.py` has no single root to
  resolve against) and a missing directory degrading to an empty tier;
  (b) `PluginSet.host_mcp_servers()` → merged into the effective
  `Options.mcp_servers` at the Client build, value shape (`SdkMcpServer`)
  enforced in the projection so the error names the plugin, alias collisions
  loud against both other plugins and the recipe; (c) `HostConfig.plugin_config`
  → `SdkHost.plugin_config_overrides`, verbatim for an unknown plugin name and a
  shallow per-key overlay for the built-in four, applied after the
  reduced/orchestration split; (d) `load_plugins(strict=False)` with a
  hand-rolled specifier evaluator (no `packaging` dependency) —
  `PluginVersionWarning` by default, `PluginError` under `strict`, unrecognized
  specifiers warned-not-enforced, absent distribution metadata tolerated;
  (e) the reference docs corrected, plus the two strictness fixes (non-int
  `priority` → `PluginError`; an unreadable single-file name under an `enabled`
  allow-list is skipped with `UnnamedPluginFileWarning`, not executed).
  `plugin-surfaces.md` / `plugin-manifest.md` / `write-a-plugin.md` /
  `sdk-options.md` and all four zh mirrors updated in the same slice.

Suggested order: S1 → S3 (cheap, high model-facing value) → S2 → S4 → S5 → S6.

## Deferred (recorded, not this effort)

Parallel tool dispatch; manual compaction verb; native mid-history
`role:"system"` channel; multi-breakpoint rolling cache markers; image token
estimation (`estimate_messages_tokens` sees ~25 tokens per image); the
`task_seed` reminder seam (declared, never fired — either fire it at seed or
remove it, fold into S6's owner call if convenient); `noeta/runtime/compaction.py`
rename (production-dead `CompactionWorker` shadowing the compaction name);
structured-output native-path retry nudge; unifying the three independent
`ctx − max_out − buffer` derivations; `run_skill_script` mounted with zero
scripts; deactivation verb for skills; Windows device-name slugs.

## Acceptance criteria

1. `make check` green on every slice (pytest + coverage ≥ 85, mypy --strict on
   protocols, lint-naming, lint-imports); stable-prefix goldens unchanged
   except where a slice deliberately edits a schema/prompt, and then the diff
   is confined to that surface.
2. S1: the three compaction failure modes above are each reproduced by a test
   that fails on `main` today and passes after the slice.
3. S2: an uncatalogued model logs warnings and still compacts, prices as
   "unknown" (warned), and does not refuse images; `opus`/`sonnet` aliases
   resolve to the 5-family; `thinking="disabled", effort="max"` fails at
   compile with a clear message.
4. S3: `grep -rn "spawn_subagent\|spawns array" packages/noeta-sdk/noeta/builtins/react/impl/run_workflow.md`
   is empty; no model-visible string contains `ask_user_question`; running the
   explore preset's prompt guidance under `default` mode triggers zero
   shell-approval suspensions for the commands the prompt itself recommends.
5. S4: writing a memory mid-conversation makes it appear in the index resident
   on the next turn of the *same* task; a recall on a message containing only
   stopword overlaps injects nothing; no single recalled body exceeds 4 096
   bytes inline.
6. S5: the five checks listed in the slice, as tests.
7. S6: zero surfaces in `standard_registry()` without either a code consumer
   or a docs sentence saying "host-resolved listing"; `write-a-plugin.md`'s
   config instructions work as written against the SDK path.
