// QuestionPrompt answer model (B17 / U6).
//
// A pending question can carry BOTH a choice and a freeform "other answer", and
// the backend accepts both fields. The old QuestionPrompt replaced the whole
// answer object on each edit, so picking a choice wiped any typed text and vice
// versa — half the payload was lost. These pure helpers merge per-field so the
// two coexist, and decide submit-readiness. Kept framework-free so they are unit
// tested directly (the component logic around them stays trivial).

"use strict";

// Merge a partial answer patch ({ choice_id } or { text }) into the answers map
// WITHOUT clobbering the other field.
function applyAnswerPatch(answers, questionId, patch) {
  const current = (answers && answers[questionId]) || {};
  return { ...answers, [questionId]: { ...current, ...patch } };
}

// A single question is satisfied when it carries a chosen choice OR non-empty
// freeform text. Every question is required: the wire protocol has no optional
// flag and the runtime validates an answer for every question id, so an
// "optional" notion here would only be a front-end-only false green light.
function questionSatisfied(answer) {
  if (!answer) return false;
  return Boolean(answer.choice_id) || Boolean((answer.text || "").trim());
}

// Every question answered enough to enable Submit.
function answersComplete(questions, answers) {
  return (questions || []).every((q) =>
    questionSatisfied(answers && q ? answers[q.id] : undefined),
  );
}

export { applyAnswerPatch, answersComplete, questionSatisfied };
