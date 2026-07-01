function shortId(id) {
  if (typeof id !== "string") return "";
  return id.length > 12 ? id.slice(0, 12) : id;
}

function shortHash(hash) {
  if (typeof hash !== "string") return "";
  return hash.length > 12 ? hash.slice(0, 12) : hash;
}

function safeJson(value) {
  try {
    return JSON.stringify(value, null, 2);
  } catch (error) {
    return String(value);
  }
}

function formatTokens(value) {
  if (value == null || !Number.isFinite(value)) return "0";
  if (value < 1000) return String(value);
  return `${Math.round(value / 100) / 10}k`;
}

// Rough byte→token estimate (~4 chars/token) for the trace inspector's per-region
// badges. Deliberately approximate — real per-call token counts come from the
// LLM events; this only labels regions/messages that have no recorded count.
function approxTokens(text) {
  const s = typeof text === "string" ? text : "";
  if (!s) return 0;
  return Math.max(1, Math.ceil(s.length / 4));
}

function formatCost(value) {
  if (value == null || !Number.isFinite(value) || value === 0) return "$0";
  if (value < 0.01) return `$${value.toFixed(4)}`;
  return `$${value.toFixed(value < 1 ? 3 : 2)}`;
}

// Wall-clock HH:MM for a chat bubble timestamp. Input is an event's
// `occurred_at` (Unix seconds, float). Returns "" for anything non-finite so
// callers can skip rendering rather than show "NaN:NaN".
function formatClock(occurredAt) {
  if (occurredAt == null || !Number.isFinite(occurredAt)) return "";
  const date = new Date(occurredAt * 1000);
  const hours = String(date.getHours()).padStart(2, "0");
  const minutes = String(date.getMinutes()).padStart(2, "0");
  return `${hours}:${minutes}`;
}

function formatDuration(seconds) {
  if (seconds == null || !Number.isFinite(seconds)) return "-";
  const safe = Math.max(0, seconds);
  if (safe < 1) return `${Math.round(safe * 1000)}ms`;
  if (safe < 60) return `${Math.round(safe * 10) / 10}s`;
  const whole = Math.round(safe);
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return `${minutes}m${String(rest).padStart(2, "0")}s`;
}

export {
  approxTokens,
  formatClock,
  formatCost,
  formatDuration,
  formatTokens,
  safeJson,
  shortHash,
  shortId,
};
