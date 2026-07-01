// pure decision logic for the file
// panel's two-tier live refresh, extracted from FilePanel.jsx / ChatApp.jsx for isolated
// unit testing (see file-refresh.test.js). All three are pure functions: no React, no IO.
//
//   1. editsFromEvents(events)      — scan the SSE/history event stream and find which
//                                     write/edit tools actually changed a file, and which
//                                     (normalized) relative path they changed.
//   2. previewHitSeq(events, path)  — the seq of the most recent change to the file
//                                     currently being previewed (-1 if no hit). Used to
//                                     decide whether to re-fetch the preview.
//   3. turnWentIdle(prev, next)     — the edge where a turn drops from running to idle.
//                                     Used to decide whether to re-fetch the whole file tree.
//
// Hit detection ONLY reuses the structured file path in the tool call's arguments (the
// reducer has folded arguments into vm.toolCalls); it never regex-guesses paths from body
// text or summary.

// The repo's actual write-file tool names (packages/noeta-sdk/noeta/tools/fs/edit.py):
//   - ``write`` = WriteFileTool (create/overwrite whole file, arguments.path + arguments.content)
//   - ``edit``  = ReplaceTextTool (replace a unique fragment, arguments.path + old + new)
// Both put the target file in arguments.path (workspace-relative). Other write forms (patch,
// etc.) are out of scope for v1 — only these two first-class write-file tools count.
const WRITE_EDIT_TOOLS = new Set(["write", "edit"]);

function isWriteEditTool(toolName) {
  return typeof toolName === "string" && WRITE_EDIT_TOOLS.has(toolName);
}

// Normalize a relative path to the same POSIX-relative form used by the backend's
// /tasks/{id}/file?path= and /files, then compare for a hit. Both backend endpoints emit/
// accept workspace-relative POSIX paths and use WorkspaceRoot.resolve to fold ``.`` / ``..``.
// The path the model gives in arguments.path may carry a ``./`` prefix, backslashes, extra
// slashes, or a trailing slash, so we fold the same way here to avoid missing a hit just
// because the same file was spelled differently. Out-of-bounds (``..`` escaping the root)
// returns null (the backend also rejects it with 400).
function normalizeRelPath(path) {
  let p = String(path == null ? "" : path).trim();
  if (!p) return null;
  p = p.replace(/\\/g, "/"); // backslash -> forward slash
  if (p.startsWith("/")) return null; // absolute path is not workspace-relative
  const out = [];
  for (const seg of p.split("/")) {
    if (seg === "" || seg === ".") continue; // drop empty / current-dir segments
    if (seg === "..") {
      if (out.length === 0) return null; // escaped the root -> out of bounds
      out.pop();
      continue;
    }
    out.push(seg);
  }
  if (out.length === 0) return null;
  return out.join("/");
}

// Fold the event stream into a list of write/edit changes that landed on disk:
// {callId, seq, path} (path already normalized).
//
// Hit info is split across two events: ``ToolCallStarted`` carries tool_name + arguments
// (including path), ``ToolResultRecorded`` carries success. So we record the started tool
// name/path by call_id, and emit one entry once the matching result succeeds. Only write/edit
// count, and only success===true (a dry-run, a failure, or an edit with 0 or multiple matches
// did not change the file and must not trigger a refresh).
//
// Pure function: same event sequence -> same output, sharing the reducer's "envelope is the
// single source of truth" stance.
function editsFromEvents(events) {
  const started = new Map(); // call_id -> { toolName, path }
  const edits = [];
  for (const env of Array.isArray(events) ? events : []) {
    if (!env || typeof env.type !== "string") continue;
    const p = env.payload || {};
    if (env.type === "ToolCallStarted") {
      const callId = p.call_id;
      if (!callId || !isWriteEditTool(p.tool_name)) continue;
      const args = p.arguments || {};
      started.set(callId, { toolName: p.tool_name, path: args.path });
      continue;
    }
    if (env.type === "ToolResultRecorded") {
      const callId = p.call_id;
      if (!callId || p.success !== true) continue;
      const info = started.get(callId);
      if (!info) continue; // not write/edit (or never saw its started event)
      const rel = normalizeRelPath(info.path);
      if (!rel) continue; // missing / out-of-bounds path -> no way to hit
      const seq = typeof env.seq === "number" ? env.seq : null;
      edits.push({ callId, seq, path: rel });
    }
  }
  return edits;
}

// The seq of the most recent successful change, in this batch of events, to the file
// currently being previewed (selectedPath); -1 if no hit. FilePanel compares it against the
// seq it last refreshed on, and re-fetches the preview when this one is larger. A change to
// some other file => no hit => no new seq => the current preview is not pointlessly re-fetched.
function previewHitSeq(events, selectedPath) {
  const target = normalizeRelPath(selectedPath);
  if (!target) return -1;
  let hit = -1;
  for (const edit of editsFromEvents(events)) {
    if (edit.path !== target) continue;
    const seq = typeof edit.seq === "number" ? edit.seq : -1;
    if (seq > hit) hit = seq;
  }
  return hit;
}

// The edge where a turn drops from running to idle (true -> false). The other three
// combinations (still running, still idle, just started running) all return false — the tree
// is re-fetched in full ONLY on this edge, not re-walked on every tool result, to avoid churn.
// The truth of ``working`` is computed by ChatApp's isAgentWorking (detail's wake_kind/
// status_text plus vm.status); here we only check the edge, not repeat that decision.
function turnWentIdle(prevWorking, nextWorking) {
  return prevWorking === true && nextWorking === false;
}

export {
  WRITE_EDIT_TOOLS,
  isWriteEditTool,
  normalizeRelPath,
  editsFromEvents,
  previewHitSeq,
  turnWentIdle,
};
