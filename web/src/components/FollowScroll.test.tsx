import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FollowScroll } from "./FollowScroll";

// jsdom lays nothing out: the scroller's sizes are set by hand, and the
// observers are called as a browser would after a layout.
let resized: () => void = () => {};
let endSeen: (seen: boolean) => void = () => {};

class Resize {
  constructor(cb: () => void) {
    resized = cb;
  }
  observe() {}
  disconnect() {}
}

class Intersection {
  constructor(cb: (e: { isIntersecting: boolean }[]) => void) {
    endSeen = (seen) => cb([{ isIntersecting: seen }]);
  }
  observe() {}
  disconnect() {}
}

afterEach(() => vi.unstubAllGlobals());

function sized(el: HTMLElement, size: { height: number; top: number }) {
  Object.defineProperty(el, "scrollHeight", { configurable: true, get: () => size.height });
  Object.defineProperty(el, "clientHeight", { configurable: true, get: () => 500 });
  Object.defineProperty(el, "clientWidth", { configurable: true, get: () => 800 });
  Object.defineProperty(el, "scrollTop", {
    configurable: true,
    get: () => size.top,
    set: (v: number) => (size.top = Math.min(v, size.height - 500)),
  });
}

describe("FollowScroll", () => {
  it("says New messages only for output added below, not for earlier rows drawn in above", () => {
    vi.stubGlobal("ResizeObserver", Resize);
    vi.stubGlobal("IntersectionObserver", Intersection);
    const { container } = render(
      <FollowScroll resetKey="t1">
        <p>rows</p>
      </FollowScroll>,
    );
    const scroller = container.querySelector(".scroll") as HTMLElement;
    const size = { height: 3000, top: 2500 };
    sized(scroller, size);
    // Scrolled up, the end off screen.
    fireEvent.wheel(scroller, { deltaY: -100 });
    size.top = 1000;
    fireEvent.scroll(scroller);
    act(() => endSeen(false));
    expect(screen.getByRole("button")).toHaveTextContent("Jump to latest");
    // Earlier rows drawn in above, the screen kept where it was.
    size.height += 2000;
    size.top += 2000;
    act(() => resized());
    expect(screen.getByRole("button")).toHaveTextContent("Jump to latest");
    // Output added at the end.
    size.height += 300;
    act(() => resized());
    expect(screen.getByRole("button")).toHaveTextContent("New messages");
  });
});
