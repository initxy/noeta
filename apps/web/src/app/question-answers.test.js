import assert from "node:assert/strict";
import { test } from "node:test";

import {
  applyAnswerPatch,
  answersComplete,
  questionSatisfied,
} from "./question-answers.js";

// B17 / U6 — the four scenarios the spec requires: only choice / only text /
// choice-then-text / text-then-choice. None may drop the other field.
test("applyAnswerPatch: choice and freeform text coexist, never clobber", () => {
  assert.deepEqual(applyAnswerPatch({}, "q1", { choice_id: "c1" }).q1, {
    choice_id: "c1",
  });
  assert.deepEqual(applyAnswerPatch({}, "q1", { text: "hello" }).q1, {
    text: "hello",
  });

  // choice then text
  let a = {};
  a = applyAnswerPatch(a, "q1", { choice_id: "c1" });
  a = applyAnswerPatch(a, "q1", { text: "hello" });
  assert.deepEqual(a.q1, { choice_id: "c1", text: "hello" });

  // text then choice
  let b = {};
  b = applyAnswerPatch(b, "q1", { text: "hello" });
  b = applyAnswerPatch(b, "q1", { choice_id: "c1" });
  assert.deepEqual(b.q1, { choice_id: "c1", text: "hello" });
});

test("applyAnswerPatch: scopes to the question id, leaves siblings intact", () => {
  const start = { q1: { choice_id: "a" } };
  const next = applyAnswerPatch(start, "q2", { text: "t" });
  assert.deepEqual(next.q1, { choice_id: "a" });
  assert.deepEqual(next.q2, { text: "t" });
  assert.notEqual(next, start); // immutable
});

test("answersComplete: every question needs a choice or non-empty text", () => {
  // All questions are required (the runtime has no optional flag).
  const qs = [{ id: "q1" }, { id: "q2" }];
  assert.equal(answersComplete(qs, {}), false);
  assert.equal(answersComplete(qs, { q1: { choice_id: "a" } }), false); // q2 unanswered
  assert.equal(
    answersComplete(qs, { q1: { choice_id: "a" }, q2: { text: "x" } }),
    true,
  );
  assert.equal(
    answersComplete(qs, { q1: { text: "  " }, q2: { text: "x" } }),
    false, // whitespace ≠ answer
  );
  // a choice + freeform together still satisfies
  assert.equal(
    answersComplete([{ id: "q1" }], { q1: { choice_id: "a", text: "note" } }),
    true,
  );
});

test("questionSatisfied: either field satisfies, both-present satisfies", () => {
  assert.equal(questionSatisfied({ choice_id: "a", text: "t" }), true);
  assert.equal(questionSatisfied({ choice_id: "", text: "" }), false);
  assert.equal(questionSatisfied(undefined), false);
});
