// unit tests for the "Apps" tab pure logic. Node built-in
// runner, zero new deps: `node --test src/app/app-preview.test.js`.
import assert from "node:assert/strict";
import { test } from "node:test";
import { appReloadHitSeq, latestOpenApp } from "./app-preview.js";

// Canonical envelope projections, mirroring file-refresh.test.js helpers.
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
// A ToolResultRecorded carrying a side_effects array (the open_app contract).
const openAppResult = (seq, callId, sideEffects) => ({
  type: "ToolResultRecorded",
  seq,
  payload: { call_id: callId, success: true, side_effects: sideEffects },
});

// ---- latestOpenApp --------------------------------------------------------

test("latestOpenApp returns the open_app side-effect's {url, dir, seq}", () => {
  const events = [
    openAppResult(5, "c1", [
      { type: "open_app", url: "http://127.0.0.1:7890/apps/tok/", dir: "app" },
    ]),
  ];
  assert.deepEqual(latestOpenApp(events), {
    url: "http://127.0.0.1:7890/apps/tok/",
    dir: "app",
    seq: 5,
  });
});

test("latestOpenApp returns null when there is no open_app side-effect", () => {
  const events = [
    started(1, "c1", "write", { path: "app/index.html", content: "x" }),
    result(2, "c1", true),
    // a ToolResultRecorded WITH side_effects, but not of type open_app
    openAppResult(3, "c2", [{ type: "file_write", path: "app/index.html" }]),
  ];
  assert.equal(latestOpenApp(events), null);
});

test("latestOpenApp returns null for empty / non-array input", () => {
  assert.equal(latestOpenApp([]), null);
  assert.equal(latestOpenApp(null), null);
  assert.equal(latestOpenApp(undefined), null);
});

test("latestOpenApp picks the entry with the LARGEST seq across many", () => {
  const events = [
    openAppResult(10, "c1", [
      { type: "open_app", url: "http://h/apps/a/", dir: "app" },
    ]),
    openAppResult(30, "c3", [
      { type: "open_app", url: "http://h/apps/c/", dir: "demo" },
    ]),
    openAppResult(20, "c2", [
      { type: "open_app", url: "http://h/apps/b/", dir: "site" },
    ]),
  ];
  // Latest = the seq-30 one, regardless of array order.
  assert.deepEqual(latestOpenApp(events), {
    url: "http://h/apps/c/",
    dir: "demo",
    seq: 30,
  });
});

test("latestOpenApp ignores an open_app effect with a blank url", () => {
  const events = [
    openAppResult(5, "c1", [{ type: "open_app", url: "   ", dir: "app" }]),
    openAppResult(6, "c2", [
      { type: "open_app", url: "http://h/apps/ok/", dir: "app" },
    ]),
  ];
  assert.deepEqual(latestOpenApp(events), {
    url: "http://h/apps/ok/",
    dir: "app",
    seq: 6,
  });
});

test("latestOpenApp finds open_app among several side-effects in one result", () => {
  const events = [
    openAppResult(7, "c1", [
      { type: "file_write", path: "app/index.html" },
      { type: "open_app", url: "http://h/apps/x/", dir: "app" },
    ]),
  ];
  assert.deepEqual(latestOpenApp(events), {
    url: "http://h/apps/x/",
    dir: "app",
    seq: 7,
  });
});

// ---- appReloadHitSeq ------------------------------------------------------

test("appReloadHitSeq fires on a file under the mounted dir prefix", () => {
  const events = [
    started(1, "c1", "write", { path: "app/index.html", content: "x" }),
    result(2, "c1", true),
  ];
  assert.equal(appReloadHitSeq(events, "app"), 2);
});

test("appReloadHitSeq does NOT fire for files outside the mounted dir", () => {
  const events = [
    started(1, "c1", "write", { path: "other/index.html", content: "x" }),
    result(2, "c1", true),
    // a sibling dir with the SAME prefix string must not false-match ("app" vs "apple/")
    started(3, "c2", "edit", { path: "apple/x.js", old: "a", new: "b" }),
    result(4, "c2", true),
  ];
  assert.equal(appReloadHitSeq(events, "app"), -1);
});

test("appReloadHitSeq returns the LATEST matching edit's seq", () => {
  const events = [
    started(1, "c1", "write", { path: "app/index.html", content: "1" }),
    result(2, "c1", true),
    started(3, "c2", "edit", { path: "app/app.js", old: "a", new: "b" }),
    result(4, "c2", true),
    // an edit elsewhere must not advance the hit
    started(5, "c3", "write", { path: "notes.md", content: "z" }),
    result(6, "c3", true),
  ];
  assert.equal(appReloadHitSeq(events, "app"), 4);
});

test("appReloadHitSeq matches across dir-normalization (./ and trailing slash)", () => {
  const events = [
    started(1, "c1", "edit", { path: "./app/index.html", old: "a", new: "b" }),
    result(2, "c1", true),
  ];
  assert.equal(appReloadHitSeq(events, "app/"), 2);
});

test("appReloadHitSeq ignores dry-run / failed edits (success !== true)", () => {
  const events = [
    started(1, "c1", "edit", { path: "app/index.html", old: "x", new: "y" }),
    result(2, "c1", false), // edit didn't apply → no reload
  ];
  assert.equal(appReloadHitSeq(events, "app"), -1);
});

test("appReloadHitSeq returns -1 for missing / escaping / empty dir", () => {
  const events = [
    started(1, "c1", "write", { path: "app/index.html", content: "x" }),
    result(2, "c1", true),
  ];
  assert.equal(appReloadHitSeq(events, ""), -1);
  assert.equal(appReloadHitSeq(events, null), -1);
  assert.equal(appReloadHitSeq(events, "../escape"), -1);
  assert.equal(appReloadHitSeq([], "app"), -1);
});

test("appReloadHitSeq matches files in nested subdirs of the mount", () => {
  const events = [
    started(1, "c1", "write", { path: "app/assets/style.css", content: "x" }),
    result(2, "c1", true),
  ];
  assert.equal(appReloadHitSeq(events, "app"), 2);
});
