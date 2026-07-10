// node --test coverage for the trace inspector's new-protocol folds
// (T7): tree / detail / selections / plan view,
// folded from the multiplexed envelope stream + derefed plan bodies.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  createTraceTaskCache,
  foldTraceTasksFromMux,
  foldTraceDetail,
  foldSelections,
  planRefs,
  planView,
} from "./trace-fold.js";
import {
  advanceMultiplexStore,
  createMultiplexStore,
} from "../domain/multiplex.js";

// foldTraceTasksFromMux reads the SAME incremental store the chat app uses
// (domain/multiplex.js), so these tests drive it through the real
// `advanceMultiplexStore` — the exact object trace-data.js hands it — rather
// than hand-rolling a fixture shape that could drift from the real one.
function foldTraceTasks(streamEnvelopes) {
  const mux = advanceMultiplexStore(createMultiplexStore(), streamEnvelopes, null);
  return foldTraceTasksFromMux(createTraceTaskCache(), mux);
}

const genesis = (taskId, parent, agent, seq, at) => ({
  task_id: taskId,
  type: "TaskCreated",
  seq,
  occurred_at: at,
  payload: { parent_task_id: parent, agent_name: agent, goal: "do a thing" },
});

test("foldTraceTasks builds one row per task with parent/agent/status from the stream", () => {
  const stream = [
    genesis("root", null, "main", 0, 100),
    { task_id: "root", type: "TaskStarted", seq: 1, occurred_at: 101, payload: {} },
    genesis("w1", "root", "__workflow__", 0, 102),
    { task_id: "w1", type: "TaskCompleted", seq: 1, occurred_at: 103, payload: { answer: "ok" } },
  ];
  const rows = foldTraceTasks(stream);
  const byId = Object.fromEntries(rows.map((r) => [r.task_id, r]));
  assert.equal(rows.length, 2);
  assert.equal(byId.root.parent_task_id, null);
  assert.equal(byId.root.agent_name, "main");
  assert.equal(byId.root.status, "running");
  assert.equal(byId.w1.parent_task_id, "root");
  assert.equal(byId.w1.agent_name, "__workflow__");
  assert.equal(byId.w1.status, "completed");
  assert.equal(byId.w1.created_event_time, 102);
  assert.equal(byId.w1.last_event_time, 103);
});

test("foldTraceDetail folds status/wake/model/agent/goal for a task", () => {
  const envs = [
    genesis("root", null, "main", 0, 100),
    { task_id: "root", type: "ModelBound", seq: 1, payload: { model: "opus-x" } },
    {
      task_id: "root",
      type: "TaskSuspended",
      seq: 2,
      payload: {
        wake_on: { __canonical_tag__: "human_response", handle: "noeta-code-next-goal" },
      },
    },
  ];
  const d = foldTraceDetail("root", envs);
  assert.equal(d.status, "waiting");
  assert.equal(d.wake_kind, "next-goal");
  assert.equal(d.model, "opus-x");
  assert.equal(d.model_binding, "opus-x");
  assert.equal(d.agent, "main");
  assert.equal(d.goal, "do a thing");
  assert.equal(d.event_count, 3);
  assert.equal(foldTraceDetail(null, envs), null);
});

test("foldSelections reads the per-turn request_ref anchor + inline counts", () => {
  const envs = [
    {
      task_id: "root",
      type: "LLMRequestStarted",
      seq: 5,
      payload: {
        call_id: "c1",
        model: "opus-x",
        input_tokens: 1200,
        request_ref: { hash: "r".repeat(64), size: 4096, media_type: "application/json" },
        selection: { strategy: "tail", candidates: 10, selected: 8, dropped: 2, limit: 8 },
      },
    },
    { task_id: "root", type: "ModelBound", seq: 6, payload: { model: "opus-x" } },
  ];
  const sels = foldSelections(envs);
  assert.equal(sels.length, 1);
  assert.equal(sels[0].call_id, "c1");
  assert.equal(sels[0].request_ref.hash, "r".repeat(64));
  assert.equal(sels[0].request_ref.bytes, 4096);
  assert.equal(sels[0].input_tokens, 1200);
  assert.equal(sels[0].strategy, "tail");
  assert.equal(sels[0].selected, 8);
  assert.equal(sels[0].dropped, 2);
});

test("planRefs collects ContextPlanComposed plan_ref anchors", () => {
  const envs = [
    {
      task_id: "root",
      type: "ContextPlanComposed",
      seq: 3,
      occurred_at: 200,
      payload: { plan_ref: { hash: "p".repeat(64), size: 512, media_type: "application/json" } },
    },
  ];
  const refs = planRefs(envs);
  assert.equal(refs.length, 1);
  assert.equal(refs[0].seq, 3);
  assert.equal(refs[0].plan_ref.hash, "p".repeat(64));
  assert.equal(refs[0].plan_ref.bytes, 512);
});

test("planView projects a derefed ContextPlan body; flags decode errors", () => {
  const meta = { seq: 3, occurred_at: 200, plan_ref: { hash: "p".repeat(64), bytes: 512, media_type: null } };
  const body = JSON.stringify({
    __canonical_tag__: "context_plan",
    composer_version: "v3",
    segment_hashes: { sys: "abc" },
    selected_skills: ["py", "git"],
    retrieved_resources: [
      { reason: "mention", content_ref: { hash: "c".repeat(64), size: 80, media_type: "text/plain" }, relpath: "a.txt" },
    ],
    selected_messages: [{ hash: "m".repeat(64), size: 10, media_type: "application/json" }],
    dropped_messages: [],
  });
  const v = planView(meta, body);
  assert.equal(v.decode_error, null);
  assert.equal(v.composer_version, "v3");
  assert.deepEqual(v.selected_skills, ["py", "git"]);
  assert.equal(v.retrieved_resources[0].hash, "c".repeat(64));
  assert.equal(v.retrieved_resources[0].relpath, "a.txt");
  assert.equal(v.selected_messages[0].hash, "m".repeat(64));
  assert.equal(v.dropped_messages.length, 0);
  // Faults are flagged, never shown as a valid empty plan.
  assert.equal(planView(meta, null).decode_error, "unreadable plan_ref");
  assert.equal(planView(meta, "not json").decode_error, "undecodable plan body");
  assert.equal(planView(meta, JSON.stringify({ foo: 1 })).decode_error, "not a ContextPlan");
});
