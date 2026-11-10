import { afterEach, describe, expect, it, vi } from "vitest";
import type { Recorded } from "../api/types";
import { TaskStore } from "./useTask";

const called = (seq: number): Recorded => ({ seq, at: 0, event: { type: "ModelCalled", model: "m" } });
const text = (t: string, call_seq = 1) => ({ type: "delta" as const, delta: { kind: "text" as const, text: t, call_seq } });

describe("TaskStore", () => {
  afterEach(() => vi.useRealTimers());

  it("folds streamed chunks once per frame, telling the page once", () => {
    vi.useFakeTimers();
    const store = new TaskStore();
    store.dispatch({ type: "event", rec: called(1) });
    const told = vi.fn();
    store.subscribe(told);
    store.dispatch(text("Hel"));
    store.dispatch(text("lo"));
    expect(store.get().live).toBeNull();
    vi.advanceTimersByTime(120);
    expect(store.get().live?.text).toBe("Hello");
    expect(told).toHaveBeenCalledTimes(1);
  });

  it("folds the chunks waiting before a recorded event, in order", () => {
    const store = new TaskStore();
    store.dispatch({ type: "event", rec: called(1) });
    store.dispatch(text("Hello"));
    const replied: Recorded = {
      seq: 2,
      at: 0,
      event: { type: "ModelReplied", response: { message: { role: "assistant", content: [{ type: "text", text: "Hello" }] }, stop_reason: "end_turn", usage: {} } },
    };
    store.dispatch({ type: "event", rec: replied }); // the chunk folds first, then the reply replaces it
    expect([store.get().live, store.get().items.map((i) => i.kind)]).toEqual([null, ["assistant"]]);
    store.dispatch(text("late")); // output of the call already recorded
    store.apply([]);
    expect(store.get().live).toBeNull();
  });

  it("applies a whole log in one change and drops waiting output on reset", () => {
    const store = new TaskStore();
    const told = vi.fn();
    store.subscribe(told);
    store.apply([1, 2, 3].map((seq) => ({ type: "event" as const, rec: called(seq) })));
    expect([store.get().lastSeq, told.mock.calls.length]).toEqual([3, 1]);
    store.dispatch(text("late", 3));
    store.reset();
    store.apply([]);
    expect([store.get().lastSeq, store.get().live]).toEqual([0, null]);
  });
});
