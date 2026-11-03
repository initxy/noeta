---
status: active
created: 2026-11-02
branch: ctx-final
---

# Context final closure

## Goal

Every finding of the third independent context audit against Claude Code (CC) 2.1.312 (F1-F12
and the two rule-row slips) is closed in one pass: matched to CC in noeta's own words, or kept
as a deviation written as what CC does / what noeta does / why it is better, with a row in
`parity/deviations.md`. The rule rows the audit found wrong or missing are corrected.

## Non-goals

- The owner questions carried by `docs/specs/2026-10-29-open-source-follow-ups.md` (one lean
  prompt for every model, `outputStyle`, task tools): unchanged.
- The deferred tools no recorded session loads get no rule file beyond `EnterPlanMode` (the
  index says so instead).
- Pushing, releasing, merging into main.

## Context

- The audit's notes (outside the repo) give the evidence per finding: bundle offsets into the
  2.1.312 strings, recording ids. Starting point: local main @01fb1644, `make parity` 0
  mismatches.
- No-copy rule (`parity/rules/README.md`); protocol literals kept exact.

## Decisions

- **F1 fork frame: match.** The fork's prompt goes in a `<fork-boilerplate>` block in our
  words, then `Your directive: <prompt>` (`subtasks/texts.py` `FORK_DIRECTIVE`; the tag and the
  lead are in `parity/own-words-allowed.txt`). In the fork's copy of the conversation the call
  that started it is answered as having started a copy that is the fork itself
  (`core/loop/state.py` `STARTED_CALL`); the kernel finds that call from the source's
  `spawned` record, so it stays generic (no fork knowledge in core) and works on any worker.
- **F2 withheld instruction files: match the bundle, not the audit's wording.** The bundle's
  model rendering of `withheld_memory` is empty (`withheld_memory:()=>[]`, strings offset
  ~29738571): CC tells the person (log and a UI warning), never the model. noeta withholds the
  file and notifies the person once per file (`instructions` `_withhold`, asking the permissions
  plugin's `read_deny_rule` by duck type, as `working_directories` is asked). Only deny rules
  count, as CC's `denied` reason; CC's `outside` / `unsettled` reasons have no noeta source.
  A check that fails withholds (CC's `unjudged`).
- **F3 `<tool_use_error>`: match CC's split.** CC wraps what refuses a call before the tool acts
  (unknown tool, not-loaded tool, schema misfit as `InputValidationError`, the tool's
  `validateInput`), and leaves failures while working and permission denials plain (bundle
  tool-call runner, ~28.71 MB). noeta: the kernel checks every call's input against the
  schema before any gate (`core/tool_input.py`; MCP tools, host functions and the answer tool
  set `unchecked_input`, as CC passes MCP input on), wraps unknown and withheld tools, and the
  tools' own input checks return `toolkit.tools.input_error`. CC's coercions (numbers and
  booleans as strings, JSON text for arrays and objects) pass. **Deviation:** a `null` for an
  optional argument passes as if left out (CC refuses it): noeta serves models that send one.
- **F4 seeded unchanged read: match**, plain text (a tool result's `<system-reminder>` tags are
  neutralized on every wire, by design). The instructions plugin records `mtime` and `whole`
  per shown file; `Read` reads that state by name.
- **F5 restore filter: match by exact path**: the files the instructions resident re-sends
  (start and nested, as noeta re-sends nested files too), the memory index, the plan file.
  The basename rule is gone; a nested instruction file never shown is restored like any file.
- **F6:** `EnterPlanMode`'s description completed in our words (both examples per case, what
  happens in plan mode, plan-first / just-do-it examples, closing notes); rule file
  `tools/EnterPlanMode.md` from the bundle; the index now says which deferred names have no
  file. A `tools/common.md` holds the refused-call rows (`tool.common.*`).
- **F7 MCP status: match.** The `mcp` plugin tells down and sign-in-pending servers in a
  `role: "system"` note right after the deferred-tool list (`STATUS_ORDER`), whenever the set
  recorded in the task's log differs from the one last told, and again after a compaction. The
  wording follows CC's retry variant, as noeta retries down servers at every turn start and
  after a fruitless `ToolSearch`.
- **F8 settings file in `git status`: deviation.** CC adds the file to the user's global
  excludes; noeta adds it to the repository's `.git/info/exclude` (no global change on a
  server). The replay keeps its consent file out of `git status` the same way.
- **F9 Haiku 4.5 thinking: match.** CC's main request builder sends
  `{type: "enabled", budget_tokens: max(1024, min(max_tokens - 1, ceiling - 1))}` to a model
  without adaptive thinking whenever thinking is on (the CLI default); noeta does the same for
  a catalogued Claude model that is not `is_reasoning`, and clamps the budget on the
  non-streaming retry as CC does.
- **F10:** the repeated-read note says the call was wasted (2.1.312's meaning, our words).
- **F11:** SessionStart contexts joined into one message; plain stdout stays one success line
  per hook, before it.
- **F12 unknown slash command: match** in the `skills` plugin (which already reads typed
  commands): a bare `/name` no skill or plugin command answers to gets CC's note (nothing ran,
  nearest model-invocable command within two edits, the commands there are, a plain request).
  The terminal UI refuses unknown commands itself, so only front ends with no person at them
  pass one on.
- **Rows:** `rem.quiet-nudge.4` names Mythos 5.1; the Fable / Mythos `bash_output_audience_note`
  joins the lean-prompt deviation row.

## Open questions (owner)

1. F2: the audit read CC as telling the model about a withheld instruction file; the bundle's
   model rendering of `withheld_memory` is empty, so noeta tells only the person (matched to
   the bundle). Confirm.
2. F8: `.git/info/exclude` instead of the user's global excludes file (deviation row). Confirm.
3. F3: a `null` for an optional argument passes the schema check (deviation row; CC refuses).
   Confirm.
4. F9: Haiku 4.5 now thinks with a 31,999-token budget by default, as CC; more output tokens
   per call on that model. Confirm.

## Plan

- [x] F1 fork frame and starting-call answer; tests.
- [x] F2 withheld instruction files; test.
- [x] F3 schema check, `<tool_use_error>` frames, tool input checks; tests.
- [x] F4 seeded unchanged read; F10 wording; tests.
- [x] F5 restore filter; test.
- [x] F6 EnterPlanMode description, rule files, index.
- [x] F7 MCP status note; tests.
- [x] F8 git exclude, replay fix; test.
- [x] F9 budget thinking; tests.
- [x] F11 SessionStart join; test.
- [x] F12 unknown command note; test.
- [x] Rule rows and deviation rows.
- [ ] Gates: `make check`, `make parity`, `make cluster`, `make e2e`, paired eval.

## Acceptance criteria

- Each finding has a code change with a test, or a deviation row; every rule row the audit
  named is corrected.
- `make check` green; `make parity` 0 mismatches; `make cluster` and `make e2e` pass.
- Paired eval (DeepSeek V4.1 Flash, 9 trials, main @01fb1644 vs branch): no scenario regresses
  beyond noise without an explanation.

## Risks

- The schema check refuses calls that tools used to accept (unexpected keys, wrong types):
  measured by the paired eval.
- The fork frame changes what forks see: covered by the eval's delegation scenarios.

## Progress log

- 2026-11-02: baseline `make check` green on the branch; F1-F12 implemented with tests; rule
  and deviation rows written.
