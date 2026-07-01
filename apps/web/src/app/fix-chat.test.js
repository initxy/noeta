// Regression test: transcript-backfill response_ref lookup changed from "scan
// all events every turn" to "seq-sorted index + binary search" (finding #36,
// perf). ChatApp.jsx is JSX, which node's built-in runner can't import directly,
// so we replay both pure functions here and assert the binary-search version
// matches the linear reference at every seq — the test holds as long as the
// algorithm contract is unchanged. Run:
//   node --test src/app/fix-chat.test.js
import assert from "node:assert/strict";
import { test } from "node:test";

// —— Linear reference (an exact replica of ChatApp.jsx's responseRefHashForSeq) ——
function responseRefHashForSeq(events, seq) {
  if (typeof seq !== "number") return null;
  let best = null;
  for (const env of events) {
    if (!env || env.type !== "LLMResponseRecorded") continue;
    if (typeof env.seq !== "number" || env.seq >= seq) continue;
    if (!best || env.seq > best.seq) best = env;
  }
  const ref = best?.payload?.response_ref;
  return typeof ref?.hash === "string" ? ref.hash : null;
}

// —— Under test: index build + binary-search lookup (replicas of the same-named ChatApp.jsx functions) ——
function buildResponseRefSeqIndex(events) {
  const index = [];
  for (const env of events) {
    if (!env || env.type !== "LLMResponseRecorded") continue;
    if (typeof env.seq !== "number") continue;
    const ref = env.payload?.response_ref;
    if (typeof ref?.hash !== "string") continue;
    index.push({ seq: env.seq, hash: ref.hash });
  }
  index.sort((a, b) => a.seq - b.seq);
  return index;
}

function responseRefHashForSeqIndex(index, seq) {
  if (typeof seq !== "number") return null;
  let lo = 0;
  let hi = index.length - 1;
  let best = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (index[mid].seq < seq) {
      best = index[mid];
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return best ? best.hash : null;
}

const resp = (seq, hash) => ({
  type: "LLMResponseRecorded",
  seq,
  payload: { response_ref: { hash } },
});

test("binary search returns the hash of the highest-seq response strictly before seq", () => {
  const events = [resp(1, "h1"), resp(3, "h3"), resp(7, "h7")];
  const idx = buildResponseRefSeqIndex(events);
  assert.equal(responseRefHashForSeqIndex(idx, 8), "h7");
  assert.equal(responseRefHashForSeqIndex(idx, 7), "h3"); // strictly less than: excludes seq=7
  assert.equal(responseRefHashForSeqIndex(idx, 4), "h3");
  assert.equal(responseRefHashForSeqIndex(idx, 2), "h1");
  assert.equal(responseRefHashForSeqIndex(idx, 1), null); // nothing earlier
  assert.equal(responseRefHashForSeqIndex(idx, 0), null);
});

test("out-of-order input still queries correctly after sorting (the index sorts internally)", () => {
  const events = [resp(7, "h7"), resp(1, "h1"), resp(3, "h3")];
  const idx = buildResponseRefSeqIndex(events);
  assert.equal(responseRefHashForSeqIndex(idx, 5), "h3");
});

test("skips envelopes that are not responses / lack seq / lack hash", () => {
  const events = [
    { type: "MessagesAppended", seq: 2 },
    { type: "LLMResponseRecorded", seq: 4 }, // missing response_ref
    { type: "LLMResponseRecorded", payload: { response_ref: { hash: "x" } } }, // missing seq
    resp(5, "h5"),
    null,
  ];
  const idx = buildResponseRefSeqIndex(events);
  assert.equal(idx.length, 1);
  assert.equal(responseRefHashForSeqIndex(idx, 6), "h5");
  assert.equal(responseRefHashForSeqIndex(idx, 5), null);
});

test("non-numeric seq → null (matching the linear version)", () => {
  const idx = buildResponseRefSeqIndex([resp(1, "h1")]);
  assert.equal(responseRefHashForSeqIndex(idx, "x"), null);
  assert.equal(responseRefHashForSeqIndex(idx, undefined), null);
});

test("binary-search and linear-reference versions agree at every seq (equivalence gate)", () => {
  const events = [
    resp(2, "a"),
    { type: "MessagesAppended", seq: 3 },
    resp(5, "b"),
    resp(9, "c"),
    { type: "ToolCallStarted", seq: 6 },
    resp(11, "d"),
  ];
  const idx = buildResponseRefSeqIndex(events);
  for (let seq = 0; seq <= 13; seq++) {
    assert.equal(
      responseRefHashForSeqIndex(idx, seq),
      responseRefHashForSeq(events, seq),
      `seq=${seq} the two versions disagree`,
    );
  }
});
