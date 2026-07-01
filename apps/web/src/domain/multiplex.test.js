// Unit tests for the multiplexed-stream fold (T7). Node's builtin runner:
//   node --test src/domain/multiplex.test.js
//
// Covers: per-task bucketing + seq dedup/order on an INTERLEAVED stream; the
// subtask tree derived from genesis parent links; sibling ordering by genesis
// seq; root inference; and that each task's vm matches the single-task reducer.

import assert from "node:assert/strict";
import { test } from "node:test";

import { reduceEvents } from "./reducer.js";
import {
  advanceMultiplexStore,
  bucketByTask,
  createMultiplexStore,
  genesisSeq,
  parentOf,
  reduceMultiplexed,
} from "./multiplex.js";

// Minimal canonical-ish envelopes (only the fields the fold reads).
const created = (taskId, seq, parent = null) => ({
  task_id: taskId,
  seq,
  type: "TaskCreated",
  payload: parent ? { parent_task_id: parent } : {},
});
const started = (taskId, seq) => ({
  task_id: taskId,
  seq,
  type: "TaskStarted",
  payload: {},
});
const spawned = (taskId, seq, subtaskId, agent) => ({
  task_id: taskId,
  seq,
  type: "SubtaskSpawned",
  payload: { subtask_id: subtaskId, agent_name: agent, goal: "g" },
});
const appended = (taskId, seq) => ({
  task_id: taskId,
  seq,
  type: "MessagesAppended",
  payload: { count: 1, messages_ref: { hash: `h${seq}` } },
});
const toolStarted = (taskId, seq, callId, name) => ({
  task_id: taskId,
  seq,
  type: "ToolCallStarted",
  payload: { call_id: callId, tool_name: name, arguments: {} },
});
const toolResult = (taskId, seq, callId) => ({
  task_id: taskId,
  seq,
  type: "ToolResultRecorded",
  payload: { call_id: callId, success: true, summary: "ok" },
});

// Drive the store the way chat-data does: streamEnvelopes grows by concat per
// SSE message, and advance is called with the growing array each time.
function feedIncrementally(stream, root) {
  const store = createMultiplexStore();
  let acc = [];
  let mux;
  for (const env of stream) {
    acc = acc.concat([env]);
    mux = advanceMultiplexStore(store, acc, root);
  }
  return { store, mux, acc };
}

test("bucketByTask: interleaved stream splits per task, seq-ordered + deduped", () => {
  const stream = [
    created("root", 0),
    created("sub", 0, "root"),
    started("root", 1),
    started("sub", 1),
    // a duplicate seq (resend) — last write wins, no double entry
    { task_id: "root", seq: 1, type: "TaskStarted", payload: { dup: true } },
  ];
  const buckets = bucketByTask(stream);
  assert.deepEqual([...buckets.keys()].sort(), ["root", "sub"]);
  const rootEvs = buckets.get("root");
  assert.deepEqual(
    rootEvs.map((e) => e.seq),
    [0, 1],
  );
  assert.equal(rootEvs[1].payload.dup, true); // deduped to the later copy
});

test("parentOf / genesisSeq read the genesis TaskCreated", () => {
  const evs = [created("sub", 5, "root"), started("sub", 6)];
  assert.equal(parentOf(evs), "root");
  assert.equal(genesisSeq(evs), 5);
  assert.equal(parentOf([started("x", 1)]), null); // no genesis yet
});

test("reduceMultiplexed: per-task vms match the single-task reducer", () => {
  const rootEvs = [created("root", 0), started("root", 1)];
  const subEvs = [created("sub", 0, "root"), started("sub", 1)];
  const mux = reduceMultiplexed([...rootEvs, ...subEvs], "root");
  assert.deepEqual(mux.tasks.root, reduceEvents(rootEvs));
  assert.deepEqual(mux.tasks.sub, reduceEvents(subEvs));
});

test("reduceMultiplexed: subtask tree from parent links, siblings by genesis seq", () => {
  const stream = [
    created("root", 0),
    spawned("root", 1, "b", "worker"),
    spawned("root", 2, "a", "worker"),
    // child "a" has the EARLIER genesis seq (10 < 11) so it sorts first
    created("a", 11, "root"),
    created("b", 10, "root"),
  ];
  const mux = reduceMultiplexed(stream, "root");
  assert.equal(mux.root, "root");
  assert.deepEqual(mux.parents, { root: null, a: "root", b: "root" });
  assert.deepEqual(mux.children.root, ["b", "a"]); // b genesis 10 < a genesis 11
});

test("reduceMultiplexed: root inferred as the parent-less task when not given", () => {
  const stream = [created("sub", 0, "root"), created("root", 0)];
  const mux = reduceMultiplexed(stream);
  assert.equal(mux.root, "root");
  // order is root-first then by genesis seq.
  assert.equal(mux.order[0], "root");
});

test("reduceMultiplexed: empty / malformed input is inert", () => {
  const mux = reduceMultiplexed([]);
  assert.equal(mux.root, null);
  assert.deepEqual(mux.tasks, {});
  assert.deepEqual(mux.order, []);
  assert.deepEqual(reduceMultiplexed(null).order, []);
});

// --- Incremental store (WS-A / P0-1, P0-2) ---------------------------------

test("advanceMultiplexStore: incremental fold matches reduceMultiplexed", () => {
  const stream = [
    created("root", 0),
    started("root", 1),
    spawned("root", 2, "sub", "worker"),
    created("sub", 3, "root"),
    started("sub", 4),
    appended("root", 5),
    appended("sub", 6),
    appended("root", 7),
  ];
  const { mux } = feedIncrementally(stream, "root");
  const pure = reduceMultiplexed(stream, "root");
  assert.deepEqual(mux.tasks, pure.tasks);
  assert.deepEqual(mux.parents, pure.parents);
  assert.deepEqual(mux.children, pure.children);
  assert.deepEqual(mux.order, pure.order);
  assert.equal(mux.root, pure.root);
  // per-task envelope buckets are exposed too, seq-ordered
  assert.deepEqual(
    mux.eventsByTask.root.map((e) => e.seq),
    [0, 1, 2, 5, 7],
  );
  assert.deepEqual(
    mux.eventsByTask.sub.map((e) => e.seq),
    [3, 4, 6],
  );
});

test("advanceMultiplexStore: unchanged tasks keep vm + events identity", () => {
  const store = createMultiplexStore();
  let acc = [created("root", 0), started("root", 1), created("sub", 2, "root")];
  let mux = advanceMultiplexStore(store, acc, "root");
  const rootVm = mux.tasks.root;
  const rootEvents = mux.eventsByTask.root;
  // an envelope only for "sub" must not churn "root"'s references (so its
  // bubbles' React.memo holds while a subtask streams).
  acc = acc.concat([started("sub", 3)]);
  mux = advanceMultiplexStore(store, acc, "root");
  assert.equal(mux.tasks.root, rootVm);
  assert.equal(mux.eventsByTask.root, rootEvents);
  // "sub" did change → fresh identity + correct fold
  assert.notEqual(mux.tasks.sub, undefined);
  assert.equal(mux.tasks.sub.status, "running");
});

test("advanceMultiplexStore: out-of-order tail still matches pure fold", () => {
  // seq 2 arrives before seq 1 for the same task → slow-path rebuild
  const stream = [created("root", 0), started("root", 2), appended("root", 1)];
  const { mux } = feedIncrementally(stream, "root");
  assert.deepEqual(mux.tasks.root, reduceMultiplexed(stream, "root").tasks.root);
  assert.deepEqual(
    mux.eventsByTask.root.map((e) => e.seq),
    [0, 1, 2],
  );
});

test("advanceMultiplexStore: stream cleared then refilled drops the stale bucket", () => {
  const store = createMultiplexStore();
  let mux = advanceMultiplexStore(store, [created("a", 0), started("a", 1)], "a");
  assert.deepEqual(Object.keys(mux.tasks), ["a"]);
  // session switch: stream resets to [] then a new conversation streams in
  advanceMultiplexStore(store, [], "b");
  const newStream = [created("b", 0), started("b", 1)];
  mux = advanceMultiplexStore(store, newStream, "b");
  assert.deepEqual(mux.tasks, reduceMultiplexed(newStream, "b").tasks);
  assert.deepEqual(Object.keys(mux.tasks), ["b"]);
});

test("advanceMultiplexStore: same input array returns the cached snapshot", () => {
  const store = createMultiplexStore();
  const acc = [created("root", 0), started("root", 1)];
  const a = advanceMultiplexStore(store, acc, "root");
  // a re-invoke with the SAME array ref (React StrictMode double-invoke) must
  // not re-fold; it returns the identical snapshot object.
  const b = advanceMultiplexStore(store, acc, "root");
  assert.equal(a, b);
});

// [blocking 1] regression: a duplicate seq that overwrites the genesis TaskCreated
// (last-write-wins) must drop the parent link in the incremental store exactly
// as the pure fold does — metadata is recomputed from the deduped list, not
// accumulated from raw envelopes.
test("advanceMultiplexStore: duplicate seq overwriting TaskCreated drops the parent", () => {
  const stream = [
    created("root", 0),
    created("sub", 1, "root"), // sub genesis: seq 1, parent root
    started("sub", 1), // duplicate seq 1 → last-write-wins erases the genesis
  ];
  const { mux } = feedIncrementally(stream, "root");
  const pure = reduceMultiplexed(stream, "root");
  assert.equal(mux.parents.sub, null);
  assert.deepEqual(mux.parents, pure.parents);
  assert.deepEqual(mux.children, pure.children); // sub no longer a child of root
  assert.deepEqual(mux.order, pure.order);
  assert.deepEqual(mux.tasks, pure.tasks);
});

// [blocking 1] regression: a duplicate TaskCreated at the same seq re-parents the
// task (last-write-wins), and the subtask tree must re-base accordingly.
test("advanceMultiplexStore: duplicate TaskCreated re-parents and re-bases the tree", () => {
  const stream = [
    created("root", 0),
    created("other", 1),
    created("sub", 2, "root"),
    created("sub", 2, "other"), // same seq → parent becomes "other"
  ];
  const { mux } = feedIncrementally(stream, "root");
  const pure = reduceMultiplexed(stream, "root");
  assert.equal(mux.parents.sub, "other");
  assert.deepEqual(mux.parents, pure.parents);
  assert.deepEqual(mux.children, pure.children);
});

// [general] regression: the same envelopes array re-folded under a DIFFERENT root
// must re-resolve the tree, not return the cached snapshot for the old root.
test("advanceMultiplexStore: same array but changed root re-resolves the tree", () => {
  const store = createMultiplexStore();
  const envs = [created("a", 0), created("b", 1)]; // two parent-less tasks
  const muxA = advanceMultiplexStore(store, envs, "a");
  assert.equal(muxA.root, "a");
  assert.equal(muxA.order[0], "a");
  const muxB = advanceMultiplexStore(store, envs, "b"); // SAME array ref, new root
  assert.equal(muxB.root, "b");
  assert.equal(muxB.order[0], "b");
  assert.deepEqual(muxB.tasks, reduceMultiplexed(envs, "b").tasks);
});

// [blocking 2] regression: a snapshot already handed out must never be mutated by a
// later advance. applyEnvelope mutates an existing toolCall / subtask in place,
// so cloneViewModel must copy those value objects one level deep.
test("advanceMultiplexStore: a returned snapshot is frozen against later advances", () => {
  const store = createMultiplexStore();
  let acc = [created("root", 0), toolStarted("root", 1, "c1", "read")];
  const snap1 = advanceMultiplexStore(store, acc, "root");
  assert.equal(snap1.tasks.root.toolCalls.c1.status, "started");

  acc = acc.concat([toolResult("root", 2, "c1")]);
  const snap2 = advanceMultiplexStore(store, acc, "root");
  // the new snapshot reflects the result...
  assert.equal(snap2.tasks.root.toolCalls.c1.status, "recorded");
  assert.equal(snap2.tasks.root.toolCalls.c1.success, true);
  // ...while the first snapshot stays exactly as it was returned (the started
  // state carries success: null until a result lands).
  assert.equal(snap1.tasks.root.toolCalls.c1.status, "started");
  assert.equal(snap1.tasks.root.toolCalls.c1.success, null);
  // distinct object identities across snapshots (no shared nested value)
  assert.notEqual(snap1.tasks.root.toolCalls.c1, snap2.tasks.root.toolCalls.c1);
});

// [blocking 2] regression for subtasks: SubtaskCompleted mutates an existing subtask
// in place, so an earlier snapshot's subtask status must stay "running".
test("advanceMultiplexStore: subtask status change does not mutate an old snapshot", () => {
  const store = createMultiplexStore();
  let acc = [created("root", 0), spawned("root", 1, "s1", "worker")];
  const snap1 = advanceMultiplexStore(store, acc, "root");
  assert.equal(snap1.tasks.root.subtasks.s1.status, "running");

  acc = acc.concat([
    {
      task_id: "root",
      seq: 2,
      type: "SubtaskCompleted",
      payload: { subtask_id: "s1", result: { status: "completed" } },
    },
  ]);
  const snap2 = advanceMultiplexStore(store, acc, "root");
  assert.equal(snap2.tasks.root.subtasks.s1.status, "completed");
  assert.equal(snap1.tasks.root.subtasks.s1.status, "running");
});
