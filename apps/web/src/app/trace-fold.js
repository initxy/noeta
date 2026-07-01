// Pure folds for the trace inspector on the new thin-backend protocol
// (T7). The trace page used to read a server-side
// detail/context projection (GET /tasks/{id}, /tasks/{id}/context) and a flat
// GET /tasks tree; the thin backend has none of those — it serves one
// multiplexed SSE stream (GET /stream?task=<id>) carrying the task + subtree and
// raw blobs from GET /content/{hash}. So the trace tree, the per-task detail, and
// the context provenance (the old read_models.context_view) all fold on the
// frontend from the canonical envelope stream. Kept React-free for node --test.

"use strict";

import { reduceEvents } from "../domain/reducer.js";
import { deriveStatusText } from "./chat-fold.js";

// Bucket the multiplexed stream by task_id (subtree carried on one connection).
function bucketByTask(streamEnvelopes) {
  const byTask = new Map();
  for (const env of Array.isArray(streamEnvelopes) ? streamEnvelopes : []) {
    if (!env || typeof env.task_id !== "string") continue;
    if (!byTask.has(env.task_id)) byTask.set(env.task_id, []);
    byTask.get(env.task_id).push(env);
  }
  return byTask;
}

// The trace task-tree rows. The thin backend's GET /tasks returns ROOTS ONLY, so
// the tree (root → __workflow__ → workers) is folded from the stream instead;
// one row per task_id with the fields TaskTree reads (parent link from the
// genesis TaskCreated, status/closed from the reducer fold, event times for the
// sibling sort).
function foldTraceTasks(streamEnvelopes) {
  const rows = [];
  for (const [tid, envs] of bucketByTask(streamEnvelopes)) {
    const vm = reduceEvents(envs);
    let parent = null;
    let agent = null;
    let created = null;
    let last = null;
    let lastSeq = -1;
    for (const env of envs) {
      const at = typeof env.occurred_at === "number" ? env.occurred_at : null;
      if (at != null) {
        if (created == null || at < created) created = at;
        if (last == null || at > last) last = at;
      }
      if (typeof env.seq === "number" && env.seq > lastSeq) lastSeq = env.seq;
      if (env.type === "TaskCreated") {
        const p = env.payload || {};
        parent = typeof p.parent_task_id === "string" ? p.parent_task_id : null;
        agent = typeof p.agent_name === "string" ? p.agent_name : null;
      }
    }
    rows.push({
      task_id: tid,
      parent_task_id: parent,
      agent_name: agent,
      status: vm.status,
      closed: vm.closed,
      last_seq: lastSeq,
      created_event_time: created,
      last_event_time: last,
    });
  }
  return rows;
}

// One task's DetailTable view. Folds status / wake / closed / model / todos from
// the reducer (the thin backend has no /tasks/{id} detail); the extra optional
// fields the old detail carried (wake_on / phase / decisions / context_stats)
// simply stay absent and DetailTable renders only what is present.
function foldTraceDetail(taskId, taskEnvelopes) {
  if (!taskId) return null;
  const envs = Array.isArray(taskEnvelopes) ? taskEnvelopes : [];
  const vm = reduceEvents(envs);
  let agent = null;
  let goal = null;
  for (const env of envs) {
    if (env && env.type === "TaskCreated") {
      const p = env.payload || {};
      agent = typeof p.agent_name === "string" ? p.agent_name : null;
      if (typeof p.goal === "string") goal = p.goal;
    }
  }
  return {
    task_id: taskId,
    status: vm.status,
    status_text: deriveStatusText(vm),
    wake_kind: vm.wakeKind,
    closed: vm.closed,
    model: vm.model,
    model_binding: vm.model,
    agent,
    goal,
    event_count: envs.length,
    last_seq: vm.lastSeq,
    todos: vm.todos,
  };
}

// The per-turn selection provenance — folded INLINE from each LLMRequestStarted
// (the request_ref anchor + the optional MessageSelection counts ride the
// payload, not behind a ref), mirroring read_models.context_view._selection_view.
function foldSelections(taskEnvelopes) {
  const out = [];
  for (const env of Array.isArray(taskEnvelopes) ? taskEnvelopes : []) {
    if (!env || env.type !== "LLMRequestStarted") continue;
    const p = env.payload || {};
    const sel = p.selection || {};
    const ref = p.request_ref || {};
    out.push({
      seq: typeof env.seq === "number" ? env.seq : 0,
      call_id: typeof p.call_id === "string" ? p.call_id : "",
      model: typeof p.model === "string" ? p.model : "",
      request_ref: {
        hash: ref.hash ?? null,
        bytes: ref.size ?? null,
        media_type: ref.media_type ?? null,
      },
      input_tokens: Number(p.input_tokens) || 0,
      strategy: typeof sel.strategy === "string" ? sel.strategy : "",
      candidates: Number(sel.candidates) || 0,
      selected: Number(sel.selected) || 0,
      dropped: Number(sel.dropped) || 0,
      limit: Number(sel.limit) || 0,
    });
  }
  return out;
}

// The plan-ref anchors (one per ContextPlanComposed) the trace must deref via
// /content/{hash} to fill the provenance plan view (the plan body is behind
// plan_ref, never inline — single-writer ContextPlan in ContentStore).
function planRefs(taskEnvelopes) {
  const out = [];
  for (const env of Array.isArray(taskEnvelopes) ? taskEnvelopes : []) {
    if (!env || env.type !== "ContextPlanComposed") continue;
    const ref = (env.payload && env.payload.plan_ref) || null;
    out.push({
      seq: typeof env.seq === "number" ? env.seq : 0,
      occurred_at: typeof env.occurred_at === "number" ? env.occurred_at : 0,
      plan_ref: {
        hash: ref && typeof ref.hash === "string" ? ref.hash : null,
        bytes: ref ? (ref.size ?? null) : null,
        media_type: ref ? (ref.media_type ?? null) : null,
      },
    });
  }
  return out;
}

function refSummary(r) {
  return {
    hash: r && r.hash != null ? r.hash : null,
    bytes: r && r.size != null ? r.size : null,
    media_type: r && r.media_type != null ? r.media_type : null,
  };
}

function resourceSummary(r) {
  const base = r && typeof r === "object" ? r : {};
  const cref = base.content_ref || null;
  return {
    ...base,
    reason: base.reason ?? null,
    hash: cref && cref.hash != null ? cref.hash : null,
    bytes: base.bytes ?? (cref ? cref.size ?? null : null),
    media_type: base.media_type ?? (cref ? cref.media_type ?? null : null),
  };
}

// Project a derefed ContextPlan canonical body (raw /content text) into the plan
// view shape the provenance panel reads — mirrors
// read_models.context_view._context_plan_view (ref summaries, never bodies). A
// missing / non-ContextPlan body is flagged via decode_error, never shown as a
// valid empty plan.
function planView(planMeta, bodyText) {
  const { seq, occurred_at, plan_ref } = planMeta;
  const errored = (reason) => ({
    seq,
    occurred_at,
    plan_ref,
    composer_version: "",
    segment_hashes: {},
    selected_skills: [],
    retrieved_resources: [],
    selected_messages: [],
    dropped_messages: [],
    decode_error: reason,
  });
  if (bodyText == null) return errored("unreadable plan_ref");
  let obj = null;
  try {
    obj = JSON.parse(bodyText);
  } catch (e) {
    return errored("undecodable plan body");
  }
  if (!obj || obj.__canonical_tag__ !== "context_plan") {
    return errored("not a ContextPlan");
  }
  return {
    seq,
    occurred_at,
    plan_ref,
    composer_version:
      typeof obj.composer_version === "string" ? obj.composer_version : "",
    segment_hashes:
      obj.segment_hashes && typeof obj.segment_hashes === "object"
        ? obj.segment_hashes
        : {},
    selected_skills: Array.isArray(obj.selected_skills)
      ? obj.selected_skills
      : [],
    retrieved_resources: Array.isArray(obj.retrieved_resources)
      ? obj.retrieved_resources.map(resourceSummary)
      : [],
    selected_messages: Array.isArray(obj.selected_messages)
      ? obj.selected_messages.map(refSummary)
      : [],
    dropped_messages: Array.isArray(obj.dropped_messages)
      ? obj.dropped_messages.map(refSummary)
      : [],
    decode_error: null,
  };
}

export {
  foldTraceTasks,
  foldTraceDetail,
  foldSelections,
  planRefs,
  planView,
};
