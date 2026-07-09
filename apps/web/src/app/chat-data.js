import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  advanceMultiplexStore,
  createMultiplexStore,
} from "../domain/multiplex.js";
import { emptyViewModel } from "../domain/reducer.js";
import {
  applyDelta,
  clearAll,
  clearTask,
  createStreamingState,
  resetCall,
  streamingTurnFor,
} from "../domain/streaming.js";

// Stable singletons for the "no active task / task not in the stream yet" case,
// so an empty conversation never hands consumers a fresh identity each render.
const EMPTY_VIEW_MODEL = emptyViewModel();
const EMPTY_EVENTS = [];

// U5 — grace window before a deny / batch approval decision commits (Undo lives
// in the feedback toast for this long).
const APPROVAL_UNDO_MS = 5000;
import { getJSON, postJSON } from "../shared/http.js";
import { connectionLabel } from "./connection.js";
import { base64ToBytes, dataUrlFromBase64, sha256Hex } from "./chat-images.js";
import {
  projectMessagesFull,
  projectMessagesText,
  extractThinking,
  extractQuestions,
  synthesizeDetail,
} from "./chat-fold.js";

// runtime/sdk/app three-layer refactor T7: the chat data layer
// folds the new thin-backend protocol entirely on the frontend.
//
//   * Transport — ONE SSE stream per conversation, GET /stream?task=<root>,
//     carrying the root Task AND every subtask's EventEnvelope interleaved. The
//     stream replays history (catch-up from an empty cursor) then goes live, so
//     there is no separate backfill fetch; the browser EventSource resends
//     Last-Event-ID on reconnect for gap-free resume. We demultiplex by task_id
//     (reduceMultiplexed) — the drilled-in subtask view is just another bucket
//     in the same stream, never a second connection.
//   * Commands — the eight T5 verbs (POST /tasks + /tasks/{id}/{messages,approve,
//     deny,answer,cancel,close,reopen}). Each returns 202 + an ack only; the
//     business state always flows back through the stream (single source of
//     truth), so commands are fire-and-forget — no syncActiveSession / detail
//     refetch.
//   * Large objects — message bodies / thinking / question forms / images all
//     ride a ContentRef fetched RAW from GET /content/{hash} and are projected
//     client-side (chat-fold.js), replacing the old server-side projections.
//   * Detail — there is no /tasks/{id} detail endpoint; activeDetail is
//     synthesized from the reducer view-model + the GET /tasks session-list row
//     + the derefed question bodies (chat-fold.synthesizeDetail).
//
// The thin backend is single workspace / provider / model and command-minimal,
// so the rich composer knobs (model / provider / workspace / effort / mcp_prompt
// / @-attachments / image attach / rewind / session delete) have no protocol
// surface here; their selectors degrade gracefully (capabilities advertise empty
// lists) and the dead command paths are stubbed pending their UI removal.

// Event types whose arrival should refresh the sidebar session list (status /
// title may have moved). A superset of the lifecycle events plus the activity
// markers that change a row's "running" look.
const SESSION_LIST_REFRESH_TYPES = new Set([
  "TaskCreated",
  "TaskStarted",
  "TaskWoken",
  "TaskSuspended",
  "TaskCompleted",
  "TaskFailed",
  "TaskCancelled",
  "ToolCallApprovalRequested",
  "ToolCallApprovalResolved",
  "ToolCallDenied",
  "UserQuestionRequested",
  "UserQuestionAnswered",
  "SubtaskSpawned",
  "SubtaskCompleted",
  "ConversationClosed",
  "ConversationReopened",
  "MessagesAppended",
  "ModelBound",
]);

function useChatData() {
  const initialTaskId = useMemo(
    () => new URLSearchParams(window.location.search).get("task"),
    [],
  );
  const [activeTaskId, setActiveTaskId] = useState(null);
  const [commandIn, setCommandIn] = useState(false);
  const [sandboxEnabled, setSandboxEnabled] = useState(false);
  // Per-task sandbox live-preview discovery (GET /tasks/{id}/preview).
  // ``null`` = not fetched / no sandbox; ``{token, panels}`` = preview available.
  const [previewInfo, setPreviewInfo] = useState(null);
  // The multiplexed envelope buffer for the active conversation's stream — the
  // root Task and all its subtasks interleaved. Everything folds from here.
  const [streamEnvelopes, setStreamEnvelopes] = useState([]);
  const [sessions, setSessions] = useState([]);
  const [taskListLoaded, setTaskListLoaded] = useState(false);
  const [openingSession, setOpeningSession] = useState(false);
  const [pendingGoalText, setPendingGoalText] = useState(null);
  const [connectionState, setConnectionState] = useState("idle");
  // U12 — transient messages render as a dismissable Toast stack (was an inline
  // Banner pinned absolute at top:58px, which overlapped a tall header). `toasts`
  // is a queue of { id, kind, text, action? }. The legacy showNotice/clearNotice
  // API is preserved (40+ call sites) as a single "notice channel" toast (a new
  // notice replaces the previous); pushToast adds independent toasts (U5 undo,
  // B4 copy). noticeRef still mirrors the current notice descriptor for the two
  // readers that branch on its kind (loading / error).
  const [toasts, setToasts] = useState([]);
  const noticeRef = useRef(null);
  const noticeIdRef = useRef(null);
  const toastIdRef = useRef(0);
  const toastTimersRef = useRef(new Map());
  // U5 — deferred approval resolves (the deny / batch undo window) are HOISTED
  // here (not in the ApprovalGroup component) so switching / closing a session
  // does not drop the commit: this hook outlives session switches, its timers
  // capture the ORIGINAL task_id, and a switch flushes them to that task rather
  // than silently dropping (blocker 1). `hiddenApprovals` is the optimistic-hide
  // set the ApprovalGroup filters on; a failed commit restores the card (blocker 2).
  const [hiddenApprovals, setHiddenApprovals] = useState(() => new Set());
  const approvalTimersRef = useRef(new Map()); // callId -> timeout
  const approvalPendingRef = useRef(new Map()); // callId -> { taskId, approved }
  const approvalFlushRef = useRef(null); // latest flushApprovalResolves (unmount)
  const [busyLabel, setBusyLabel] = useState("");
  // Composer selector menus, served by GET /capabilities. On the thin backend
  // agents / models / permission & effort modes / mcp_servers are populated;
  // workspaces / providers / skills / slash_commands come back empty and the
  // composer hides those selectors (graceful degradation, D7).
  const [options, setOptions] = useState({
    agents: [],
    models: [],
    permissionModes: [],
    effortModes: [],
    slashCommands: [],
    skills: [],
    workspaces: [],
    mcpServers: [],
    providers: {},
    defaultProvider: "",
    modelCapabilities: {},
  });
  // Content-addressed deref caches (keyed by ContentRef hash; shared safely
  // across tasks since the hash IS the identity). Message full + text are folded
  // from the same /content fetch; thinking + question bodies each have their own.
  const [messageFullCache, setMessageFullCache] = useState(new Map());
  const [messageTextCache, setMessageTextCache] = useState(new Map());
  const [responseThinkingCache, setResponseThinkingCache] = useState(new Map());
  const [questionsCache, setQuestionsCache] = useState(new Map());
  // Local image preview cache (Map<hash, dataUrl>). Image-input restore (T4):
  // before sending a turn, each pasted/picked image's RAW bytes are SHA-256'd in
  // the browser (= the backend ContentStore's content-address key) and stored
  // hash→data:URL here. The just-sent user bubble then renders locally (zero
  // requests); on history reload the same hash resolves via GET /content/{hash}.
  // A pure preview accelerator — never written back to the ledger (the ledger
  // stores a ContentRef, not base64).
  const [localImageCache, setLocalImageCache] = useState(new Map());

  // Subtask drill-in popup: a breadcrumb STACK of {taskId, agentName, goal}. The
  // subtask's events already ride the active stream (same task protocol), so the
  // popup is a view onto reduceMultiplexed's bucket — no separate fetch.
  const [popupStack, setPopupStack] = useState([]);

  const activeTaskRef = useRef(null);
  const sseRef = useRef(null);
  const connectionTokenRef = useRef(0);
  const seenKeysRef = useRef(new Set());
  const messageInFlight = useRef(new Set());
  const thinkingInFlight = useRef(new Set());
  const questionsInFlight = useRef(new Set());
  const messageRetryTimers = useRef(new Map());
  const messageRetryAttempts = useRef(new Map());
  const initialTaskLoadedRef = useRef(false);
  const taskListRefreshTimerRef = useRef(null);

  // Token-streaming preview buffer (ephemeral; ADR token-streaming-projection).
  // Named `delta` SSE frames accumulate in this ref BESIDE the reducer — they
  // never enter reduceEvents / the multiplex fold. Consumers repaint off the
  // cheap version counter: the delta path bumps it rAF-coalesced (at most one
  // React update per frame under a fast token stream); the clear path bumps it
  // synchronously so a clear lands in the SAME update as the envelope that
  // superseded the preview (single-repaint handover, no double bubble).
  const streamingRef = useRef(createStreamingState());
  const [streamingVersion, setStreamingVersion] = useState(0);
  const streamingRafRef = useRef(0);

  useEffect(() => {
    activeTaskRef.current = activeTaskId;
  }, [activeTaskId]);

  // --- Folded view-models ----------------------------------------------------
  // One INCREMENTAL fold of the multiplexed stream → per-task view-models + the
  // subtask tree (WS-A / P0-1). The store folds only the new tail each advance
  // and reuses the per-task vm / events references for tasks that did not change
  // — so `mux.tasks[id]` / `mux.eventsByTask[id]` stay identity-stable while a
  // task is idle, and a brand-new fold no longer churns every consumer (P0-2).
  // The active conversation and any drilled-in subtask both read their bucket
  // from here.
  const muxStoreRef = useRef(null);
  if (muxStoreRef.current === null) muxStoreRef.current = createMultiplexStore();
  const mux = useMemo(
    () => advanceMultiplexStore(muxStoreRef.current, streamEnvelopes, activeTaskId),
    [streamEnvelopes, activeTaskId],
  );
  const vm = useMemo(
    () => (activeTaskId && mux.tasks[activeTaskId]) || EMPTY_VIEW_MODEL,
    [mux, activeTaskId],
  );
  // The active task's own envelope list (ChatApp reads it directly for
  // response_ref lookup + background-job fold). Reused identity from the store
  // when the active task is unchanged this advance.
  const events = useMemo(
    () => (activeTaskId && mux.eventsByTask[activeTaskId]) || EMPTY_EVENTS,
    [mux, activeTaskId],
  );
  const backgroundJobs = useMemo(() => foldBackgroundJobs(events), [events]);

  // --- Token-streaming preview -----------------------------------------------
  // Coalesced repaint for the delta path: one version bump per animation frame
  // no matter how many token frames arrived (mirrors the rAF pattern used for
  // the panel resize drag).
  const scheduleStreamingRepaint = useCallback(() => {
    if (streamingRafRef.current) return;
    streamingRafRef.current = window.requestAnimationFrame(() => {
      streamingRafRef.current = 0;
      setStreamingVersion((v) => v + 1);
    });
  }, []);

  // Replace the streaming state NOW (same tick, no rAF) — used by the clears
  // (MessagesAppended / LLMRetryScheduled / reconnect / session switch) so the
  // preview never outlives the truth that superseded it. A no-op (same state
  // reference back from the pure helpers) skips the repaint entirely.
  const commitStreamingState = useCallback((next) => {
    if (next === streamingRef.current) return;
    streamingRef.current = next;
    setStreamingVersion((v) => v + 1);
  }, []);

  // The active root task's streaming turn ({callId, blocks} or null). Subtask
  // deltas are buffered too, but v1 renders only the root task's preview (spec
  // non-goal); the version counter is the ref's change signal.
  const streamingTurn = useMemo(
    () => (activeTaskId ? streamingTurnFor(streamingRef.current, activeTaskId) : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activeTaskId, streamingVersion],
  );

  const popupTaskId = popupStack.length
    ? popupStack[popupStack.length - 1].taskId
    : null;
  const popupVm = useMemo(
    () => (popupTaskId && mux.tasks[popupTaskId]) || EMPTY_VIEW_MODEL,
    [mux, popupTaskId],
  );
  const popupEvents = useMemo(
    () => (popupTaskId && mux.eventsByTask[popupTaskId]) || EMPTY_EVENTS,
    [mux, popupTaskId],
  );

  const dismissToast = useCallback((id) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
    const timer = toastTimersRef.current.get(id);
    if (timer != null) {
      window.clearTimeout(timer);
      toastTimersRef.current.delete(id);
    }
    if (noticeIdRef.current === id) {
      noticeIdRef.current = null;
      noticeRef.current = null;
    }
  }, []);

  // Add an independent toast. Non-error kinds auto-dismiss after 8s (U12); error
  // stays until manually closed. `action` = { label, onClick } renders a button
  // (U5 undo / B4 retry). Returns the toast id.
  const pushToast = useCallback(
    (kind, text, { action, duration } = {}) => {
      const id = (toastIdRef.current += 1);
      setToasts((prev) => [...prev, { id, kind, text, action }]);
      const ms = duration != null ? duration : kind === "error" ? 0 : 8000;
      if (ms > 0) {
        toastTimersRef.current.set(
          id,
          window.setTimeout(() => dismissToast(id), ms),
        );
      }
      return id;
    },
    [dismissToast],
  );

  // The legacy "notice channel": at most one notice toast; a new notice replaces
  // the previous. noticeRef mirrors its descriptor for kind-based branching.
  // Zero-change semantics vs the old inline Banner: a notice PERSISTS until
  // clearNotice() or the next showNotice() (e.g. "Loading session..." must stay
  // up until the SSE opens), so it is pushed with duration:0 (no auto-dismiss),
  // unlike an independent pushToast which auto-dismisses after 8s.
  const showNotice = useCallback(
    (kind, text) => {
      if (noticeIdRef.current != null) dismissToast(noticeIdRef.current);
      if (!text) {
        noticeRef.current = null;
        return;
      }
      noticeRef.current = { kind, text };
      noticeIdRef.current = pushToast(kind, text, { duration: 0 });
    },
    [dismissToast, pushToast],
  );

  const clearNotice = useCallback(() => {
    if (noticeIdRef.current != null) dismissToast(noticeIdRef.current);
    noticeRef.current = null;
  }, [dismissToast]);

  const resetSessionState = useCallback(() => {
    setStreamEnvelopes([]);
    seenKeysRef.current = new Set();
    commitStreamingState(clearAll(streamingRef.current));
    setMessageFullCache(new Map());
    setMessageTextCache(new Map());
    setResponseThinkingCache(new Map());
    setQuestionsCache(new Map());
    for (const timer of messageRetryTimers.current.values()) {
      window.clearTimeout(timer);
    }
    messageInFlight.current = new Set();
    thinkingInFlight.current = new Set();
    questionsInFlight.current = new Set();
    messageRetryTimers.current = new Map();
    messageRetryAttempts.current = new Map();
    setOpeningSession(false);
    setPendingGoalText(null);
  }, [commitStreamingState]);

  const loadTaskList = useCallback(
    async ({ silent = false } = {}) => {
      try {
        const { status, body } = await getJSON("/tasks");
        if (status === 200 && Array.isArray(body)) {
          setSessions(body);
          setTaskListLoaded(true);
        }
      } catch (error) {
        if (!silent) showNotice("error", "Failed to load session list.");
      }
    },
    [showNotice],
  );

  const scheduleTaskListRefresh = useCallback(() => {
    if (taskListRefreshTimerRef.current != null) return;
    taskListRefreshTimerRef.current = window.setTimeout(() => {
      taskListRefreshTimerRef.current = null;
      loadTaskList({ silent: true });
    }, 80);
  }, [loadTaskList]);

  const closeSse = useCallback(() => {
    if (sseRef.current) {
      sseRef.current.close();
      sseRef.current = null;
    }
  }, []);

  // Open the multiplexed SSE stream for a conversation root. The stream replays
  // the full subtree from the start (no Last-Event-ID on first connect) then
  // stays live; the browser resends the last id: on reconnect for resume.
  const startStream = useCallback(
    (taskId = activeTaskRef.current) => {
      const token = ++connectionTokenRef.current;
      closeSse();
      // Deltas are not replayed on reconnect (no SSE id on delta frames), so a
      // NEW EventSource must never inherit a half-streamed preview — the final
      // MessagesAppended will repaint the truth through the envelope path.
      commitStreamingState(clearAll(streamingRef.current));
      if (!taskId) {
        setConnectionState("idle");
        return;
      }
      setConnectionState("connecting");
      let es;
      try {
        es = new EventSource(`/stream?task=${encodeURIComponent(taskId)}`);
      } catch (error) {
        setConnectionState("offline");
        return;
      }
      sseRef.current = es;
      const isCurrent = () =>
        sseRef.current === es && token === connectionTokenRef.current;

      es.onopen = () => {
        if (!isCurrent()) return;
        setConnectionState("live");
        if (noticeRef.current && noticeRef.current.kind === "loading") {
          clearNotice();
        }
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
        // Dedup by task_id+seq — the stream may redeliver a tail on reconnect.
        if (typeof env.seq === "number") {
          const key = `${env.task_id}:${env.seq}`;
          if (seenKeysRef.current.has(key)) return;
          seenKeysRef.current.add(key);
        }
        setStreamEnvelopes((current) => current.concat([env]));
        if (SESSION_LIST_REFRESH_TYPES.has(env.type)) scheduleTaskListRefresh();
        // Token streaming: the durable envelope supersedes the ephemeral
        // preview. MessagesAppended = the turn's real content landed (drop the
        // task's buffer — per tool-loop round: stream → clear → stream again);
        // LLMRetryScheduled = the in-flight attempt failed and the retry will
        // re-stream from scratch (drop that call's accumulated text).
        // Committed synchronously so the clear renders in the same React
        // update as the envelope append (streaming bubble out, final bubble in).
        if (env.type === "MessagesAppended") {
          commitStreamingState(clearTask(streamingRef.current, env.task_id));
        } else if (env.type === "LLMRetryScheduled") {
          const callId =
            typeof env.payload?.call_id === "string" ? env.payload.call_id : null;
          commitStreamingState(resetCall(streamingRef.current, env.task_id, callId));
        }
      };
      // Named `delta` frames — ephemeral token previews of the assistant turn
      // in flight ({task_id, call_id, kind, text, index}). They carry no SSE
      // id (the resume cursor never moves; onmessage never sees them) and are
      // dropped wholesale on reconnect. Accumulate into the ref and repaint at
      // most once per animation frame; a malformed frame is a no-op inside
      // applyDelta (same state reference back).
      es.addEventListener("delta", (message) => {
        if (!isCurrent()) return;
        let delta = null;
        try {
          delta = JSON.parse(message.data);
        } catch (error) {
          return;
        }
        const next = applyDelta(streamingRef.current, delta);
        if (next === streamingRef.current) return;
        streamingRef.current = next;
        scheduleStreamingRepaint();
      });
      es.onerror = () => {
        if (!isCurrent()) return;
        setConnectionState("connecting");
        // The browser is about to auto-reconnect; deltas in flight were lost
        // and will not be replayed — drop any half-streamed preview now.
        commitStreamingState(clearAll(streamingRef.current));
      };
    },
    [
      clearNotice,
      closeSse,
      commitStreamingState,
      scheduleStreamingRepaint,
      scheduleTaskListRefresh,
    ],
  );

  useEffect(
    () => () => {
      closeSse();
      for (const timer of messageRetryTimers.current.values()) {
        window.clearTimeout(timer);
      }
      messageRetryTimers.current = new Map();
      messageRetryAttempts.current = new Map();
      for (const timer of toastTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      toastTimersRef.current = new Map();
      // U5 — best-effort commit any in-window deferred approval decisions before
      // this hook tears down (flush also clears their timers), so an unmount does
      // not silently drop them.
      approvalFlushRef.current?.();
      if (taskListRefreshTimerRef.current != null) {
        window.clearTimeout(taskListRefreshTimerRef.current);
        taskListRefreshTimerRef.current = null;
      }
      if (streamingRafRef.current) {
        window.cancelAnimationFrame(streamingRafRef.current);
        streamingRafRef.current = 0;
      }
    },
    [closeSse],
  );

  // --- Subtask drill-in popup (stack management only; data is in the stream) --
  const openSubtask = useCallback((node) => {
    if (!node || !node.taskId) return;
    setPopupStack([node]);
  }, []);

  const drillSubtask = useCallback((node) => {
    if (!node || !node.taskId) return;
    setPopupStack((stack) => [...stack, node]);
  }, []);

  const gotoBreadcrumb = useCallback((index) => {
    setPopupStack((stack) => stack.slice(0, index + 1));
  }, []);

  const closeSubtask = useCallback(() => {
    setPopupStack([]);
  }, []);

  const applyCapabilities = useCallback(async () => {
    try {
      const resp = await fetch("/capabilities");
      if (resp.ok) {
        const caps = await resp.json();
        setCommandIn(!!(caps && caps.command_in));
        setSandboxEnabled(!!(caps && caps.sandbox_enabled));
        setOptions({
          agents: Array.isArray(caps?.agents) ? caps.agents : [],
          models: Array.isArray(caps?.models) ? caps.models : [],
          permissionModes: Array.isArray(caps?.permission_modes)
            ? caps.permission_modes
            : [],
          effortModes: Array.isArray(caps?.effort_modes)
            ? caps.effort_modes
            : [],
          slashCommands: Array.isArray(caps?.slash_commands)
            ? caps.slash_commands
            : [],
          skills: Array.isArray(caps?.skills) ? caps.skills : [],
          workspaces: Array.isArray(caps?.workspaces) ? caps.workspaces : [],
          mcpServers: Array.isArray(caps?.mcp_servers) ? caps.mcp_servers : [],
          providers:
            caps?.providers && typeof caps.providers === "object"
              ? caps.providers
              : {},
          defaultProvider:
            typeof caps?.default_provider === "string"
              ? caps.default_provider
              : "",
          modelCapabilities:
            caps?.model_capabilities &&
            typeof caps.model_capabilities === "object"
              ? caps.model_capabilities
              : {},
        });
        if (!(caps && caps.command_in)) {
          showNotice("info", "Observation-only server. Commands are disabled.");
        }
        return;
      }
    } catch (error) {}
    setCommandIn(false);
    showNotice("info", "Observation-only server. Commands are disabled.");
  }, [showNotice]);

  // --- MCP connector management (peripheral service, /mcp/servers*) -----------------
  const createMcpServer = useCallback(
    async (input) => {
      const type = input?.type === "stdio" ? "stdio" : "http";
      const trimmedAlias = String(input?.alias || "").trim();
      if (!trimmedAlias) {
        showNotice("error", "MCP server needs an alias.");
        return null;
      }
      let payload;
      if (type === "stdio") {
        const command = String(input?.command || "").trim();
        if (!command) {
          showNotice("error", "A stdio MCP server needs a command.");
          return null;
        }
        payload = {
          alias: trimmedAlias,
          type: "stdio",
          command,
          args: Array.isArray(input?.args) ? input.args : [],
          env: input?.env && typeof input.env === "object" ? input.env : {},
        };
      } else {
        const trimmedUrl = String(input?.url || "").trim();
        if (!trimmedUrl) {
          showNotice("error", "An http MCP server needs a url.");
          return null;
        }
        payload = {
          alias: trimmedAlias,
          type: "http",
          url: trimmedUrl,
          headers:
            input?.headers && typeof input.headers === "object"
              ? input.headers
              : {},
        };
      }
      if (Array.isArray(input?.tools)) payload.tools = input.tools;
      try {
        const { status, body } = await postJSON("/mcp/servers", payload);
        if (status === 201 && body && body.alias) {
          await applyCapabilities();
          clearNotice();
          return body;
        }
        showNotice(
          "error",
          `Could not add MCP server: ${body?.error || body?.message || `HTTP ${status}`}`,
        );
        return null;
      } catch (error) {
        showNotice("error", "Network error adding MCP server.");
        return null;
      }
    },
    [applyCapabilities, clearNotice, showNotice],
  );

  const discoverMcpServerTools = useCallback(
    async (alias) => {
      const a = String(alias || "").trim();
      if (!a) return null;
      try {
        const res = await fetch(`/mcp/servers/${encodeURIComponent(a)}/tools`);
        const body = await res.json().catch(() => null);
        if (res.status === 200 && Array.isArray(body?.tools)) return body.tools;
        showNotice(
          "error",
          `Could not list tools: ${body?.error || `HTTP ${res.status}`}`,
        );
        return null;
      } catch (error) {
        showNotice("error", "Network error listing MCP tools.");
        return null;
      }
    },
    [showNotice],
  );

  const setMcpServerTools = useCallback(
    async (alias, tools) => {
      const a = String(alias || "").trim();
      if (!a) return null;
      try {
        const res = await fetch(`/mcp/servers/${encodeURIComponent(a)}/tools`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ tools: tools === null ? null : tools }),
        });
        const body = await res.json().catch(() => null);
        if (res.status === 200 && body && body.alias) {
          await applyCapabilities();
          clearNotice();
          return body;
        }
        showNotice(
          "error",
          `Could not save tool subset: ${body?.error || `HTTP ${res.status}`}`,
        );
        return null;
      } catch (error) {
        showNotice("error", "Network error saving tool subset.");
        return null;
      }
    },
    [applyCapabilities, clearNotice, showNotice],
  );

  const updateMcpServer = useCallback(
    async (alias, input) => {
      const a = String(alias || "").trim();
      if (!a) return null;
      try {
        const res = await fetch(`/mcp/servers/${encodeURIComponent(a)}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(input || {}),
        });
        const body = await res.json().catch(() => null);
        if (res.status === 200 && body && body.alias) {
          await applyCapabilities();
          clearNotice();
          return body;
        }
        showNotice(
          "error",
          `Could not edit MCP server: ${body?.error || `HTTP ${res.status}`}`,
        );
        return null;
      } catch (error) {
        showNotice("error", "Network error editing MCP server.");
        return null;
      }
    },
    [applyCapabilities, clearNotice, showNotice],
  );

  const deleteMcpServer = useCallback(
    async (alias) => {
      const a = String(alias || "").trim();
      if (!a) return false;
      try {
        const res = await fetch(`/mcp/servers/${encodeURIComponent(a)}`, {
          method: "DELETE",
        });
        // New backend returns 200 {deleted: alias} (the old one returned 204).
        if (res.status === 200 || res.status === 204) {
          await applyCapabilities();
          clearNotice();
          return true;
        }
        const body = await res.json().catch(() => null);
        showNotice(
          "error",
          `Could not delete MCP server: ${body?.error || `HTTP ${res.status}`}`,
        );
        return false;
      } catch (error) {
        showNotice("error", "Network error deleting MCP server.");
        return false;
      }
    },
    [applyCapabilities, clearNotice, showNotice],
  );

  // --- Workspace (project) registry (peripheral service, /workspaces) ---------------
  // Mirrors the MCP CRUD pattern: POST the new project, then refresh /capabilities so
  // options.workspaces (the sidebar grouping + composer workspace picker source)
  // picks it up. The path is validated server-side (absolute / is a dir / zero
  // whitelist); a bad path comes back 400 {error} and is surfaced.
  const createWorkspace = useCallback(
    async ({ path, name } = {}) => {
      const trimmedPath = String(path || "").trim();
      if (!trimmedPath) {
        showNotice("error", "A project needs an absolute path.");
        return null;
      }
      const payload = { path: trimmedPath };
      const trimmedName = String(name || "").trim();
      if (trimmedName) payload.name = trimmedName;
      try {
        const { status, body } = await postJSON("/workspaces", payload);
        if (status === 201 && body && body.id) {
          await applyCapabilities();
          clearNotice();
          return body;
        }
        showNotice(
          "error",
          `Could not add project: ${body?.error || body?.message || `HTTP ${status}`}`,
        );
        return null;
      } catch (error) {
        showNotice("error", "Network error adding project.");
        return null;
      }
    },
    [applyCapabilities, clearNotice, showNotice],
  );

  // --- File panel data plane (peripheral service, /files + /file) -------------------
  // The thin backend serves a single sandboxed workspace; the ?task= param is
  // accepted but the root is the room's workspace_dir. Shapes:
  //   GET /files?task=<id>        → {root, tree}  (nested name/path/type/children)
  //   GET /file?task=<id>&path=…  → {path, size, truncated, content}  (text only)
  const discoverTaskFiles = useCallback(async (taskId) => {
    const id = String(taskId || "").trim();
    if (!id) return null;
    try {
      const res = await fetch(`/files?task=${encodeURIComponent(id)}`);
      const body = await res.json().catch(() => null);
      if (res.status === 200 && body && Array.isArray(body.tree)) {
        return { root: body.root || "", tree: body.tree };
      }
      return null;
    } catch (error) {
      return null;
    }
  }, []);

  const readTaskFile = useCallback(async (taskId, path) => {
    const id = String(taskId || "").trim();
    const p = String(path || "").trim();
    if (!id || !p) return { kind: "error" };
    try {
      const res = await fetch(
        `/file?task=${encodeURIComponent(id)}&path=${encodeURIComponent(p)}`,
      );
      const body = await res.json().catch(() => null);
      if (res.status === 200 && body && typeof body.content === "string") {
        return {
          kind: "text",
          path: body.path || p,
          size: body.size || 0,
          truncated: !!body.truncated,
          content: body.content,
        };
      }
      return { kind: "error" };
    } catch (error) {
      return { kind: "error" };
    }
  }, []);

  useEffect(() => {
    applyCapabilities();
    loadTaskList();
  }, [applyCapabilities, loadTaskList]);

  // Sandbox live-preview discovery: fetch GET /tasks/{id}/preview for the
  // active session when sandbox is enabled. Returns {token, panels} or 404.
  useEffect(() => {
    if (!sandboxEnabled || !activeTaskId) {
      setPreviewInfo(null);
      return undefined;
    }
    let cancelled = false;
    fetch(`/tasks/${encodeURIComponent(activeTaskId)}/preview`)
      .then((res) => (res.ok ? res.json() : null))
      .then((data) => {
        if (!cancelled) setPreviewInfo(data);
      })
      .catch(() => {
        if (!cancelled) setPreviewInfo(null);
      });
    return () => {
      cancelled = true;
    };
  }, [sandboxEnabled, activeTaskId]);

  const selectTask = useCallback(
    (taskId) => {
      // Re-clicking the loaded session is a no-op (except to retry an error).
      if (
        taskId &&
        taskId === activeTaskRef.current &&
        noticeRef.current?.kind !== "error"
      ) {
        return;
      }
      closeSse();
      setActiveTaskId(taskId);
      activeTaskRef.current = taskId;
      resetSessionState();
      if (!taskId) {
        setConnectionState("idle");
        clearNotice();
        return;
      }
      showNotice("loading", "Loading session...");
      loadTaskList({ silent: true });
      startStream(taskId);
    },
    [clearNotice, closeSse, loadTaskList, resetSessionState, showNotice, startStream],
  );

  const newSession = useCallback(() => {
    if (!commandIn) return;
    closeSse();
    setConnectionState("idle");
    setActiveTaskId(null);
    activeTaskRef.current = null;
    resetSessionState();
    loadTaskList({ silent: true });
    clearNotice();
  }, [clearNotice, closeSse, commandIn, loadTaskList, resetSessionState]);

  // Hard-delete a session via DELETE /tasks/{id}: the backend purges the task's
  // stream (and its subtask tree). Unlike the command verbs, this answers
  // synchronously — 200 {deleted:[...]} on success, 409 when the conversation is
  // actively running, 404 when it is already gone.
  const deleteSession = useCallback(
    async (taskId) => {
      const id = String(taskId || "").trim();
      if (!id) return false;
      try {
        const res = await fetch(`/tasks/${encodeURIComponent(id)}`, {
          method: "DELETE",
        });
        const body = await res.json().catch(() => null);
        if (res.status === 200) {
          // If the conversation on screen (or one of its subtasks) was purged,
          // drop back to a fresh session; otherwise just resync the sidebar.
          const removed = Array.isArray(body?.deleted) ? body.deleted : [id];
          if (removed.includes(activeTaskRef.current)) newSession();
          else loadTaskList({ silent: true });
          clearNotice();
          return true;
        }
        if (res.status === 409) {
          showNotice("error", "Can't delete a running conversation — stop it first.");
          return false;
        }
        if (res.status === 404) {
          // Already gone — just resync the sidebar so the stale row drops out.
          loadTaskList({ silent: true });
          return true;
        }
        showNotice(
          "error",
          `Could not delete session: ${body?.error || `HTTP ${res.status}`}`,
        );
        return false;
      } catch (error) {
        showNotice("error", "Network error deleting session.");
        return false;
      }
    },
    [activeTaskRef, clearNotice, loadTaskList, newSession, showNotice],
  );

  useEffect(() => {
    if (!initialTaskId || initialTaskLoadedRef.current) return;
    initialTaskLoadedRef.current = true;
    selectTask(initialTaskId);
  }, [initialTaskId, selectTask]);

  const showCommandError = useCallback(
    (status, body) => {
      if (body && body.reason === "invalid_model_selector") {
        showNotice(
          "error",
          `Model '${body.selector}' not allowed. Choose one of: ${
            Array.isArray(body.allowed) ? body.allowed.join(", ") : "?"
          }`,
        );
        return;
      }
      if (body && body.reason === "unknown_agent") {
        showNotice(
          "error",
          `Unknown agent '${body.agent}'. Available: ${
            Array.isArray(body.available) ? body.available.join(", ") : "?"
          }`,
        );
        return;
      }
      showNotice(
        "error",
        `Command failed: ${body?.error || body?.message || `HTTP ${status}`}`,
      );
    },
    [showNotice],
  );

  // Create a conversation or send the next goal. The codex-style command surface
  // is {goal, agent?, permission_mode?, enabled_mcp?, images?, model?, effort?,
  // workspace?} (workspace-and-session-path.md addendum 2026-06-28). model /
  // effort are per-turn (carried on BOTH /tasks and /messages); workspace is a
  // create-once durable binding so it rides ONLY the opening POST /tasks — later
  // turns fold-resolve it (zero mapping). Empty selectors are omitted, keeping a
  // bare turn byte-identical to the pre-codex request.
  const submitGoal = useCallback(
    async ({ goal, agent, permission, enabledMcp, images, model, effort, workspace }) => {
      const trimmed = String(goal || "").trim();
      if (!trimmed) return false;
      const permissionMode = permission || undefined;
      const enabledMcpList =
        Array.isArray(enabledMcp) && enabledMcp.length ? enabledMcp : undefined;
      const modelSelector = model ? String(model) : undefined;
      const effortSelector = effort ? String(effort) : undefined;
      const workspaceSelector = workspace ? String(workspace) : undefined;
      // T4 / spec D7 — the pasted/picked images for this turn. The request carries
      // ONLY {media_type, data_base64}; base64 lives solely on the wire (the host
      // decodes → ContentStore → ImageBlock(ContentRef); the ledger never stores
      // base64). Empty ⇒ omitted (byte-identical to a text-only turn).
      const imagesPayload =
        Array.isArray(images) && images.length
          ? images
              .map((img) =>
                img && img.media_type && img.data_base64
                  ? {
                      media_type: String(img.media_type),
                      data_base64: String(img.data_base64),
                    }
                  : null,
              )
              .filter(Boolean)
          : undefined;
      // T4 / spec D6 — before sending, SHA-256 each image's DECODED RAW BYTES
      // (== the backend ContentStore address) and cache hash→data:URL, so the
      // moment this user message reappears in the stream as an ImageBlock (same
      // hash), the bubble renders locally without waiting on GET /content/{hash}.
      // The hash MUST be over the raw bytes, not the base64 string, or the key
      // won't match the backend and the reloaded bubble breaks. Best-effort: a
      // missing crypto.subtle (very old browser) just skips the optimistic cache.
      if (imagesPayload && imagesPayload.length) {
        try {
          const pairs = await Promise.all(
            imagesPayload.map(async (img) => {
              const hash = await sha256Hex(base64ToBytes(img.data_base64));
              return [hash, dataUrlFromBase64(img.media_type, img.data_base64)];
            }),
          );
          setLocalImageCache((prev) => {
            const next = new Map(prev);
            for (const [hash, url] of pairs) if (hash) next.set(hash, url);
            return next;
          });
        } catch (error) {
          // Optimistic preview only; on failure the /content/{hash} path backstops.
        }
      }

      if (activeTaskRef.current === null) {
        const chosenAgent = String(agent || "").trim();
        if (!chosenAgent) {
          showNotice("error", "Agent name is required.");
          return false;
        }
        closeSse();
        resetSessionState();
        setOpeningSession(true);
        setPendingGoalText(trimmed);
        setConnectionState("connecting");
        clearNotice();
        let succeeded = false;
        try {
          const { status, body } = await postJSON("/tasks", {
            goal: trimmed,
            agent: chosenAgent,
            permission_mode: permissionMode,
            enabled_mcp: enabledMcpList,
            // T4 / spec D7: the opening turn may carry images (base64 on the wire).
            images: imagesPayload,
            // codex addendum: per-turn model/effort + the create-once workspace
            // binding ride the opening turn (undefined ⇒ dropped by JSON.stringify).
            model: modelSelector,
            effort: effortSelector,
            workspace: workspaceSelector,
          });
          if (status === 202 && body && body.task_id) {
            setActiveTaskId(body.task_id);
            activeTaskRef.current = body.task_id;
            setOpeningSession(false);
            // Keep the optimistic opening user bubble until the SSE catch-up and
            // /content deref produce the real user turn. POST /tasks returns only
            // after the first drive, so clearing it here creates a blank/loading
            // gap for a brand-new first session if body backfill is slow.
            startStream(body.task_id);
            loadTaskList({ silent: true });
            succeeded = true;
          } else {
            setConnectionState("idle");
            showCommandError(status, body);
          }
        } catch (error) {
          setConnectionState("idle");
          showNotice("error", "Network error creating session.");
        } finally {
          if (!succeeded) {
            setOpeningSession(false);
            setPendingGoalText(null);
          }
        }
        return succeeded;
      }

      setBusyLabel("Sending...");
      const taskId = activeTaskRef.current;
      let succeeded = false;
      try {
        const { status, body } = await postJSON(
          `/tasks/${encodeURIComponent(taskId)}/messages`,
          {
            goal: trimmed,
            permission_mode: permissionMode,
            enabled_mcp: enabledMcpList,
            // T4 / spec D7: per-turn images (base64 on the wire only).
            images: imagesPayload,
            // codex addendum: model/effort are per-turn; workspace is NOT sent on
            // a follow-up turn — the backend fold-resolves the durable binding.
            model: modelSelector,
            effort: effortSelector,
          },
        );
        if (status === 202) {
          clearNotice();
          succeeded = true;
        } else {
          showCommandError(status, body);
        }
      } catch (error) {
        showNotice("error", "Network error sending goal.");
      } finally {
        setBusyLabel("");
      }
      return succeeded;
    },
    [
      clearNotice,
      closeSse,
      loadTaskList,
      resetSessionState,
      showCommandError,
      showNotice,
      startStream,
    ],
  );

  const setApprovalHidden = useCallback((callIds, on) => {
    setHiddenApprovals((prev) => {
      const next = new Set(prev);
      for (const id of callIds) {
        if (on) next.add(id);
        else next.delete(id);
      }
      return next;
    });
  }, []);

  // POST a single approve/deny to a SPECIFIC task; returns true on success (202).
  const postApprovalResolve = useCallback(
    async (taskId, callId, approved) => {
      if (!taskId) return false;
      try {
        const verb = approved ? "approve" : "deny";
        const { status, body } = await postJSON(
          `/tasks/${encodeURIComponent(taskId)}/${verb}`,
          { call_id: callId, reason: approved ? undefined : "denied from chat UI" },
        );
        if (status === 202) {
          clearNotice();
          return true;
        }
        showCommandError(status, body);
        return false;
      } catch (error) {
        showNotice("error", "Network error resolving approval.");
        return false;
      }
    },
    [clearNotice, showCommandError, showNotice],
  );

  // Immediate single resolve (approve now): optimistic hide; if the commit fails
  // (network / 4xx), RESTORE the card so the user can retry (blocker 2).
  const resolveApproval = useCallback(
    async (callId, approved) => {
      const taskId = activeTaskRef.current;
      if (!taskId) return false;
      setApprovalHidden([callId], true);
      const ok = await postApprovalResolve(taskId, callId, approved);
      if (!ok) setApprovalHidden([callId], false);
      return ok;
    },
    [postApprovalResolve, setApprovalHidden],
  );

  // Cancel a still-pending deferred resolve (the Undo action): clear its timer,
  // forget it, and un-hide the card(s).
  const undoApprovalResolve = useCallback(
    (callIds) => {
      for (const callId of callIds) {
        const timer = approvalTimersRef.current.get(callId);
        if (timer != null) window.clearTimeout(timer);
        approvalTimersRef.current.delete(callId);
        approvalPendingRef.current.delete(callId);
      }
      setApprovalHidden(callIds, false);
    },
    [setApprovalHidden],
  );

  // Deferred resolve with a 5s undo window (deny + batch): hide optimistically,
  // capture the ORIGINAL task, schedule the commit, and show a toast whose Undo
  // action cancels it. A failed commit un-hides the card (blocker 2).
  const deferApprovalResolve = useCallback(
    (callIds, approved) => {
      const taskId = activeTaskRef.current;
      if (!taskId || !callIds.length) return;
      setApprovalHidden(callIds, true);
      for (const callId of callIds) {
        approvalPendingRef.current.set(callId, { taskId, approved });
        approvalTimersRef.current.set(
          callId,
          window.setTimeout(async () => {
            approvalTimersRef.current.delete(callId);
            approvalPendingRef.current.delete(callId);
            const ok = await postApprovalResolve(taskId, callId, approved);
            if (!ok) setApprovalHidden([callId], false);
          }, APPROVAL_UNDO_MS),
        );
      }
      const n = callIds.length;
      const text = approved
        ? `Approved ${n} ${n > 1 ? "items" : "item"}`
        : n > 1
          ? `Denied ${n} items; the model will try another approach`
          : "Denied; the model will try another approach";
      pushToast("info", text, {
        action: { label: "Undo", onClick: () => undoApprovalResolve(callIds) },
        duration: APPROVAL_UNDO_MS,
      });
    },
    [postApprovalResolve, pushToast, setApprovalHidden, undoApprovalResolve],
  );

  // Commit every in-flight deferred resolve NOW (a session switch / unmount) so a
  // pending decision is never silently dropped (blocker 1). Each commits to its OWN
  // captured task, not the current one.
  const flushApprovalResolves = useCallback(() => {
    const entries = [...approvalPendingRef.current.entries()];
    if (!entries.length) return;
    for (const [callId, rec] of entries) {
      const timer = approvalTimersRef.current.get(callId);
      if (timer != null) window.clearTimeout(timer);
      approvalTimersRef.current.delete(callId);
      approvalPendingRef.current.delete(callId);
      postApprovalResolve(rec.taskId, callId, rec.approved);
    }
    setHiddenApprovals(new Set());
  }, [postApprovalResolve]);

  approvalFlushRef.current = flushApprovalResolves;

  // Session switch → flush the previous session's deferred decisions (commit to
  // their original task). This hook does not unmount on a switch, so the timers
  // survive; flushing makes the outcome deterministic + immediate.
  useEffect(() => {
    flushApprovalResolves();
  }, [activeTaskId, flushApprovalResolves]);

  const submitQuestionAnswer = useCallback(
    async (questionId, answers) => {
      const taskId = activeTaskRef.current;
      if (!taskId) return;
      setBusyLabel("Submitting...");
      try {
        const { status, body } = await postJSON(
          `/tasks/${encodeURIComponent(taskId)}/answer`,
          { question_id: questionId, answers },
        );
        if (status === 202) clearNotice();
        else showCommandError(status, body);
      } catch (error) {
        showNotice("error", "Network error submitting answer.");
      } finally {
        setBusyLabel("");
      }
    },
    [clearNotice, showCommandError, showNotice],
  );

  const cancelSession = useCallback(
    async (targetTaskId = null) => {
      const taskId = targetTaskId || activeTaskRef.current;
      if (!taskId) return false;
      setBusyLabel("Cancelling...");
      try {
        const { status, body } = await postJSON(
          `/tasks/${encodeURIComponent(taskId)}/cancel`,
          { reason: "cancelled from chat UI" },
        );
        if (status === 202) {
          loadTaskList({ silent: true });
          return true;
        }
        showCommandError(status, body);
        return false;
      } catch (error) {
        showNotice("error", "Network error cancelling session.");
        return false;
      } finally {
        setBusyLabel("");
      }
    },
    [loadTaskList, showCommandError, showNotice],
  );

  const closeSession = useCallback(async () => {
    const taskId = activeTaskRef.current;
    if (!taskId) return;
    setBusyLabel("Closing...");
    try {
      const { status, body } = await postJSON(
        `/tasks/${encodeURIComponent(taskId)}/close`,
        { reason: "closed from chat UI" },
      );
      if (status === 202) clearNotice();
      else showCommandError(status, body);
    } catch (error) {
      showNotice("error", "Network error closing session.");
    } finally {
      setBusyLabel("");
    }
  }, [clearNotice, showCommandError, showNotice]);

  const reopenSession = useCallback(async () => {
    const taskId = activeTaskRef.current;
    if (!taskId) return;
    setBusyLabel("Reopening...");
    try {
      const { status, body } = await postJSON(
        `/tasks/${encodeURIComponent(taskId)}/reopen`,
        { reason: "reopened from chat UI" },
      );
      if (status === 202) clearNotice();
      else showCommandError(status, body);
    } catch (error) {
      showNotice("error", "Network error reopening session.");
    } finally {
      setBusyLabel("");
    }
  }, [clearNotice, showCommandError, showNotice]);

  // --- Content deref (GET /content/{hash}, raw bytes) ------------------------
  const fetchContentText = useCallback(async (hash) => {
    try {
      const res = await fetch(`/content/${encodeURIComponent(hash)}`);
      if (!res.ok) return null;
      return await res.text();
    } catch (error) {
      return null;
    }
  }, []);

  // BATCH message-body backfill: one /content fetch per still-missing hash fills
  // BOTH the full (canonical message array) and text ([{role,text}]) caches with
  // a single setState each — O(1) re-renders for an N-message history.
  const ensureMessageBodiesBatch = useCallback(
    async (hashes) => {
      if (!Array.isArray(hashes)) return;
      const toFetch = [];
      for (const hash of hashes) {
        if (!hash) continue;
        if (!messageFullCache.has(hash) && !messageInFlight.current.has(hash)) {
          messageInFlight.current.add(hash);
          toFetch.push(hash);
        }
      }
      if (!toFetch.length) return;
      const texts = await Promise.all(toFetch.map((h) => fetchContentText(h)));
      const resolved = [];
      const failed = [];
      texts.forEach((text, i) => {
        const projection = projectCacheableMessages(text);
        if (projection) resolved.push({ hash: toFetch[i], projection });
        else failed.push(toFetch[i]);
      });
      toFetch.forEach((h) => messageInFlight.current.delete(h));
      for (const { hash } of resolved) {
        messageRetryAttempts.current.delete(hash);
        const timer = messageRetryTimers.current.get(hash);
        if (timer != null) window.clearTimeout(timer);
        messageRetryTimers.current.delete(hash);
      }
      if (resolved.length) {
        setMessageFullCache((cache) => {
          const next = new Map(cache);
          resolved.forEach(({ hash, projection }) => next.set(hash, projection.full));
          return next;
        });
        setMessageTextCache((cache) => {
          const next = new Map(cache);
          resolved.forEach(({ hash, projection }) => next.set(hash, projection.text));
          return next;
        });
        if (resolved.some(({ projection }) => messagesContainUser(projection.full))) {
          setPendingGoalText(null);
        }
      }
      for (const hash of failed) {
        const attempts = messageRetryAttempts.current.get(hash) || 0;
        if (attempts >= 5 || messageRetryTimers.current.has(hash)) continue;
        messageRetryAttempts.current.set(hash, attempts + 1);
        const delayMs = Math.min(8000, 500 * 2 ** attempts);
        const timer = window.setTimeout(() => {
          messageRetryTimers.current.delete(hash);
          ensureMessageBodiesBatch([hash]);
        }, delayMs);
        messageRetryTimers.current.set(hash, timer);
      }
    },
    [fetchContentText, messageFullCache],
  );

  const ensureMessageBodies = useCallback(
    (hash) => {
      if (!hash) return;
      ensureMessageBodiesBatch([hash]);
    },
    [ensureMessageBodiesBatch],
  );

  const ensureThinkingBatch = useCallback(
    async (hashes) => {
      if (!Array.isArray(hashes)) return;
      const toFetch = [];
      for (const hash of hashes) {
        if (!hash) continue;
        if (
          !responseThinkingCache.has(hash) &&
          !thinkingInFlight.current.has(hash)
        ) {
          thinkingInFlight.current.add(hash);
          toFetch.push(hash);
        }
      }
      if (!toFetch.length) return;
      const texts = await Promise.all(toFetch.map((h) => fetchContentText(h)));
      toFetch.forEach((h) => thinkingInFlight.current.delete(h));
      setResponseThinkingCache((cache) => {
        const next = new Map(cache);
        toFetch.forEach((h, i) => next.set(h, extractThinking(texts[i])));
        return next;
      });
    },
    [fetchContentText, responseThinkingCache],
  );

  const ensureThinking = useCallback(
    (hash) => {
      if (!hash) return;
      ensureThinkingBatch([hash]);
    },
    [ensureThinkingBatch],
  );

  // Deref pending question bodies behind questions_ref so synthesizeDetail can
  // build the renderable QuestionPrompt form. Driven internally off the folded
  // pending-question refs (the old protocol got this from /tasks/{id} detail).
  const ensureQuestionsBatch = useCallback(
    async (refs) => {
      if (!Array.isArray(refs)) return;
      const toFetch = [];
      for (const ref of refs) {
        if (!ref) continue;
        if (!questionsCache.has(ref) && !questionsInFlight.current.has(ref)) {
          questionsInFlight.current.add(ref);
          toFetch.push(ref);
        }
      }
      if (!toFetch.length) return;
      const texts = await Promise.all(toFetch.map((h) => fetchContentText(h)));
      toFetch.forEach((h) => questionsInFlight.current.delete(h));
      setQuestionsCache((cache) => {
        const next = new Map(cache);
        toFetch.forEach((h, i) => next.set(h, extractQuestions(texts[i])));
        return next;
      });
    },
    [fetchContentText, questionsCache],
  );

  useEffect(() => {
    const refs = [];
    for (const q of vm.pendingQuestions || []) {
      if (q && q.questionsRef) refs.push(q.questionsRef);
    }
    for (const q of popupVm.pendingQuestions || []) {
      if (q && q.questionsRef) refs.push(q.questionsRef);
    }
    if (refs.length) ensureQuestionsBatch(refs);
  }, [vm.pendingQuestions, popupVm.pendingQuestions, ensureQuestionsBatch]);

  // --- Synthesized detail (no /tasks/{id} endpoint; folded from the stream) --
  const activeRow = useMemo(
    () => sessions.find((s) => s && s.task_id === activeTaskId) || null,
    [sessions, activeTaskId],
  );
  const activeDetail = useMemo(
    () => synthesizeDetail(activeTaskId, vm, activeRow, questionsCache),
    [activeTaskId, vm, activeRow, questionsCache],
  );

  const popupNode = popupStack.length
    ? popupStack[popupStack.length - 1]
    : null;
  const popupDetail = useMemo(
    () =>
      synthesizeDetail(
        popupTaskId,
        popupVm,
        popupNode
          ? { title: popupNode.goal, agent_name: popupNode.agentName }
          : null,
        questionsCache,
      ),
    [popupTaskId, popupVm, popupNode, questionsCache],
  );

  // backfillHistory / syncActiveSession: the stream is the single source of
  // truth and auto-resumes, so these reduce to a reconnect / list refresh for
  // any caller that still invokes them.
  const backfillHistory = useCallback(() => {
    startStream(activeTaskRef.current);
  }, [startStream]);

  const syncActiveSession = useCallback(() => {
    loadTaskList({ silent: true });
  }, [loadTaskList]);

  const status = useMemo(
    () => ({
      connection: connectionLabel(connectionState),
      canSend:
        commandIn &&
        (activeTaskId === null
          ? taskListLoaded
          : vm.wakeKind === "next-goal" && !vm.closed),
      terminal:
        vm.status === "completed" ||
        vm.status === "failed" ||
        vm.status === "cancelled",
      closed: !!vm.closed,
    }),
    [activeTaskId, commandIn, connectionState, taskListLoaded, vm],
  );

  return {
    activeDetail,
    activeTaskId,
    backfillHistory,
    backgroundJobs,
    busyLabel,
    cancelSession,
    clearNotice,
    closeSession,
    commandIn,
    sandboxEnabled,
    createMcpServer,
    updateMcpServer,
    deleteMcpServer,
    createWorkspace,
    deleteSession,
    discoverMcpServerTools,
    discoverTaskFiles,
    readTaskFile,
    setMcpServerTools,
    ensureMessageBodies,
    ensureMessageBodiesBatch,
    ensureThinking,
    ensureThinkingBatch,
    events,
    loadTaskList,
    localImageCache,
    messageFullCache,
    messageTextCache,
    newSession,
    toasts,
    dismissToast,
    pushToast,
    openingSession,
    options,
    pendingGoalText,
    previewInfo,
    popupStack,
    popupEvents,
    popupVm,
    popupDetail,
    popupTaskId,
    openSubtask,
    drillSubtask,
    gotoBreadcrumb,
    closeSubtask,
    reopenSession,
    resolveApproval,
    deferApprovalResolve,
    undoApprovalResolve,
    hiddenApprovals,
    responseThinkingCache,
    selectTask,
    sessions,
    setPendingGoalText,
    showNotice,
    status,
    streamingTurn,
    submitGoal,
    submitQuestionAnswer,
    syncActiveSession,
    vm,
  };
}

// Session-level background-shell jobs, folded purely
// from the active session's event stream. The BackgroundShell* events ride the
// same stream the chat already merges and serialize the ref canonically, so we
// fold them here. Append-only, mirroring the server's audit list — terminal jobs
// stay visible.
//   Started → running entry (spawn-snapshot ref)
//   Polled  → advance ref to the latest snapshot
//   Exited  → "exited" + final_ref + exit_code
//   Killed  → "killed" + signal
//   Lost    → "lost" after host restart / orphan recovery
function foldBackgroundJobs(events) {
  const byId = new Map();
  const order = [];
  for (const env of events) {
    if (!env || typeof env.type !== "string") continue;
    const p = env.payload || {};
    const jobId = p.job_id;
    if (!jobId) continue;
    switch (env.type) {
      case "BackgroundShellStarted": {
        if (!byId.has(jobId)) order.push(jobId);
        byId.set(jobId, {
          jobId,
          command: typeof p.command === "string" ? p.command : "",
          status: "running",
          spawnedBy: p.spawned_by_task_id || null,
          ref: p.ref || null,
        });
        break;
      }
      case "BackgroundShellPolled": {
        const job = byId.get(jobId);
        if (job && p.ref) job.ref = p.ref;
        break;
      }
      case "BackgroundShellExited": {
        const job = byId.get(jobId);
        if (job) {
          job.status = "exited";
          if (p.final_ref) job.ref = p.final_ref;
          if (typeof p.exit_code === "number") job.exitCode = p.exit_code;
        }
        break;
      }
      case "BackgroundShellKilled": {
        const job = byId.get(jobId);
        if (job) {
          job.status = "killed";
          if (typeof p.signal === "number") job.signal = p.signal;
        }
        break;
      }
      case "BackgroundShellLost": {
        const job = byId.get(jobId);
        if (job) job.status = "lost";
        break;
      }
      default:
        break;
    }
  }
  return order.map((id) => byId.get(id));
}

function projectCacheableMessages(text) {
  const full = projectMessagesFull(text);
  if (!Array.isArray(full)) return null;
  return { full, text: projectMessagesText(full) };
}

function messagesContainUser(full) {
  return Array.isArray(full) && full.some((message) => message?.role === "user");
}

export { useChatData, projectCacheableMessages, messagesContainUser };
