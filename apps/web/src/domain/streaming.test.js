// Unit tests for the token-streaming preview buffer (slice 4). Node's runner:
//   node --test src/domain/streaming.test.js
//
// Deltas are ephemeral previews of the assistant turn in flight (ADR
// token-streaming-projection.md): they never enter reduceEvents — this buffer
// lives beside the reducer and is cleared/reset by the durable envelopes
// (MessagesAppended / LLMRetryScheduled) folded there.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  applyDelta,
  clearAll,
  clearTask,
  createStreamingState,
  resetCall,
  streamingTurnFor,
} from "./streaming.js";

const delta = (overrides = {}) => ({
  task_id: "t1",
  call_id: "c1",
  kind: "text",
  text: "chunk",
  index: 0,
  ...overrides,
});

const applyAll = (deltas, state = createStreamingState()) =>
  deltas.reduce((acc, d) => applyDelta(acc, d), state);

test("applyDelta: accumulates text per block across kinds", () => {
  const state = applyAll([
    delta({ kind: "thinking", index: 0, text: "Let me " }),
    delta({ kind: "thinking", index: 0, text: "think." }),
    delta({ kind: "text", index: 1, text: "Hello" }),
    delta({ kind: "text", index: 1, text: ", world" }),
  ]);
  const turn = streamingTurnFor(state, "t1");
  assert.deepEqual(turn, {
    callId: "c1",
    blocks: [
      { kind: "thinking", text: "Let me think.", index: 0 },
      { kind: "text", text: "Hello, world", index: 1 },
    ],
  });
});

test("streamingTurnFor: blocks come back ordered by index", () => {
  // Arrival order 2, 0, 1 — the selector orders by content-block index.
  const state = applyAll([
    delta({ index: 2, text: "third" }),
    delta({ kind: "thinking", index: 0, text: "first" }),
    delta({ index: 1, text: "second" }),
  ]);
  const turn = streamingTurnFor(state, "t1");
  assert.deepEqual(
    turn.blocks.map((b) => b.index),
    [0, 1, 2],
  );
  assert.deepEqual(
    turn.blocks.map((b) => b.text),
    ["first", "second", "third"],
  );
});

test("applyDelta: a new call_id replaces the task's buffer wholesale", () => {
  // Tool loop: stream (c1) → MessagesAppended → stream again (c2). Even if the
  // clear were missed, a c2 delta must not append onto c1's leftovers.
  const state = applyAll([
    delta({ call_id: "c1", text: "old attempt" }),
    delta({ call_id: "c2", text: "fresh" }),
  ]);
  const turn = streamingTurnFor(state, "t1");
  assert.deepEqual(turn, {
    callId: "c2",
    blocks: [{ kind: "text", text: "fresh", index: 0 }],
  });
});

test("applyDelta: buffers tasks independently (subtask deltas do not bleed)", () => {
  const state = applyAll([
    delta({ task_id: "root", text: "root text" }),
    delta({ task_id: "sub", call_id: "c9", text: "subtask text" }),
  ]);
  assert.equal(streamingTurnFor(state, "root").blocks[0].text, "root text");
  assert.equal(streamingTurnFor(state, "sub").blocks[0].text, "subtask text");
});

test("clearTask: drops one task, keeps the others' references", () => {
  const state = applyAll([
    delta({ task_id: "t1" }),
    delta({ task_id: "t2", text: "other" }),
  ]);
  const cleared = clearTask(state, "t1");
  assert.equal(streamingTurnFor(cleared, "t1"), null);
  // The untouched task keeps its entry identity (immutable-enough for memo).
  assert.equal(cleared.tasks.get("t2"), state.tasks.get("t2"));
  assert.equal(streamingTurnFor(cleared, "t2").blocks[0].text, "other");
});

test("clearTask: no buffer for the task → same state reference (no repaint)", () => {
  const state = applyAll([delta()]);
  assert.equal(clearTask(state, "unknown"), state);
});

test("clearAll: reconnect drops every buffer; empty state is a no-op", () => {
  const state = applyAll([delta({ task_id: "t1" }), delta({ task_id: "t2" })]);
  const cleared = clearAll(state);
  assert.equal(streamingTurnFor(cleared, "t1"), null);
  assert.equal(streamingTurnFor(cleared, "t2"), null);
  // Already-empty state comes back by reference (no repaint on idle reconnects).
  assert.equal(clearAll(cleared), cleared);
});

test("resetCall: retry drops the buffered call for its task", () => {
  const state = applyAll([delta({ text: "half-streamed" })]);
  const reset = resetCall(state, "t1", "c1");
  assert.equal(streamingTurnFor(reset, "t1"), null);
});

test("resetCall: a mismatched call_id is a no-op (same reference)", () => {
  const state = applyAll([delta({ call_id: "c2" })]);
  assert.equal(resetCall(state, "t1", "c1"), state);
  // Defensive: a retry event without a call_id clears unconditionally.
  assert.equal(streamingTurnFor(resetCall(state, "t1", null), "t1"), null);
  // No buffer at all → same reference.
  assert.equal(resetCall(state, "t9", "c1"), state);
});

test("streamingTurnFor: null for an unbuffered task or empty-text blocks", () => {
  const empty = createStreamingState();
  assert.equal(streamingTurnFor(empty, "t1"), null);
  // A call that has only produced empty text has nothing visible to paint —
  // the caller keeps the bare typing indicator instead of an empty bubble.
  const blank = applyAll([delta({ text: "" })]);
  assert.equal(streamingTurnFor(blank, "t1"), null);
});

test("applyDelta: malformed frames are ignored (same state reference)", () => {
  const state = applyAll([delta()]);
  const bad = [
    delta({ task_id: "" }),
    delta({ call_id: 7 }),
    delta({ kind: "tool" }),
    delta({ text: 42 }),
    delta({ index: "0" }),
    delta({ index: NaN }),
    null,
  ];
  for (const frame of bad) {
    assert.equal(applyDelta(state, frame), state);
  }
});

test("applyDelta: immutable-enough — the previous state is untouched", () => {
  const before = applyAll([delta({ text: "abc" })]);
  const snapshot = streamingTurnFor(before, "t1");
  const after = applyDelta(before, delta({ text: "def" }));
  assert.notEqual(after, before);
  assert.notEqual(after.tasks.get("t1"), before.tasks.get("t1"));
  // The already-selected turn from the old state did not mutate.
  assert.deepEqual(streamingTurnFor(before, "t1"), snapshot);
  assert.equal(streamingTurnFor(after, "t1").blocks[0].text, "abcdef");
});
