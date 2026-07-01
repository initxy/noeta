// issue 04 — unit tests for the file-panel live-refresh pure logic.
// Node built-in runner, zero new deps: `node --test src/app/file-refresh.test.js`.
import assert from "node:assert/strict";
import { test } from "node:test";
import {
  editsFromEvents,
  isWriteEditTool,
  normalizeRelPath,
  previewHitSeq,
  turnWentIdle,
} from "./file-refresh.js";

// Helpers to build the canonical envelope projections the reducer / SSE folds.
const started = (seq, callId, toolName, args) => ({
  type: "ToolCallStarted",
  seq,
  payload: { call_id: callId, tool_name: toolName, arguments: args },
});
const result = (seq, callId, success) => ({
  type: "ToolResultRecorded",
  seq,
  payload: { call_id: callId, success },
});

test("isWriteEditTool recognises only write/edit", () => {
  assert.equal(isWriteEditTool("write"), true);
  assert.equal(isWriteEditTool("edit"), true);
  assert.equal(isWriteEditTool("read"), false);
  assert.equal(isWriteEditTool("patch"), false);
  assert.equal(isWriteEditTool("shell"), false);
  assert.equal(isWriteEditTool(null), false);
  assert.equal(isWriteEditTool(undefined), false);
});

test("normalizeRelPath canonicalises to a POSIX relative path", () => {
  assert.equal(normalizeRelPath("src/app.js"), "src/app.js");
  assert.equal(normalizeRelPath("./src/app.js"), "src/app.js");
  assert.equal(normalizeRelPath("src//app.js"), "src/app.js");
  assert.equal(normalizeRelPath("src/app.js/"), "src/app.js");
  assert.equal(normalizeRelPath("src\\app.js"), "src/app.js");
  assert.equal(normalizeRelPath("src/lib/../app.js"), "src/app.js");
  assert.equal(normalizeRelPath("  src/app.js  "), "src/app.js");
});

test("normalizeRelPath rejects empty / absolute / escaping paths as null", () => {
  assert.equal(normalizeRelPath(""), null);
  assert.equal(normalizeRelPath("   "), null);
  assert.equal(normalizeRelPath(null), null);
  assert.equal(normalizeRelPath(undefined), null);
  assert.equal(normalizeRelPath("/etc/passwd"), null);
  assert.equal(normalizeRelPath("../secret"), null);
  assert.equal(normalizeRelPath("src/../../escape"), null);
  assert.equal(normalizeRelPath("."), null);
});

test("editsFromEvents yields a successful write's normalized path", () => {
  const events = [
    started(1, "c1", "write", { path: "./src/new.js", content: "x" }),
    result(2, "c1", true),
  ];
  assert.deepEqual(editsFromEvents(events), [
    { callId: "c1", seq: 2, path: "src/new.js" },
  ]);
});

test("editsFromEvents yields a successful edit's path", () => {
  const events = [
    started(3, "c2", "edit", { path: "a/b.py", old: "x", new: "y" }),
    result(4, "c2", true),
  ];
  assert.deepEqual(editsFromEvents(events), [
    { callId: "c2", seq: 4, path: "a/b.py" },
  ]);
});

test("editsFromEvents skips dry-run / failed write-edits (success !== true)", () => {
  const events = [
    started(1, "c1", "edit", { path: "a.py", old: "x", new: "y" }),
    result(2, "c1", false), // edit didn't match / failed → no file change
    started(3, "c2", "write", { path: "b.py", content: "z" }),
    // no result yet (in flight) → not an applied change
  ];
  assert.deepEqual(editsFromEvents(events), []);
});

test("editsFromEvents ignores non-write/edit tools", () => {
  const events = [
    started(1, "c1", "read", { path: "a.py" }),
    result(2, "c1", true),
    started(3, "c2", "shell", { command: "ls" }),
    result(4, "c2", true),
  ];
  assert.deepEqual(editsFromEvents(events), []);
});

test("editsFromEvents drops a write whose path is missing / escapes", () => {
  const events = [
    started(1, "c1", "write", { content: "no path" }),
    result(2, "c1", true),
    started(3, "c2", "write", { path: "../../oops", content: "x" }),
    result(4, "c2", true),
  ];
  assert.deepEqual(editsFromEvents(events), []);
});

test("editsFromEvents collects multiple edits in stream order", () => {
  const events = [
    started(1, "c1", "write", { path: "a.js", content: "1" }),
    result(2, "c1", true),
    started(3, "c2", "edit", { path: "dir/b.js", old: "x", new: "y" }),
    result(4, "c2", true),
  ];
  assert.deepEqual(editsFromEvents(events), [
    { callId: "c1", seq: 2, path: "a.js" },
    { callId: "c2", seq: 4, path: "dir/b.js" },
  ]);
});

test("previewHitSeq returns the latest matching edit's seq", () => {
  const events = [
    started(1, "c1", "write", { path: "src/app.js", content: "a" }),
    result(2, "c1", true),
    started(3, "c2", "edit", { path: "src/app.js", old: "a", new: "b" }),
    result(4, "c2", true),
  ];
  assert.equal(previewHitSeq(events, "src/app.js"), 4);
});

test("previewHitSeq matches across path-normalization differences", () => {
  const events = [
    started(1, "c1", "edit", { path: "./src/app.js", old: "a", new: "b" }),
    result(2, "c1", true),
  ];
  // selected path comes from the tree (canonical), arg path had a ./ prefix.
  assert.equal(previewHitSeq(events, "src/app.js"), 2);
});

test("previewHitSeq returns -1 when only OTHER files were edited", () => {
  const events = [
    started(1, "c1", "write", { path: "other.js", content: "x" }),
    result(2, "c1", true),
  ];
  // The currently-previewed file is untouched → no in-vain re-pull.
  assert.equal(previewHitSeq(events, "src/app.js"), -1);
});

test("previewHitSeq returns -1 for no selection / no events", () => {
  assert.equal(previewHitSeq([], "src/app.js"), -1);
  assert.equal(previewHitSeq(null, "src/app.js"), -1);
  assert.equal(previewHitSeq([], null), -1);
});

test("turnWentIdle fires only on the running→idle edge", () => {
  assert.equal(turnWentIdle(true, false), true); // the edge
  assert.equal(turnWentIdle(false, false), false); // stayed idle
  assert.equal(turnWentIdle(true, true), false); // still running
  assert.equal(turnWentIdle(false, true), false); // just started running
});
