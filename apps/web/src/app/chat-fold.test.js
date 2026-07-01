// node --test coverage for the pure new-protocol fold/projection helpers
// (T7). These fold raw /content blobs + the reducer
// view-model into the shapes ChatApp consumes, replacing the old server-side
// detail/messages projections.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  projectMessagesFull,
  projectMessagesText,
  extractThinking,
  extractQuestions,
  deriveStatusText,
  synthesizeDetail,
} from "./chat-fold.js";

const messagesBlob = JSON.stringify([
  {
    __canonical_tag__: "message",
    role: "user",
    content: [
      { __canonical_tag__: "text_block", text: "hello " },
      { __canonical_tag__: "text_block", text: "world" },
      { __canonical_tag__: "image_block", source: { hash: "abc", media_type: "image/png" } },
    ],
  },
  {
    __canonical_tag__: "message",
    role: "assistant",
    content: [{ __canonical_tag__: "text_block", text: "hi there" }],
  },
]);

test("projectMessagesFull returns the canonical message array, null on garbage", () => {
  const full = projectMessagesFull(messagesBlob);
  assert.equal(Array.isArray(full), true);
  assert.equal(full.length, 2);
  assert.equal(full[0].role, "user");
  assert.equal(projectMessagesFull("not json"), null);
  assert.equal(projectMessagesFull(JSON.stringify({ not: "an array" })), null);
});

test("projectMessagesText concatenates text_blocks per role, drops non-text", () => {
  const text = projectMessagesText(projectMessagesFull(messagesBlob));
  assert.deepEqual(text, [
    { role: "user", text: "hello world" },
    { role: "assistant", text: "hi there" },
  ]);
  // The image block did not leak into the user prose.
  assert.equal(text[0].text.includes("abc"), false);
  assert.deepEqual(projectMessagesText(null), []);
});

test("extractThinking pulls thinking_block texts; null on fault, [] when none", () => {
  const responseBlob = JSON.stringify({
    __canonical_tag__: "llm_response",
    content: [
      { __canonical_tag__: "thinking_block", text: "let me think" },
      { __canonical_tag__: "text_block", text: "the answer" },
      { __canonical_tag__: "thinking_block", text: "   " },
    ],
  });
  assert.deepEqual(extractThinking(responseBlob), ["let me think"]);
  assert.equal(extractThinking("not json"), null);
  assert.deepEqual(extractThinking(JSON.stringify({ content: [] })), []);
});

test("extractQuestions reads the {questions:[...]} body, [] on fault", () => {
  const blob = JSON.stringify({
    questions: [{ id: "q1", header: "Pick", question: "Which?", choices: [{ id: "a" }] }],
  });
  const qs = extractQuestions(blob);
  assert.equal(qs.length, 1);
  assert.equal(qs[0].id, "q1");
  assert.deepEqual(extractQuestions("nope"), []);
  assert.deepEqual(extractQuestions(JSON.stringify({})), []);
});

test("deriveStatusText maps wakeKind first, then status vocabulary", () => {
  assert.equal(deriveStatusText({ wakeKind: "next-goal", status: "waiting" }), "Ready");
  assert.equal(deriveStatusText({ wakeKind: "approval", status: "waiting" }), "Waiting for approval");
  assert.equal(deriveStatusText({ wakeKind: null, status: "running" }), "Running");
  assert.equal(deriveStatusText({ wakeKind: null, status: "completed" }), "Completed");
  assert.equal(deriveStatusText(null), "");
});

test("synthesizeDetail folds vm + session row + question bodies into the detail shape", () => {
  const questionsByRef = new Map([["qref", [{ id: "q1", question: "Which?" }]]]);
  const vm = {
    status: "waiting",
    wakeKind: "question",
    closed: false,
    model: "gpt-x",
    pendingApprovals: [],
    pendingQuestions: [
      { questionId: "Q1", reason: "need input", questionsRef: "qref" },
    ],
  };
  const row = { task_id: "t1", title: "My session", agent_name: "main" };
  const detail = synthesizeDetail("t1", vm, row, questionsByRef);
  assert.equal(detail.task_id, "t1");
  assert.equal(detail.title, "My session");
  assert.equal(detail.agent, "main");
  assert.equal(detail.wake_kind, "question");
  assert.equal(detail.question_id, "Q1");
  assert.equal(detail.status_text, "Waiting for answer");
  assert.equal(detail.closed, false);
  assert.equal(detail.model, "gpt-x");
  assert.equal(detail.pending_questions.length, 1);
  assert.equal(detail.pending_questions[0].question_id, "Q1");
  assert.deepEqual(detail.pending_questions[0].questions, [{ id: "q1", question: "Which?" }]);
  // A pending approval surfaces as approval_call_id.
  const detail2 = synthesizeDetail(
    "t2",
    { status: "waiting", wakeKind: "approval", pendingApprovals: [{ callId: "c9" }], pendingQuestions: [] },
    null,
    null,
  );
  assert.equal(detail2.approval_call_id, "c9");
  assert.equal(detail2.title, null);
  // No task → null detail.
  assert.equal(synthesizeDetail(null, vm, row, questionsByRef), null);
});
