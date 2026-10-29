# ADR-026: A blocked prompt is erased by a core record, and plugin state follows it

- Status: accepted
- Date: 2026-10-29
- Refines: [ADR-001](001-task-is-an-event-log-with-one-writer.md),
  [ADR-002](002-one-extension-interface-and-entry-points.md)

## Context

Claude Code erases a prompt a `UserPromptSubmit` hook blocks: the model never sees it, on that
turn or later. In noeta the person's message is already in the log when the hook runs, the log
is append-only (ADR-001), and the request is a function of the fold. What the message set off
is in the log too: a `/command` or `/skill` expansion recorded as `inline` text after it, and
plugin state keyed by it (a typed skill's activation, a markdown command's allow rules).

## Decision

- A core record kind, `erased` (`records.Erased` in `noeta/core/records.py`, written with
  `TaskView.erase_messages`), carries the log seqs of the person's messages to take out. The
  log keeps them; the fold (`noeta/core/loop/state.py`) drops from `messages` and `history`
  the `human` and `inline` user messages at those seqs, never a tool result or another agent's
  message. Where a person's message shares its seq with other text (a stop's marker released
  with it), only that message and what follows it at the seq go.
- Plugin state follows: the fold calls every plugin reducer that holds state with
  `records.ERASED_KIND` and the erased seqs, so a plugin takes back what it recorded for those
  messages. A plugin that records something for a person's input keys it by that input's seq
  (the skills plugin restores the active entry a typed `/skill` replaced; markdown commands
  remove the rules a `/command` added).
- The hooks plugin (`noeta/plugins/hooks/extension.py`) runs `UserPromptSubmit` before every
  model call that carries new human messages, not only a turn's first, and records the last
  seq it checked (`prompted`), so each message is checked once and before the model sees it.
  Its first look at a task starts after the model's last reply or the compaction's end, never
  at seq 0. A block erases the messages from the first blocked one on, with their `inline`
  expansions, and fails the turn, mid-turn as at its start.

## Rationale

The log stays the record of what happened, so audit and replay still show the blocked prompt,
while the fold, which is what builds requests, drops it, the same split compaction already
makes. One core kind that every reducer sees means the kernel needs to know nothing of what a
plugin derived from a message, and a plugin written later can take its own state back.
Checking before every call, not per turn, is what keeps a message sent mid-turn from reaching
the model unchecked.

Rejected: deleting or rewriting the logged message (breaks the append-only log and every
snapshot taken after it); a hooks-plugin record each other plugin reads (plugins never read
each other's records, ADR-002); filtering the blocked text in a `before_model` rewrite (the
request would depend on extension order, and `history` would still carry it into a
continued task); checking only a turn's first call (a message joining mid-turn would be seen
unchecked).

## Consequences

- Every plugin reducer with state may be called with `records.ERASED_KIND`; one that ignores
  it keeps what it recorded for an erased message.
- An older worker that does not know the `erased` kind leaves such a log to a newer worker
  (ADR-023).
- A task first seen by the hooks plugin with messages the model already answered does not
  re-check them.
