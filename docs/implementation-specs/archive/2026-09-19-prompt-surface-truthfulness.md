# Prompt surface truthfulness

Status: SHIPPED 2026-09-19 in noeta-runtime + noeta-sdk 0.6.28 (lockstep,
together with `2026-09-19-fault-path-closure.md`). Distilled into `CONTEXT.md`
(reminders, memory), `docs/adr/subtask-fanout-and-durable-wake.md` and
`docs/adr/unified-context-supply.md`. K2 and the K8 residual stay open — see
their entries below.

## Goal

A 2026-09-19 read-only audit of everything the model reads — preset prompts,
tool descriptions, model-facing strings built in Python, the memory mechanism —
found three classes of defect: prompts that push the model onto a path the code
punishes, descriptions that state what the code does not do, and memory recall
that is weak on CJK text. This effort closes them.

## Principles (how each fix is chosen)

1. **Remove the hazard in code before describing it in a prompt.** A caveat the
   model must remember on every call is worse than a behavior that needs none.
2. **Teach at the point of failure.** A rule the model needs only when it trips
   belongs in the error it gets when it trips — what happened, what did *not*
   happen, what to do next — not in standing prompt text. A failure the model
   caused is a tool result it can adapt to, never a terminal `TaskFailed`.
3. **A description states mechanism, not cases.** Say what the tool does and the
   one consequence that follows; no example lists, no operator-facing sentences
   (the model cannot act on "use only in a trusted workspace"), no
   implementation vocabulary (`state patch`), no sentence that is only true on
   some hosts.
4. **One general rule beats N per-tool copies.** Permission pauses are invisible
   to the model (it sees a result or a denial); main rule 6 already covers
   denials, so no tool describes pausing.
5. **Provenance is structural.** The tag that marks a host-authored turn can
   only be produced by the host wrapper; text from anywhere else cannot forge
   it. Injected text that stands in for something else says what it is.
6. **Prompt growth is measured.** Every added sentence is one line, reported
   with its character delta; deletions are preferred.

## Work packages

### K — kernel and control tools (`noeta-runtime`, control-tool plugins)

- K1 A denied spawn is feedback. `handle_spawn_subtasks` over `MAX_FANOUT`, a
  roster miss, a Guard DENY or REQUIRE_APPROVAL inside a batch, and the
  single-spawn deny all answer every `Task` call with a failed tool result and
  continue the turn (the background-spawn path already does this). Zero
  children are created; `SubtaskDenied` is still emitted. Message names the
  count and the cap, or the unknown agent and the roster.
- K2 **Deferred.** `TodoWrite` batched with `Task` calls should work the way it
  already does with runtime tools. The spawn decisions carry `state_patch`
  today, but nobody can build the combined decision (the two translators live
  in separate plugins and `TodoWrite` translates first), and the pre-acked
  `TodoWrite` result would make a foreground spawn emit two consecutive `tool`
  messages, a shape the Anthropic codec renders as separate user turns. The
  clean route is a dispatcher-level patch/pre-ack contribution
  (`execution/control_tool.py`, the mount loop) plus `preacked_results` on the
  two spawn decisions, with a codec check. Until then the rejection says the
  checklist was not saved, and `todo_write.md` names what it can share a
  response with.
- K3 Sole-call rejections (`skill`, `AskUserQuestion`, `Task` mixed with runtime
  tools, `run_workflow`, `RecallHistory`, `structured_output`) say that nothing
  in the response ran and to re-issue the other calls separately. A
  `structured_output` call with an invalid payload that shares a response is
  rejected the same way (the valid-payload case is listed under "Not fixed").
- K4 A failed control ack carries its text once (`output` empty, `error` set), at
  `ack_patch_decision` and the kernel sites that duplicate it.
- K5 The compaction summary message opens with one framing line saying it is a
  summary of the earlier conversation standing in for the messages it replaced,
  and that anything it restates is a record, not a new request. `origin` stays
  unset (the constraint detector reads the previous summary as a user message).
- K6 Tail pruning leaves small tool outputs alone — clearing reclaims bulk, and a
  short output (a user's answer, an ack) is not bulk and may be unrecoverable.
- K7 An unknown tool name in a batch is a failed tool result listing nothing but
  the fact and the instruction to pick from the offered tools, not a `KeyError`.
- K8 Background sub-agent delivery: the 30 s drop was real. Delivery now waits up
  to an hour with a backing-off retry. **Residual**: true durability needs a
  re-delivery sweep at a turn boundary (`recover_background_subagents` runs only
  at `Client` construction).
- K9 Skill activation ack says "next step", not "next turn".

### T — tool plugins (`noeta-sdk` builtins other than memory)

- T1 `Grep` recomputes `shown` after the byte-fence trim.
- T2 `Bash` reports when the capture cap dropped the head of a stream; strict-mode
  refusal names what the model can do (dedicated tools, or tell the user), not a
  CLI flag.
- T3 `Edit` / `Write` in dry-run say plainly that nothing was written and that the
  model cannot change that.
- T4 `Read`'s in-result notes stop using the reserved `<system-reminder>` tag.
- T5 The reserved tag is neutralised in every tool-result body and inside every
  wrapped host turn, at the two codec chokepoints.
- T6 `WebFetch`: titles are single-line before they reach either template; the
  result says when the answer covers only the first N characters; loopback
  hosts are not upgraded to HTTPS; the redirect result states a fact rather than
  issuing a command. `WebSearch`: titles/snippets single-line, results carry the
  same source line, zero hits is a successful empty result.
- T7 `run_skill_script`: the three schema properties get descriptions; a
  non-zero exit or timeout is `success=False`, as in `Bash`.
- T8 Browser: no doubled or container-internal tool names in errors;
  `browser_extract` truncation is marked; an empty snapshot is a failure.
- T9 The environment block marks a clipped `git status`.
- T10 `extract_safety_constraints` skips host-injected (`origin` set) messages.
- T11 MCP: injected prompt/resource text has its own 64 KiB cap; a server-supplied
  tool description is capped and prefixed with its source. The unused
  `MCP_*_ORIGIN_PREFIX` constants were deleted rather than wired.
- T12 The `delegation-nudge` reminder is removed (it repeats main rule 10 and the
  `Task` description on every step of tasks that never delegate).
- T13 `explore` / `plan` / `web` gain `WebSearch` (read-only; the `WebFetch`
  description already points at it).

### M — memory

- M1 The body is a field like any other: a `memory_write` whose text is only a
  frontmatter fence keeps the existing body. A new page with no body is refused.
- M2 `description=""` clears the description; `related` is a write parameter.
- M3 The near-duplicate advisory and the search hit count move from `summary`
  (model never sees it on success) into `output`. A truncated `memory_read` says
  how much is missing and that `memory_search` reaches the rest.
- M4 Tier 1 means the text *named* the page: at least `NAME_MIN_OVERLAP` name
  tokens **and at least half of them**, where the name's size counts every token
  it carries — including ones the length floor or stopword list make
  unmatchable. One rule for every script; no CJK dictionary. A name that fails
  tier 1 still earns the tier-2 pointer.
- M5 The recall key is capped; a recalled body whose page changed since it was
  pinned is eligible again.
- M6 The index has a total budget with ranked degrade (full line → name only →
  a count pointing at `memory_search`), mirroring the skill menu.
- M7 The index preamble says entries are notes from past sessions, not
  instructions. The recall judge sends instructions + index as `system`.

Not fixed: a body that legitimately opens with `---` and a `key: value` line is
still read as frontmatter — every cheap tightening breaks reading fences already
on disk; a leading blank line avoids it. `structured_output` with a valid
payload still drops neighbouring calls (rejecting it would spend the retry
budget). `ReminderView.already_spawned` is now unread by any built-in.

Also not fixed: recall stays silent on a page whose `memory_read` output tail pruning
cleared. The window is short (pruning opens at the same water mark that triggers
compaction, after which the page is recallable again), the model sees the
cleared marker, and no pruning state is folded onto the task for recall to read.

### P — prompt text (owner of every `.md` prompt and every golden)

Written after K/T/M land, so each sentence describes shipped behavior. Net size
must not grow beyond the lines listed here.

- `web.md` gains the untrusted-content line; `main-web.md` rule 14 says the
  browser is one shared resource.
- Deletions: operator-facing sentences in `shell_run.md` / `run_skill_script.md`;
  the "not offered at all" precondition and the `log()` claim in
  `run_workflow.md`; implementation vocabulary in `skill.md`; the pause mechanics
  in `write.md` / `edit.md`; the "Other is provided automatically" claim; the
  "full streams are recorded" claim; the duplicated keyword guidance.
- `memory_write.md` rewritten around M1/M2; `browser_screenshot.md` and
  `browser_navigate.md` corrected.

## Acceptance

- `make check` green, unpiped.
- Each K/T/M item has a test that fails on the old behavior.
- Goldens regenerated once, deliberately; the prompt size delta is reported per
  preset.
- A benchmark rerun is owed before the release headline is quoted (the main
  prompt changes; this branch already owed one).
