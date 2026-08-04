# Claude Code tool-surface alignment

Status: draft — awaiting owner review
Owner: initxy

## Goal

The model-facing tool surface — names, input schemas, result rendering,
truncation behavior, and descriptions — matches Claude Code, so a model trained
on Claude Code's tools hits its strongest behavioral prior instead of fighting
JSON-wrapped results, dead-weight refs, and schema shapes it has never seen.

Once done, the following are true:

1. Every tool result the model sees is **plain text** in Claude Code's format —
   no JSON wrapper, no `content_ref` / `stdout_ref` / hash fields, no
   `ensure_ascii` escape blow-up on CJK content.
2. `ContentRef`s never appear in any model-visible surface (tool output text,
   background notices, tool descriptions). They remain exactly where they work
   today: `ToolResult.artifacts`, `ToolResultRecorded.output_ref`,
   `ContextPlan.cleared_outputs` — the audit/replay layer is untouched.
3. Tool names, parameters, capacity limits, and error strings follow Claude
   Code, except where a documented Noeta host concept (workspace fencing, write
   authorization, allowlist shell tier, dry-run mode) genuinely requires
   otherwise.
4. Every tool description (`*.md`) states what the tool actually does — the
   current lies ("Reads the whole file by default", "dereference the ref",
   "files-with-matches first") are gone.

## Non-goals

- **PDF reading** (owner decision: keep refusing PDFs).
- **WebFetch prompt-extraction**: Claude Code runs a small model over the
  fetched page (`url` + `prompt`). Deferred — it would put an LLM call inside a
  tool, a new seam. This effort aligns the output format only (plain markdown).
  Revisit as its own effort if page-reading quality becomes a problem.
- **System-prompt (preset) alignment** beyond deleting ref/deref language.
  `presets/prompts/*` content is a separate effort.
- Noeta-native tools with no Claude Code counterpart: `memory_*`, `open_app`,
  `browser_*`, skill tools. They keep their names and shapes (their outputs do
  ride the same plain-text rendering fix in S1).
- SSRF hardening of `WebFetch` (no scheme/private-IP guard today). Noted, out
  of scope here.
- The audit layer: three-event envelope, EventLog offload, rewind baselines,
  PermissionGuard risk levels, write fencing. All unchanged.

## Why (evidence from the review, 2026-08-03)

- All three provider adapters `json.dumps` dict outputs into the tool_result
  text (`anthropic.py:714`, `openai_responses.py:1026`, `openai_compat.py:641`)
  with default `ensure_ascii=True` — CJK content expands up to 6× as `\uXXXX`.
- No tool's `input_schema` accepts a ref/hash, and the runtime knows it:
  `_decision_handlers.py:453,525` and `composer.py:858` all say a model-facing
  hash is "dead weight it could only misread". Yet `shell_poll.md`, `read.md`,
  `shell_run.md` and `driver.py:1314` tell the model to "dereference the ref",
  and `_background_exit_notice` hands it a bare hash — while its sibling
  `_background_subagent_notice` inlines the real text precisely because a hash
  is "an opaque hash pointer it cannot read". Background shell output is
  therefore unreachable by the model today.
- Capacity is far below Claude Code: 64 KB write cap, 64 KB read inline cap
  (below read's own 2000-line window for ordinary source files), 2 KB/1 KB
  shell tails vs ~30000 chars, 4 KB grep/glob budgets.

## Key decisions

- **D1 — Plain text, refs off the model surface.** `ToolResult.output` for
  every model-facing tool becomes a rendered string; refs live only in
  `artifacts`. The wire layer keeps `json.dumps` (with `ensure_ascii=False`)
  solely as a fallback for host/MCP tools that still return dicts.
- **D2 — Rename to Claude Code names.** Provider-visible `name` strings only;
  plugin/builtin directory names, capability flags, and event vocabulary keep
  their current identifiers. `read`→`Read`, `write`→`Write`, `edit`→`Edit`,
  `glob`→`Glob`, `grep`→`Grep`, `shell_run`→`Bash`, `shell_poll`→`BashOutput`,
  `shell_kill`→`KillShell`, `todo_write`→`TodoWrite`,
  `ask_user_question`→`AskUserQuestion`, `webfetch`→`WebFetch`,
  `web_search`→`WebSearch`, `spawn_subagent`→`Task`. The `Task` tool name
  coexists with the kernel `Task` primitive — docs always say "the Task tool".
- **D3 — Delete `apply_patch`.** Claude Code has no such tool, and
  `edit.py:99` already argues against a diff applier. Hard removal.
- **D4 — `Edit`/`Write` preconditions move to a session read registry.** The
  current content-store probe (`edit.py:70`) has false positives: the store is
  content-addressed and shared across tasks/sessions, and rewind baselines are
  deliberately stored under the same media type. Replace with a host-wired
  registry on `ToolContext` (same wiring shape as `background_runner`):
  `Read` records `(path → sha256)` per root task; `Edit` and `Write`-overwrite
  require an entry for the path whose hash still matches the file's current
  bytes. Error strings follow Claude Code: "File has not been read yet." /
  "File has been modified since read."
- **D5 — Grep stays pure-Python, gains Claude Code's parameters.** No new
  dependency, no rg binary discovery (recorded tool results make cross-host
  nondeterminism a non-issue for replay, but a missing binary would make the
  tool's behavior host-dependent at *live* time). Adds `output_mode`
  (`content` / `files_with_matches` / `count`), `-i`, `-A`/`-B`/`-C`,
  `head_limit`, `multiline`; always skips `.git` and a fixed junk-dir list
  (`node_modules`, `__pycache__`, `.venv`/`venv`, `dist`, `build`,
  `.mypy_cache`, `.tox`, `.ruff_cache`). Pattern syntax remains Python `re`
  and the description says so. Full `.gitignore` parsing is out of scope.
- **D6 — Control tools take Claude Code schemas.** `TodoWrite` items become
  `{content, status, activeForm}` (drop `id`); `AskUserQuestion` becomes 1–4
  questions `{question, header ≤12 chars, options: 2–4 of {label,
  description}, multiSelect}` with an auto-appended "Other" free-text option
  replacing `allow_freeform`. Host-side answer JSON changes shape (breaking;
  noeta-agent sweep).
- **D7 — `TodoWrite` becomes batchable.** Claude Code models batch TodoWrite
  with other calls; today's "must be the sole call" ack wastes a round trip
  every time. Extend the control-tool ack/patch path so an acked control call
  can coexist with runtime tool calls in one turn (patch applied, remaining
  calls routed to `handle_tool_calls`, one assistant message, every tool_use
  answered). `AskUserQuestion` keeps the solo constraint (it suspends the
  turn; mixing is genuinely invalid). Fallback if the engine change balloons:
  keep the constraint and say "call it alone" in the description — but the
  engine change is the aligned end state.
- **D8 — `Task` fan-out via multiple tool_use blocks.** One `Task` call =
  `{description, prompt, subagent_type}` = one sub-agent; parallelism = several
  `Task` blocks in one assistant turn (the engine already batches a turn's
  calls). The delegation translator collects all `Task` blocks of the turn into
  the existing fan-out machinery. `background: true` stays as a documented
  Noeta extension (ADR `background-subagent`), still restricted to a single
  spawn.
- **D9 — Byte-equivalent resume of pre-change recordings is given up.** The
  rendering change alters outbound request bytes, so replay-verify against old
  recordings will flag drift. Accepted; release-noted. New recordings are
  self-consistent.

## Slices

Ordered; each lands green (`make check`) on its own. Tool descriptions move
with their tool's slice — contract and prose change together.

### S0 — Precondition: clear the working tree

The uncommitted InjectedMessage work (16 files, gates already green) is
committed and pushed before any of this starts. No mixing.

### S1 — Plain-text rendering, refs off the surface

The cross-cutting slice; everything after it is per-tool.

- Every builtin tool's `output` becomes a rendered string (formats fixed in
  the appendix). Structured fields the model needs (truncation notices, line
  ranges, exit codes) are embedded in the text, Claude Code-style.
- `_tool_result_text` (anthropic) and both OpenAI equivalents: string outputs
  pass through verbatim; residual dict outputs (host/MCP tools) use
  `json.dumps(..., ensure_ascii=False)`. Drop the `[error] ` prefix — the
  error text plus the wire-level error flag is the whole contract.
- `read`/`shell`/`webfetch`/`web_search` stop emitting `content_ref` /
  `stdout_ref` / `stderr_ref` / `ref` fields in `output`; artifacts continue to
  carry the refs for audit.
- `_background_exit_notice` inlines the output tail (Bash truncation rules)
  instead of the hash; `shell_poll` inlines new output (full shape in S3).
- Every `.md` loses its ref/deref/artifact language in the same commit.
- Acceptance: a grep over `packages/noeta-sdk/noeta/builtins/**/*.md` and the
  model-facing rendering paths finds no `content_ref`/`deref`/`hash` mention;
  a CJK file read costs ~1× its UTF-8 size in the wire body, not ~6×.

### S2 — Capacity and truncation alignment

- `Read`: drop the canonical-bytes halving; truncation is the line window
  alone (2000 lines default, 2000 chars/line, matching Claude Code), with a
  1 MiB safety ceiling that reports "use offset/limit" instead of silently
  shrinking. `INLINE_CONTENT_MAX_BYTES` users are re-audited per tool.
- `Write`: `WRITE_FILE_MAX_BYTES` 64 KB → removed; keep an 8 MiB runaway guard.
- `Bash`: inline output cap 30000 chars (was 2 KB/1 KB tails); over-cap keeps
  head and tail halves around an elision marker naming the dropped count.
- `Grep`/`Glob`: inline budget 4 KB → 32 KB; `Glob` cap stays 200 paths with a
  Claude Code-style truncation notice.
- Acceptance: reading a 2000-line/80-col file returns all 2000 lines; a 100 KB
  write succeeds; a 25 KB test log survives inline.

### S3 — Tool-logic parity (fs + shell)

- `Edit`: read-first precondition (D4); reject `old == new` ("old_string and
  new_string must be different"); success returns the Claude Code confirmation
  plus a `cat -n` snippet around the change. Error strings follow Claude Code
  ("String to replace not found in file.", "Found N matches …").
- `Write`: parent directories auto-created; read-first via D4 registry;
  success returns "File created successfully at: <path>" / update confirmation.
- `Read`: `cat -n`-style line numbers (right-aligned number + tab); empty-file
  and offset-past-EOF warnings as plain text.
- `Glob`: results sorted by mtime (newest first), alphabetical tiebreak.
- `Grep`: D5 parameters, junk-dir exclusion, output modes.
- `Bash`: stdout then stderr (labeled section only when both present); nonzero
  exit → error result whose text ends "Exit code N"; timeout → "Command timed
  out after Ns" plus partial output.
- `BashOutput`: returns only output produced since the previous `BashOutput`
  call (plain text), a status line, `exit_code` once exited, optional `filter`
  regex applied per line. The runner's polled `(ref, offset)` event keeps
  pinning what the model saw.
- `apply_patch` deleted (D3).
- Acceptance: each behavior has a direct test; the background-job path is
  covered end-to-end (spawn → poll sees increments → exit notice carries the
  tail inline).

### S4 — Control tools (schemas + engine)

- `TodoWrite` schema and rename (D6), batchable turns (D7 — the engine-level
  item, sequenced last inside this slice).
- `AskUserQuestion` schema and rename (D6); answer codec re-shaped; solo
  constraint kept.
- `Task` rename and per-call shape (D8); `spawn_subagent`'s spawns-array form
  is removed (hard break, matching the repo's no-compat convention).
- Acceptance: control-tool schema goldens regenerated once per final shape;
  a turn mixing `TodoWrite` + `Read` patches todos and runs the read with every
  tool_use answered.

### S5 — Rename sweep + docs

- D2 renames everywhere the *model-visible* name appears: tool `name` fields,
  control-tool constants' values, allowlists/whitelists in presets and specs,
  every `.md` description, tests, goldens.
- Docs: `docs/reference/*` (api, sdk-types, glossary) + `docs/zh/**` mirrors +
  CHANGELOG. `CONTEXT.md` gains the "model-visible tool names follow Claude
  Code; plugin/flag identifiers do not" rule.
- `scripts/lint-naming.py` must stay green (tool names are strings, not
  identifiers — no rule conflict expected; verify).

### S6 — Release

- Both packages bump (runtime: limits/driver/decision-handlers/background
  shell; sdk: builtins/adapters/presets). Hard model-surface break → propose
  **0.6.0** lockstep (maintainer call per AGENTS.md).
- Release notes name D9 (old-recording replay drift) and the noeta-agent
  breakage list: tool names, answer JSON shape, spawns-array removal,
  `apply_patch` gone.

## Appendix — output format contracts (fixed here so slices don't drift)

- **Read**:
  `     1\t<line>` per line (`cat -n` style). Empty file → system-reminder-style
  warning text. Offset past EOF → warning naming the file's line count.
- **Edit/Write success**: Claude Code confirmation line; Edit additionally a
  `cat -n` snippet (~5 lines around the change).
- **Glob**: newline-separated workspace-relative paths (Noeta keeps relative —
  cheaper and unambiguous inside a workspace); truncation notice on cap.
- **Grep** `content` mode: `path:line:content` per match, `-A/-B/-C` context
  separated by `--`; `files_with_matches`: paths; `count`: `count:path` lines.
- **Bash**: raw stdout; stderr appended (with a `stderr:` label only when both
  streams are non-empty); truncation per S2; exit-code line only on failure.
- **BashOutput**: `status: running|exited(exit_code=N)` line, then new output.
- **TodoWrite ack**: Claude Code's "Todos have been modified successfully"
  ack text.
- **WebFetch**: `Title: <title>` + `URL: <final url>` header lines, blank line,
  markdown body; truncation notice names the total size.
- **WebSearch**: the numbered markdown hit list, no envelope.
- **Task result**: the sub-agent's final text, verbatim (already the case).

## Risks

- **D7 (batchable TodoWrite)** is the only engine-semantics change; it touches
  the single-Decision-per-turn seam. Isolated at the end of S4 with a named
  fallback.
- **Goldens**: schema goldens are byte-order contracts; regenerate once per
  slice that legitimately changes them, never hand-patch.
- **noeta-agent**: downstream sweep grows (names, answer shape, spawns array,
  apply_patch). Tracked in the existing sweep backlog, executed after S6.
- **`.ignore` gotcha**: `rg` silently skips `tests/`; verification greps use
  `grep -rn` or `rg --no-ignore`.

## Verification

`make check` green per slice (pytest coverage ≥ 85, mypy --strict on
protocols, lint-naming, lint-imports). Final review: the S1 acceptance grep,
one manual interactive session against a real workspace (read → edit → bash →
background job → notices), and a golden-diff read-through before regeneration.
