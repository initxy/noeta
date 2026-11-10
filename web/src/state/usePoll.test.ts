import { renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { usePoll } from "./usePoll";

const hide = (hidden: boolean) => {
  Object.defineProperty(document, "hidden", { configurable: true, get: () => hidden });
  document.dispatchEvent(new Event("visibilitychange"));
};

describe("usePoll", () => {
  afterEach(() => {
    hide(false);
    vi.useRealTimers();
  });

  it("polls while shown, slower while hidden, and once at once when shown again", () => {
    vi.useFakeTimers();
    const run = vi.fn();
    renderHook(() => usePoll(run, 1000, 10_000));
    vi.advanceTimersByTime(3000);
    expect(run).toHaveBeenCalledTimes(3);
    hide(true);
    vi.advanceTimersByTime(9000);
    expect(run).toHaveBeenCalledTimes(3);
    hide(false);
    expect(run).toHaveBeenCalledTimes(4);
  });

  it("does not poll a hidden page when told not to, nor at all once stopped", () => {
    vi.useFakeTimers();
    const run = vi.fn();
    const { rerender } = renderHook(({ every }: { every: number | null }) => usePoll(run, every, null), { initialProps: { every: 1000 as number | null } });
    hide(true);
    vi.advanceTimersByTime(5000);
    expect(run).not.toHaveBeenCalled();
    hide(false);
    rerender({ every: null });
    vi.advanceTimersByTime(5000);
    expect(run).toHaveBeenCalledTimes(1); // shown again, before it was stopped
  });
});
