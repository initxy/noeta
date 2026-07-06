// Token-streaming preview buffer (token-streaming slice 4; ADR
// docs/adr/token-streaming-projection.md). Pure, framework-agnostic,
// side-effect-free — the React data layer (chat-data.js) holds one instance in
// a ref and repaints off a version counter.
//
// Deltas are EPHEMERAL: they ride the SSE stream as named `delta` frames
// (`{task_id, call_id, kind, text, index}`), never enter the reducer /
// EventLog, and are never replayed on reconnect. This module only accumulates
// them per task → per call → per content-block index so the UI can preview the
// assistant turn in flight; the durable truth is still the MessagesAppended
// envelope that follows (which clears the buffer via `clearTask`). A failed
// attempt's LLMRetryScheduled drops the half-streamed call via `resetCall`.
//
// Updates are immutable-enough for React: every change returns a fresh state
// object with a fresh entry for the touched task, while untouched tasks keep
// their references; a no-op returns the SAME state reference so callers can
// cheaply skip a repaint.
//
// The state shape:
//
//   { tasks: Map<taskId, { callId, blocks: Map<index, { kind, text }> }> }
//
// One buffered call per task: multiple LLM calls stream sequentially within a
// turn (tool loop), so a delta carrying a different call_id than the buffered
// one means a NEW call started and the stale buffer is replaced wholesale.

"use strict";

function createStreamingState() {
  return { tasks: new Map() };
}

// Wire-contract guard: a malformed frame is ignored (the buffer never throws
// on bad input — forward-compatible, like the reducer's unknown-event stance).
function isValidDelta(delta) {
  return (
    !!delta &&
    typeof delta.task_id === "string" &&
    delta.task_id !== "" &&
    typeof delta.call_id === "string" &&
    delta.call_id !== "" &&
    (delta.kind === "text" || delta.kind === "thinking") &&
    typeof delta.text === "string" &&
    typeof delta.index === "number" &&
    Number.isFinite(delta.index)
  );
}

// Fold one delta frame into the buffer: append its text to the (task, call,
// index) block. A different call_id replaces the task's buffer (new call); a
// kind flip on the same index restarts that block (defensive — the provider
// contract keeps kind constant per index). Returns the same state reference
// for an invalid delta.
function applyDelta(state, delta) {
  if (!isValidDelta(delta)) return state;
  const prev = state.tasks.get(delta.task_id);
  const sameCall = !!prev && prev.callId === delta.call_id;
  const blocks = new Map(sameCall ? prev.blocks : undefined);
  const existing = blocks.get(delta.index);
  const text =
    existing && existing.kind === delta.kind
      ? existing.text + delta.text
      : delta.text;
  blocks.set(delta.index, { kind: delta.kind, text });
  const tasks = new Map(state.tasks);
  tasks.set(delta.task_id, { callId: delta.call_id, blocks });
  return { tasks };
}

// Drop a task's buffer entirely — the turn's real content landed
// (MessagesAppended), so the preview is superseded. No-op (same reference)
// when the task has no buffer.
function clearTask(state, taskId) {
  if (!state.tasks.has(taskId)) return state;
  const tasks = new Map(state.tasks);
  tasks.delete(taskId);
  return { tasks };
}

// Drop the buffered call for a task — the in-flight attempt failed and a retry
// will re-stream from scratch (LLMRetryScheduled; call_id is stable across
// attempts). A null/undefined callId clears unconditionally; a mismatched
// callId is a no-op (the buffer already belongs to a different call).
function resetCall(state, taskId, callId) {
  const entry = state.tasks.get(taskId);
  if (!entry) return state;
  if (callId != null && entry.callId !== callId) return state;
  const tasks = new Map(state.tasks);
  tasks.delete(taskId);
  return { tasks };
}

// Drop every task's buffer — a stream (re)connect or session switch: deltas
// are never replayed, so no preview may survive the connection that carried
// it. No-op (same reference) when nothing is buffered.
function clearAll(state) {
  if (!state.tasks.size) return state;
  return createStreamingState();
}

// The renderable streaming turn for a task: `{callId, blocks}` with blocks
// ordered by content-block index, or null when there is nothing visible to
// paint (no buffer, or only empty-text blocks) — the caller keeps the bare
// typing indicator up in that case.
function streamingTurnFor(state, taskId) {
  const entry = taskId != null ? state.tasks.get(taskId) : undefined;
  if (!entry) return null;
  const blocks = [];
  for (const [index, block] of entry.blocks) {
    if (block.text) blocks.push({ kind: block.kind, text: block.text, index });
  }
  if (!blocks.length) return null;
  blocks.sort((a, b) => a.index - b.index);
  return { callId: entry.callId, blocks };
}

export {
  createStreamingState,
  applyDelta,
  clearAll,
  clearTask,
  resetCall,
  streamingTurnFor,
};
