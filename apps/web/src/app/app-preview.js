// pure decision logic for the "App"
// tab, extracted from ChatApp.jsx for unit testing (see app-preview.test.js). Both pure
// functions avoid React and IO; they read from the already-folded event stream:
//
//   1. latestOpenApp(events)        — {url, dir, seq} of the **latest** open_app
//                                     side_effect in the stream, or null. When the model
//                                     calls ``open_app``,
//                                     the backend puts a {type:"open_app", url, dir} into
//                                     payload.side_effects of that ToolResultRecorded
//                                     (verified to reach the frontend via the event
//                                     stream, see chat-data.js).
//   2. appReloadHitSeq(events, dir) — reuses file-refresh's editsFromEvents; returns the
//                                     seq of the most recent successful change to any file
//                                     under the mount dir's ``dir + "/"`` prefix, or -1 on
//                                     no match. A match with an advancing seq means the
//                                     iframe should reload. It
//                                     reuses the same change detection rather than building
//                                     a separate refresh channel.
//
// Same stance as file-refresh.js: the envelope is the only source of truth; match info
// comes only from structured fields (side_effects / arguments.path), never from regexing
// the body or summary.

import { editsFromEvents, normalizeRelPath } from "./file-refresh.js";

// The **latest** ``open_app`` side-effect in the stream. Scans all ToolResultRecorded,
// picks side_effects with type==="open_app" and a usable url, and takes the one with the
// largest seq (seq is monotonic and totally orders one session). Returns {url, dir, seq}
// or null.
//
// Why max seq rather than "last seen": events may be merged out of order (mergeEvents
// dedupes and sorts by seq, but this function doesn't rely on input order), so comparing
// by seq is most robust. A missing seq (should not happen) is treated as -1 and can never
// override an entry with a real seq.
function latestOpenApp(events) {
  let best = null; // { url, dir, seq }
  for (const env of Array.isArray(events) ? events : []) {
    if (!env || env.type !== "ToolResultRecorded") continue;
    const p = env.payload || {};
    const effects = Array.isArray(p.side_effects) ? p.side_effects : [];
    const seq = typeof env.seq === "number" ? env.seq : -1;
    for (const eff of effects) {
      if (!eff || eff.type !== "open_app") continue;
      const url = typeof eff.url === "string" ? eff.url.trim() : "";
      if (!url) continue; // an open_app with no url can't be rendered, skip it
      const dir = typeof eff.dir === "string" ? eff.dir : "";
      if (!best || seq > best.seq) best = { url, dir, seq };
    }
  }
  return best;
}

// The seq of the most recent successful write/edit to any file under the mount dir
// ``appDir`` (``appDir + "/"`` prefix), or -1 on no match. Reuses file-refresh's
// editsFromEvents (only successful write/edit, paths already normalized) and adds one
// "prefix match" filter: edits to files outside the mount dir don't match, so the app
// iframe isn't reloaded needlessly (acceptance
// criterion 2).
//
// appDir is normalized too (the model's dir may have ./, trailing slash, etc.); empty or
// out-of-bounds yields -1. The prefix compare uses ``dir + "/"`` rather than bare ``dir``
// to avoid "app" falsely matching "apple/x.js".
function appReloadHitSeq(events, appDir) {
  const root = normalizeRelPath(appDir);
  if (!root) return -1;
  const prefix = root + "/";
  let hit = -1;
  for (const edit of editsFromEvents(events)) {
    if (typeof edit.path !== "string") continue;
    if (!edit.path.startsWith(prefix)) continue; // match files under the mount dir prefix
    const seq = typeof edit.seq === "number" ? edit.seq : -1;
    if (seq > hit) hit = seq;
  }
  return hit;
}

export { latestOpenApp, appReloadHitSeq };
