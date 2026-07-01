// Pure projections that fold new-protocol /content blobs + the reducer view-model
// into the shapes the chat surface consumes (T7).
//
// The new thin backend (T5/T6) has no /tasks/{id} detail and no server-side
// message/question projection: the SSE stream carries only canonical envelopes,
// and large bodies ride a ContentRef fetched RAW from GET /content/{hash}. So the
// frontend folds everything itself. These helpers are the impure-free core of
// that fold (no React, no fetch) so node --test covers them directly.

"use strict";

// Parse a /content blob (raw canonical JSON text) → JS value, or null on fault.
// Canonical serialization is JSON with `__canonical_tag__` markers on typed
// values; the frontend reads those tags to recover block kinds.
function parseContent(text) {
  if (typeof text !== "string" || !text) return null;
  try {
    return JSON.parse(text);
  } catch (e) {
    return null;
  }
}

// The FULL canonical message list behind a MessagesAppended.messages_ref blob:
// to_canonical_bytes(list[Message]) → a JSON array of
// {__canonical_tag__:"message", role, content:[blocks]}. Returns the array (the
// renderer reads block kinds — text/image/thinking — off it) or null on fault.
function projectMessagesFull(text) {
  const obj = parseContent(text);
  return Array.isArray(obj) ? obj : null;
}

// Text-only projection (the new-protocol equivalent of the old server-side
// /tasks/{id}/messages/{hash} endpoint): [{role, text}] where text is the
// concatenated text_block prose. Non-text blocks (thinking / tool-use / image)
// are dropped, matching the old projection so the role/prose helpers in ChatApp
// keep working unchanged.
function projectMessagesText(fullArray) {
  if (!Array.isArray(fullArray)) return [];
  return fullArray.map((message) => {
    const blocks =
      message && Array.isArray(message.content) ? message.content : [];
    const text = blocks
      .filter(
        (b) =>
          b && b.__canonical_tag__ === "text_block" && typeof b.text === "string",
      )
      .map((b) => b.text)
      .join("");
    return {
      role: message && typeof message.role === "string" ? message.role : "",
      text,
    };
  });
}

// The thinking strings behind an LLMResponseRecorded.response_ref blob: the blob
// is the canonical LLMResponse carrying a `.content` block list; return the
// thinking_block texts (mirrors the old deref, "thinking only lives in
// response_ref"). Returns null on a parse fault so the cache can distinguish
// "not fetched yet" (undefined) from "fetched, none" ([]).
function extractThinking(text) {
  const obj = parseContent(text);
  if (!obj) return null;
  const blocks = Array.isArray(obj.content) ? obj.content : [];
  return blocks
    .filter(
      (b) =>
        b &&
        b.__canonical_tag__ === "thinking_block" &&
        typeof b.text === "string" &&
        b.text.trim(),
    )
    .map((b) => b.text);
}

// The question specs behind a UserQuestionRequested.questions_ref blob:
// to_canonical_bytes({questions:[...]}) where each question is
// {id, header, question, choices:[{id,...}]}. Returns the questions array (the
// QuestionPrompt form reads it) or [] on any fault.
function extractQuestions(text) {
  const obj = parseContent(text);
  return obj && Array.isArray(obj.questions) ? obj.questions : [];
}

// A human status label from the folded view-model. The thin backend has no
// status_text projection, so we derive one from the reducer's status + wakeKind
// (the same vocabulary the send gate keys off).
const STATUS_TEXT = {
  created: "Created",
  running: "Running",
  waiting: "Waiting",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
  unknown: "",
};

function deriveStatusText(vm) {
  if (!vm) return "";
  switch (vm.wakeKind) {
    case "next-goal":
      return "Ready";
    case "approval":
      return "Waiting for approval";
    case "question":
      return "Waiting for answer";
    case "subtask":
      return "Waiting for subtask";
    case "human":
      return "Waiting for input";
    case "timer":
      return "Waiting (timer)";
    default:
      return STATUS_TEXT[vm.status] || vm.status || "";
  }
}

// Synthesize the `activeDetail` object ChatApp consumes from the reducer vm + the
// session-list row + the derefed question bodies. The new protocol folds every
// detail field from the stream (D7) — there is no /tasks/{id} detail endpoint.
// `questionsByRef` is a Map<questionsRefHash, questions[]> the data layer fills
// by derefing each pending question's questions_ref via /content/{hash}.
function synthesizeDetail(taskId, vm, sessionRow, questionsByRef) {
  if (!taskId) return null;
  const v = vm || {};
  const approvals = Array.isArray(v.pendingApprovals) ? v.pendingApprovals : [];
  const questions = Array.isArray(v.pendingQuestions) ? v.pendingQuestions : [];
  const row = sessionRow || null;
  return {
    task_id: taskId,
    title: row && row.title ? row.title : null,
    goal: row && row.title ? row.title : null,
    agent: row && row.agent_name ? row.agent_name : null,
    status: v.status || "unknown",
    status_text: deriveStatusText(v),
    closed: !!v.closed,
    wake_kind: v.wakeKind || null,
    approval_call_id: approvals.length ? approvals[0].callId : null,
    question_id: questions.length ? questions[0].questionId : null,
    pending_questions: questions.map((q) => ({
      question_id: q.questionId,
      reason: q.reason || null,
      questions:
        (q.questionsRef &&
          questionsByRef &&
          typeof questionsByRef.get === "function" &&
          questionsByRef.get(q.questionsRef)) ||
        [],
    })),
    model: v.model || null,
    // The thin backend binds a single model / provider / workspace at the room,
    // so there is no per-conversation binding to hydrate the composer from; the
    // selectors degrade gracefully (capabilities advertise empty lists).
    model_binding: null,
    provider_binding: null,
    workspace_dir: null,
    dispatcher_status: null,
  };
}

export {
  parseContent,
  projectMessagesFull,
  projectMessagesText,
  extractThinking,
  extractQuestions,
  deriveStatusText,
  synthesizeDetail,
};
