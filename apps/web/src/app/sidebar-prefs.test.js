// node --test coverage for the pure sidebar prefs/filter helpers
// (workspace-and-session-path.md addendum 2026-06-28). Mirrors chat-fold.test.js:
// node:test + node:assert, no DOM. A fake window.localStorage exercises the
// pinned round-trip; tearing it down exercises the graceful-degradation path.

import { test } from "node:test";
import assert from "node:assert/strict";

import {
  loadPinnedSessions,
  savePinnedSessions,
  filterSessionsBySearch,
  partitionPinned,
} from "./sidebar-prefs.js";

function withFakeLocalStorage(run) {
  const store = new Map();
  const prior = globalThis.window;
  globalThis.window = {
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
      removeItem: (k) => store.delete(k),
    },
  };
  try {
    run();
  } finally {
    globalThis.window = prior;
  }
}

test("loadPinnedSessions returns an empty Set when localStorage is unavailable", () => {
  // No window global in plain node → the try/catch degrades to an empty Set.
  const pinned = loadPinnedSessions();
  assert.equal(pinned instanceof Set, true);
  assert.equal(pinned.size, 0);
});

test("savePinnedSessions / loadPinnedSessions round-trip through localStorage", () => {
  withFakeLocalStorage(() => {
    savePinnedSessions(new Set(["t1", "t2"]));
    const loaded = loadPinnedSessions();
    assert.equal(loaded.has("t1"), true);
    assert.equal(loaded.has("t2"), true);
    assert.equal(loaded.size, 2);
  });
});

test("loadPinnedSessions drops non-string / non-array junk", () => {
  withFakeLocalStorage(() => {
    window.localStorage.setItem(
      "noeta.sidebar.pinnedSessions",
      JSON.stringify(["ok", 5, null, { x: 1 }]),
    );
    const loaded = loadPinnedSessions();
    assert.deepEqual([...loaded], ["ok"]);
    window.localStorage.setItem("noeta.sidebar.pinnedSessions", JSON.stringify({}));
    assert.equal(loadPinnedSessions().size, 0);
  });
});

test("filterSessionsBySearch is case-insensitive on title; empty query passes through", () => {
  const rows = [
    { task_id: "a", title: "Refactor the Engine" },
    { task_id: "b", title: "fix CSS bug" },
    { task_id: "c" }, // no title
  ];
  assert.deepEqual(filterSessionsBySearch(rows, ""), rows);
  assert.deepEqual(
    filterSessionsBySearch(rows, "ENGINE").map((r) => r.task_id),
    ["a"],
  );
  assert.deepEqual(
    filterSessionsBySearch(rows, "  css  ").map((r) => r.task_id),
    ["b"],
  );
  // A titleless row never matches a non-empty query.
  assert.deepEqual(filterSessionsBySearch(rows, "zzz"), []);
  assert.deepEqual(filterSessionsBySearch(null, "x"), []);
});

test("partitionPinned splits pinned rows (preserving order) from the rest", () => {
  const rows = [
    { task_id: "a" },
    { task_id: "b" },
    { task_id: "c" },
  ];
  const { pinnedRows, rest } = partitionPinned(rows, new Set(["c", "a"]));
  assert.deepEqual(pinnedRows.map((r) => r.task_id), ["a", "c"]);
  assert.deepEqual(rest.map((r) => r.task_id), ["b"]);
  // No pins → everything stays in rest.
  const none = partitionPinned(rows, new Set());
  assert.equal(none.pinnedRows.length, 0);
  assert.equal(none.rest.length, 3);
});
