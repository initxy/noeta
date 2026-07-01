import { KS_ICONS } from "../../shared/icons.js";

function ksCategory(t) {
  if (/^Tool/.test(t)) return t.includes("Approval") || t.includes("Denied")
    ? "cat-governance" : "cat-tool";
  if (/^LLM/.test(t)) return "cat-llm";
  if (t.includes("Context") || t === "AgentBound") return "cat-context";
  if (t.includes("Question") || t.includes("Subtask")) return "cat-governance";
  if (t.startsWith("Messages")) return "cat-message";
  return "cat-lifecycle";
}

const KS_CATEGORIES = [
  { cls: "cat-lifecycle", label: "lifecycle", icon: KS_ICONS.flag },
  { cls: "cat-tool", label: "tool", icon: KS_ICONS.tool },
  { cls: "cat-llm", label: "llm", icon: KS_ICONS.bolt },
  { cls: "cat-context", label: "context", icon: KS_ICONS.layers },
  { cls: "cat-governance", label: "governance", icon: KS_ICONS.flag },
  { cls: "cat-message", label: "message", icon: KS_ICONS.message },
];

function traceRows(envelopes) {
  const rows = [];
  for (const env of envelopes || []) {
    if (!env || typeof env.type !== "string") continue;
    const payload = env.payload || {};
    rows.push({
      seq: typeof env.seq === "number" ? env.seq : null,
      type: env.type,
      summary: traceSummary(env.type, payload),
      payload: payload,
      category: ksCategory(env.type),
      occurredAt:
        typeof env.occurred_at === "number" ? env.occurred_at : null,
    });
  }
  return rows;
}

function traceSummary(type, payload) {
  switch (type) {
    case "ToolCallStarted":
      return (payload.tool_name || "tool") + " started";
    case "ToolResultRecorded":
      return (payload.success === true ? "ok" : "error") +
        (payload.summary ? " · " + payload.summary : "");
    case "ToolCallApprovalRequested":
      return "approval " + (payload.tool_name || payload.call_id || "");
    case "ToolCallApprovalResolved":
      return payload.approved === true ? "approved" : "denied";
    case "ToolCallDenied":
      return "denied" + (payload.reason ? " · " + payload.reason : "");
    case "TaskSuspended":
    case "TaskCancelled":
    case "TaskFailed":
      return payload.reason || "";
    case "ConversationClosed":
      return payload.reason || payload.closed_by || "";
    case "ConversationReopened":
      return payload.reopened_by || "";
    case "ModelBound":
      return payload.model || "";
    case "SubtaskSpawned":
      return payload.agent_name || payload.subtask_id || "";
    case "CompactionRequested":
      return (
        "compaction triggered · " +
        (payload.reason === "overflow"
          ? "overflow fallback"
          : payload.reason === "proactive"
            ? "proactive"
            : payload.reason || "") +
        (payload.estimated_tokens
          ? ` · ~${payload.estimated_tokens} tok`
          : "")
      );
    case "Compacted":
      return (
        "summary compaction · folded " +
        (payload.boundary_count != null ? payload.boundary_count : "?") +
        " msgs → summary"
      );
    default:
      return "";
  }
}

// Lifecycle / pipeline plumbing that clutters the timeline without describing
// what the agent actually did. Hidden from the main timeline by default and
// routed into the collapsible "raw events" drawer; the "show all" toggle puts
// them back inline. Kept as an explicit set so the line between "noise" and
// "meaningful action" is one obvious place to retune later.
const NOISE_LIFECYCLE_TYPES = new Set([
  "TaskCreated",
  "AgentBound",
  "ModelBound",
  "TaskHostBound",
  "MessagesAppended",
  "TaskStarted",
  "TaskSnapshot",
  "TaskSuspended",
  "TaskWoken",
]);

// A row is "meaningful" (kept in the main timeline) unless it is one of the
// known lifecycle plumbing events above. Anything new defaults to meaningful so
// a freshly added event type is never silently hidden.
function isNoiseRow(row) {
  return NOISE_LIFECYCLE_TYPES.has(row?.type);
}

// Group a flat list of trace rows into turns. A turn starts at each
// `LLMRequestStarted` and absorbs every following event up to (but not
// including) the next request; any rows before the first request land in a
// leading "setup" group. Rows keep their identity, so the timeline can still
// render each one as a `.timeline-row`.
function groupTurns(rows) {
  const groups = [];
  let current = null;
  let turnNo = 0;
  for (const row of rows || []) {
    if (row.type === "LLMRequestStarted") {
      turnNo += 1;
      current = { kind: "turn", turnNo, headSeq: row.seq, rows: [row] };
      groups.push(current);
      continue;
    }
    if (!current) {
      current = { kind: "setup", turnNo: 0, headSeq: null, rows: [] };
      groups.push(current);
    }
    current.rows.push(row);
  }
  return groups;
}

function collectArtifacts(envelopes) {
  const refs = [];
  const seen = new Set();
  const toolNames = new Map();
  for (const env of envelopes || []) {
    if (!env || !env.payload) continue;
    if (
      env.type === "ToolCallStarted" &&
      env.payload.call_id &&
      env.payload.tool_name
    ) {
      toolNames.set(env.payload.call_id, env.payload.tool_name);
      continue;
    }
    if (env.type !== "ToolResultRecorded") continue;
    const callId = env.payload.call_id || null;
    const toolName = callId ? toolNames.get(callId) || null : null;
    const seq = typeof env.seq === "number" ? env.seq : null;
    const output = env.payload.output_ref;
    if (output && output.hash && !seen.has(output.hash)) {
      seen.add(output.hash);
      refs.push({
        label: "output",
        hash: output.hash,
        mediaType: output.media_type || "",
        callId,
        toolName,
        seq,
      });
    }
    const artifacts = Array.isArray(env.payload.artifacts)
      ? env.payload.artifacts
      : [];
    artifacts.forEach((art, i) => {
      if (!art || !art.hash || seen.has(art.hash)) return;
      seen.add(art.hash);
      refs.push({
        label: "artifact " + (i + 1),
        hash: art.hash,
        mediaType: art.media_type || "",
        callId,
        toolName,
        seq,
      });
    });
  }
  return refs;
}

export {
  KS_CATEGORIES,
  NOISE_LIFECYCLE_TYPES,
  collectArtifacts,
  groupTurns,
  isNoiseRow,
  ksCategory,
  traceRows,
  traceSummary,
};
