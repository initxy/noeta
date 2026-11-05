---
status: active
created: 2026-11-05
branch: v1-history
---

# The full history out of the fold, and an approval's note beside its call's result

## Goal

1. A task's snapshot stops growing with the task's life: `TaskState.history` (every message
   ever added, never compacted) leaves the fold. The event log is the only full transcript;
   the fold keeps the conversation the model sees (bounded by compaction) and each plugin's
   own small state. No compatibility: the snapshot version changes, old snapshots are
   refolded from the log.
2. An approval answered Yes with words (the TUI's Tab-to-amend, and the web's equivalent if
   it has one) puts those words beside that call's result, as Claude Code does, instead of
   posting them as a separate person message.

## Non-goals

- Changing what the model sees in a compacted conversation.
- A migration for old snapshots or old logs.

## Context

`TaskState.history` / `history_seqs` (`noeta/core/loop/state.py`) are read by the memory plugin
(memory pages already read), todos (turns since a TodoWrite / reminder), compaction (person
message count, recent files, the saved transcript file), permissions modes (has the model
replied), checkpoints (message to log seq for rewind), tool_search (tools loaded), and
`continued()` for `history_from`. Each snapshot rewrites them whole.

The Yes-with-words path today (`tui/`, 2026-11-04 alignment spec, archived) sends the words as
a `Say(when="now")` person message: it counts as a person turn for todos, compaction and
`silent_reply_after`, and may be taken in a step after other calls' results.

## Decisions

- D1 Each reader keeps what it needs in its own state, folded from events (`on_event` /
  records), or reads the log on demand where it is rare (the saved transcript at compaction,
  rewind). No reader scans an unbounded in-memory list.
- D2 A continued task (`history_from`) carries the source's compacted conversation and its
  plugins' states (`continue_state`); anything older is read from the source's log on demand.
- D3 An approval answer may carry a note; the runner appends it as a text block to that
  call's result (wording in our own words), exactly as a decline's reason goes into its
  refusal. No person message is posted.

## Plan

- [ ] H History out of the fold (D1, D2), snapshot version bumped, tests per reader.
- [x] N Approval note beside the result (D3), TUI switched to it, web if it offers words.
- [ ] Integrate, full gates, review.

## Acceptance criteria

- A test shows a snapshot's size stays bounded across compactions of a long task.
- Each converted reader has a test that its behavior is unchanged.
- An approved call with a note: the note is in that call's result; no person message.
- `make check`, `make frontends`, `make parity` (0), `make cluster`, `make eval` green.

## Risks

- H touches the fold and snapshots; `make cluster` and the store contract tests guard it.

## Progress log
- 2026-11-05 Created; owner: "两个都做吧".
- 2026-11-05 N done: an approval's `Answer.data` `{"message": ...}` on a yes (the decline's
  field) becomes that call's `ToolFinished.note`, so the words follow the batch's results in
  their user message as a plain text block (Claude Code 2.1.318 pushes `acceptFeedback` as
  `{type: "text"}` right after the `tool_result`, verbatim, with no framing; so do we, which
  leaves no wording for the own-words check). Set in `_finish` and on a recovered "outcome
  unknown" result, read from the logged answer, so a crash before or after the call starts
  keeps it; a sub-agent's card answered at the root posts to the child as before.
  `answer_choice` carries the message on every yes kind; the TUI no longer sends a
  `Say(when="now")`. Web unchanged: its card has words on Deny only. `tests/core/test_approval_note.py`.
