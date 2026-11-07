import { act, render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { isHostSlot, Slot, SlotRegistry, SlotsContext } from "./slots";

afterEach(() => vi.useRealTimers());

function Page({ registry, children }: { registry: SlotRegistry; children: ReactNode }) {
  return <SlotsContext.Provider value={registry}>{children}</SlotsContext.Provider>;
}

const Text = (text: string) => () => <span>{text}</span>;

describe("SlotRegistry", () => {
  it("knows the page's own slots", () => {
    expect(["statusbar", "sidebar", "settings", "session.header", "composer", "theme", "tool:Bash", "request:Bash", "record:todos/created", "codeblock:mermaid"].every(isHostSlot)).toBe(true);
    expect(isHostSlot("tool:")).toBe(false);
    expect(isHostSlot("my-plugin.panel")).toBe(false);
  });

  it("orders contributions, removes one by its remover and every one of a plugin on withdraw", () => {
    const r = new SlotRegistry();
    const removeB = r.add("b", "sidebar", { order: 2 }, Text("b"));
    r.add("a", "sidebar", { order: 1 }, Text("a"));
    r.add("a", "statusbar", {}, Text("a2"));
    expect(r.contributions("sidebar").map((c) => c.plugin)).toEqual(["a", "b"]);
    const before = r.version;
    removeB();
    removeB(); // a second call changes nothing
    expect(r.contributions("sidebar").map((c) => c.plugin)).toEqual(["a"]);
    expect(r.version).toBe(before + 1);
    r.withdraw("a");
    expect(r.contributions("sidebar")).toEqual([]);
    expect(r.contributions("statusbar")).toEqual([]);
  });

  it("resolves a wait once a plugin's slot is rendered", async () => {
    const r = new SlotRegistry();
    await expect(r.wait("p", "statusbar", 10)).resolves.toBeUndefined();
    const waited = r.wait("p", "other.panel", 1000);
    render(
      <Page registry={r}>
        <Slot name="other.panel" />
      </Page>,
    );
    await expect(waited).resolves.toBeUndefined();
    expect(r.problems()).toEqual([]);
  });

  it("times a wait out and keeps the problem under the waiting plugin's name", async () => {
    vi.useFakeTimers();
    const r = new SlotRegistry();
    const waited = r.wait("waiter", "never.there", 5000);
    const failed = expect(waited).rejects.toThrow("never.there");
    vi.advanceTimersByTime(5000);
    await failed;
    expect(r.problems("waiter")).toEqual([{ plugin: "waiter", slot: "never.there", kind: "wait-timeout", message: "waited 5000 ms for the slot never.there, which never came" }]);
    r.withdraw("waiter");
    expect(r.problems()).toEqual([]);
  });

  it("drops a switched-off plugin's waits without a problem", async () => {
    vi.useFakeTimers();
    const r = new SlotRegistry();
    const waited = r.wait("p", "x.panel", 5000);
    r.withdraw("p");
    await expect(waited).rejects.toThrow("switched off");
    vi.advanceTimersByTime(6000);
    expect(r.problems()).toEqual([]);
  });
});

describe("Slot", () => {
  it("shows its own content without a registry or contributions", () => {
    render(<Slot name="composer">own</Slot>);
    expect(screen.getByText("own")).toBeInTheDocument();
  });

  it("mounts: single replaces, list adds, keyed keeps the latest per key, wrap wraps", () => {
    const r = new SlotRegistry();
    const { container } = render(
      <Page registry={r}>
        <Slot name="composer" props={{ label: "L" }}>
          <i>own</i>
        </Slot>
      </Page>,
    );
    act(() => {
      r.add("a", "composer", { mount: "list", order: 2 }, ({ label }: { label: string }) => <b>list-{label}</b>);
      r.add("a", "composer", { mount: "keyed", key: "k" }, Text("keyed-old"));
      r.add("b", "composer", { mount: "keyed", key: "k" }, Text("keyed-new"));
    });
    expect(container.textContent).toBe("ownkeyed-newlist-L");
    act(() => {
      r.add("c", "composer", { mount: "single" }, ({ children }: { children?: ReactNode }) => <u>single[{children}]</u>);
      r.add("d", "composer", { mount: "wrap", order: 1 }, ({ children }: { children?: ReactNode }) => <div>inner({children})</div>);
      r.add("e", "composer", { mount: "wrap", order: 0 }, ({ children }: { children?: ReactNode }) => <div>outer({children})</div>);
    });
    expect(container.textContent).toBe("outer(inner(single[own]keyed-newlist-L))");
  });

  it("puts a list or keyed contribution with a negative order before the slot's own content", () => {
    const r = new SlotRegistry();
    r.add("a", "statusbar", { order: -1 }, Text("first"));
    r.add("b", "statusbar", { mount: "keyed", order: -2 }, Text("before-first"));
    r.add("c", "statusbar", {}, Text("after"));
    const { container } = render(
      <Page registry={r}>
        <Slot name="statusbar">
          <i>own</i>
        </Slot>
      </Page>,
    );
    expect(container.textContent).toBe("before-firstfirstownafter");
  });

  it("keeps a contribution's error in its own boundary, naming its plugin", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const r = new SlotRegistry();
    const Broken = () => {
      throw new Error("boom");
    };
    render(
      <Page registry={r}>
        <Slot name="sidebar" />
        <Slot name="composer">
          <i>composer</i>
        </Slot>
      </Page>,
    );
    act(() => {
      r.add("bad", "sidebar", {}, Broken);
      r.add("good", "sidebar", {}, Text("fine"));
      r.add("worse", "composer", { mount: "wrap" }, Broken);
    });
    expect(screen.getByText("fine")).toBeInTheDocument();
    expect(screen.getByText("composer")).toBeInTheDocument(); // a broken wrap leaves what it wrapped
    expect(screen.getAllByRole("alert").map((a) => a.textContent)).toEqual(["bad: boom", "worse: boom"]);
    expect(r.problems("bad")).toEqual([{ plugin: "bad", slot: "sidebar", kind: "render", message: "boom" }]);
  });
});
