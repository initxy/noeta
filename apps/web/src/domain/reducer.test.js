// Unit tests for the single-task reducer's detail fold (T7). Node's runner:
//   node --test src/domain/reducer.test.js
//
// Covers the wake-kind + closed folding the new-protocol send gate reads
// the equivalents of the old /tasks/{id}
// detail's `wake_kind` / `closed`, now folded from the raw envelope stream.

import assert from "node:assert/strict";
import { test } from "node:test";

import { classifyWakeOn, reduceEvents } from "./reducer.js";

// Canonical-tagged wake_on shapes (noeta.protocols.canonical.to_canonical puts
// the tag on `__canonical_tag__`).
const humanWake = (handle) => ({ __canonical_tag__: "human_response", handle });
const suspended = (seq, wakeOn) => ({
  task_id: "t1",
  seq,
  type: "TaskSuspended",
  payload: { reason: "waiting_human", wake_on: wakeOn },
});
const woken = (seq) => ({ task_id: "t1", seq, type: "TaskWoken", payload: {} });
const closed = (seq) => ({
  task_id: "t1",
  seq,
  type: "ConversationClosed",
  payload: { closed_by: "user", reason: "done" },
});
const reopened = (seq) => ({
  task_id: "t1",
  seq,
  type: "ConversationReopened",
  payload: { reopened_by: "user" },
});

test("classifyWakeOn: human-response handles map to their kind", () => {
  assert.equal(classifyWakeOn(humanWake("noeta-code-next-goal")), "next-goal");
  assert.equal(classifyWakeOn(humanWake("approval-abc")), "approval");
  assert.equal(classifyWakeOn(humanWake("question-q1")), "question");
  assert.equal(classifyWakeOn(humanWake("something-else")), "human");
});

test("classifyWakeOn: subtask + timer + unknown conditions", () => {
  assert.equal(
    classifyWakeOn({ __canonical_tag__: "subtask_completed", subtask_id: "s" }),
    "subtask",
  );
  assert.equal(
    classifyWakeOn({
      __canonical_tag__: "subtask_group_completed",
      group_id: "g",
    }),
    "subtask",
  );
  assert.equal(
    classifyWakeOn({ __canonical_tag__: "timer_fired", fire_at: 1 }),
    "timer",
  );
  assert.equal(classifyWakeOn(null), null);
  assert.equal(classifyWakeOn({ __canonical_tag__: "mystery" }), null);
});

test("reduceEvents: suspend sets wakeKind, woken clears it", () => {
  const vmSuspended = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
    { task_id: "t1", seq: 1, type: "TaskStarted", payload: {} },
    suspended(2, humanWake("noeta-code-next-goal")),
  ]);
  assert.equal(vmSuspended.wakeKind, "next-goal");

  // A re-lease clears the stale wake-kind.
  const vmWoken = reduceEvents([
    suspended(2, humanWake("noeta-code-next-goal")),
    woken(3),
  ]);
  assert.equal(vmWoken.wakeKind, null);
});

test("reduceEvents: approval suspend then resolved-then-suspend tracks latest", () => {
  // Park on approval, then (after the human approves) re-suspend on next-goal:
  // the latest suspend wins.
  const vm = reduceEvents([
    suspended(1, humanWake("approval-call-7")),
    woken(2),
    suspended(3, humanWake("noeta-code-next-goal")),
  ]);
  assert.equal(vm.wakeKind, "next-goal");
});

test("reduceEvents: terminal clears wakeKind", () => {
  const vm = reduceEvents([
    suspended(1, humanWake("noeta-code-next-goal")),
    { task_id: "t1", seq: 2, type: "TaskCompleted", payload: { answer: "hi" } },
  ]);
  assert.equal(vm.wakeKind, null);
  assert.equal(vm.status, "completed");
});

test("reduceEvents: closed flag folds from ConversationClosed/Reopened", () => {
  const vmClosed = reduceEvents([
    suspended(1, humanWake("noeta-code-next-goal")),
    closed(2),
  ]);
  assert.equal(vmClosed.closed, true);
  // Close is orthogonal to status — still waiting, still next-goal.
  assert.equal(vmClosed.status, "waiting");
  assert.equal(vmClosed.wakeKind, "next-goal");

  const vmReopened = reduceEvents([closed(2), reopened(3)]);
  assert.equal(vmReopened.closed, false);
});

test("reduceEvents: TaskCompleted answer does not double-render the final message", () => {
  // A completed subtask: its final answer arrives BOTH as a trailing
  // MessagesAppended (the bubble) and as TaskCompleted.answer. The reducer must
  // not also push an assistant_text turn, or the subtask drawer shows the same
  // prose twice (the root chat never hits TaskCompleted, which is why only the
  // drawer doubled).
  const vm = reduceEvents([
    { task_id: "s1", seq: 0, type: "TaskCreated", payload: {} },
    { task_id: "s1", seq: 1, type: "TaskStarted", payload: {} },
    {
      task_id: "s1",
      seq: 2,
      type: "MessagesAppended",
      payload: { count: 1, messages_ref: { hash: "abc" } },
    },
    { task_id: "s1", seq: 3, type: "TaskCompleted", payload: { answer: "done" } },
  ]);
  assert.equal(vm.status, "completed");
  assert.equal(vm.turns.filter((t) => t.kind === "assistant_text").length, 0);
  assert.equal(vm.turns.filter((t) => t.kind === "message").length, 1);
});

test("reduceEvents: TaskCompleted keeps inline answer when no message carried it", () => {
  // Degenerate fallback: a stream that completes with an answer but never
  // appended a final assistant message still surfaces the answer inline, so it
  // is never silently dropped.
  const vm = reduceEvents([
    { task_id: "s1", seq: 0, type: "TaskCreated", payload: {} },
    { task_id: "s1", seq: 1, type: "TaskCompleted", payload: { answer: "hi" } },
  ]);
  const inline = vm.turns.filter((t) => t.kind === "assistant_text");
  assert.equal(inline.length, 1);
  assert.equal(inline[0].text, "hi");
});

test("reduceEvents: defaults are running-ready (no suspend, not closed)", () => {
  const vm = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
  ]);
  assert.equal(vm.wakeKind, null);
  assert.equal(vm.closed, false);
});

// --- LLMRetryScheduled: the live transient-retry fold ----------------------

const retryEvent = (seq, attempt, extra = {}) => ({
  task_id: "t1",
  seq,
  type: "LLMRetryScheduled",
  payload: {
    call_id: "llm-1",
    attempt,
    max_retries: 8,
    delay_seconds: 2.5,
    category: "transient",
    error: "429 Too Many Requests",
    ...extra,
  },
});

test("reduceEvents: LLMRetryScheduled folds the live retry badge", () => {
  const vm = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
    retryEvent(1, 1),
  ]);
  assert.deepEqual(vm.llmRetry, {
    callId: "llm-1",
    attempt: 1,
    maxRetries: 8,
    delaySeconds: 2.5,
    category: "transient",
    error: "429 Too Many Requests",
    seq: 1,
  });
});

test("reduceEvents: repeated retries update ONE warning turn in place", () => {
  const vm = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
    retryEvent(1, 1),
    retryEvent(2, 2, { delay_seconds: 5.0 }),
  ]);
  const warnings = vm.turns.filter(
    (t) => t.kind === "warning" && t.label === "llm-retry",
  );
  assert.equal(warnings.length, 1);
  assert.equal(warnings[0].attempt, 2);
  assert.equal(warnings[0].delaySeconds, 5.0);
  assert.equal(vm.llmRetry.attempt, 2);
});

test("reduceEvents: LLMResponseRecorded clears the live retry badge", () => {
  const vm = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
    retryEvent(1, 1),
    {
      task_id: "t1",
      seq: 2,
      type: "LLMResponseRecorded",
      payload: { call_id: "llm-1", stop_reason: "end_turn" },
    },
  ]);
  assert.equal(vm.llmRetry, null);
  // The timeline keeps the episode's summary marker.
  assert.equal(
    vm.turns.filter((t) => t.kind === "warning" && t.label === "llm-retry")
      .length,
    1,
  );
});

test("reduceEvents: TaskRewound prunes a stale retry badge", () => {
  const vm = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
    retryEvent(1, 1),
    { task_id: "t1", seq: 2, type: "TaskRewound", payload: { target_seq: 0 } },
  ]);
  assert.equal(vm.llmRetry, null);
});

test("reduceEvents: StepAttemptAbandoned prunes the dead attempt (exclusive boundary)", () => {
  // The seal re-bases to just BEFORE abandoned_from_seq: the interrupted
  // attempt's ghost tool call (started, never finished) must go; everything
  // strictly before the boundary stays. No tombstone turn is added.
  const vm = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
    { task_id: "t1", seq: 1, type: "ContextPlanComposed", payload: {} },
    {
      task_id: "t1",
      seq: 2,
      type: "ToolCallStarted",
      payload: { call_id: "ghost", tool_name: "shell_run", arguments: {} },
    },
    {
      task_id: "t1",
      seq: 3,
      type: "StepAttemptAbandoned",
      payload: { abandoned_from_seq: 1, reason: "crash_recovery" },
    },
  ]);
  assert.equal(vm.toolCalls["ghost"], undefined);
  assert.equal(
    vm.turns.some((t) => typeof t.seq === "number" && t.seq >= 1),
    false,
  );
});

test("reduceEvents: re-base markers restore the todo checklist at the boundary", () => {
  // set_todos is replace-all state, not an append log: after a seal the
  // dead attempt's checklist must not linger — the last snapshot surviving
  // the boundary is restored (the runtime fold reverts to exactly that).
  const setTodos = (seq, todos) => ({
    task_id: "t1",
    seq,
    type: "TaskStatePatched",
    payload: { patch: { set_todos: todos } },
  });
  const vm = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
    setTodos(1, [{ id: "a", content: "keep me", status: "pending" }]),
    { task_id: "t1", seq: 2, type: "ContextPlanComposed", payload: {} },
    setTodos(3, [{ id: "b", content: "dead todo", status: "pending" }]),
    {
      task_id: "t1",
      seq: 4,
      type: "StepAttemptAbandoned",
      payload: { abandoned_from_seq: 2, reason: "auto_redrive" },
    },
  ]);
  assert.equal(vm.todos.length, 1);
  assert.equal(vm.todos[0].id, "a");
  // A rewind past every snapshot clears the checklist entirely.
  const vm2 = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
    setTodos(1, [{ id: "a", content: "x", status: "pending" }]),
    { task_id: "t1", seq: 2, type: "TaskRewound", payload: { target_seq: 0 } },
  ]);
  assert.equal(vm2.todos.length, 0);
});

test("reduceEvents: image/* artifacts fold into vm.images keyed by call id", () => {
  // A browser_screenshot that records an image/png artifact should surface
  // inline under its tool call — the image analogue of the text/x-diff path.
  // Non-image artifacts (e.g. a diff) must NOT leak into vm.images.
  const vm = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
    {
      task_id: "t1",
      seq: 1,
      type: "ToolCallStarted",
      payload: { call_id: "c1", tool_name: "browser_screenshot", arguments: {} },
    },
    {
      task_id: "t1",
      seq: 2,
      type: "ToolResultRecorded",
      payload: {
        call_id: "c1",
        success: true,
        summary: "screenshot captured",
        artifacts: [
          { hash: "img-hash", media_type: "image/png", size: 28730 },
          { hash: "diff-hash", media_type: "text/x-diff", size: 40 },
        ],
      },
    },
  ]);
  assert.equal(vm.images.length, 1);
  assert.equal(vm.images[0].hash, "img-hash");
  assert.equal(vm.images[0].callId, "c1");
  assert.equal(vm.images[0].toolName, "browser_screenshot");
  assert.equal(vm.images[0].mediaType, "image/png");
  // The diff artifact lands in vm.diffs, not vm.images.
  assert.equal(vm.diffs.length, 1);
  assert.equal(vm.diffs[0].hash, "diff-hash");
});

test("reduceEvents: re-base markers prune images from the dead tail", () => {
  // An image captured in an abandoned attempt must not linger once the dead
  // tail is folded out — mirroring how diffs/todos are pruned at the boundary.
  const vm = reduceEvents([
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {} },
    { task_id: "t1", seq: 1, type: "ContextPlanComposed", payload: {} },
    {
      task_id: "t1",
      seq: 2,
      type: "ToolCallStarted",
      payload: { call_id: "ghost", tool_name: "browser_screenshot", arguments: {} },
    },
    {
      task_id: "t1",
      seq: 3,
      type: "ToolResultRecorded",
      payload: {
        call_id: "ghost",
        success: true,
        summary: "screenshot captured",
        artifacts: [{ hash: "ghost-img", media_type: "image/png" }],
      },
    },
    {
      task_id: "t1",
      seq: 4,
      type: "StepAttemptAbandoned",
      payload: { abandoned_from_seq: 2, reason: "crash_recovery" },
    },
  ]);
  assert.equal(vm.images.length, 0);
  assert.equal(vm.toolCalls["ghost"], undefined);
});
