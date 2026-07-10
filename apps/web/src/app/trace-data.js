import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { connectionLabel } from "./connection.js";
import {
  advanceMultiplexStore,
  createMultiplexStore,
} from "../domain/multiplex.js";
import {
  createTraceTaskCache,
  foldTraceTasksFromMux,
  foldTraceDetail,
  foldSelections,
  planRefs,
  planView,
} from "./trace-fold.js";

// A stable empty-array sentinel for "no bucket yet" so an inspected task with
// no envelopes (or no active task) never hands consumers a fresh array
// identity each render (mirrors chat-data.js's EMPTY_EVENTS).
const EMPTY_EVENTS = [];

// runtime/sdk/app three-layer refactor T7: the trace inspector
// folds the new thin-backend protocol on the frontend. It used to read a global
// /events SSE, a /tasks/{id} detail, a /tasks/{id}/events backfill, a flat
// /tasks tree, and a /tasks/{id}/context provenance projection. The thin backend
// has none of those — it serves ONE multiplexed SSE stream per conversation
// (GET /stream?task=<id>, the task + its subtree interleaved) and raw blobs from
// GET /content/{hash}. So:
//   * events — the inspected task's own envelopes (filtered from the stream).
//   * tasks — the subtask TREE, folded from the stream (the thin /tasks returns
//     roots only, so the tree can't come from there).
//   * activeDetail — folded from the inspected task's envelopes (no detail EP).
//   * activeContext — provenance folded from the stream: per-turn `selections`
//     inline from LLMRequestStarted, `plans` by derefing each ContextPlanComposed
//     plan_ref via /content/{hash} (the old read_models.context_view, client-side).

function currentTaskId() {
  return new URLSearchParams(window.location.search).get("task");
}

function useTraceData() {
  const [taskId, setTaskId] = useState(() => currentTaskId());
  // The task the SSE stream is rooted at. `/stream?task=<root>` already carries
  // the root's WHOLE subtree (backend stream.discover_tree + stream_frames), so
  // switching the inspected `taskId` to another node already in that subtree is
  // a pure filter change — the stream (and `streamEnvelopes`) must NOT be
  // re-rooted, or the parent/sibling tasks drop out of the folded TaskTree.
  // streamRoot only changes when navigating to a task OUTSIDE the current
  // subtree (a direct URL open, or Back/Forward to a different conversation).
  const [streamRoot, setStreamRoot] = useState(() => currentTaskId());
  const [streamEnvelopes, setStreamEnvelopes] = useState([]);
  const [planBodyCache, setPlanBodyCache] = useState(new Map());
  const [selectedSeq, setSelectedSeq] = useState(null);
  const [connectionState, setConnectionState] = useState("idle");
  const [notice, setNotice] = useState(null);
  const sseRef = useRef(null);
  const tokenRef = useRef(0);
  const seenKeysRef = useRef(new Set());
  // Every task_id the current stream has delivered an envelope for — lets a
  // navigation tell an in-subtree filter switch (keep the connection) from an
  // out-of-subtree jump (re-root + reconnect). Reset with the stream.
  const streamTaskIdsRef = useRef(new Set());
  const planInFlight = useRef(new Set());

  // One INCREMENTAL fold of the multiplexed subtree stream (the same
  // domain/multiplex.js store the chat app uses, WS-A / P0-1): it folds only
  // the newly arrived tail each advance and reuses the per-task vm / events
  // references for buckets that did not change, so a message for one worker no
  // longer re-buckets + re-`reduceEvents`s the WHOLE subtree (was O(N) per SSE
  // message, O(N^2) over a session). `root` is passed as null — trace builds
  // its own tree from parent_task_id (buildTaskTree in TraceApp.jsx), so the
  // store's root-first `order`/`children` are unused here.
  const muxStoreRef = useRef(null);
  if (muxStoreRef.current === null) muxStoreRef.current = createMultiplexStore();
  // Per-task cache for the couple of tree-row fields the shared vm doesn't
  // carry (agent_name genesis, created/last event-time bounds) — see
  // trace-fold.js's foldTraceTasksFromMux.
  const taskCacheRef = useRef(null);
  if (taskCacheRef.current === null) taskCacheRef.current = createTraceTaskCache();

  const mux = useMemo(
    () => advanceMultiplexStore(muxStoreRef.current, streamEnvelopes, null),
    [streamEnvelopes],
  );
  // The inspected task's own timeline — a direct bucket lookup (was a full
  // `.filter()` over the whole multiplexed stream on every message). Reused by
  // the store when the inspected task's bucket did not change this advance, so
  // the detail/selection/plan folds below skip their own recompute for free
  // (their useMemo deps key off this reference).
  const events = useMemo(
    () => (taskId && mux.eventsByTask[taskId]) || EMPTY_EVENTS,
    [mux, taskId],
  );
  // The inspected task's already-folded view-model, same identity-reuse rule —
  // avoids a second `reduceEvents` pass in foldTraceDetail below.
  const activeVm = useMemo(
    () => (taskId && mux.tasks[taskId]) || null,
    [mux, taskId],
  );
  // The subtask tree (root → __workflow__ → workers), folded from the store.
  const tasks = useMemo(
    () => foldTraceTasksFromMux(taskCacheRef.current, mux),
    [mux],
  );
  const activeDetail = useMemo(
    () => foldTraceDetail(taskId, events, activeVm),
    [taskId, events, activeVm],
  );
  const selections = useMemo(() => foldSelections(events), [events]);
  const planMetas = useMemo(() => planRefs(events), [events]);
  const activeContext = useMemo(
    () => ({
      plans: planMetas.map((meta) =>
        planView(
          meta,
          meta.plan_ref.hash ? planBodyCache.get(meta.plan_ref.hash) : null,
        ),
      ),
      selections,
    }),
    [planMetas, selections, planBodyCache],
  );

  const selectedEvent = useMemo(
    () => events.find((event) => event && event.seq === selectedSeq) || null,
    [events, selectedSeq],
  );

  const closeSse = useCallback(() => {
    if (sseRef.current) {
      sseRef.current.close();
      sseRef.current = null;
    }
  }, []);

  // Deref each ContextPlanComposed plan_ref body (raw /content bytes) so the
  // provenance panel can show the plan view. One fetch per still-missing hash.
  useEffect(() => {
    const toFetch = [];
    for (const meta of planMetas) {
      const hash = meta.plan_ref.hash;
      if (!hash) continue;
      if (!planBodyCache.has(hash) && !planInFlight.current.has(hash)) {
        planInFlight.current.add(hash);
        toFetch.push(hash);
      }
    }
    if (!toFetch.length) return;
    let cancelled = false;
    (async () => {
      const texts = await Promise.all(
        toFetch.map(async (hash) => {
          try {
            const res = await fetch(`/content/${encodeURIComponent(hash)}`);
            return res.ok ? await res.text() : null;
          } catch (e) {
            return null;
          }
        }),
      );
      toFetch.forEach((hash) => planInFlight.current.delete(hash));
      if (cancelled) return;
      setPlanBodyCache((cache) => {
        const next = new Map(cache);
        toFetch.forEach((hash, i) => next.set(hash, texts[i]));
        return next;
      });
    })();
    return () => {
      cancelled = true;
    };
  }, [planMetas, planBodyCache]);

  // Switch the inspected task. If the target already belongs to the current
  // stream's subtree (an envelope with its task_id has arrived), keep the live
  // connection and only move the filter; otherwise re-root the stream at it —
  // which resets the stream state + reconnects via the streamRoot effects below.
  const selectTask = useCallback((nextTaskId) => {
    if (!nextTaskId) return;
    setTaskId(nextTaskId);
    if (!streamTaskIdsRef.current.has(nextTaskId)) {
      setStreamRoot(nextTaskId);
    }
  }, []);

  const navigateToTask = useCallback(
    (nextTaskId) => {
      if (!nextTaskId || nextTaskId === taskId) return;
      const url = new URL(window.location.href);
      url.searchParams.set("task", nextTaskId);
      window.history.pushState({}, "", `${url.pathname}${url.search}${url.hash}`);
      selectTask(nextTaskId);
    },
    [taskId, selectTask],
  );

  useEffect(() => {
    const onPopState = () => selectTask(currentTaskId());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [selectTask]);

  // Reset the per-inspected-task view state on ANY task switch (both an
  // in-stream filter change and a full re-root): the old selection points at an
  // event on the previously inspected task's timeline.
  useEffect(() => {
    setSelectedSeq(null);
  }, [taskId]);

  // Reset the stream-scoped state ONLY when the stream is actually re-rooted.
  // An in-subtree navigation keeps streamEnvelopes (and the folded TaskTree)
  // intact; a re-root clears them so the new subtree starts from a clean slate.
  // startLiveTail (keyed off streamRoot) reconnects right after.
  useEffect(() => {
    tokenRef.current += 1;
    seenKeysRef.current = new Set();
    streamTaskIdsRef.current = new Set();
    setStreamEnvelopes([]);
    setPlanBodyCache(new Map());
    planInFlight.current = new Set();
    setConnectionState("idle");
    setNotice(null);
    // A re-root starts a brand-new subtree — drop the incremental fold state
    // too, so no stale per-task cache entry from the old subtree lingers.
    muxStoreRef.current = createMultiplexStore();
    taskCacheRef.current = createTraceTaskCache();
  }, [streamRoot]);

  const startLiveTail = useCallback(
    (manual = false) => {
      if (!streamRoot) {
        setNotice({ kind: "error", text: "No task id in the URL." });
        return;
      }
      const token = ++tokenRef.current;
      closeSse();
      seenKeysRef.current = new Set();
      setConnectionState(manual ? "reconnecting" : "connecting");
      setNotice({ kind: "loading", text: "Loading trace..." });
      let es;
      try {
        es = new EventSource(`/stream?task=${encodeURIComponent(streamRoot)}`);
      } catch (error) {
        setConnectionState("offline");
        return;
      }
      sseRef.current = es;
      const isCurrent = () =>
        es === sseRef.current && token === tokenRef.current;
      es.onopen = () => {
        if (!isCurrent()) return;
        setConnectionState("live");
        setNotice(null);
      };
      es.onmessage = (message) => {
        if (!isCurrent()) return;
        let env = null;
        try {
          env = JSON.parse(message.data);
        } catch (error) {
          return;
        }
        if (!env || typeof env.task_id !== "string") return;
        if (typeof env.seq === "number") {
          const key = `${env.task_id}:${env.seq}`;
          if (seenKeysRef.current.has(key)) return;
          seenKeysRef.current.add(key);
        }
        // Record every task_id the subtree stream delivers so navigateToTask
        // can keep the connection when switching to a node already in it.
        streamTaskIdsRef.current.add(env.task_id);
        setStreamEnvelopes((current) => current.concat([env]));
      };
      es.onerror = () => {
        if (!isCurrent()) return;
        setConnectionState("reconnecting");
      };
    },
    [closeSse, streamRoot],
  );

  useEffect(() => {
    startLiveTail(false);
    return () => closeSse();
  }, [closeSse, startLiveTail]);

  return {
    activeContext,
    activeDetail,
    connection: connectionLabel(connectionState),
    events,
    navigateToTask,
    notice,
    selectedEvent,
    selectedSeq,
    setSelectedSeq,
    startLiveTail,
    tasks,
    taskId,
  };
}

export { useTraceData };
