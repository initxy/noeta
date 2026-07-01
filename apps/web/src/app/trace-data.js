import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { connectionLabel } from "./connection.js";
import {
  foldTraceTasks,
  foldTraceDetail,
  foldSelections,
  planRefs,
  planView,
} from "./trace-fold.js";

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
  const [streamEnvelopes, setStreamEnvelopes] = useState([]);
  const [planBodyCache, setPlanBodyCache] = useState(new Map());
  const [selectedSeq, setSelectedSeq] = useState(null);
  const [connectionState, setConnectionState] = useState("idle");
  const [notice, setNotice] = useState(null);
  const sseRef = useRef(null);
  const tokenRef = useRef(0);
  const seenKeysRef = useRef(new Set());
  const planInFlight = useRef(new Set());

  // The inspected task's own timeline (the stream carries the whole subtree).
  const events = useMemo(
    () =>
      taskId
        ? streamEnvelopes.filter((env) => env && env.task_id === taskId)
        : [],
    [streamEnvelopes, taskId],
  );
  // The subtask tree (root → __workflow__ → workers), folded from the stream.
  const tasks = useMemo(() => foldTraceTasks(streamEnvelopes), [streamEnvelopes]);
  const activeDetail = useMemo(
    () => foldTraceDetail(taskId, events),
    [taskId, events],
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

  const navigateToTask = useCallback(
    (nextTaskId) => {
      if (!nextTaskId || nextTaskId === taskId) return;
      const url = new URL(window.location.href);
      url.searchParams.set("task", nextTaskId);
      window.history.pushState({}, "", `${url.pathname}${url.search}${url.hash}`);
      setTaskId(nextTaskId);
    },
    [taskId],
  );

  useEffect(() => {
    const onPopState = () => setTaskId(currentTaskId());
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  // Reset all per-task state on a task switch.
  useEffect(() => {
    tokenRef.current += 1;
    seenKeysRef.current = new Set();
    setStreamEnvelopes([]);
    setPlanBodyCache(new Map());
    planInFlight.current = new Set();
    setSelectedSeq(null);
    setConnectionState("idle");
    setNotice(null);
  }, [taskId]);

  const startLiveTail = useCallback(
    (manual = false) => {
      if (!taskId) {
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
        es = new EventSource(`/stream?task=${encodeURIComponent(taskId)}`);
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
        setStreamEnvelopes((current) => current.concat([env]));
      };
      es.onerror = () => {
        if (!isCurrent()) return;
        setConnectionState("reconnecting");
      };
    },
    [closeSse, taskId],
  );

  useEffect(() => {
    startLiveTail(false);
    return () => closeSse();
  }, [closeSse, startLiveTail]);

  // The stream replays history + stays live and auto-resumes on reconnect, so
  // the old explicit backfill / context refetch reduce to a reconnect.
  const backfillHistory = useCallback(() => startLiveTail(true), [startLiveTail]);
  const fetchContext = useCallback(() => {}, []);

  return {
    activeContext,
    activeDetail,
    backfillHistory,
    connection: connectionLabel(connectionState),
    events,
    fetchContext,
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
