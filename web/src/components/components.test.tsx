import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { Api } from "../api/client";
import type { Pending, TaskSummary } from "../api/types";
import { emptyTask, type Item } from "../state/transcript";
import { ApiContext } from "../state/useTask";
import { ApiError } from "../api/client";
import { Composer, menuQuery } from "./Composer";
import { UsageBadge } from "./Controls";
import { Markdown } from "@noeta-ai/ui";
import { SlotRegistry, SlotsContext, type RequestSlotProps } from "@noeta-ai/web-sdk";
import { PendingCards } from "./PendingCard";
import { Sidebar } from "./Sidebar";
import { ToolCard } from "./ToolCard";
import { TaskTranscript, Transcript } from "./Transcript";

type Tool = Extract<Item, { kind: "tool" }>;
const tool = (t: Partial<Tool>): Tool => ({ kind: "tool", id: "c1", name: "some_tool", input: {}, status: "done", ...t });

// A tool no plugin gives a view of; each built-in plugin's view is tested
// with its web part (web/src/plugins/builtin.test.tsx).
describe("ToolCard", () => {
  it("shows a tool's output, and its input once opened", async () => {
    const { container } = render(<ToolCard t={tool({ input: { path: "/a/b/c/d.txt", flag: true }, result: "a\nb" })} />);
    expect(screen.getByText(/a\s+b/)).toBeInTheDocument();
    expect(screen.getByText("…/c/d.txt")).toBeInTheDocument();
    expect(container.querySelector("pre.input")).toBeNull();
    await userEvent.click(screen.getByText("some_tool"));
    expect(container.querySelector("pre.input")).toHaveTextContent('"flag": true');
  });

  it("shows a yes's words under the call's result", () => {
    const { container, rerender } = render(<ToolCard t={tool({ input: { command: "ls" }, result: "a", note: "then run the tests" })} />);
    expect(container.querySelector(".tool-note")).toHaveTextContent("⎿ then run the tests");
    rerender(<ToolCard t={tool({ input: { command: "ls" }, result: "a" })} />);
    expect(container.querySelector(".tool-note")).toBeNull();
  });

  it("folds a long output", async () => {
    const long = Array.from({ length: 40 }, (_, i) => `line ${i}`).join("\n");
    render(<ToolCard t={tool({ input: { command: "seq 40" }, result: long })} />);
    expect(screen.queryByText(/line 39/)).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Show all 40 lines" }));
    expect(screen.getByText(/line 39/)).toBeInTheDocument();
  });

  it("names an MCP tool by its server and tool", () => {
    render(<ToolCard t={tool({ name: "mcp__github__search", input: { query: "bug" }, result: "found" })} />);
    expect(screen.getByText("github · search")).toBeInTheDocument();
  });

  it("follows another task's conversation (ctx.Transcript)", async () => {
    const api = {
      events: vi.fn(async () => []),
      watch: vi.fn(async (_id: string, _after: number, on: { event: (r: unknown) => void }) => {
        on.event({ seq: 1, at: 0, event: { type: "ModelReplied", response: { message: { role: "assistant", content: [{ type: "text", text: "explored it" }] }, stop_reason: "end_turn", usage: {} } } });
      }),
    } as unknown as Api;
    render(
      <ApiContext.Provider value={api}>
        <TaskTranscript taskId="t.c1.1" />
      </ApiContext.Provider>,
    );
    expect(await screen.findByText("explored it")).toBeInTheDocument();
    expect(api.watch).toHaveBeenCalledWith("t.c1.1", 0, expect.anything(), expect.anything());
  });
});

describe("Transcript", () => {
  it("renders markdown, a collapsed thought, live text and queued messages", async () => {
    const state = {
      ...emptyTask(),
      busy: true,
      items: [
        { kind: "user", id: "u", text: "hi", images: [] },
        { kind: "thinking", id: "t", text: "secret plan" },
        { kind: "assistant", id: "a", text: "Some `code` here" },
      ] as Item[],
      live: { callSeq: 9, attempt: 1, text: "stream**ing**", thinking: "" },
      queued: [{ seq: 3, text: "later" }],
    };
    render(<Transcript state={state} />);
    expect(screen.getByText("code").tagName).toBe("CODE");
    expect(screen.queryByText("secret plan")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Thought/ }));
    expect(screen.getByText("secret plan")).toBeInTheDocument();
    expect(screen.getByTestId("streaming")).toHaveTextContent("streaming");
    expect(screen.getByText("later")).toBeInTheDocument();
    expect(screen.getByText("queued")).toBeInTheDocument();
  });
});

const approval: Pending = {
  task_id: "t1",
  request_id: "c1",
  kind: "approval",
  tool: "Bash",
  input: { command: "rm -rf build" },
  prompt: "This command needs your approval",
  detail: {},
  agent: "main",
  title: "Bash command",
  choices: [
    { id: "allow", label: "Yes" },
    { id: "always", label: "Always allow Bash(rm *)" },
    { id: "mode:acceptEdits", label: "Yes, and accept edits" },
    { id: "deny", label: "No" },
  ],
};

describe("PendingCards", () => {
  it("answers an approval with each of Claude Code's choices", async () => {
    const answer = vi.fn().mockResolvedValue({});
    const api = { answer } as unknown as Api;
    const answered = vi.fn();
    render(<PendingCards pending={[approval]} api={api} answered={answered} />);
    await userEvent.click(screen.getByRole("button", { name: /Always allow/ }));
    expect(answer).toHaveBeenLastCalledWith("t1", "c1", "allow", { always: true });
    await userEvent.click(screen.getByRole("button", { name: /accept edits/ }));
    expect(answer).toHaveBeenLastCalledWith("t1", "c1", "allow", { data: { mode: "acceptEdits" } });
    await userEvent.click(screen.getByRole("button", { name: /^4 No/ }));
    await userEvent.type(screen.getByPlaceholderText(/what to do differently/), "use git clean");
    await userEvent.click(screen.getByRole("button", { name: "Deny with feedback" }));
    expect(answer).toHaveBeenLastCalledWith("t1", "c1", "deny", { data: { message: "use git clean" } });
    expect(answered).toHaveBeenCalledTimes(3);
  });

  it("answers from the keyboard: a digit picks, arrows and Enter take the highlighted, Esc says no", async () => {
    const answer = vi.fn().mockResolvedValue({});
    const api = { answer } as unknown as Api;
    const { unmount } = render(<PendingCards pending={[approval]} api={api} answered={() => undefined} />);
    expect(screen.getByRole("dialog")).toHaveFocus();
    await userEvent.keyboard("2");
    expect(answer).toHaveBeenLastCalledWith("t1", "c1", "allow", { always: true });
    await userEvent.keyboard("{ArrowDown}{ArrowDown}{Enter}");
    expect(answer).toHaveBeenLastCalledWith("t1", "c1", "allow", { data: { mode: "acceptEdits" } });
    await userEvent.keyboard("{Escape}");
    expect(answer).toHaveBeenLastCalledWith("t1", "c1", "deny", { data: null });
    unmount();
  });

  it("leaves the focus in a box the person is typing in, so their digits are not an answer", async () => {
    const answer = vi.fn().mockResolvedValue({});
    const api = { answer } as unknown as Api;
    const { rerender } = render(
      <>
        <textarea aria-label="Message" />
        <PendingCards pending={[]} api={api} answered={() => undefined} />
      </>,
    );
    const box = screen.getByLabelText("Message");
    await userEvent.type(box, "step ");
    rerender(
      <>
        <textarea aria-label="Message" />
        <PendingCards pending={[approval]} api={api} answered={() => undefined} />
      </>,
    );
    expect(box).toHaveFocus();
    await userEvent.keyboard("12");
    expect(box).toHaveValue("step 12");
    expect(answer).not.toHaveBeenCalled();
  });

  it("shows why an answer failed, and refreshes the cards when it was answered elsewhere", async () => {
    const answer = vi.fn().mockRejectedValueOnce(new ApiError(500, "store down")).mockRejectedValueOnce(new ApiError(409, "answered"));
    const answered = vi.fn();
    render(<PendingCards pending={[approval]} api={{ answer } as unknown as Api} answered={answered} />);
    await userEvent.click(screen.getByRole("button", { name: /^1 Yes/ }));
    expect(await screen.findByText(/not sent: store down/)).toBeInTheDocument();
    expect(answered).not.toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: /^1 Yes/ }));
    expect(await screen.findByText(/already answered elsewhere/)).toBeInTheDocument();
    expect(answered).toHaveBeenCalledTimes(1);
  });

  it("shows what the tool's plugin shows of the call it asks about (the request slot)", () => {
    const registry = new SlotRegistry();
    registry.add("p", "request:Bash", {}, ({ request }: RequestSlotProps) => <pre>asked: {String(request.input.command)} by {request.agent}</pre>);
    render(
      <SlotsContext.Provider value={registry}>
        <PendingCards pending={[approval]} api={{} as Api} answered={() => undefined} />
      </SlotsContext.Provider>,
    );
    expect(screen.getByText("asked: rm -rf build by main")).toBeInTheDocument();
  });

  it("answers a question with picked options or other text, and says which agent asks", async () => {
    const answer = vi.fn().mockResolvedValue({});
    const question: Pending = {
      ...approval,
      kind: "question",
      tool: "AskUserQuestion",
      agent: "Explore",
      input: {},
      title: "AskUserQuestion",
      detail: {
        questions: [
          { id: "0", question: "Leave plan mode?", options: [{ label: "Yes" }, { label: "No, keep planning" }] },
          { id: "1", question: "Which?", multiSelect: true, options: [{ label: "A" }, { label: "B" }] },
        ],
      },
    };
    render(<PendingCards pending={[question]} api={{ answer } as unknown as Api} answered={() => undefined} />);
    expect(screen.getByText("from the Explore agent")).toBeInTheDocument();
    const submit = screen.getByRole("button", { name: "Submit" });
    expect(submit).toBeDisabled();
    await userEvent.click(screen.getByLabelText("Yes"));
    await userEvent.click(screen.getByLabelText("A"));
    await userEvent.click(screen.getByLabelText("B"));
    await userEvent.click(submit);
    expect(answer).toHaveBeenCalledWith("t1", "c1", null, { data: { answers: { "0": { selected: ["Yes"] }, "1": { selected: ["A", "B"] } } } });
  });
});

describe("Composer", () => {
  it("knows what the caret completes", () => {
    expect(menuQuery("/com", 4)).toEqual({ kind: "slash", q: "com", start: 0 });
    expect(menuQuery("/compact now", 12)).toBeNull();
    expect(menuQuery("see @src/ma", 11)).toEqual({ kind: "mention", q: "src/ma", start: 4 });
    expect(menuQuery("mail me@x", 9)).toBeNull();
  });

  const commands = [{ name: "compact", description: "Summarize", argument_hint: "[focus]", local: false }];

  it("sends on Enter, lists slash commands, stops on Esc", async () => {
    const onSend = vi.fn();
    const onStop = vi.fn();
    render(<Composer busy commands={commands} findFiles={async () => []} onSend={onSend} onStop={onStop} />);
    const box = screen.getByLabelText("Message");
    await userEvent.type(box, "/co");
    expect(screen.getByRole("listbox", { name: "commands" })).toHaveTextContent("/compact");
    await userEvent.keyboard("{Tab}");
    expect(box).toHaveValue("/compact ");
    await userEvent.type(box, "the tests{Enter}");
    expect(onSend).toHaveBeenCalledWith("/compact the tests", []);
    expect(box).toHaveValue("");
    await userEvent.keyboard("{Escape}");
    expect(onStop).toHaveBeenCalled();
    await userEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(onStop).toHaveBeenCalledTimes(2);
  });

  it("runs the highlighted slash command on Enter and cycles the mode on Shift+Tab", async () => {
    const onSend = vi.fn();
    const onCycleMode = vi.fn();
    const two = [...commands, { name: "context", description: "", argument_hint: "", local: false }];
    render(<Composer busy={false} commands={two} findFiles={async () => []} onSend={onSend} onStop={() => undefined} onCycleMode={onCycleMode} />);
    const box = screen.getByLabelText("Message");
    await userEvent.type(box, "/c{ArrowDown}{Enter}");
    expect(onSend).toHaveBeenCalledWith("/context", []);
    expect(box).toHaveValue("");
    await userEvent.keyboard("{Shift>}{Tab}{/Shift}");
    expect(onCycleMode).toHaveBeenCalledTimes(1);
  });

  it("cycles the mode on Shift+Tab with the slash menu open, keeps the text after the command word on Enter, and lets an input method confirm with Enter", async () => {
    const onSend = vi.fn();
    const onCycleMode = vi.fn();
    render(<Composer busy={false} commands={commands} findFiles={async () => []} onSend={onSend} onStop={() => undefined} onCycleMode={onCycleMode} />);
    const box = screen.getByLabelText("Message") as HTMLTextAreaElement;
    await userEvent.type(box, "/co");
    await userEvent.keyboard("{Shift>}{Tab}{/Shift}");
    expect(onCycleMode).toHaveBeenCalledTimes(1);
    expect(box).toHaveValue("/co");
    await userEvent.type(box, "mp the tests");
    box.setSelectionRange(3, 3); // the caret inside the word "/comp"
    fireEvent.select(box);
    expect(screen.getByRole("listbox", { name: "commands" })).toBeInTheDocument();
    fireEvent.keyDown(box, { key: "Enter", isComposing: true });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledWith("/compact the tests", []);
  });

  it("takes a draft to edit (a rewound prompt)", async () => {
    const { rerender } = render(<Composer busy={false} commands={[]} findFiles={async () => []} onSend={() => undefined} onStop={() => undefined} />);
    rerender(<Composer busy={false} commands={[]} findFiles={async () => []} onSend={() => undefined} onStop={() => undefined} draft={{ text: "try again", n: 1 }} />);
    expect(screen.getByLabelText("Message")).toHaveValue("try again");
  });

  it("completes @ mentions from the server's files", async () => {
    const findFiles = vi.fn().mockResolvedValue(["src/main.py"]);
    render(<Composer busy={false} commands={[]} findFiles={findFiles} onSend={() => undefined} onStop={() => undefined} />);
    const box = screen.getByLabelText("Message");
    await userEvent.type(box, "read @ma");
    expect(await screen.findByRole("option", { name: "@src/main.py" })).toBeInTheDocument();
    expect(findFiles).toHaveBeenLastCalledWith("ma");
    await userEvent.keyboard("{Enter}");
    expect(box).toHaveValue("read @src/main.py ");
  });

  it("attaches a pasted image to the message", async () => {
    const onSend = vi.fn();
    render(<Composer busy={false} commands={[]} findFiles={async () => []} onSend={onSend} onStop={() => undefined} />);
    const box = screen.getByLabelText("Message");
    const png = new File([new Uint8Array([137, 80, 78, 71])], "shot.png", { type: "image/png" });
    fireEvent.paste(box, { clipboardData: { files: [png] } });
    expect(await screen.findByText("shot.png")).toBeInTheDocument();
    await userEvent.type(box, "what is this{Enter}");
    await waitFor(() => expect(onSend).toHaveBeenCalledWith("what is this", [{ mediaType: "image/png", data: "iVBORw==", name: "shot.png" }]));
  });
});

describe("Sidebar", () => {
  it("groups sessions by directory with their status", async () => {
    const s = (task_id: string, cwd: string, status: TaskSummary["status"], waiting_on: string | null, title = ""): TaskSummary => ({
      task_id,
      agent: "main",
      status,
      waiting_on,
      end_status: null,
      created_at: 0,
      updated_at: Date.now() / 1000,
      title,
      cwd,
      prompt: `prompt ${task_id}`,
    });
    const onOpen = vi.fn();
    render(
      <Sidebar
        sessions={[s("a", "/w/app", "waiting", "approval", "Fix bug"), s("b", "/w/app", "running", null), s("c", "/w/lib", "ended", null)]}
        current="a"
        onOpen={onOpen}
        onNew={() => undefined}
      />,
    );
    expect(screen.getByText("app")).toBeInTheDocument();
    expect(screen.getByText("lib")).toBeInTheDocument();
    expect(screen.getByLabelText("Needs you")).toBeInTheDocument();
    expect(screen.getByLabelText("Running")).toBeInTheDocument();
    expect(screen.getByText("prompt b")).toBeInTheDocument(); // untitled: its prompt
    await userEvent.click(screen.getByText("Fix bug"));
    expect(onOpen).toHaveBeenCalledWith("a");
  });

  it("shows the open session's live title and status, and offers to disconnect", async () => {
    const row: TaskSummary = { task_id: "a", agent: "main", status: "ready", waiting_on: null, end_status: null, created_at: 0, updated_at: 0, title: "", cwd: "/w", prompt: "" };
    const onDisconnect = vi.fn();
    render(<Sidebar sessions={[row]} current="a" live={{ title: "Live title", status: "needs-you" }} onOpen={() => undefined} onNew={() => undefined} onDisconnect={onDisconnect} />);
    expect(screen.getByText("Live title")).toBeInTheDocument();
    expect(screen.getByLabelText("Needs you")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Disconnect" }));
    expect(onDisconnect).toHaveBeenCalled();
  });

  const row = (task_id: string, title: string, prompt: string, cwd = "/w/app"): TaskSummary => ({
    task_id,
    agent: "main",
    status: "waiting",
    waiting_on: "input",
    end_status: null,
    created_at: 0,
    updated_at: 0,
    title,
    cwd,
    prompt,
  });

  it("narrows the list by title, first prompt or folder as the search box is typed in", async () => {
    render(
      <Sidebar
        sessions={[row("a", "Fix the parser", "the parser drops commas"), row("b", "", "write release notes"), row("c", "Lint", "run ruff", "/w/lib")]}
        current="b"
        live={{ title: "Release notes", status: "idle" }}
        onOpen={() => undefined}
        onNew={() => undefined}
      />,
    );
    const box = screen.getByRole("searchbox", { name: "Search sessions" });
    await userEvent.type(box, "PARSER");
    expect(screen.getByText("Fix the parser")).toBeInTheDocument();
    expect(screen.queryByText("Release notes")).toBeNull();
    await userEvent.clear(box);
    await userEvent.type(box, "release notes"); // the open session's live title
    expect(screen.getByText("Release notes")).toBeInTheDocument();
    expect(screen.queryByText("Fix the parser")).toBeNull();
    await userEvent.clear(box);
    await userEvent.type(box, "lib");
    expect(screen.getByText("Lint")).toBeInTheDocument();
    await userEvent.type(box, " nothing");
    expect(screen.getByText("No sessions match")).toBeInTheDocument();
    await userEvent.type(box, "{Escape}");
    expect(box).toHaveValue("");
    expect(screen.getByText("Fix the parser")).toBeInTheDocument();
  });

  it("archives a row, and switches to the archived list where a row is restored", async () => {
    const onArchive = vi.fn();
    const onShowArchived = vi.fn();
    const { rerender } = render(
      <Sidebar sessions={[row("a", "Fix the parser", "")]} current={null} onOpen={() => undefined} onNew={() => undefined} onArchive={onArchive} onShowArchived={onShowArchived} />,
    );
    await userEvent.click(screen.getByRole("button", { name: "Archive" }));
    expect(onArchive).toHaveBeenCalledWith("a", true);
    await userEvent.click(screen.getByRole("button", { name: "Archived" }));
    expect(onShowArchived).toHaveBeenCalledWith(true);
    rerender(<Sidebar sessions={[row("a", "Fix the parser", "")]} current={null} archived onOpen={() => undefined} onNew={() => undefined} onArchive={onArchive} onShowArchived={onShowArchived} />);
    await userEvent.click(screen.getByRole("button", { name: "Unarchive" }));
    expect(onArchive).toHaveBeenCalledWith("a", false);
    await userEvent.click(screen.getByRole("button", { name: "Back to sessions" }));
    expect(onShowArchived).toHaveBeenCalledWith(false);
    rerender(<Sidebar sessions={[]} current={null} archived onOpen={() => undefined} onNew={() => undefined} onArchive={onArchive} onShowArchived={onShowArchived} />);
    expect(screen.getByText("No archived sessions")).toBeInTheDocument();
  });
});

describe("Markdown", () => {
  it("opens links in a new tab and does not load remote images", () => {
    const { container } = render(<Markdown text={"[docs](https://example.com/d) ![chart](https://evil.example/pixel.png) ![dot](data:image/png;base64,iVBORw==)"} />);
    const link = screen.getByRole("link", { name: "docs" });
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener noreferrer");
    expect(container.querySelector('img[src^="https:"]')).toBeNull();
    expect(screen.getByRole("link", { name: "Image: chart" })).toHaveAttribute("href", "https://evil.example/pixel.png");
    expect(container.querySelector("img")).toHaveAttribute("src", "data:image/png;base64,iVBORw==");
  });

  it("drops a javascript: link", () => {
    render(<Markdown text={"[x](javascript:alert(1))"} />);
    expect(screen.getByText("x").closest("a")?.getAttribute("href") ?? "").not.toMatch(/javascript/);
  });
});

describe("UsageBadge", () => {
  const usage = { tasks: 1, cost_usd: 0.5, usage: { uncached: 90_000 }, by_model: {} };
  it("shows tokens, cost and the context's share of the window", () => {
    const { rerender, container } = render(<UsageBadge usage={usage} contextTokens={50_000} window={200_000} />);
    expect(container).toHaveTextContent("90.0k tokens · $0.50 · 25% context");
    rerender(<UsageBadge usage={usage} contextTokens={50_000} window={0} />);
    expect(container).not.toHaveTextContent("% context");
    rerender(<UsageBadge usage={null} contextTokens={50_000} window={200_000} />);
    expect(container).toBeEmptyDOMElement();
  });
});
