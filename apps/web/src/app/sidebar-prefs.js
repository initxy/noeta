// Sidebar session-list prefs + pure filters for the codex-style sidebar
// (workspace-and-session-path.md addendum 2026-06-28). Pinned sessions persist
// to localStorage keyed by task_id, mirroring the collapsed-groups try/catch
// contract in ChatApp (a disabled / full localStorage degrades to "no memory"
// and never throws). The search + partition helpers are pure so the sidebar's
// filtering logic is unit-testable without a DOM.

const PINNED_SESSIONS_KEY = "noeta.sidebar.pinnedSessions";

function loadPinnedSessions() {
  try {
    const raw = window.localStorage.getItem(PINNED_SESSIONS_KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(
      Array.isArray(arr) ? arr.filter((id) => typeof id === "string") : [],
    );
  } catch (e) {
    return new Set();
  }
}

function savePinnedSessions(set) {
  try {
    window.localStorage.setItem(
      PINNED_SESSIONS_KEY,
      JSON.stringify([...set]),
    );
  } catch (e) {
    /* ignored when localStorage is unavailable */
  }
}

// Case-insensitive client-side filter on a session row's title. Empty query =
// pass-through (the whole list). Rows without a title fall back to never
// matching a non-empty query (they only show when the search box is empty).
function filterSessionsBySearch(rows, query) {
  const list = Array.isArray(rows) ? rows : [];
  const q = String(query || "").trim().toLowerCase();
  if (!q) return list;
  return list.filter((row) =>
    String(row?.title || "").toLowerCase().includes(q),
  );
}

// Split rows into the pinned subset (in their original list order) and the rest,
// so the sidebar can render a "Pinned" group above the workspace groups.
function partitionPinned(rows, pinned) {
  const pinnedRows = [];
  const rest = [];
  for (const row of Array.isArray(rows) ? rows : []) {
    if (row && pinned && pinned.has(row.task_id)) pinnedRows.push(row);
    else rest.push(row);
  }
  return { pinnedRows, rest };
}

export {
  PINNED_SESSIONS_KEY,
  loadPinnedSessions,
  savePinnedSessions,
  filterSessionsBySearch,
  partitionPinned,
};
