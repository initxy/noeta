import { Markdown } from "@noeta-ai/ui";
import { Slot, SlotRegistry, SlotsContext, type CodeBlockSlotProps, type RecordSlotProps, type WrapProps, type StatusbarSlotProps, type ToolSlotProps } from "@noeta-ai/web-sdk";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useEffect, useState, type ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type Api } from "../api/client";
import type { MarketEntry, PluginInfo, UiEntry } from "../api/types";
import type { Event, Recorded } from "../api/types";
import { emptyTask, reduce, type Action } from "../state/transcript";
import { Composer, newComposerKeep } from "./Composer";
import { PluginsDialog } from "./PluginsDialog";
import { Transcript } from "./Transcript";
import { Notices, StatusBar, Widgets } from "./PluginUi";
import { ToolCard } from "./ToolCard";

const info = (p: Partial<PluginInfo>): PluginInfo => ({
  name: "panel",
  version: "0.1.0",
  description: "A panel",
  source: "/home/me/.noeta/plugins/panel",
  builtin: false,
  enabled: true,
  switchable: true,
  broken: null,
  parts: ["backend", "web"],
  ui_api: 1,
  web: { entry: "/plugins/panel/web/index.js?v=1" },
  ...p,
});

function dialog(api: Partial<Api>, plugins: PluginInfo[], opts: { taskOff?: string[]; registry?: SlotRegistry; refresh?: () => Promise<void> } = {}) {
  const registry = opts.registry ?? new SlotRegistry();
  return render(
    <SlotsContext.Provider value={registry}>
      <PluginsDialog
        api={api as Api}
        listing={{ plugins, ui_api: [1, 1] }}
        refresh={opts.refresh ?? (() => Promise.resolve())}
        taskId="t1"
        taskOff={opts.taskOff ?? []}
        status={(name) => (name === "broken-web" ? { state: "failed", error: "could not load: 404" } : undefined)}
        registry={registry}
        onClose={() => undefined}
      />
    </SlotsContext.Provider>,
  );
}

describe("PluginsDialog", () => {
  it("switches a plugin for this session and for new sessions", async () => {
    const setTaskPluginsOff = vi.fn().mockResolvedValue({});
    const setPluginDefault = vi.fn().mockResolvedValue({});
    const refresh = vi.fn().mockResolvedValue(undefined);
    dialog({ setTaskPluginsOff, setPluginDefault }, [info({}), info({ name: "fs", builtin: true, switchable: false, parts: ["backend"], web: null })], { taskOff: ["web"], refresh });
    const row = screen.getByText("panel").closest("li")!;
    const [session, fresh] = [...row.querySelectorAll<HTMLInputElement>("input[role=switch]")];
    expect(session.checked && fresh.checked).toBe(true);
    await userEvent.click(session);
    expect(setTaskPluginsOff).toHaveBeenCalledWith("t1", ["web", "panel"]);
    expect(session.checked).toBe(false); // as asked, until the log shows it
    await userEvent.click(fresh);
    expect(setPluginDefault).toHaveBeenCalledWith("panel", false);
    expect(refresh).toHaveBeenCalled();
    const fs = screen.getByText("fs").closest("li")!;
    expect(fs.querySelectorAll("input")).toHaveLength(0); // a task is built on it
    expect(fs).toHaveTextContent("built-in");
  });

  it("sends a quick second switch on top of the first, while the log shows neither yet", async () => {
    const setTaskPluginsOff = vi.fn().mockResolvedValue({});
    const plugins = [info({ name: "a" }), info({ name: "b" }), info({ name: "c" })];
    const registry = new SlotRegistry();
    const at = (taskOff: string[]) => (
      <SlotsContext.Provider value={registry}>
        <PluginsDialog
          api={{ setTaskPluginsOff } as unknown as Api}
          listing={{ plugins, ui_api: [1, 1] }}
          refresh={() => Promise.resolve()}
          taskId="t1"
          taskOff={taskOff}
          status={() => undefined}
          registry={registry}
          onClose={() => undefined}
        />
      </SlotsContext.Provider>
    );
    const { rerender } = render(at([]));
    const sessionSwitch = (name: string) => screen.getByText(name).closest("li")!.querySelector<HTMLInputElement>("input[role=switch]")!;
    await userEvent.click(sessionSwitch("a"));
    await userEvent.click(sessionSwitch("b"));
    expect(setTaskPluginsOff.mock.calls).toEqual([
      ["t1", ["a"]],
      ["t1", ["a", "b"]],
    ]);
    rerender(at(["a"])); // the log shows the first switch only
    expect(sessionSwitch("a").checked).toBe(false);
    expect(sessionSwitch("b").checked).toBe(false);
    await userEvent.click(sessionSwitch("c"));
    expect(setTaskPluginsOff).toHaveBeenLastCalledWith("t1", ["a", "b", "c"]);
    rerender(at(["a", "b", "c"]));
    await userEvent.click(sessionSwitch("a")); // back on, on top of what the log shows
    expect(setTaskPluginsOff).toHaveBeenLastCalledWith("t1", ["b", "c"]);
    expect(sessionSwitch("a").checked).toBe(true);
  });

  it("puts a switch back when sending it fails", async () => {
    const setTaskPluginsOff = vi.fn().mockRejectedValue(new Error("offline"));
    dialog({ setTaskPluginsOff }, [info({ name: "a" })]);
    const session = screen.getByText("a").closest("li")!.querySelector<HTMLInputElement>("input[role=switch]")!;
    await userEvent.click(session);
    await waitFor(() => expect(session.checked).toBe(true));
    expect(screen.getByText("offline")).toBeInTheDocument();
  });

  it("names what is wrong: broken, a ui_api the page does not run, a failed web part, a slot never come", () => {
    const registry = new SlotRegistry();
    registry.report({ plugin: "waiter", slot: "x.panel", kind: "wait-timeout", message: "waited 5000 ms for the slot x.panel, which never came" });
    dialog({}, [info({ name: "gone", broken: "joins no agent" }), info({ name: "future", ui_api: 9 }), info({ name: "broken-web" }), info({ name: "waiter" })], { registry });
    const text = (name: string) => screen.getByText(name).closest("li")!.textContent;
    expect(text("gone")).toContain("joins no agent");
    expect(text("future")).toContain("needs ui_api 9");
    expect(text("broken-web")).toContain("web part could not load: 404");
    expect(text("waiter")).toContain("waited 5000 ms for the slot x.panel");
  });

  it("installs from Discover only after showing source, commit and parts, and explains a 403", async () => {
    const entry: MarketEntry = { marketplace: "official", name: "lint", description: "Lints", version: "1.0", source: "github:initxy/noeta-plugins", commit: "abc1234", parts: ["backend", "web"], ui_api: 1, installed: false };
    const installPlugin = vi.fn().mockRejectedValueOnce(new ApiError(403, "this server does not install")).mockResolvedValueOnce(info({ name: "lint" }));
    dialog({ market: vi.fn().mockResolvedValue([entry]), installPlugin }, []);
    await userEvent.click(screen.getByRole("tab", { name: "Discover" }));
    await userEvent.click(await screen.findByRole("button", { name: "Install…" }));
    const confirm = screen.getByRole("dialog", { name: "Install lint" });
    expect(confirm).toHaveTextContent("github:initxy/noeta-plugins");
    expect(confirm).toHaveTextContent("abc1234");
    expect(confirm).toHaveTextContent("backend, web");
    expect(installPlugin).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(installPlugin).toHaveBeenCalledWith("lint", "official", "abc1234");
    expect(await screen.findByText(/--allow-plugin-install/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Install" }));
    await waitFor(() => expect(screen.getByText("Installed lint. New sessions run it.")).toBeInTheDocument());
  });

  it("reviews a source typed in, its commit read from @<commit>", async () => {
    const installPlugin = vi.fn().mockResolvedValue(info({ name: "tool" }));
    dialog({ market: vi.fn().mockResolvedValue([]), installPlugin }, []);
    await userEvent.click(screen.getByRole("tab", { name: "Discover" }));
    expect(await screen.findByText("No marketplace lists a plugin.")).toBeInTheDocument();
    await userEvent.type(screen.getByLabelText("Install from a source"), "https://github.com/me/tool.git@0123abcd");
    await userEvent.click(screen.getByRole("button", { name: "Review…" }));
    const confirm = screen.getByRole("dialog", { name: "Install tool" });
    expect(confirm).toHaveTextContent("0123abcd");
    await userEvent.click(screen.getByRole("button", { name: "Install" }));
    expect(installPlugin).toHaveBeenCalledWith("https://github.com/me/tool.git@0123abcd");
  });

  it("shows a Settings tab only once a plugin adds to the settings slot", async () => {
    const registry = new SlotRegistry();
    dialog({}, [], { registry });
    expect(screen.queryByRole("tab", { name: "Settings" })).toBeNull();
    act(() => void registry.add("mcp", "settings", {}, () => <div>MCP servers</div>));
    await userEvent.click(screen.getByRole("tab", { name: "Settings" }));
    expect(screen.getByText("MCP servers")).toBeInTheDocument();
  });
});

describe("UI data", () => {
  const items: UiEntry[] = [
    { plugin: "a", type: "status", key: "s", text: "3 greeted", tone: "warn" },
    { plugin: "a", type: "widget", key: "w1", markdown: "**above**", placement: "above" },
    { plugin: "b", type: "widget", key: "w2", markdown: "below", placement: "below" },
    { plugin: "b", type: "notice", key: "n", text: "heads up", level: "warn" },
  ];

  it("shows status items, widgets by placement and notices that close", async () => {
    const { container } = render(
      <>
        <Notices items={items} />
        <Widgets items={items} placement="above" />
        <Widgets items={items} placement="below" />
        <StatusBar items={items} context={{ tokens: 0, window: 0 }} />
      </>,
    );
    expect(screen.getByText("3 greeted")).toHaveClass("tone-warn");
    expect(container.querySelector(".widgets-above strong")).toHaveTextContent("above");
    expect(container.querySelector(".widgets-below")).toHaveTextContent("below");
    expect(screen.getByRole("status")).toHaveTextContent("b: heads up");
    await userEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByRole("status")).toBeNull();
  });
});

describe("the page's slots", () => {
  const page = (registry: SlotRegistry, children: ReactNode) => render(<SlotsContext.Provider value={registry}>{children}</SlotsContext.Provider>);

  it("a fenced block is the codeblock slot of its language", () => {
    const registry = new SlotRegistry();
    registry.add("mermaid", "codeblock:mermaid", { mount: "single" }, ({ code, language }: CodeBlockSlotProps) => (
      <div data-testid="diagram">
        {language}:{code}
      </div>
    ));
    const { container } = page(registry, <Markdown text={"```mermaid\ngraph TD; A-->B\n```\n\n```python\nx = 1\n```"} />);
    expect(screen.getByTestId("diagram")).toHaveTextContent("mermaid:graph TD; A-->B");
    expect(container.querySelector("code.language-python")).toHaveTextContent("x = 1"); // python keeps its own view
  });

  it("a tool card's body is the tool's slot, even for a tool that shows none", () => {
    const registry = new SlotRegistry();
    registry.add("shell", "tool:Bash", { mount: "single" }, ({ call }: ToolSlotProps) => <div>custom {String(call.input.command)}</div>);
    registry.add("extra", "tool:Read", {}, ({ call }: ToolSlotProps) => <div>read {call.status}</div>);
    page(
      registry,
      <>
        <ToolCard t={{ kind: "tool", id: "c1", name: "Bash", input: { command: "ls" }, status: "done", result: "a" }} />
        <ToolCard t={{ kind: "tool", id: "c2", name: "Read", input: { file_path: "/a" }, status: "running" }} />
      </>,
    );
    expect(screen.getByText("custom ls")).toBeInTheDocument();
    expect(screen.queryByText("a")).toBeNull(); // the generic view's output is replaced
    expect(screen.getByText("read running")).toBeInTheDocument();
  });

  it("a card whose plugin shows nothing while closed is one line, with its result's length, and open gets `open`", async () => {
    const registry = new SlotRegistry();
    registry.add("p", "tool:Look", { mount: "single" }, ({ call, open }: ToolSlotProps) => (open ? <div>opened {call.result}</div> : null));
    const { container } = page(registry, <ToolCard t={{ kind: "tool", id: "c1", name: "Look", input: {}, status: "done", result: "x\ny" }} />);
    expect(container.querySelector(".tool")).toHaveClass("tool-compact");
    expect(container.querySelector(".tool-head")).toHaveTextContent("· 2 lines");
    await userEvent.click(screen.getByText("Look"));
    expect(screen.getByText(/opened x/)).toBeInTheDocument();
    expect(container.querySelector(".tool")).toHaveClass("tool-compact"); // the look it had closed
    expect(container.querySelector(".tool-head")).not.toHaveTextContent("lines");
  });

  it("the status bar's own content is the model picker and the items; a negative order comes first; the context is its props", () => {
    const registry = new SlotRegistry();
    registry.add("a", "statusbar", { order: -1 }, () => <span>first</span>);
    registry.add("b", "statusbar", {}, ({ context }: StatusbarSlotProps) => <span>last {context.tokens}</span>);
    const { container } = page(
      registry,
      <div className="pickers">
        <StatusBar items={[]} context={{ tokens: 7, window: 10 }}>
          <span>picker</span>
        </StatusBar>
      </div>,
    );
    expect(container.querySelector(".pickers")).toHaveTextContent(/^firstpickerlast 7$/);
    expect(container.querySelector(".pickers > .statusbar")).toBeInTheDocument();
  });
});

describe("the page's own content in a slot", () => {
  it("keeps what is typed in the composer while plugins' contributions come and go, a wrap of it too", async () => {
    const registry = new SlotRegistry();
    function Footer() {
      const [keep] = useState(newComposerKeep);
      return (
        <SlotsContext.Provider value={registry}>
          <Slot name="composer">
            <Composer busy={false} commands={[]} findFiles={async () => []} onSend={() => undefined} onStop={() => undefined} keep={keep} draft={{ text: "rewound", n: 1 }} />
          </Slot>
        </SlotsContext.Provider>
      );
    }
    render(<Footer />);
    const box = screen.getByLabelText("Message");
    await userEvent.type(box, " and more");
    let remove = () => undefined as void;
    act(() => void (remove = registry.add("p", "composer", { order: -1 }, () => <span>beside</span>)));
    act(() => void registry.add("q", "composer", { key: "k", mount: "keyed" }, () => <span>keyed</span>));
    expect(screen.getByLabelText("Message")).toBe(box); // never remounted
    act(() => remove());
    expect(screen.getByLabelText("Message")).toBe(box);
    let unwrap = () => undefined as void;
    act(() => void (unwrap = registry.add("permissions", "composer", { mount: "wrap" }, ({ children }: WrapProps) => <div data-testid="wrap">{children}</div>)));
    expect(screen.getByTestId("wrap")).toContainElement(screen.getByLabelText("Message"));
    expect(screen.getByLabelText("Message")).toHaveValue("rewound and more");
    act(() => unwrap());
    expect(screen.getByLabelText("Message")).toHaveValue("rewound and more"); // the draft is not put in again
    act(() => registry.withdraw("q"));
    expect(screen.getByLabelText("Message")).toHaveValue("rewound and more");
  });
});

describe("the page's own content in a slot, put in and remounted at once", () => {
  it("keeps a draft put in just as a wrap comes (a rewind opens another session)", () => {
    const registry = new SlotRegistry();
    const keep = newComposerKeep();
    // Its effect runs after the composer's, as the page's plugin host does.
    function Wraps({ n }: { n: number }) {
      useEffect(() => (n ? registry.add("permissions", "composer", { mount: "wrap" }, ({ children }: WrapProps) => <div>{children}</div>) : undefined), [n]);
      return null;
    }
    const at = (draft?: { text: string; n: number }) => (
      <SlotsContext.Provider value={registry}>
        <Slot name="composer">
          <Composer busy={false} commands={[]} findFiles={async () => []} onSend={() => undefined} onStop={() => undefined} keep={keep} draft={draft} />
        </Slot>
        <Wraps n={draft?.n ?? 0} />
      </SlotsContext.Provider>
    );
    const { rerender } = render(at());
    rerender(at({ text: "hello two", n: 1 }));
    expect(screen.getByLabelText("Message")).toHaveValue("hello two");
  });
});

describe("plugins' records in the transcript", () => {
  let seq = 0;
  const ev = (event: Event): Action => ({ type: "event", rec: { seq: ++seq, at: 0, event } as Recorded });
  const record = (extension: string, kind: string, data?: unknown): Action => ev({ type: "ExtensionRecorded", extension, kind, data });
  const said = (text: string, inbox: number): Action =>
    ev({ type: "InputTaken", through: inbox, items: [{ seq: inbox, at: 0, body: { kind: "message", message: { role: "user", content: [{ type: "text", text }], origin: "human" } } }] });

  it("shows a record where it was recorded once a contribution shows its kind, and no other", () => {
    const noisy = Array.from({ length: 500 }, (_, i) => record("noisy", "tick", { i }));
    const st = [said("first", 1), ...noisy, record("panel", "note", { text: "between" }), said("second", 2)].reduce(reduce, emptyTask());
    const registry = new SlotRegistry();
    const { container } = render(
      <SlotsContext.Provider value={registry}>
        <Transcript state={st} />
      </SlotsContext.Provider>,
    );
    expect(container.querySelector(".transcript")!.textContent).not.toContain("between");
    act(() => void registry.add("panel", "record:panel/note", {}, ({ record }: RecordSlotProps) => <div className="note">{(record.data as { text: string }).text}</div>));
    expect(container.querySelector(".transcript")!.textContent).toMatch(/^firstbetweensecond/);
    expect(container.querySelectorAll(".note")).toHaveLength(1);
  });
});
