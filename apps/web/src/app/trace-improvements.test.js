// Regression tests for the four trace-view improvements:
//   1. selecting a turn header shows the whole-turn summary,
//   2. lifecycle plumbing moves to a collapsible "raw events" drawer,
//   3. content_refs deref INLINE (no modal) with a fold strategy,
//   4. provenance no longer surfaces a "micro-compaction" row when nothing was
//      cleared, and lists active context residents.
//
// projection.js is plain ESM (importable), so its classification is asserted by
// import. TraceApp.jsx is a JSX module, so its load-bearing wiring is asserted
// against the source text (the same approach as fix-trace.test.js).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import {
  NOISE_LIFECYCLE_TYPES,
  isNoiseRow,
  groupTurns,
} from "../pages/trace/projection.js";

const src = readFileSync(
  fileURLToPath(new URL("./TraceApp.jsx", import.meta.url)),
  "utf8",
);

// --- #2: lifecycle noise classification -----------------------------------

test("NOISE_LIFECYCLE_TYPES holds exactly the pipeline plumbing events", () => {
  const expected = [
    "TaskCreated",
    "AgentBound",
    "ModelBound",
    "TaskHostBound",
    "MessagesAppended",
    "TaskStarted",
    "TaskSnapshot",
    "TaskSuspended",
    "TaskWoken",
  ];
  for (const t of expected) {
    assert.ok(NOISE_LIFECYCLE_TYPES.has(t), `${t} should be classed as noise`);
  }
  assert.equal(NOISE_LIFECYCLE_TYPES.size, expected.length);
});

test("isNoiseRow: lifecycle is noise; meaningful actions and unknowns are not", () => {
  assert.equal(isNoiseRow({ type: "ModelBound" }), true);
  assert.equal(isNoiseRow({ type: "TaskSnapshot" }), true);
  // Meaningful actions stay in the main timeline.
  assert.equal(isNoiseRow({ type: "LLMRequestStarted" }), false);
  assert.equal(isNoiseRow({ type: "ToolCallStarted" }), false);
  assert.equal(isNoiseRow({ type: "SubtaskSpawned" }), false);
  assert.equal(isNoiseRow({ type: "Compacted" }), false);
  assert.equal(isNoiseRow({ type: "ContextContentRecorded" }), false);
  // Unknown / new types default to meaningful (never silently hidden).
  assert.equal(isNoiseRow({ type: "SomeFutureEvent" }), false);
  assert.equal(isNoiseRow(null), false);
});

test("RawEventsDrawer wires the show-all toggle and a default-collapsed drawer", () => {
  assert.match(src, /function RawEventsDrawer\s*\(/, "RawEventsDrawer is missing");
  assert.match(src, /showRawEvents/, "show-all-events state is missing");
  // Main timeline drops noise unless show-all is on.
  assert.match(
    src,
    /matchedRows\.filter\(\(row\)\s*=>\s*!isNoiseRow\(row\)\)/,
    "the main timeline should drop noise rows by default",
  );
});

// --- #1: turn header selects the whole-turn summary ------------------------

test("turn header selects the head request (routes detail pane to TurnView)", () => {
  // A dedicated select button targets the turn's head seq; the chevron toggles
  // the per-event list separately.
  assert.match(src, /turn-group-select/, "turn header needs a selectable region");
  assert.match(src, /turn-group-toggle/, "turn header keeps a separate expand toggle");
  const groupFn = src.slice(
    src.indexOf("function TimelineGroup"),
    src.indexOf("function RawEventsDrawer"),
  );
  assert.match(groupFn, /headSeq != null \? onSelect\(headSeq\)/, "selecting a turn must select its head seq");
});

test("TurnView surfaces the per-turn tool calls", () => {
  assert.match(src, /function turnToolEvents\s*\(/, "turnToolEvents collector is missing");
  assert.match(src, /<TurnToolsRegion\b/, "TurnView should render the per-turn tool region");
});

// turnToolEvents window logic, re-implemented inline to pin the contract.
function turnToolEvents(events, requestSeq) {
  if (typeof requestSeq !== "number") return [];
  let nextRequestSeq = Infinity;
  for (const event of events) {
    if (event?.type === "LLMRequestStarted" && typeof event.seq === "number" && event.seq > requestSeq) {
      nextRequestSeq = Math.min(nextRequestSeq, event.seq);
    }
  }
  const starts = new Map();
  const out = [];
  for (const event of events) {
    if (typeof event?.seq !== "number") continue;
    if (event.seq <= requestSeq || event.seq >= nextRequestSeq) continue;
    if (event.type === "ToolCallStarted") {
      const callId = event.payload?.call_id || null;
      const entry = { callId, started: event, result: null };
      if (callId) starts.set(callId, entry);
      out.push(entry);
    } else if (event.type === "ToolResultRecorded") {
      const callId = event.payload?.call_id || null;
      const existing = callId ? starts.get(callId) : null;
      if (existing) existing.result = event;
      else out.push({ callId, started: null, result: event });
    }
  }
  return out;
}

test("turnToolEvents pairs starts with results within the turn window", () => {
  const events = [
    { seq: 1, type: "LLMRequestStarted" },
    { seq: 2, type: "ToolCallStarted", payload: { call_id: "a", tool_name: "read" } },
    { seq: 3, type: "ToolResultRecorded", payload: { call_id: "a", success: true } },
    { seq: 4, type: "LLMRequestStarted" }, // next turn — out of window
    { seq: 5, type: "ToolCallStarted", payload: { call_id: "b" } },
  ];
  const out = turnToolEvents(events, 1);
  assert.equal(out.length, 1, "only the call inside the turn window is collected");
  assert.equal(out[0].callId, "a");
  assert.ok(out[0].started && out[0].result, "the start and result are paired");
});

// --- #3: content_refs deref inline, not via a modal ------------------------

test("RefChip derefs inline (no openPreview) with a fold cap", () => {
  const chipFn = src.slice(src.indexOf("function RefChip("), src.indexOf("function RefBody"));
  assert.doesNotMatch(chipFn, /openPreview\(/, "the inline chip must not open the modal");
  assert.match(chipFn, /useContentBody\(/, "the inline chip derefs via the shared content hook");
  assert.match(src, /REF_INLINE_CAP/, "a fold cap for large bodies is missing");
  assert.match(src, /function RefBody\b/, "the inline deref body renderer is missing");
});

test("nested content_refs recurse inline with a depth guard", () => {
  const bodyFn = src.slice(src.indexOf("function RefBody"), src.indexOf("function ArtifactRow"));
  assert.match(bodyFn, /collectPayloadRefs\(value\)/, "nested refs are collected from the deref'd body");
  assert.match(bodyFn, /depth < 6/, "a depth guard prevents ref-cycle blowups");
  assert.match(bodyFn, /<RefChip\b/, "nested refs render as their own inline chips");
});

// --- #4: provenance micro-compaction + active residents --------------------

test("micro-compaction row is conditional on a non-zero cleared count", () => {
  const provFn = src.slice(
    src.indexOf("function ProvenanceRegion"),
    src.indexOf("// ---", src.indexOf("function ProvenanceRegion")),
  );
  assert.match(provFn, /clearedCount > 0 \?/, "micro-compaction row must be gated on clearedCount > 0");
  // The misleading always-on "none" value is gone.
  assert.doesNotMatch(provFn, /:\s*"none"/, '"none" fallback for micro-compaction should be removed');
});

test("active residents are derived from ContextContentRecorded", () => {
  assert.match(src, /function activeResidents\s*\(/, "activeResidents helper is missing");
  const fn = src.slice(src.indexOf("function activeResidents"), src.indexOf("function collectPayloadRefs"));
  assert.match(fn, /ContextContentRecorded/, "residents come from ContextContentRecorded events");
  assert.match(src, /active residents/, "provenance should label the active residents block");
});

// activeResidents dedup/scope logic, re-implemented inline to pin the contract.
function activeResidents(events, requestSeq) {
  const byKey = new Map();
  for (const env of events || []) {
    if (env?.type !== "ContextContentRecorded") continue;
    if (typeof requestSeq === "number" && typeof env.seq === "number" && env.seq > requestSeq) continue;
    const p = env.payload || {};
    if (!p.kind || !p.name) continue;
    byKey.set(`${p.kind}:${p.name}`, { kind: p.kind, name: p.name, policy: p.policy || null, seq: env.seq });
  }
  return [...byKey.values()].sort(
    (a, b) => String(a.kind).localeCompare(String(b.kind)) || String(a.name).localeCompare(String(b.name)),
  );
}

test("activeResidents dedups by kind:name, scopes to requestSeq, keeps latest", () => {
  const events = [
    { seq: 1, type: "ContextContentRecorded", payload: { kind: "environment", name: "env", version: "1", policy: "fresh" } },
    { seq: 2, type: "ContextContentRecorded", payload: { kind: "skill", name: "tdd", version: "1", policy: "pinned" } },
    { seq: 5, type: "ContextContentRecorded", payload: { kind: "environment", name: "env", version: "2", policy: "fresh" } },
  ];
  // Scoped to seq 3: the second environment recording (seq 5) is out of window.
  const scoped = activeResidents(events, 3);
  assert.deepEqual(
    scoped.map((r) => `${r.kind}:${r.name}`),
    ["environment:env", "skill:tdd"],
    "residents are deduped by kind:name and sorted",
  );
  // No requestSeq → all residents; later env recording wins (kept latest).
  const all = activeResidents(events, null);
  assert.equal(all.find((r) => r.kind === "environment").seq, 5);
});

// Sanity: groupTurns still partitions into setup + turns (unchanged contract).
test("groupTurns still opens a turn at each LLMRequestStarted", () => {
  const rows = [
    { seq: 1, type: "TaskStarted" },
    { seq: 2, type: "LLMRequestStarted" },
    { seq: 3, type: "ToolCallStarted" },
    { seq: 4, type: "LLMRequestStarted" },
  ];
  const groups = groupTurns(rows);
  assert.equal(groups[0].kind, "setup");
  assert.equal(groups[1].kind, "turn");
  assert.equal(groups[1].turnNo, 1);
  assert.equal(groups[2].turnNo, 2);
});
