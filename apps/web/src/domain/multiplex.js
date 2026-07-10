// Multiplexed-stream fold (T7).
//
// The new task protocol (T5) delivers ONE SSE stream per conversation carrying
// the root Task AND all its subtasks' EventEnvelopes interleaved
// (GET /stream?task=<root>); the frontend demultiplexes by `task_id`. This
// module is the pure folding layer on top of the single-task `reduceEvents`:
//
//     reduceMultiplexed(EventEnvelope[], root) -> MultiplexViewModel
//
// It NEVER invents an event schema — it only buckets the canonical envelope
// stream by `task_id`, dedups + orders each bucket by `seq`, folds each through
// the SAME `reduceEvents` the single-task view uses, and derives the subtask
// tree from each task's genesis `TaskCreated.parent_task_id`. Keeping the fold
// shared means the drilled-in subtask view and the root view are byte-for-byte
// the same projection logic (the whole point of D7: the frontend folds the raw
// stream; the backend stays thin).
//
// The view-model shape:
//
//   {
//     root:     string | null,          // the conversation root task_id
//     tasks:    { [taskId]: ConversationViewModel },  // one vm per task
//     parents:  { [taskId]: parentTaskId | null },    // genesis parent link
//     children: { [taskId]: childTaskId[] },          // seq-ordered subtasks
//     order:    string[],               // every taskId, root-first then by genesis seq
//   }
//
// A subtask drill-in is then just "render tasks[subtaskId]" — no second SSE,
// no second fold path.

"use strict";

import { applyEnvelope, emptyViewModel, reduceEvents } from "./reducer.js";

// Bucket a multiplexed envelope list into per-task, seq-deduped, seq-ordered
// streams. Envelopes for different tasks interleave on one connection; an
// envelope with a missing/duplicate `seq` is handled the same way `mergeEvents`
// does for a single task (no-seq kept in arrival order; seq-keyed deduped,
// last-write-wins, then sorted).
function bucketByTask(envelopes) {
  const bySeq = new Map(); // taskId -> Map<seq, env>
  const noSeq = new Map(); // taskId -> env[]
  for (const env of Array.isArray(envelopes) ? envelopes : []) {
    if (!env || typeof env.task_id !== "string") continue;
    const tid = env.task_id;
    if (typeof env.seq === "number") {
      if (!bySeq.has(tid)) bySeq.set(tid, new Map());
      bySeq.get(tid).set(env.seq, env);
    } else {
      if (!noSeq.has(tid)) noSeq.set(tid, []);
      noSeq.get(tid).push(env);
    }
  }
  const out = new Map();
  const taskIds = new Set([...bySeq.keys(), ...noSeq.keys()]);
  for (const tid of taskIds) {
    const ordered = Array.from((bySeq.get(tid) || new Map()).values()).sort(
      (a, b) => a.seq - b.seq,
    );
    out.set(tid, (noSeq.get(tid) || []).concat(ordered));
  }
  return out;
}

// The parent_task_id from a task's genesis TaskCreated (null for the root or a
// stream whose genesis has not yet arrived).
function parentOf(taskEnvelopes) {
  for (const env of taskEnvelopes) {
    if (env && env.type === "TaskCreated") {
      const p = env.payload || {};
      return typeof p.parent_task_id === "string" ? p.parent_task_id : null;
    }
  }
  return null;
}

// The genesis seq of a task's stream (its lowest seq) — used to order siblings
// deterministically (a subtask spawned earlier sorts first). A stream with no
// numeric seq sorts last (Infinity).
function genesisSeq(taskEnvelopes) {
  let min = Infinity;
  for (const env of taskEnvelopes) {
    if (env && typeof env.seq === "number" && env.seq < min) min = env.seq;
  }
  return min;
}

// Fold a multiplexed envelope stream into per-task view-models + the subtask
// tree. `root` is the conversation root task_id (the one the SSE was opened
// for); when omitted it is inferred as the parent-less task in the stream.
function reduceMultiplexed(envelopes, root = null) {
  const buckets = bucketByTask(envelopes);
  const tasks = {};
  const parents = {};
  const children = {};
  const genesis = {};
  for (const [tid, evs] of buckets) {
    tasks[tid] = reduceEvents(evs);
    parents[tid] = parentOf(evs);
    genesis[tid] = genesisSeq(evs);
  }
  // Infer the root when not given: the (first) task with no parent in-stream.
  let resolvedRoot = root;
  if (resolvedRoot === null) {
    for (const tid of buckets.keys()) {
      if (!parents[tid]) {
        resolvedRoot = tid;
        break;
      }
    }
  }
  // Build the children index, ordering siblings by genesis seq.
  for (const tid of buckets.keys()) {
    const parent = parents[tid];
    if (parent && Object.prototype.hasOwnProperty.call(tasks, parent)) {
      (children[parent] = children[parent] || []).push(tid);
    }
  }
  for (const parent of Object.keys(children)) {
    children[parent].sort((a, b) => genesis[a] - genesis[b]);
  }
  // A stable traversal order: root first, then the rest by genesis seq.
  const order = Array.from(buckets.keys()).sort((a, b) => {
    if (a === resolvedRoot) return -1;
    if (b === resolvedRoot) return 1;
    return genesis[a] - genesis[b];
  });
  return { root: resolvedRoot, tasks, parents, children, order };
}

// ---------------------------------------------------------------------------
// Incremental fold (WS-A / P0-1)
//
// `reduceMultiplexed` above is pure and re-folds the WHOLE stream on every call.
// The live UI appends one envelope per SSE message, so calling it per message is
// O(N²) (re-bucket + re-fold all N envelopes each time) and hands every consumer
// a brand-new view-model identity, defeating React.memo on the chat bubbles
// (P0-2). The incremental store below folds only the NEW tail each advance and
// reuses the per-task vm / events references for tasks that did not change, so
// memo holds. The pure function stays as the semantic baseline (tests + the
// slow-path rebuild here both lean on it), and this never touches the reducer
// contract — it only drives `applyEnvelope` / `reduceEvents` incrementally.
// ---------------------------------------------------------------------------

// Clone a view-model so a fresh, immutable identity is handed to React while the
// per-call mutation in `applyEnvelope` lands on our own copies. Scalars ride the
// spread; every collection `applyEnvelope` mutates gets a fresh container. The
// toolCall / subtask VALUE objects are copied one level deep too: `applyEnvelope`
// mutates an existing call in place (ToolResultRecorded: status started→recorded)
// and an existing subtask in place (SubtaskCompleted), so a shallow map copy
// would let a later advance retroactively mutate an already-returned snapshot —
// breaking React's immutable-props assumption and risking stale memo renders.
function cloneViewModel(vm) {
  const toolCalls = {};
  for (const id of Object.keys(vm.toolCalls)) toolCalls[id] = { ...vm.toolCalls[id] };
  const subtasks = {};
  for (const id of Object.keys(vm.subtasks)) subtasks[id] = { ...vm.subtasks[id] };
  return {
    ...vm,
    turns: vm.turns.slice(),
    toolCalls,
    subtasks,
    pendingApprovals: vm.pendingApprovals.slice(),
    pendingQuestions: vm.pendingQuestions.slice(),
    diffs: vm.diffs.slice(),
    images: vm.images.slice(),
    todos: vm.todos.slice(),
  };
}

// Order one task's envelopes exactly as `bucketByTask` does (no-seq kept in
// arrival order first, then seq-keyed deduped last-write-wins and sorted). Used
// by the slow-path rebuild so an out-of-order / no-seq / duplicate envelope
// yields a result byte-identical to `reduceMultiplexed`.
function orderTaskEnvelopes(envelopes) {
  const bySeq = new Map();
  const noSeq = [];
  for (const env of envelopes) {
    if (!env || typeof env.task_id !== "string") continue;
    if (typeof env.seq === "number") bySeq.set(env.seq, env);
    else noSeq.push(env);
  }
  const ordered = Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
  return noSeq.concat(ordered);
}

// Highest numeric seq in a task's envelopes (−Infinity if none) — the dedup
// bookmark for the monotonic fast-path check. Mirror of `genesisSeq` (the min).
function maxNumericSeq(envelopes) {
  let max = -Infinity;
  for (const env of envelopes) {
    if (env && typeof env.seq === "number" && env.seq > max) max = env.seq;
  }
  return max;
}

function createMultiplexStore() {
  return {
    folded: 0, // how many of the input array we've consumed
    lastInput: null, // last input array ref (same ref → return cached snapshot)
    rootArg: null, // last `root` argument (a change forces a rebuild)
    byTask: new Map(), // tid -> { envs, vm, maxSeq, genesisSeq, parent }
    parents: {},
    children: {},
    order: [],
    root: null,
    snapshot: null,
  };
}

function resetMultiplexStore(store) {
  store.folded = 0;
  store.byTask = new Map();
  store.parents = {};
  store.children = {};
  store.order = [];
  store.root = null;
  store.snapshot = null;
}

// Rebuild the subtask tree (parents / children / order / root) from the current
// per-task state. Only called when a genesis (TaskCreated / new task) arrived,
// so a steady stream of message/tool envelopes leaves the tree refs untouched.
function rebuildMultiplexTree(store, root) {
  const parents = {};
  const genesis = {};
  for (const [tid, st] of store.byTask) {
    parents[tid] = st.parent;
    genesis[tid] = st.genesisSeq;
  }
  let resolvedRoot = root;
  if (resolvedRoot === null) {
    for (const tid of store.byTask.keys()) {
      if (!parents[tid]) {
        resolvedRoot = tid;
        break;
      }
    }
  }
  const children = {};
  for (const tid of store.byTask.keys()) {
    const parent = parents[tid];
    if (parent && store.byTask.has(parent)) {
      (children[parent] = children[parent] || []).push(tid);
    }
  }
  for (const parent of Object.keys(children)) {
    children[parent].sort((a, b) => genesis[a] - genesis[b]);
  }
  const order = Array.from(store.byTask.keys()).sort((a, b) => {
    if (a === resolvedRoot) return -1;
    if (b === resolvedRoot) return 1;
    return genesis[a] - genesis[b];
  });
  store.parents = parents;
  store.children = children;
  store.order = order;
  store.root = resolvedRoot;
}

// Advance `store` to cover `envelopes` (an append-only array). Returns a mux
// object { root, tasks, eventsByTask, parents, children, order } whose per-task
// `tasks[tid]` / `eventsByTask[tid]` references are REUSED for tasks unchanged
// in this advance. Folding only the new tail makes the common case O(active-task
// size) per envelope instead of O(N) over the whole stream.
function advanceMultiplexStore(store, envelopes, root = null) {
  const all = Array.isArray(envelopes) ? envelopes : [];
  // Identical input ref AND same root → nothing new; return the cached snapshot.
  // Guards React StrictMode's double-invoke and a discarded-then-retried
  // concurrent render. The root check matters because the same array can be
  // re-folded under a different conversation root (which re-resolves the tree);
  // that case must fall through to the root-change reset below.
  if (all === store.lastInput && root === store.rootArg && store.snapshot) {
    return store.snapshot;
  }

  // The stream was cleared/replaced (length shrank, e.g. a session switch) or
  // the conversation root changed → rebuild from scratch so no bucket leaks.
  if (all.length < store.folded || (root !== store.rootArg && store.folded > 0)) {
    resetMultiplexStore(store);
  }
  store.rootArg = root;
  store.lastInput = all;

  // Bucket only the new tail by task.
  const batch = new Map();
  for (let i = store.folded; i < all.length; i += 1) {
    const env = all[i];
    if (!env || typeof env.task_id !== "string") continue;
    if (!batch.has(env.task_id)) batch.set(env.task_id, []);
    batch.get(env.task_id).push(env);
  }
  store.folded = all.length;

  let treeDirty = store.snapshot === null;
  for (const [tid, newEnvs] of batch) {
    let st = store.byTask.get(tid);
    if (!st) {
      st = {
        envs: [],
        vm: emptyViewModel(),
        maxSeq: -Infinity,
        genesisSeq: Infinity,
        parent: null,
      };
      store.byTask.set(tid, st);
      treeDirty = true;
    }
    // Fast path only when every new envelope is seq-monotonic past what we've
    // folded; a no-seq / duplicate / out-of-order envelope drops this one task
    // to a full rebuild so the result stays identical to reduceMultiplexed.
    let monotonic = true;
    let running = st.maxSeq;
    for (const env of newEnvs) {
      if (typeof env.seq !== "number" || env.seq <= running) {
        monotonic = false;
        break;
      }
      running = env.seq;
    }
    if (monotonic) {
      // Fast path: nothing is deduped/reordered, so metadata can advance
      // incrementally from the new envelopes.
      const vm = cloneViewModel(st.vm);
      for (const env of newEnvs) applyEnvelope(vm, env);
      st.vm = vm;
      st.envs = st.envs.concat(newEnvs);
      for (const env of newEnvs) {
        if (typeof env.seq === "number") {
          if (env.seq > st.maxSeq) st.maxSeq = env.seq;
          if (env.seq < st.genesisSeq) st.genesisSeq = env.seq;
        }
        if (env.type === "TaskCreated") {
          const p = env.payload || {};
          st.parent = typeof p.parent_task_id === "string" ? p.parent_task_id : null;
          treeDirty = true; // a genesis link establishes / re-orders the tree
        }
      }
    } else {
      // Slow path: dedup / reorder may DROP or REPLACE the genesis TaskCreated
      // (a duplicate seq is last-write-wins). Recompute the task metadata from the
      // canonical ordered list — never incrementally from raw newEnvs — so it
      // stays identical to reduceMultiplexed; a parent / genesis change re-bases
      // the subtask tree.
      st.envs = orderTaskEnvelopes(st.envs.concat(newEnvs));
      st.vm = reduceEvents(st.envs);
      const prevParent = st.parent;
      const prevGenesis = st.genesisSeq;
      st.parent = parentOf(st.envs);
      st.genesisSeq = genesisSeq(st.envs);
      st.maxSeq = maxNumericSeq(st.envs);
      if (st.parent !== prevParent || st.genesisSeq !== prevGenesis) treeDirty = true;
    }
  }

  if (treeDirty) rebuildMultiplexTree(store, root);

  // Assemble the public maps. Tasks unchanged this advance keep their previous
  // vm / envs references, so React.memo on their bubbles holds.
  const tasks = {};
  const eventsByTask = {};
  for (const [tid, st] of store.byTask) {
    tasks[tid] = st.vm;
    eventsByTask[tid] = st.envs;
  }
  store.snapshot = {
    root: store.root,
    tasks,
    eventsByTask,
    parents: store.parents,
    children: store.children,
    order: store.order,
  };
  return store.snapshot;
}

export {
  reduceMultiplexed,
  bucketByTask,
  parentOf,
  genesisSeq,
  createMultiplexStore,
  advanceMultiplexStore,
  cloneViewModel,
  orderTaskEnvelopes,
};
