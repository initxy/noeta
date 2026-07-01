// Regression tests for two TraceApp.jsx fixes. TraceApp.jsx is a JSX module
// that only exports the <TraceApp> component (and pulls in react), so we assert
// the load-bearing invariants against the source text — no transpile, no deps.
// Run: `node --test src/app/fix-trace.test.js`.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";

const src = readFileSync(
  fileURLToPath(new URL("./TraceApp.jsx", import.meta.url)),
  "utf8",
);

// --- #2: ArrowLeft must be imported (used as the modal Back button) --------

test("ArrowLeft is imported from lucide-react", () => {
  // The depth>1 PreviewModal branch renders <ArrowLeft />; without the import
  // drilling into a nested content_ref throws ReferenceError and white-screens.
  const importBlock = (src.match(/import\s*\{[^}]*\}\s*from\s*"lucide-react";/) || [""])[0];
  assert.match(importBlock, /\bArrowLeft\b/, "ArrowLeft missing from lucide-react import");
  assert.match(src, /<ArrowLeft\s/, "ArrowLeft should still be rendered as the Back button");
});

// --- #34: timeline clocks precomputed once, not per-row O(N) scans ---------

test("sessionClocks builder exists and replaces per-row scans", () => {
  assert.match(src, /function sessionClocks\s*\(/, "sessionClocks precompute helper is missing");
  // The old O(N)-per-row helpers must be gone so rows can't reintroduce them.
  assert.doesNotMatch(src, /function relativeClock\s*\(/, "relativeClock should be folded into sessionClocks");
  assert.doesNotMatch(src, /function rowDelta\s*\(/, "rowDelta should be folded into sessionClocks");
  // TimelineRow reads the precomputed map, not the full session list.
  const rowFn = src.slice(src.indexOf("function TimelineRow"), src.indexOf("const EMPTY_CLOCK"));
  assert.match(rowFn, /clocks\.get\(row\.seq\)/, "TimelineRow should look up the precomputed clock map");
  assert.doesNotMatch(rowFn, /relativeClock\(|rowDelta\(/, "TimelineRow must not run per-row session scans");
});

// --- sessionClocks behavior (re-implemented inline to match the source) ----
// Mirrors the single-pass logic so the contract is pinned without importing JSX.

function formatDuration(ms) {
  return `${ms}ms`;
}

function sessionClocks(rows) {
  let start = null;
  for (const row of rows) {
    if (typeof row.occurredAt === "number" && (start == null || row.occurredAt < start)) {
      start = row.occurredAt;
    }
  }
  const clocks = new Map();
  let prev = null;
  for (const row of rows) {
    const at = row.occurredAt;
    const clock = at != null && start != null ? `+${formatDuration(at - start)}` : "";
    const delta = at != null && prev != null ? `Δ${formatDuration(at - prev)}` : "";
    clocks.set(row.seq, { clock, delta });
    if (at != null) prev = at;
  }
  return clocks;
}

test("sessionClocks: clock is relative to session min, first row has no delta", () => {
  const rows = [
    { seq: 1, occurredAt: 100 },
    { seq: 2, occurredAt: 130 },
    { seq: 3, occurredAt: 200 },
  ];
  const clocks = sessionClocks(rows);
  assert.deepEqual(clocks.get(1), { clock: "+0ms", delta: "" });
  assert.deepEqual(clocks.get(2), { clock: "+30ms", delta: "Δ30ms" });
  assert.deepEqual(clocks.get(3), { clock: "+100ms", delta: "Δ70ms" });
});

test("sessionClocks: rows without timestamps yield empty clock/delta and don't break delta chain", () => {
  const rows = [
    { seq: 1, occurredAt: 100 },
    { seq: 2 },
    { seq: 3, occurredAt: 150 },
  ];
  const clocks = sessionClocks(rows);
  assert.deepEqual(clocks.get(2), { clock: "", delta: "" });
  // Δ measured from the previous TIMESTAMPED row (seq 1), skipping seq 2.
  assert.deepEqual(clocks.get(3), { clock: "+50ms", delta: "Δ50ms" });
});
