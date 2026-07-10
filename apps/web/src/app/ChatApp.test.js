// Regression coverage for the composer-draft session-bleed fix: switching
// sessions used to leave session A's still-typed text / queued images sitting
// in the composer, so hitting Enter on session B sent them there instead.
//
// ChatApp.jsx is a JSX module (pulls in react + lucide-react), so — same
// approach as fix-trace.test.js / trace-improvements.test.js — the pure
// `applySessionDraftSwitch` helper is re-implemented inline here to pin its
// behavioral contract, and the source text is asserted against to confirm
// ChatApp.jsx actually wires that same logic into the session-switch effect
// (rather than importing the JSX module directly, which node --test can't
// parse).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const src = readFileSync(
  fileURLToPath(new URL("./ChatApp.jsx", import.meta.url)),
  "utf8",
);

// --- structural wiring -------------------------------------------------

test("applySessionDraftSwitch is defined and exported for reuse/testing", () => {
  assert.match(
    src,
    /function applySessionDraftSwitch\(draftsMap, previousTid, tid, current\)/,
    "applySessionDraftSwitch helper is missing or its signature changed",
  );
  assert.match(
    src,
    /export \{ ChatApp, applySessionDraftSwitch \};/,
    "applySessionDraftSwitch should stay exported",
  );
});

test("the session-switch effect saves/restores the draft via the helper", () => {
  assert.match(
    src,
    /const sessionDraftsRef = useRef\(new Map\(\)\);/,
    "the per-session draft map is missing",
  );
  const effectStart = src.indexOf("const hydratedTaskRef");
  const effectSrc = src.slice(
    effectStart,
    src.indexOf("}, [chat.activeTaskId]);", effectStart),
  );
  assert.match(
    effectSrc,
    /applySessionDraftSwitch\(sessionDraftsRef\.current, previousTid, tid, \{/,
    "the hydration effect must run the draft through applySessionDraftSwitch",
  );
  assert.match(effectSrc, /setComposerText\(draft\.text\);/, "composerText must be restored from the draft");
  assert.match(effectSrc, /setPastedImages\(draft\.images\);/, "pastedImages must be restored from the draft");
});

test("ChatComposer receives the lifted pastedImages state as controlled props", () => {
  assert.match(
    src,
    /pastedImages=\{pastedImages\}/,
    "ChatComposer should receive the lifted pastedImages state",
  );
  assert.match(
    src,
    /onPastedImagesChange=\{setPastedImages\}/,
    "ChatComposer should receive a way to update the lifted pastedImages state",
  );
});

// --- behavioral contract (mirrored inline) ------------------------------
// Mirrors ChatApp.jsx's applySessionDraftSwitch exactly, so the contract is
// pinned without importing the JSX module.
function applySessionDraftSwitch(draftsMap, previousTid, tid, current) {
  if (previousTid !== undefined) {
    if (current.text || (current.images && current.images.length)) {
      draftsMap.set(previousTid, { text: current.text, images: current.images });
    } else {
      draftsMap.delete(previousTid);
    }
  }
  const draft = draftsMap.get(tid);
  return { text: draft?.text || "", images: draft?.images || [] };
}

test("first run (previousTid undefined) stashes nothing and returns a blank draft", () => {
  const drafts = new Map();
  const draft = applySessionDraftSwitch(drafts, undefined, null, { text: "ignored", images: [] });
  assert.deepEqual(draft, { text: "", images: [] });
  assert.equal(drafts.size, 0);
});

test("switching away from a session with a draft stashes it, and the incoming session starts blank", () => {
  const drafts = new Map();
  // Landing (null) -> session A: nothing to stash yet.
  applySessionDraftSwitch(drafts, undefined, "A", { text: "", images: [] });
  // Type a draft in A, then switch to B: A's draft must be stashed, B starts blank.
  const images = [{ id: "img1", media_type: "image/png", data_base64: "x" }];
  const draftForB = applySessionDraftSwitch(drafts, "A", "B", { text: "hello from A", images });
  assert.deepEqual(draftForB, { text: "", images: [] });
  assert.deepEqual(drafts.get("A"), { text: "hello from A", images });
});

test("switching back to a session with a stashed draft restores its text and images", () => {
  const drafts = new Map();
  applySessionDraftSwitch(drafts, undefined, "A", { text: "", images: [] });
  const images = [{ id: "img1" }];
  applySessionDraftSwitch(drafts, "A", "B", { text: "hello from A", images });
  // Switch back from B (blank) to A: A's stashed draft must come back.
  const draftForA = applySessionDraftSwitch(drafts, "B", "A", { text: "", images: [] });
  assert.deepEqual(draftForA, { text: "hello from A", images });
});

test("a blank outgoing draft clears any previously stashed entry (e.g. after a successful send)", () => {
  const drafts = new Map();
  applySessionDraftSwitch(drafts, undefined, "A", { text: "", images: [] });
  applySessionDraftSwitch(drafts, "A", "B", { text: "hello from A", images: [] });
  assert.equal(drafts.has("A"), true);
  // Back on A: draft restores, user sends (clearing text), then switches to C.
  applySessionDraftSwitch(drafts, "B", "A", { text: "", images: [] });
  const draftForC = applySessionDraftSwitch(drafts, "A", "C", { text: "", images: [] });
  assert.deepEqual(draftForC, { text: "", images: [] });
  assert.equal(drafts.has("A"), false, "a blank draft must not linger in the map");
});
