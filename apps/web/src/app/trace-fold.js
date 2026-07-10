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

// ---------------------------------------------------------------------------
// The trace task-tree rows, folded off the SHARED incremental multiplex store
// (domain/multiplex.js's `advanceMultiplexStore`) instead of re-bucketing +
// re-reducing the WHOLE subtree stream on every SSE message. The store already
// gives us, per task_id, the reduced view-model (`mux.tasks[tid]`) and the
// deduped/ordered envelope bucket (`mux.eventsByTask[tid]`), with BOTH
// references reused across an advance for any task that received no new
// envelope this round. `foldTraceTasksFromMux` only needs to extend that with
// the couple of fields the shared vm doesn't carry (agent_name genesis,
// created/last event-time bounds) — done via a per-task cache keyed on the
// store's own bucket identity, so a task whose bucket is unchanged this
// advance is a pure cache hit, never a rescan.
// ---------------------------------------------------------------------------

// One cache entry per task_id: the last envelope bucket it was scanned
// against (`envs`/`len`) plus the accumulated agent_name / goal / event-time
// bounds / last_seq, and the assembled row (invalidated to null whenever the
// bucket changes so the caller rebuilds it once).
function createTraceTaskCache() {
  return { byTask: new Map() };
}

// Scan envelopes [start, end) into the running accumulator (created/last event
// time bounds, highest seq, genesis agent_name/goal — the genesis event is
// unique per task but we mirror the old scan-to-the-end semantics so a
// malformed multi-genesis stream keeps resolving to the LAST TaskCreated seen,
// exactly like the previous full-stream fold did).
function scanTraceEnvRange(envs, start, end, acc) {
  for (let i = start; i < end; i += 1) {
    const env = envs[i];
    if (!env) continue;
    const at = typeof env.occurred_at === "number" ? env.occurred_at : null;
    if (at != null) {
      if (acc.created == null || at < acc.created) acc.created = at;
      if (acc.last == null || at > acc.last) acc.last = at;
    }
    if (typeof env.seq === "number" && env.seq > acc.lastSeq) acc.lastSeq = env.seq;
    if (env.type === "TaskCreated") {
      const p = env.payload || {};
      acc.agent = typeof p.agent_name === "string" ? p.agent_name : null;
      acc.goal = typeof p.goal === "string" ? p.goal : null;
    }
  }
}

// Refresh one task's cache entry against its CURRENT envelope bucket. The
// common live-stream case is a plain append (the multiplex store's fast path:
// `st.envs = st.envs.concat(newEnvs)`), verified cheaply via the boundary
// element so only the NEW tail is scanned; anything else (dedup / reorder /
// shrink — the store's slow path, or a stream reset) re-scans from scratch so
// the result stays byte-identical to a full re-fold.
function refreshTraceTaskEntry(entry, envs) {
  if (envs === entry.envs) return entry;
  const prefixIntact =
    entry.len > 0 &&
    entry.len <= envs.length &&
    envs[entry.len - 1] === entry.envs[entry.len - 1];
  if (entry.len === 0 || prefixIntact) {
    scanTraceEnvRange(envs, entry.len, envs.length, entry);
  } else {
    entry.created = null;
    entry.last = null;
    entry.lastSeq = -1;
    entry.agent = null;
    entry.goal = null;
    scanTraceEnvRange(envs, 0, envs.length, entry);
  }
  entry.envs = envs;
  entry.len = envs.length;
  entry.row = null;
  return entry;
}

function traceTaskEntry(cache, tid, envs) {
  let entry = cache.byTask.get(tid);
  if (!entry) {
    entry = {
      envs: [],
      len: 0,
      agent: null,
      goal: null,
      created: null,
      last: null,
      lastSeq: -1,
      row: null,
    };
    cache.byTask.set(tid, entry);
  }
  return refreshTraceTaskEntry(entry, envs);
}

// The trace task-tree rows. The thin backend's GET /tasks returns ROOTS ONLY,
// so the tree (root → __workflow__ → workers) is folded from the stream
// instead; one row per task_id with the fields TaskTree reads (parent link
// from the genesis TaskCreated, status/closed from the reducer fold, event
// times for the sibling sort). `mux` is the object `advanceMultiplexStore`
// returns; `cache` is a `createTraceTaskCache()` instance the caller keeps
// across advances (a React ref in trace-data.js).
function foldTraceTasksFromMux(cache, mux) {
  const tasksVm = (mux && mux.tasks) || {};
  const eventsByTask = (mux && mux.eventsByTask) || {};
  const parents = (mux && mux.parents) || {};
  const rows = [];
  for (const tid of Object.keys(tasksVm)) {
    const envs = eventsByTask[tid] || [];
    const entry = traceTaskEntry(cache, tid, envs);
    if (!entry.row) {
      const vm = tasksVm[tid];
      entry.row = {
        task_id: tid,
        parent_task_id: parents[tid] ?? null,
        agent_name: entry.agent,
        status: vm.status,
        closed: vm.closed,
        last_seq: entry.lastSeq,
        created_event_time: entry.created,
        last_event_time: entry.last,
      };
    }
    rows.push(entry.row);
  }
  return rows;
}

// One task's DetailTable view. Folds status / wake / closed / model / todos from
// the reducer (the thin backend has no /tasks/{id} detail); the extra optional
// fields the old detail carried (wake_on / phase / decisions / context_stats)
// simply stay absent and DetailTable renders only what is present.
//
// `vm` is an OPTIONAL already-folded ConversationViewModel (the multiplex
// store's `mux.tasks[taskId]`, kept identity-stable across SSE messages that
// don't touch this task) — when the caller already has one there is no need
// to pay for a second `reduceEvents(envs)` pass over the same envelopes. Callers
// that only have the raw envelopes (e.g. this file's own tests) omit it and get
// the previous self-contained behavior.
function foldTraceDetail(taskId, taskEnvelopes, vm = null) {
  if (!taskId) return null;
  const envs = Array.isArray(taskEnvelopes) ? taskEnvelopes : [];
  const resolved = vm || reduceEvents(envs);
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
    status: resolved.status,
    status_text: deriveStatusText(resolved),
    wake_kind: resolved.wakeKind,
    closed: resolved.closed,
    model: resolved.model,
    model_binding: resolved.model,
    agent,
    goal,
    event_count: envs.length,
    last_seq: resolved.lastSeq,
    todos: resolved.todos,
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
  createTraceTaskCache,
  foldTraceTasksFromMux,
  foldTraceDetail,
  foldSelections,
  planRefs,
  planView,
};
