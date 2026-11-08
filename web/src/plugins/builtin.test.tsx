// The built-in plugins' web parts, each through the page's own loader, ctx
// and slots (as a third-party part runs), and each gone once switched off.
import { Slot, SLOTS, SlotRegistry, SlotsContext } from "@initxy/noeta-web-sdk";
import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Api } from "../api/client";
import type { Event, Pending, PluginInfo, Recorded } from "../api/types";
import { Composer } from "../components/Composer";
import { PendingCards } from "../components/PendingCard";
import { StatusBar } from "../components/PluginUi";
import { ToolCard } from "../components/ToolCard";
import type { Item } from "../state/transcript";
import { ApiContext } from "../state/useTask";
import { BUILTIN_WEB_PARTS } from "./builtin";
import { contextLeft } from "./compaction";
import { pluginContext, ThemeSource } from "./context";
import { TaskEvents } from "./events";
import { PluginHost } from "./host";
import { webParts } from "./usePlugins";

afterEach(() => vi.restoreAllMocks());

const listed = (name: string): PluginInfo => ({
  name,
  version: "",
  description: "",
  source: "package noeta",
  builtin: true,
  enabled: true,
  switchable: false,
  broken: null,
  parts: ["backend"],
  ui_api: null,
  web: null,
});

let seq = 0;
const rec = (event: Event): Recorded => ({ seq: ++seq, at: 0, event });
const record = (extension: string, kind: string, data?: unknown) => rec({ type: "ExtensionRecorded", extension, kind, data } as Event);

/** A page running the built-in parts of `names` for task t1. */
async function page(names: string[], log: Recorded[] = []) {
  const registry = new SlotRegistry();
  const events = new TaskEvents("t1");
  const api = {
    events: vi.fn(async () => log),
    watch: vi.fn(async () => undefined),
    signal: vi.fn(async () => ({})),
    send: vi.fn(async () => ({})),
    answer: vi.fn(async () => ({})),
    pluginCall: vi.fn(async () => null),
  };
  const host = new PluginHost(registry, (plugin) => pluginContext(plugin, { api: api as unknown as Api, events, registry, theme: new ThemeSource("light") }));
  const run = (off: string[] = []) => act(() => host.sync(webParts(names.map(listed), off, () => Promise.reject(new Error("not served")))));
  await run();
  const show = (ui: ReactNode) =>
    render(
      <ApiContext.Provider value={api as unknown as Api}>
        <SlotsContext.Provider value={registry}>{ui}</SlotsContext.Provider>
      </ApiContext.Provider>,
    );
  const emit = (...recs: Recorded[]) => act(() => recs.forEach((r) => events.emit(r)));
  return { registry, events, api, host, run, show, emit };
}

type Tool = Extract<Item, { kind: "tool" }>;
const tool = (t: Partial<Tool>): Tool => ({ kind: "tool", id: "c1", name: "Bash", input: {}, status: "done", ...t });
const pending = (p: Partial<Pending>): Pending => ({
  task_id: "t1",
  request_id: "r1",
  kind: "approval",
  tool: "Bash",
  input: {},
  prompt: "",
  detail: {},
  agent: "main",
  title: "Approve",
  ...p,
});

describe("the built-in web parts", () => {
  it("each adds through its own ctx only, and all it added goes when it is switched off", async () => {
    const names = Object.keys(BUILTIN_WEB_PARTS);
    const p = await page(names);
    for (const name of names) {
      expect(p.host.status(name)).toEqual({ state: "active" });
      const slots = ["sidebar", "statusbar", "settings", "session.header", "composer"].flatMap((s) => p.registry.contributions(s));
      const all = [...new Set(slots.map((c) => c.plugin))];
      expect(all.every((n) => names.includes(n))).toBe(true);
    }
    await p.run(names);
    for (const name of names) expect(p.host.status(name)).toBeUndefined();
    expect(["sidebar", "statusbar", "settings", "session.header", "composer", "tool:Bash", "request:Bash"].flatMap((s) => p.registry.contributions(s))).toEqual([]);
  });

  it("are loaded only for a built-in the task runs", async () => {
    expect(webParts([{ ...listed("todos"), builtin: false }], [], () => Promise.resolve({}))).toEqual([]);
    expect(webParts([listed("todos")], ["todos"], () => Promise.resolve({}))).toEqual([]);
    expect(webParts([], [], () => Promise.resolve({}))).toEqual([]);
  });
});

describe("todos", () => {
  it("lists the task list beside the conversation, history first, and leaves when switched off", async () => {
    const p = await page(["todos"]);
    p.events.keep(record("todos", "created", { id: "0", subject: "Earlier" }));
    p.show(<Slot name={SLOTS.sidebar} />);
    expect(screen.getByRole("complementary", { name: "Tasks" })).toHaveTextContent("Earlier");
    p.emit(
      record("todos", "created", { id: "1", subject: "Read", active_form: "Reading" }),
      record("todos", "created", { id: "2", subject: "Write" }),
      record("todos", "updated", { id: "1", fields: { status: "in_progress" } }),
      record("todos", "deleted", { id: "0" }),
      record("todos", "updated", { id: "2", fields: { status: "completed" } }),
    );
    const panel = screen.getByRole("complementary", { name: "Tasks" });
    expect(panel).toHaveTextContent("Tasks 1/2");
    expect(panel).toHaveTextContent("◐Reading");
    expect(panel).not.toHaveTextContent("Earlier");
    await p.run(["todos"]);
    expect(screen.queryByRole("complementary", { name: "Tasks" })).toBeNull();
  });

  it("shows nothing for an empty list, and a task tool's call as one line", async () => {
    const p = await page(["todos"]);
    const { container } = p.show(
      <>
        <aside className="side-panels">
          <Slot name={SLOTS.sidebar} />
        </aside>
        <ToolCard t={tool({ name: "TaskCreate", input: { subject: "x" }, result: "Task #1 created" })} />
      </>,
    );
    expect(container.querySelector(".side-panels")).toBeEmptyDOMElement();
    expect(container.querySelector(".tool")).toHaveClass("tool-compact");
    expect(container.querySelector(".tool-head")).toHaveTextContent("· 1 lines");
  });
});

describe("compaction", () => {
  it("counts down to the auto-compact point in the status bar", async () => {
    const p = await page(["compaction"]);
    const { container, rerender } = p.show(<StatusBar items={[]} context={{ tokens: 90_000, window: 200_000 }} />);
    expect(container).not.toHaveTextContent("auto-compact");
    p.emit(record("compaction", "threshold", { threshold: 100_000, window: 120_000, auto: true }));
    expect(container).toHaveTextContent("10% until auto-compact");
    rerender(
      <ApiContext.Provider value={p.api as unknown as Api}>
        <SlotsContext.Provider value={p.registry}>
          <StatusBar items={[]} context={{ tokens: 10_000, window: 200_000 }} />
        </SlotsContext.Provider>
      </ApiContext.Provider>,
    );
    expect(container).not.toHaveTextContent("auto-compact");
  });

  it("counts down only within 20K tokens of the point, as the TUI does", () => {
    const at = { threshold: 100_000, window: 120_000, auto: true };
    expect(contextLeft(10_000, at)).toBeNull();
    expect(contextLeft(79_999, at)).toBeNull();
    expect(contextLeft(90_000, at)).toEqual({ text: "10% until auto-compact", low: false });
    expect(contextLeft(130_000, at)?.text).toBe("0% until auto-compact");
    const off = contextLeft(108_000, { ...at, auto: false });
    expect(off?.low).toBe(true);
    expect(off?.text).toBe("Context low (10% remaining) · Run /compact to compact & continue");
    expect(contextLeft(90_000, null)).toBeNull();
  });
});

describe("permissions", () => {
  it("shows the mode first in the status bar, switches it, and cycles it on Shift+Tab", async () => {
    const p = await page(["permissions"], [rec({ type: "TaskCreated", agent: "main", config: { permission_mode: "acceptEdits" } } as Event)]);
    const { container } = p.show(
      <div className="pickers">
        <StatusBar items={[]} context={{ tokens: 0, window: 0 }}>
          <span>model</span>
        </StatusBar>
        <Slot name={SLOTS.composer}>
          <Composer busy={false} commands={[]} findFiles={async () => []} onSend={() => undefined} onStop={() => undefined} />
        </Slot>
      </div>,
    );
    const picker = screen.getByLabelText("Permission mode");
    expect(container.querySelector(".pickers")?.firstElementChild).toBe(picker);
    await screen.findByDisplayValue("Accept edits"); // the mode the task was made with
    p.emit(record("permissions", "modes", { modes: ["default", "plan"], cycle: ["default", "plan"], names: { plan: "Plan" } }), record("permissions", "mode", { mode: "default" }));
    expect(picker).toHaveValue("default");
    await userEvent.selectOptions(picker, "plan");
    expect(p.api.signal).toHaveBeenLastCalledWith("t1", "permission_mode", { mode: "plan" }, undefined);
    fireEvent.keyDown(screen.getByLabelText("Message"), { key: "Tab", shiftKey: true });
    expect(p.api.signal).toHaveBeenLastCalledWith("t1", "permission_mode", { mode: "plan" }, undefined);
    p.emit(record("permissions", "plan_exit", { mode: "plan" }));
    fireEvent.keyDown(screen.getByLabelText("Message"), { key: "Tab", shiftKey: true });
    expect(p.api.signal).toHaveBeenLastCalledWith("t1", "permission_mode", { mode: "default" }, undefined);
    await p.run(["permissions"]);
    expect(screen.queryByLabelText("Permission mode")).toBeNull();
  });
});

describe("plan_mode", () => {
  it("puts the plan on the review card and, once answered, on the call's card", async () => {
    const p = await page(["plan_mode"]);
    const question = pending({ kind: "question", tool: "ExitPlanMode", input: { plan: "# The plan\n\n1. do it" }, detail: { questions: [{ id: "0", question: "Go?", options: [{ label: "Yes" }] }] } });
    const { container } = p.show(
      <>
        <PendingCards pending={[question]} api={p.api as unknown as Api} answered={() => undefined} />
        <ToolCard t={tool({ id: "c2", name: "ExitPlanMode", status: "waiting", input: { plan: "# Waiting" } })} />
        <ToolCard t={tool({ id: "c3", name: "ExitPlanMode", input: { plan: "# Done" } })} />
      </>,
    );
    expect(screen.getByText("Ready to code? Here is the plan:")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "The plan" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Waiting" })).toBeNull();
    expect(screen.getByRole("heading", { name: "Done" })).toBeInTheDocument();
    await p.run(["plan_mode"]);
    expect(screen.queryByText("Ready to code? Here is the plan:")).toBeNull();
    expect(container).not.toHaveTextContent("Done");
  });
});

describe("subtasks", () => {
  it("names the agent and its job and opens the sub-agent's conversation", async () => {
    const p = await page(["subtasks"]);
    p.api.watch.mockImplementation((async (_id: string, _after: number, on: { event: (r: Recorded) => void }) => {
      on.event(rec({ type: "ModelReplied", response: { message: { role: "assistant", content: [{ type: "text", text: "explored it" }] }, stop_reason: "end_turn", usage: {} } } as Event));
    }) as never);
    p.show(<ToolCard t={tool({ name: "Agent", input: { subagent_type: "Explore", description: "look" }, children: [{ id: "t.c1.1", status: "done" }] })} />);
    expect(screen.getByText("Explore · look")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Show the sub-agent's work \(done\)/ }));
    expect(await screen.findByText("explored it")).toBeInTheDocument();
    expect(p.api.watch).toHaveBeenCalledWith("t.c1.1", 0, expect.anything(), expect.anything());
  });
});

describe("workflow", () => {
  it("follows the run its call started", async () => {
    const p = await page(["workflow"]);
    p.emit(
      record("workflow", "run", { run_id: "wf_1", name: "review", description: "Review the diff", call_id: "c1" }),
      record("workflow", "agent", { run_id: "wf_1", key: "k1", child_id: "t.wf.1", label: "reader", phase: "read" }),
      record("workflow", "run", { run_id: "wf_2", name: "other", description: "", call_id: "c9" }),
    );
    const { container } = p.show(<ToolCard t={tool({ name: "Workflow", input: { script: "…" }, result: "started" })} />);
    expect(container).toHaveTextContent("review · Review the diff · running");
    expect(screen.getByRole("button", { name: /reader · read/ })).toBeInTheDocument();
    p.emit(record("workflow", "ended", { run_id: "wf_1", status: "completed", error: "" }));
    expect(container).toHaveTextContent("· completed");
    expect(container).not.toHaveTextContent("other");
  });
});

describe("findings", () => {
  it("lists a review's findings on its call's card", async () => {
    const p = await page(["findings"]);
    const input = { level: "high", findings: [{ file: "a.py", line: 3, summary: "Off by one", category: "correctness", verdict: "CONFIRMED" }, { file: "b.py", summary: "Slow", short_summary: "Slow loop" }] };
    const { container } = p.show(<ToolCard t={tool({ name: "ReportFindings", input, result: "Recorded 2 findings." })} />);
    expect(container).toHaveTextContent("Code review (high): 2 findings");
    expect(container).toHaveTextContent("a.py:3 — Off by one · correctness · confirmed");
    expect(container).toHaveTextContent("b.py — Slow loop");
  });
});

describe("title", () => {
  it("shows the recorded title in the header and renames by the plugin's signal", async () => {
    const p = await page(["title"]);
    p.show(
      <Slot name={SLOTS.sessionHeader} props={{ title: "Listed title" }}>
        <button className="title">host title</button>
      </Slot>,
    );
    expect(screen.getByRole("button", { name: "Listed title" })).toBeInTheDocument();
    p.emit(record("title", "title", { title: "Fix the build" }));
    vi.spyOn(window, "prompt").mockReturnValue("  Renamed ");
    await userEvent.click(screen.getByRole("button", { name: "Fix the build" }));
    expect(p.api.signal).toHaveBeenCalledWith("t1", "rename", { title: "Renamed" }, undefined);
    await p.run(["title"]);
    expect(screen.getByRole("button", { name: "host title" })).toBeInTheDocument();
  });
});

describe("memory, mcp and hooks", () => {
  it("each has a section in the plugins dialog's settings", async () => {
    const p = await page(["memory", "mcp", "hooks"]);
    const { container } = p.show(<Slot name={SLOTS.settings} />);
    expect(container).toHaveTextContent("no memory directory yet");
    expect(container).toHaveTextContent("No MCP server in this session.");
    expect(container).toHaveTextContent("No hook has acted in this session.");
    p.emit(
      record("memory", "dir", { dir: "/home/me/.noeta/memory/p" }),
      record("mcp", "listed", { servers: [{ alias: "github", tools: [{ name: "a" }, { name: "b" }] }] }),
      record("mcp", "server_down", { alias: "jira", reason: "timed out" }),
      record("hooks", "stopped", { hook: "Stop hook stopped continuation" }),
      record("hooks", "context", { hook: "PreToolUse:Bash hook additional context" }),
      record("hooks", "context", { hook: "PreToolUse:Bash hook additional context" }),
      record("hooks", "once", { key: "x" }),
    );
    expect(container).toHaveTextContent("/home/me/.noeta/memory/p");
    expect(container).toHaveTextContent("github 2 tools");
    expect(container).toHaveTextContent("jira not reached: timed out");
    expect(container).toHaveTextContent("Stop hook stopped continuation");
    expect(container).toHaveTextContent("PreToolUse:Bash hook additional context ×2");
    p.emit(record("mcp", "server_up", { alias: "jira" }));
    expect(container).not.toHaveTextContent("jira");
    await p.run(["memory", "mcp", "hooks"]);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("fs", () => {
  it("shows an edit as a diff: proposed before it runs, the recorded one after", async () => {
    const p = await page(["fs"]);
    const { rerender, container } = p.show(<ToolCard t={tool({ name: "Edit", status: "waiting", input: { file_path: "/a/b/c.py", old_string: "x = 1", new_string: "x = 2" } })} />);
    expect(container.querySelector(".diff-del")).toHaveTextContent("-x = 1");
    expect(container.querySelector(".diff-add")).toHaveTextContent("+x = 2");
    expect(screen.getByText("waiting for you")).toBeInTheDocument();
    expect(screen.getByText("…/b/c.py")).toBeInTheDocument();
    rerender(
      <SlotsContext.Provider value={p.registry}>
        <ToolCard t={tool({ name: "Edit", input: { file_path: "/a/b/c.py" }, meta: { diff: "@@ -1 +1 @@\n-old\n+new\n", added: 1, removed: 1 } })} />
      </SlotsContext.Provider>,
    );
    expect(container.querySelector(".diff-hunk")).toHaveTextContent("@@ -1 +1 @@");
    expect(container.querySelector(".diffstat")).toHaveTextContent("+1 -1");
  });

  it("shows the change an approval asks about, and a Read as one line", async () => {
    const p = await page(["fs"]);
    const edit = pending({ tool: "Edit", title: "Edit file", input: { file_path: "/w/a.py", old_string: "x = 1", new_string: "x = 2" } });
    const { container } = p.show(
      <>
        <PendingCards pending={[edit]} api={p.api as unknown as Api} answered={() => undefined} />
        <ToolCard t={tool({ id: "c2", name: "Read", input: { file_path: "/a" }, result: "x\ny\nz", note: "skim it" })} />
      </>,
    );
    expect(screen.getByText("/w/a.py")).toBeInTheDocument();
    expect(container.querySelector(".card .diff-del")).toHaveTextContent("-x = 1");
    expect(container.querySelector(".card .diff-add")).toHaveTextContent("+x = 2");
    const read = container.querySelector('.tool[data-tool="Read"]')!;
    expect(read).toHaveClass("tool-compact");
    expect(read).toHaveTextContent("· 3 lines");
    expect(read.querySelector(".tool-note")).toHaveTextContent("skim it");
    await userEvent.click(screen.getByText("Read"));
    expect(read.querySelector(".tool-output")).toHaveTextContent("x y z");
    await p.run(["fs"]);
    expect(container.querySelector(".card .diff-del")).toBeNull();
  });
});

describe("shell", () => {
  it("shows a command, its output as it streams and once done, and the command an approval asks about", async () => {
    const p = await page(["shell"]);
    const { container, rerender } = p.show(<ToolCard t={tool({ status: "running", input: { command: "ls -la" }, progress: "partial" })} />);
    expect(screen.getByText("$ ls -la")).toBeInTheDocument();
    expect(screen.getByText("partial")).toBeInTheDocument();
    rerender(
      <SlotsContext.Provider value={p.registry}>
        <ToolCard t={tool({ input: { command: "ls -la" }, result: "a\nb" })} />
        <PendingCards pending={[pending({ input: { command: "rm -rf build" } })]} api={p.api as unknown as Api} answered={() => undefined} />
      </SlotsContext.Provider>,
    );
    expect(screen.getByText(/a\s+b/)).toBeInTheDocument();
    expect(screen.getByText("$ rm -rf build")).toBeInTheDocument();
    await p.run(["shell"]);
    expect(container).not.toHaveTextContent("$ ls -la");
    expect(container).not.toHaveTextContent("$ rm -rf build");
  });
});

describe("web", () => {
  it("shows what a WebFetch asked of the page once opened", async () => {
    const p = await page(["web"]);
    const { container } = p.show(<ToolCard t={tool({ name: "WebFetch", input: { url: "https://x.dev", prompt: "the version" }, result: "1.2" })} />);
    expect(container.querySelector("pre.input")).toBeNull();
    await userEvent.click(screen.getByText("WebFetch"));
    expect(container.querySelector("pre.input")).toHaveTextContent(/^the version$/);
  });
});
