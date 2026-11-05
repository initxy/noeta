---
status: done
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

- [x] H History out of the fold (D1, D2), snapshot version bumped, tests per reader.
- [x] N Approval note beside the result (D3), TUI switched to it, web if it offers words.
- [x] Integrate, full gates, review.

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
- 2026-11-05 H done: `history` / `history_seqs` out of `TaskState`, snapshot `FORMAT` 2 (messages stored plainly). Readers: tool_search and memory record what a result loaded / read (`after_tool`, in the result's append); todos keys its reminder by log seq; checkpoints and compaction's turn use the kernel's `prompt_seq` (the person's newest message; one int, since a plugin cannot fold `InputTaken`); restore candidates from the compacted conversation; the saved transcript read from the log on demand (`compaction/transcript.py`); sub-agent "has replied" from messages or a compaction. 120-call test: snapshot after 14 compactions 255 KB before, 5.9 KB now (5.2 KB after the first). Left: `call_ids` still grows a few bytes per call; ADR-017 / ADR-026 described `history`, refined by ADR-030.
- 2026-11-05 Review fixes (`v1-history-fix`): a failed log read costs the saved transcript, not the compaction; todos counts the replies a `/compact` kept (a reminder at or before the compaction's `through` is folded away; keeping their log seqs would need `Compacted` to carry them); a continued task's compaction `seen_through` resets to 0; an approval note also follows a refusal after the yes (re-check deny, tool gone), and two identical notes both stay (Claude Code pushes each `acceptFeedback` after its own result, unlabelled; the decline marker comes once anyway); a stopped turn already kept the notes (the stop answers every open call first); the TUI and web show the note under the call's result, also on reopen; the fork's saved transcript answers its starting call `STARTED_CALL` (`records.spawning_call`). Skipped: `tool_search` still records `loaded` from the output as of its own `after_tool` (recording from the final result needs a hook after all `after_tool`s in the same append). Gates: `make check`, `make tui`, `make web`, `make parity` 0.
- 2026-11-05 Integrated on `v1-history` after an independent review (no P0/P1; P2s fixed on
  `v1-history-fix`; skipped: a running task that predates the change loses its loaded tools
  and read memory pages on upgrade, as no compatibility was wanted; `tool_search` records
  `loaded` before later `after_tool`s). ADR-030 records the decision. The delegation
  scenario's timeouts were honest sub-agent work at ~43 output tokens/s (raised to 900 s),
  and the eval's usage now counts sub-agents' calls. check 3582 passed, 90.37%; make tui 73;
  make web 58 + e2e 16; make desktop 33 + e2e 3; parity 0; cluster 12; e2e 7; eval no
  regression, no errors or timeouts.
