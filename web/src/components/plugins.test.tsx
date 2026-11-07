import { Markdown } from "@noeta-ai/ui";
import { SlotRegistry, SlotsContext, type CodeBlockSlotProps, type StatusbarSlotProps, type ToolSlotProps } from "@noeta-ai/web-sdk";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type Api } from "../api/client";
import type { MarketEntry, PluginInfo, UiEntry } from "../api/types";
import { PluginsDialog } from "./PluginsDialog";
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
