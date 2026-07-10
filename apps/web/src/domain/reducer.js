// Noeta chat reducer — the ONE piece of TS/JS logic worth sharing with the
// CLI. Pure, framework-agnostic, side-effect-free:
//
//     reduceEvents(EventEnvelope[]) -> ConversationViewModel
//
// The canonical contract is always the EventEnvelope (recorded bytes are the
// moat); NDJSON / SSE are only projections of it. This reducer NEVER invents
// an event schema — it only *folds* the canonical envelope projection the
// server already serialises (noeta.cli._common.envelope_to_json_obj) into a
// chat view-model. The DOM renderer (chat.js) is intentionally NOT shared
// with the CLI terminal renderer; the reducer is.
//
// The view-model shape:
//
//   {
//     taskId:    string | null,        // first envelope's task_id
//     status:    string,               // lifecycle state ("created" → ...)
//     model:     string | null,        // last ModelBound selector
//     turns:     Turn[],               // ordered conversation timeline
//     toolCalls: { [call_id]: ToolCall },
//     pendingApprovals: Approval[],    // unresolved ToolCallApprovalRequested
//     pendingQuestions: Question[],    // unresolved UserQuestionRequested
//     diffs:     Diff[],               // text/x-diff artifacts (proposed edits)
//     images:    ImageRef[],           // image/* artifacts (e.g. browser screenshots)
//     todos:     Todo[],               // current todo_write checklist (replace-all)
//     lastSeq:   number,               // highest seq folded (dedup bookmark)
//   }
//
//   Turn = { kind, seq, ...payload-derived fields }
//     kind ∈ { "message", "tool", "assistant_text", "model", "lifecycle",
//              "approval", "approval_resolved", "denied", "subtask" }
//
// A Turn is the renderable unit the chat surface walks in order; the renderer
// groups consecutive turns into Claude-style bubbles. Keeping the timeline
// flat + ordered (not pre-bucketed) keeps the reducer trivially testable.

"use strict";

function emptyViewModel() {
  return {
    taskId: null,
    status: "unknown",
    model: null,
    turns: [],
    toolCalls: {},
    // subtaskId -> { status: "running"|"completed"|"failed", agentName, goal,
    // error, seq }. Folded from SubtaskSpawned (running) + SubtaskCompleted
    // (whose payload.result.status gives completed vs failed) so the chat
    // surface can paint a live status on each delegation chip and aggregate the
    // still-running ones into the "executing" strip above the composer
    // The "__workflow__" agentName is a workflow node.
    subtasks: {},
    pendingApprovals: [],
    pendingQuestions: [],
    diffs: [],
    // image/* artifacts (browser screenshots and the like) surfaced inline under
    // the tool call that produced them — the image analogue of `diffs`, which is
    // the text/x-diff analogue. Same {callId, toolName, hash, mediaType, seq}
    // shape so the renderer mirrors the diff disclosure path.
    images: [],
    // Current todo_write checklist (CW18b replace-all): [{id, content, status}].
    // Folded from the latest TaskStatePatched(set_todos) so the composer's todo
    // strip reflects the model's current plan.
    todos: [],
    // Every set_todos snapshot seen, as [{seq, todos}]. `todos` above is
    // always the last entry's list; the history exists ONLY so the re-base
    // markers (TaskRewound / StepAttemptAbandoned) can restore the checklist
    // as it stood at the kept boundary — without it a pruned dead tail would
    // leave the dead attempt's todos on screen while the runtime fold has
    // already reverted them.
    todoLog: [],
    // What a SUSPENDED task is waiting on, classified from the latest
    // TaskSuspended's typed `wake_on` (T7 detail
    // fold): "next-goal" | "approval" | "question" | "human" | "subtask" |
    // "timer" | null. `null` while running / before the first suspend. The
    // composer's send gate keys off `wakeKind === "next-goal"` (the task is
    // parked waiting for the user's next message) — the new-protocol equivalent
    // of the old `/tasks/{id}` detail's `wake_kind`. Cleared back to null on the
    // next TaskWoken / TaskStarted (the task re-leased and is running again).
    wakeKind: null,
    // Conversation close/archive lifecycle, folded from ConversationClosed /
    // ConversationReopened ("No synthesized terminal"). ORTHOGONAL to `status` — a closed conversation stays
    // "waiting"; this is the new-protocol equivalent of the detail's `closed`.
    closed: false,
    // The LIVE transient-retry state of the in-flight LLM call, folded from
    // LLMRetryScheduled: {callId, attempt, maxRetries, delaySeconds, category,
    // error, seq}. Set on each scheduled backoff, cleared by the call's
    // LLMResponseRecorded (the trio always completes, success or error), so a
    // truthy value means "the agent is stalled in a retry backoff right now"
    // — the composer indicator / status text key off it.
    llmRetry: null,
    lastSeq: -1,
  };
}

// The next-goal wake handle (mirror of execution.multi_turn.NEXT_GOAL_WAKE_HANDLE)
// and the approval / question handle prefixes (control_semantics). Kept in sync
// with the Python constants; a suspended task whose wake_on is a human-response
// handle is classified by which of these the handle matches.
const NEXT_GOAL_WAKE_HANDLE = "noeta-code-next-goal";
const APPROVAL_HANDLE_PREFIX = "approval-";
const QUESTION_HANDLE_PREFIX = "question-";

// Classify a TaskSuspended's typed `wake_on` (a canonical-tagged dict, see
// noeta.protocols.canonical.to_canonical — the tag rides on `__canonical_tag__`)
// into the wake-kind the send gate / status badges read. Returns null for an
// unrecognised / absent condition (forward-compatible, like the reducer's
// unknown-event stance).
function classifyWakeOn(wakeOn) {
  if (!wakeOn || typeof wakeOn !== "object") return null;
  const tag = wakeOn.__canonical_tag__;
  if (tag === "human_response") {
    const handle = typeof wakeOn.handle === "string" ? wakeOn.handle : "";
    if (handle === NEXT_GOAL_WAKE_HANDLE) return "next-goal";
    if (handle.startsWith(APPROVAL_HANDLE_PREFIX)) return "approval";
    if (handle.startsWith(QUESTION_HANDLE_PREFIX)) return "question";
    return "human";
  }
  if (tag === "subtask_completed" || tag === "subtask_group_completed") {
    return "subtask";
  }
  if (tag === "timer_fired") return "timer";
  return null;
}

// Map a lifecycle / control event type to the conversation status it implies.
// Interactive turns end on a trailing TaskSuspended (final=False), so a
// suspend is "waiting", NOT terminal. Only the one-shot run path
// reaches TaskCompleted/TaskFailed terminal states.
const STATUS_FOR_TYPE = {
  TaskCreated: "created",
  TaskStarted: "running",
  TaskWoken: "running",
  ModelBound: null, // status unchanged
  TaskSuspended: "waiting",
  TaskCompleted: "completed",
  TaskFailed: "failed",
  TaskCancelled: "cancelled",
};

// Fold one envelope into the accumulating view-model (mutates `vm`). Split out
// so the reducer body stays a thin ordered loop and each event type's effect
// is independently legible.
function applyEnvelope(vm, env) {
  if (!env || typeof env.type !== "string") return;
  if (typeof env.seq === "number") {
    if (env.seq <= vm.lastSeq) return; // dedup against the seq bookmark
    vm.lastSeq = env.seq;
  }
  if (vm.taskId === null && typeof env.task_id === "string") {
    vm.taskId = env.task_id;
  }
  const p = env.payload || {};
  const seq = typeof env.seq === "number" ? env.seq : null;

  // Lifecycle status transition (if any).
  if (Object.prototype.hasOwnProperty.call(STATUS_FOR_TYPE, env.type)) {
    const next = STATUS_FOR_TYPE[env.type];
    if (next !== null) vm.status = next;
  }

  // Wake-kind fold: a TaskSuspended parks the task on a typed condition; every
  // re-lease (TaskStarted / TaskWoken) and every terminal clears it back to
  // null so a stale "waiting on X" never lingers once the task is running again.
  switch (env.type) {
    case "TaskSuspended":
      vm.wakeKind = classifyWakeOn(p.wake_on);
      break;
    case "TaskStarted":
    case "TaskWoken":
    case "TaskCompleted":
    case "TaskFailed":
    case "TaskCancelled":
      vm.wakeKind = null;
      break;
    default:
      break;
  }

  switch (env.type) {
    case "ModelBound":
      if (typeof p.model === "string") vm.model = p.model;
      vm.turns.push({ kind: "model", seq, model: p.model || null });
      break;

    case "MessagesAppended":
      // Bodies live behind messages_ref in ContentStore; the canonical
      // envelope only carries a count + the ref. We pass the ref hash through
      // so the renderer can lazily deref the actual prose via the task-scoped
      // /tasks/{id}/messages/{hash} endpoint and paint user/assistant bubbles.
      // The reducer itself still invents NO body text (stays pure / shared
      // with the CLI) — it only forwards the hash the renderer dereferences.
      //
      // NOTE: the round-role (user vs agent) is NOT derived here. The old
      // `_sawResponse` heuristic (reset on TaskCreated/TaskWoken, set on
      // LLMResponseRecorded) was unreliable: every resume flow (tool
      // approval, HITL answer, subtask completion) fires TaskWoken BEFORE
      // the role='tool' MessagesAppended, so tool-result appends were
      // mis-tagged "user" and split one assistant round into two bubbles.
      // The renderer (chat.js) now derives the turn role authoritatively
      // from the dereferenced messages' own `role` field via
      // peekTurnMessageRole(). We set a conservative default of "agent"
      // here so any renderer that still keys off turn.role will not split
      // rounds spuriously; the canonical decision lives in the renderer.
      vm.turns.push({
        kind: "message",
        seq,
        count: p.count || 0,
        role: "agent",
        messagesRef:
          p.messages_ref && typeof p.messages_ref.hash === "string"
            ? p.messages_ref.hash
            : null,
      });
      break;

    case "ToolCallStarted": {
      const call = {
        callId: p.call_id,
        toolName: p.tool_name,
        arguments: p.arguments || {},
        status: "started",
        summary: null,
        success: null,
        seq,
      };
      vm.toolCalls[p.call_id] = call;
      vm.turns.push({ kind: "tool", seq, callId: p.call_id });
      break;
    }

    case "ToolResultRecorded": {
      const call = vm.toolCalls[p.call_id] || {
        callId: p.call_id,
        toolName: null,
        arguments: {},
        seq,
      };
      call.status = "recorded";
      call.success = p.success === true;
      call.summary = typeof p.summary === "string" ? p.summary : null;
      call.outputRef = p.output_ref || null;
      vm.toolCalls[p.call_id] = call;
      // Surface every text/x-diff artifact as a proposed edit (diff view).
      const arts = Array.isArray(p.artifacts) ? p.artifacts : [];
      for (const art of arts) {
        if (!art || !art.hash) continue;
        if (art.media_type === "text/x-diff") {
          vm.diffs.push({
            callId: p.call_id,
            toolName: call.toolName,
            hash: art.hash,
            mediaType: art.media_type,
            seq,
          });
        } else if (/^image\//.test(art.media_type || "")) {
          // An image/* artifact (browser screenshot, etc.) is shown inline under
          // the tool call — the glanceable counterpart to a diff disclosure.
          vm.images.push({
            callId: p.call_id,
            toolName: call.toolName,
            hash: art.hash,
            mediaType: art.media_type,
            seq,
          });
        }
      }
      break;
    }

    case "ToolCallFinished": {
      const call = vm.toolCalls[p.call_id];
      if (call) call.status = "finished";
      break;
    }

    case "ToolCallApprovalRequested":
      // A gated tool call is waiting for a human decision. Render an inline
      // approval prompt; remove it from pending when resolved/denied.
      vm.pendingApprovals.push({
        callId: p.call_id,
        toolName: p.tool_name,
        arguments: p.arguments || {},
        seq,
      });
      vm.turns.push({
        kind: "approval",
        seq,
        callId: p.call_id,
        toolName: p.tool_name,
      });
      break;

    case "ToolCallApprovalResolved":
      vm.pendingApprovals = vm.pendingApprovals.filter(
        (a) => a.callId !== p.call_id
      );
      vm.turns.push({
        kind: "approval_resolved",
        seq,
        callId: p.call_id,
        toolName: p.tool_name,
        approved: p.approved === true,
        reason: typeof p.reason === "string" ? p.reason : null,
      });
      break;

    case "UserQuestionRequested":
      vm.pendingQuestions.push({
        questionId: p.question_id,
        callId: p.call_id,
        questionCount: p.question_count || 0,
        reason: typeof p.reason === "string" ? p.reason : null,
        // The full question body (prompts + choices) lives in ContentStore
        // behind ``questions_ref`` (the envelope stays under the 4 KB cap). The
        // new protocol has no /tasks/{id} detail to project it, so we fold the
        // ref hash here and the data layer derefs it via /content/{hash} to
        // build the renderable question form.
        questionsRef:
          p.questions_ref && typeof p.questions_ref.hash === "string"
            ? p.questions_ref.hash
            : null,
        seq,
      });
      vm.turns.push({
        kind: "question",
        seq,
        questionId: p.question_id,
        questionCount: p.question_count || 0,
        reason: typeof p.reason === "string" ? p.reason : null,
      });
      break;

    case "UserQuestionAnswered":
      vm.pendingQuestions = vm.pendingQuestions.filter(
        (q) => q.questionId !== p.question_id
      );
      vm.turns.push({
        kind: "question_answered",
        seq,
        questionId: p.question_id,
        answerCount: p.answer_count || 0,
        answeredBy: typeof p.answered_by === "string" ? p.answered_by : null,
      });
      break;

    case "TaskStatePatched": {
      const activated = Array.isArray(p.patch?.activate_skills)
        ? p.patch.activate_skills.filter(
            (skill) => typeof skill === "string" && skill.trim()
          )
        : [];
      if (activated.length) {
        vm.turns.push({
          kind: "skill_loaded",
          seq,
          skills: activated,
        });
      }
      // CW18b: ``set_todos`` is a replace-all checklist of {id, content,
      // status}. Fold the latest snapshot into vm.todos so the composer's todo
      // strip always shows the model's current plan. The key is OMITTED from
      // the patch when None (a no-op), so only overwrite when it is present.
      if (Array.isArray(p.patch?.set_todos)) {
        vm.todos = p.patch.set_todos
          .filter((todo) => todo && typeof todo === "object")
          .map((todo) => ({
            id: typeof todo.id === "string" ? todo.id : "",
            content: typeof todo.content === "string" ? todo.content : "",
            status: typeof todo.status === "string" ? todo.status : "pending",
          }));
        vm.todoLog.push({ seq, todos: vm.todos });
      }
      break;
    }

    case "ToolCallDenied":
      vm.pendingApprovals = vm.pendingApprovals.filter(
        (a) => a.callId !== p.call_id
      );
      vm.turns.push({
        kind: "denied",
        seq,
        callId: p.call_id,
        toolName: p.tool_name,
        reason: typeof p.reason === "string" ? p.reason : null,
      });
      break;

    case "SubtaskSpawned": {
      const goal = typeof p.goal === "string" ? p.goal : null;
      vm.turns.push({
        kind: "subtask",
        seq,
        subtaskId: p.subtask_id,
        agentName: p.agent_name,
        goal,
      });
      // Preserve a terminal status already folded from an out-of-order stream;
      // a spawn normally precedes its completion, so default to running.
      const prior = vm.subtasks[p.subtask_id];
      const priorTerminal =
        prior && (prior.status === "completed" || prior.status === "failed");
      vm.subtasks[p.subtask_id] = {
        status: priorTerminal ? prior.status : "running",
        error: priorTerminal ? prior.error || null : null,
        agentName: p.agent_name,
        goal,
        seq,
      };
      break;
    }

    case "SubtaskCompleted": {
      // The parent stream carries the child's terminal SubtaskResult, so the
      // chip distinguishes success from failure — a failed child still arrives
      // here as SubtaskCompleted (not SubtaskFailed), with result.status="failed".
      const failed = p.result && p.result.status === "failed";
      const status = failed ? "failed" : "completed";
      const error =
        failed && typeof p.result.error === "string" ? p.result.error : null;
      const existing = vm.subtasks[p.subtask_id];
      if (existing) {
        existing.status = status;
        existing.error = error;
      } else
        vm.subtasks[p.subtask_id] = {
          status,
          error,
          agentName: null,
          goal: null,
          seq,
        };
      // No timeline turn: the existing SubtaskSpawned chip just flips state.
      break;
    }

    case "TaskCompleted":
      // The engine appends the terminal answer as the final assistant Message
      // (synthesizing one from the answer when the policy didn't attach its own
      // — noeta runtime _decision_handlers.handle_finish) BEFORE emitting
      // TaskCompleted, so by now that `message` turn is already folded in and
      // its prose paints a bubble. A separate `assistant_text` turn would
      // double-render the same text — visible only in the subtask drawer (the
      // root chat stays suspended on next-goal and never reaches TaskCompleted).
      // Surface the inline answer only as a fallback for the degenerate stream
      // where, against that engine contract, no message turn carried it, so the
      // answer is never silently dropped.
      if (!vm.turns.some((turn) => turn.kind === "message")) {
        vm.turns.push({
          kind: "assistant_text",
          seq,
          text: stringifyAnswer(p.answer),
        });
      }
      break;

    case "TaskFailed":
      vm.turns.push({
        kind: "lifecycle",
        seq,
        label: "failed",
        detail: typeof p.reason === "string" ? p.reason : null,
      });
      break;

    case "TaskSuspended":
      vm.turns.push({
        kind: "lifecycle",
        seq,
        label: "suspended",
        detail: typeof p.reason === "string" ? p.reason : null,
      });
      break;

    case "TaskCancelled":
      vm.turns.push({
        kind: "lifecycle",
        seq,
        label: "cancelled",
        detail: typeof p.reason === "string" ? p.reason : null,
      });
      break;

    // conversation rewind. The marker re-bases the timeline onto
    // the state folded at ``target_seq``: everything the dead
    // ``target_seq+1..M`` segment accreted (the rewound user message, its AI
    // output, any later turn) is dropped so the transcript stops cleanly before
    // the target (D9: like a WeChat message recall, no tombstone). We mirror the runtime fold's rebase by
    // pruning every projection keyed past the target. The events themselves stay
    // on the stream (append-only) — the reducer just doesn't render the dead
    // tail. A stream may carry several markers (repeated rewinds); each prunes
    // again from its own ``target_seq``.
    case "TaskRewound": {
      const target = typeof p.target_seq === "number" ? p.target_seq : seq;
      pruneDeadTail(vm, (s) => typeof s !== "number" || s <= target);
      break;
    }

    // Crash-recovery seal (StepAttemptAbandoned): the runtime folded the
    // state back to just BEFORE the interrupted attempt's
    // ``ContextPlanComposed`` (its ``abandoned_from_seq``) and re-based the
    // stream there — the TaskRewound pattern scoped to one attempt, with an
    // exclusive boundary. Mirror it so the dead partial attempt (e.g. a tool
    // call started but never finished) doesn't linger as a ghost projection;
    // the re-driven attempt's events land on the clean baseline. Like
    // TaskRewound, no tombstone turn (D9): an auto re-drive is silent, and
    // the park path's operator notice arrives as a normal message turn.
    case "StepAttemptAbandoned": {
      const from =
        typeof p.abandoned_from_seq === "number" ? p.abandoned_from_seq : seq;
      pruneDeadTail(vm, (s) => typeof s !== "number" || s < from);
      break;
    }

    // The runtime scheduled a live backoff for a transient LLM failure
    // (rate limit / flaky transport). Fold the latest attempt into vm.llmRetry
    // (the live "stalled, retrying" badge) and keep ONE warning turn per
    // call_id in the timeline, updated in place as attempts accumulate — the
    // durable record reads "this call was retried N times", not N rows.
    case "LLMRetryScheduled": {
      const retry = {
        callId: typeof p.call_id === "string" ? p.call_id : null,
        attempt: typeof p.attempt === "number" ? p.attempt : 0,
        maxRetries: typeof p.max_retries === "number" ? p.max_retries : 0,
        delaySeconds: typeof p.delay_seconds === "number" ? p.delay_seconds : 0,
        category: typeof p.category === "string" ? p.category : null,
        error: typeof p.error === "string" ? p.error : null,
        seq,
      };
      vm.llmRetry = retry;
      const existing = vm.turns.find(
        (turn) =>
          turn.kind === "warning" &&
          turn.label === "llm-retry" &&
          turn.callId === retry.callId
      );
      if (existing) {
        existing.attempt = retry.attempt;
        existing.maxRetries = retry.maxRetries;
        existing.delaySeconds = retry.delaySeconds;
        existing.error = retry.error;
      } else {
        vm.turns.push({
          kind: "warning",
          seq,
          label: "llm-retry",
          callId: retry.callId,
          attempt: retry.attempt,
          maxRetries: retry.maxRetries,
          delaySeconds: retry.delaySeconds,
          error: retry.error,
        });
      }
      break;
    }

    // The retry loop always ends by recording the call's response (recovered
    // or budget-exhausted error) — either way the backoff stall is over.
    case "LLMResponseRecorded":
      vm.llmRetry = null;
      break;

    // an enabled MCP server could not be connected and was
    // skipped at task start; the task ran with the remaining servers' tools.
    // Surface it as a warning turn so the user sees which connector failed and
    // why (the alias is a clean name; no credential ever rides the event).
    case "McpServerSkipped":
      vm.turns.push({
        kind: "warning",
        seq,
        label: "mcp-skipped",
        alias: typeof p.alias === "string" ? p.alias : null,
        detail: typeof p.reason === "string" ? p.reason : null,
      });
      break;

    // Conversation close / reopen ("No synthesized terminal"). A close is a lifecycle flag ORTHOGONAL to status —
    // we deliberately do NOT touch vm.status (a closed conversation stays
    // "waiting"); the renderer greys the closed session and the send gate adds a
    // reopen affordance. Reopen is advisory (a new goal works regardless) but we
    // still fold it so the flag tracks the latest lifecycle event.
    case "ConversationClosed":
      vm.closed = true;
      break;

    case "ConversationReopened":
      vm.closed = false;
      break;

    default:
      // Unknown / observability-only event types are folded into lastSeq /
      // status above but do not add a timeline turn. The reducer never
      // throws on an unrecognised type — forward-compatible by design.
      break;
  }
}

// Drop every projection whose seq the ``keep`` predicate rejects — the
// re-base shared by TaskRewound and StepAttemptAbandoned, the two
// snapshot-shaped markers that fold a dead tail out of the stream.
function pruneDeadTail(vm, keep) {
  vm.turns = vm.turns.filter((turn) => keep(turn.seq));
  vm.diffs = vm.diffs.filter((diff) => keep(diff.seq));
  vm.images = vm.images.filter((img) => keep(img.seq));
  vm.pendingApprovals = vm.pendingApprovals.filter((a) => keep(a.seq));
  vm.pendingQuestions = vm.pendingQuestions.filter((q) => keep(q.seq));
  for (const [callId, call] of Object.entries(vm.toolCalls)) {
    if (!keep(call.seq)) delete vm.toolCalls[callId];
  }
  for (const [subId, sub] of Object.entries(vm.subtasks)) {
    if (!keep(sub.seq)) delete vm.subtasks[subId];
  }
  if (vm.llmRetry && !keep(vm.llmRetry.seq)) vm.llmRetry = null;
  // The checklist is replace-all state, not an append log: restore the last
  // set_todos snapshot that survives the boundary (the runtime fold reverts
  // to exactly that state), else the dead attempt's todos would linger.
  vm.todoLog = vm.todoLog.filter((entry) => keep(entry.seq));
  vm.todos = vm.todoLog.length
    ? vm.todoLog[vm.todoLog.length - 1].todos
    : [];
}

// Render a TaskCompleted answer (string or canonical-tagged object) as
// readable text without throwing on an arbitrary shape.
function stringifyAnswer(answer) {
  if (answer == null) return "";
  if (typeof answer === "string") return answer;
  try {
    return JSON.stringify(answer);
  } catch (e) {
    return String(answer);
  }
}

// The single public entry point. Pure: same input → same output, no I/O.
function reduceEvents(envelopes) {
  const vm = emptyViewModel();
  if (!Array.isArray(envelopes)) return vm;
  for (const env of envelopes) {
    applyEnvelope(vm, env);
  }
  return vm;
}

// ES-module exports: loaded by the browser via <script type="module"> in
// chat.html and by the node-based reducer unit test (test_chat_reducer.py).
// One source file, one export surface — the renderer and the test import the
// SAME reducer, which is the whole point of keeping it framework-agnostic.
export { reduceEvents, emptyViewModel, applyEnvelope, classifyWakeOn, STATUS_FOR_TYPE };
