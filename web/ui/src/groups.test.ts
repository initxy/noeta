import { describe, expect, it } from "vitest";
import { agentsSummary, editsSummary, groupItems, groupSummary, shellKind, shortText, type GroupItem as Item, type ToolCallItem as Tool } from "./groups";

let n = 0;
const call = (name: string, input: Record<string, unknown> = {}, more: Partial<Tool> = {}): Tool => ({ kind: "tool", id: `c${++n}`, name, input, status: "done", ...more });
const say = (text: string): Item => ({ kind: "assistant", id: `a${++n}`, text });
const person = (text: string): Item => ({ kind: "user", id: `u${++n}`, text });
const shape = (items: Item[]) => groupItems(items).map((b) => (b.kind === "item" ? b.item.kind : b.kind));

describe("shellKind", () => {
  it("counts a command that only looks as a read, a search or a listing", () => {
    expect(shellKind("cat a.py")).toBe("read");
    expect(shellKind("cd src && head -n 20 a.py | grep x")).toBe("read");
    expect(shellKind("git log --oneline -5")).toBe("read");
    expect(shellKind("rg -n foo src 2>/dev/null")).toBe("search");
    expect(shellKind("find . -name '*.py'")).toBe("search");
    expect(shellKind("ls -la")).toBe("list");
  });

  it("counts anything that may change something as a shell command, git commit and push apart", () => {
    expect(shellKind("npm test")).toBe("shell");
    expect(shellKind("cat a > b")).toBe("shell");
    expect(shellKind("find . -name x -delete")).toBe("shell");
    expect(shellKind("ls && rm -rf build")).toBe("shell");
    expect(shellKind("echo $(whoami)")).toBe("shell");
    expect(shellKind('git commit -m "x"')).toBe("git");
    expect(shellKind("git push origin main")).toBe("git");
    expect(shellKind("gh pr create --fill")).toBe("git");
  });
});

describe("groupItems", () => {
  it("folds quiet calls into one run; short text between them stays in, long text and edits end it", () => {
    const items = [
      person("look"),
      call("Read", { file_path: "/a" }),
      say("Now the tests."),
      call("Grep", { pattern: "x" }),
      call("Bash", { command: "npm test" }),
      say("Here is a long answer.\n\nWith paragraphs.\n\nAnd more."),
      call("Read", { file_path: "/b" }),
      call("Edit", { file_path: "/b" }),
      call("Glob", { pattern: "*" }),
      say("Done."),
    ];
    expect(shape(items)).toEqual(["user", "group", "assistant", "group", "tool", "group", "assistant"]);
    const first = groupItems(items)[1];
    expect(first.kind === "group" && first.items.map((i) => i.kind)).toEqual(["tool", "assistant", "tool", "tool"]);
  });

  it("keeps bookkeeping calls and finished approvals inside a run, a failed call counted", () => {
    const items = [call("Read", { file_path: "/a" }), call("TodoWrite"), call("ToolSearch"), call("Bash", { command: "ls" }, { status: "error", result: "boom" })];
    expect(shape(items)).toEqual(["group"]);
    expect(shape([call("TodoWrite"), person("hi")])).toEqual(["skip", "user"]);
  });

  it("merges edits of one file in a row, and agents started together", () => {
    const items = [
      call("Edit", { file_path: "/w/calc.py" }, { meta: { added: 3, removed: 1 } }),
      call("Edit", { file_path: "/w/calc.py" }, { meta: { added: 9, removed: 3 } }),
      call("Edit", { file_path: "/w/other.py" }),
      call("Agent", { description: "a" }),
      call("Agent", { description: "b" }),
    ];
    const blocks = groupItems(items);
    expect(blocks.map((b) => b.kind)).toEqual(["edits", "item", "agents"]);
    const edits = blocks[0];
    expect(edits.kind === "edits" && editsSummary(edits.path, edits.calls)).toBe("Edited calc.py ×2 (+12 −4)");
    const agents = blocks[2];
    expect(agents.kind === "agents" && agentsSummary(agents.calls)).toBe("Ran 2 agents");
  });

  it("keeps a run's place (its id and range) as calls join it, and keeps the records' places", () => {
    const one = [call("Read", { file_path: "/a" })];
    const two = [...one, say("ok"), call("Read", { file_path: "/b" })];
    expect(groupItems(one)[0].id).toBe(groupItems(two)[0].id);
    expect(groupItems(two)[0]).toMatchObject({ from: 0, to: 3 });
    // A text after the run's last call is drawn on its own until another call follows.
    expect(shape([...one, say("ok")])).toEqual(["group", "assistant"]);
  });
});

describe("groupSummary", () => {
  it("names the run in Claude Code's words, reads by distinct file", () => {
    const items = [
      call("Read", { file_path: "/a" }),
      call("Read", { file_path: "/a" }),
      call("Bash", { command: "cat /b" }),
      call("Grep", { pattern: "x" }),
      call("Glob", { pattern: "*.ts" }),
      call("Bash", { command: "npm test" }),
      call("mcp__github__list_issues"),
      call("mcp__github__get_issue"),
      call("Bash", { command: 'git commit -m "x"' }, { result: "[main 1a2b3c4] x\n 1 file changed" }),
      call("Bash", { command: "git push origin main" }),
      say("Checking."),
    ];
    expect(groupSummary(items)).toMatchObject({
      line: "Committed 1a2b3c4, pushed to main, searched for 2 patterns, read 2 files, called github 2 times, ran 1 shell command",
      active: false,
      text: "Checking.",
    });
  });

  it("while a call runs: the present tense, and its command under the line", () => {
    const s = groupSummary([call("Read", { file_path: "/a" }), call("Bash", { command: "npm test" }, { status: "running" })]);
    expect(s).toMatchObject({ line: "Reading 1 file, running 1 shell command…", active: true, hint: "npm test" });
  });
});

describe("shortText", () => {
  it("lets one or two plain lines stay in a run, not code, a list or a heading", () => {
    expect(shortText("Let me check the tests.")).toBe(true);
    expect(shortText("- one\n- two")).toBe(false);
    expect(shortText("```\nx\n```")).toBe(false);
    expect(shortText("# Plan")).toBe(false);
    expect(shortText("a\nb\nc")).toBe(false);
  });
});
