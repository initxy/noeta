import { describe, expect, it } from "vitest";
import type { Api } from "../api/client";
import type { Event, Recorded } from "../api/types";
import { emptyTask, reduce, sessionStatus, type Action, type TaskState } from "./transcript";
import { earlierLogs } from "./useTask";

let seq = 0;
const ev = (event: Event): Action => ({ type: "event", rec: { seq: ++seq, at: 0, event } as Recorded });
const run = (...actions: Action[]): TaskState => actions.reduce(reduce, emptyTask());
const person = (text: string, inbox = 1): Action =>
  ev({ type: "InputTaken", through: inbox, items: [{ seq: inbox, at: 0, body: { kind: "message", message: { role: "user", content: [{ type: "text", text }], origin: "human" } } }] });
const reply = (...content: object[]): Action =>
  ev({ type: "ModelReplied", response: { message: { role: "assistant", content: content as never }, stop_reason: "end_turn", usage: { uncached: 100, output: 20 } } });
const record = (extension: string, kind: string, data?: unknown): Action => ev({ type: "ExtensionRecorded", extension, kind, data });

describe("reduce", () => {
  it("shows the person's prompt and the recorded reply, not other user turns", () => {
    const st = run(
      ev({ type: "TaskCreated", agent: "main", config: { cwd: "/w", permission_mode: "plan" } }),
      person("hi"),
      ev({
        type: "InputTaken",
        through: 2,
        items: [{ seq: 2, at: 0, body: { kind: "message", message: { role: "user", content: [{ type: "text", text: "notice" }], origin: "system" } } }],
      }),
      ev({ type: "ModelCalled", model: "m1" }),
      reply({ type: "thinking", text: "hmm" }, { type: "text", text: "**hello**" }),
      ev({ type: "Waiting", on: "input" }),
    );
    expect(st.items.map((i) => i.kind)).toEqual(["user", "thinking", "assistant"]);
    expect(st).toMatchObject({ cwd: "/w", mode: "plan", model: "m1", busy: false, waitingOn: "input", contextTokens: 120 });
  });

  it("streams live text for the current call and drops it once the reply is recorded", () => {
    let st = run(person("hi"), ev({ type: "ModelCalled", model: "m" }));
    const called = st.called;
    st = reduce(st, { type: "delta", delta: { kind: "text", text: "Hel", call_seq: called } });
    st = reduce(st, { type: "delta", delta: { kind: "text", text: "lo", call_seq: called } });
    expect(st.live?.text).toBe("Hello");
    st = reduce(st, reply({ type: "text", text: "Hello" }));
    expect(st.live).toBeNull();
    // A late fragment of the replied call is not shown again.
    st = reduce(st, { type: "delta", delta: { kind: "text", text: "x", call_seq: called } });
    expect(st.live).toBeNull();
  });

  it("starts a retry's stream over and ignores the failed attempt's tail", () => {
    let st = run(ev({ type: "ModelCalled", model: "m" }));
    const c = st.called;
    st = reduce(st, { type: "delta", delta: { kind: "text", text: "bad", call_seq: c, attempt: 1 } });
    st = reduce(st, { type: "delta", delta: { kind: "text", text: "good", call_seq: c, attempt: 2 } });
    st = reduce(st, { type: "delta", delta: { kind: "text", text: "tail", call_seq: c, attempt: 1 } });
    expect(st.live?.text).toBe("good");
  });

  it("follows a tool call from proposed through waiting to its result and diff", () => {
    let st = run(reply({ type: "tool_use", id: "c1", name: "Edit", input: { file_path: "/a", old_string: "x", new_string: "y" } }));
    expect(st.items[0]).toMatchObject({ kind: "tool", status: "pending" });
    st = reduce(st, ev({ type: "Waiting", on: "approval", ids: ["c1"] }));
    expect(st.items[0]).toMatchObject({ status: "waiting" });
    expect(st.busy).toBe(true);
    st = reduce(st, ev({ type: "ToolStarted", call: { id: "c1", name: "Edit", input: {} } }));
    st = reduce(st, { type: "delta", delta: { kind: "tool", call_id: "c1", text: "50%" } });
    expect(st.items[0]).toMatchObject({ status: "running", progress: "50%" });
    st = reduce(st, ev({ type: "ToolFinished", result: { call_id: "c1", content: [{ type: "text", text: "ok" }] }, meta: { diff: "+y" } }));
    expect(st.items[0]).toMatchObject({ status: "done", result: "ok", meta: { diff: "+y" }, progress: undefined });
  });

  it("links an Agent call to its sub-agent and its end", () => {
    const st = run(
      reply({ type: "tool_use", id: "c1", name: "Agent", input: {} }),
      record("subtasks", "launched", { call_id: "c1", task_id: "t.c1.1" }),
      ev({ type: "InputTaken", through: 3, items: [{ seq: 3, at: 0, body: { kind: "child_ended", child_id: "t.c1.1", status: "done" } }] }),
    );
    expect(st.items[0]).toMatchObject({ child: "t.c1.1", childStatus: "done" });
  });

  it("reads the plugins' records: title, mode, modes and the task list", () => {
    const st = run(
      record("title", "title", { title: "Fix it" }),
      record("permissions", "modes", { modes: ["default", "plan"], cycle: ["default", "plan"], names: {} }),
      record("permissions", "mode", { mode: "acceptEdits" }),
      record("todos", "created", { id: "1", subject: "Read", active_form: "Reading" }),
      record("todos", "created", { id: "2", subject: "Write" }),
      record("todos", "updated", { id: "1", fields: { status: "in_progress" } }),
      record("todos", "deleted", { id: "2" }),
    );
    expect(st.title).toBe("Fix it");
    expect(st.mode).toBe("acceptEdits");
    expect(st.modes?.cycle).toEqual(["default", "plan"]);
    expect(st.todos).toEqual([{ id: "1", subject: "Read", activeForm: "Reading", status: "in_progress" }]);
  });

  it("marks interrupts, failures, compaction and the end", () => {
    let st = run(ev({ type: "ModelCalled", model: "m" }));
    st = reduce(st, { type: "delta", delta: { kind: "text", text: "par", call_seq: st.called } });
    st = reduce(st, record("core", "turn_stopped", { partial: "par" }));
    expect(st.live).toBeNull();
    expect(st.busy).toBe(false);
    st = reduce(st, record("core", "turn_failed", { reason: "provider", detail: "429" }));
    st = reduce(st, ev({ type: "Compacted", summary: "so far", through: 3 }));
    st = reduce(st, ev({ type: "TaskEnded", status: "failed", error: "boom" }));
    expect(st.items.map((i) => (i.kind === "notice" ? `${i.tone}:${i.text}` : i.kind))).toEqual([
      "assistant",
      "info:Interrupted by you",
      "error:Turn failed: provider: 429",
      "compaction",
      "error:Session failed: boom",
    ]);
    expect(st.ended).toBe("failed");
  });

  it("keeps a queued message until the task takes it", () => {
    let st = reduce(emptyTask(), { type: "queued", seq: 4, text: "next" });
    expect(st.queued).toEqual([{ seq: 4, text: "next" }]);
    st = reduce(st, person("next", 4));
    expect(st.queued).toEqual([]);
    expect(st.items[0]).toMatchObject({ kind: "user", text: "next" });
  });

  it("ignores a send receipt that arrives after the stream showed the message taken", () => {
    let st = run(person("fast", 7), ev({ type: "ModelCalled", model: "m" }), reply({ type: "text", text: "done" }), ev({ type: "Waiting", on: "input" }));
    st = reduce(st, { type: "queued", seq: 7, text: "fast" });
    expect(st.queued).toEqual([]);
    expect(st.busy).toBe(false);
    st = reduce(st, { type: "queued", seq: 8, text: "later" });
    expect(st.queued).toEqual([{ seq: 8, text: "later" }]);
  });

  it("ends a wait on an approval once its answer is taken, not on a message meanwhile", () => {
    let st = run(ev({ type: "Waiting", on: "approval", ids: ["c1"] }));
    expect(st.waitingOn).toBe("approval");
    // A background job's end (or a message) is taken; the approval still stands.
    st = reduce(st, ev({ type: "InputTaken", through: 2, items: [{ seq: 2, at: 0, body: { kind: "message", when: "now", message: { role: "user", content: [{ type: "text", text: "job done" }], origin: "system" } } }] }));
    st = reduce(st, person("also this", 3));
    expect(st.waitingOn).toBe("approval");
    st = reduce(st, ev({ type: "InputTaken", through: 4, items: [{ seq: 4, at: 0, body: { kind: "answer", request_id: "c1", decision: "allow" } }] }));
    expect(st.waitingOn).toBeNull();
  });

  it("shows a typed slash command as the person typed it", () => {
    const st = run(person("<command-message>greet</command-message>\n<command-name>/greet</command-name>\n<command-args>the team</command-args>"));
    expect(st.items[0]).toMatchObject({ kind: "user", text: "/greet the team" });
  });

  it("shows the conversation a task continues before its own, without its turn state", () => {
    const old = (event: Event, n: number): Action => ({ type: "history", from: "old", rec: { seq: n, at: 0, event } as Recorded });
    let st = run(
      old({ type: "TaskCreated", agent: "main", config: { cwd: "/w" } }, 1),
      old({ type: "InputTaken", through: 9, items: [{ seq: 9, at: 0, body: { kind: "message", message: { role: "user", content: [{ type: "text", text: "first" }], origin: "human" } } }] }, 2),
      old({ type: "ModelCalled", model: "m" }, 3),
      old({ type: "ExtensionRecorded", extension: "title", kind: "title", data: { title: "Old title" } }, 4),
      old({ type: "TaskEnded", status: "completed" }, 5),
    );
    expect(st).toMatchObject({ lastSeq: 0, taken: 0, busy: false, ended: null, title: "Old title", cwd: "/w" });
    expect(st.items).toEqual([{ kind: "user", id: "old:u2.9", text: "first", images: [] }]);
    st = reduce(st, person("second", 1));
    expect(st.items.map((i) => (i.kind === "user" ? i.text : i.kind))).toEqual(["first", "second"]);
  });
});

describe("sessionStatus", () => {
  it("names what the sidebar shows", () => {
    expect(sessionStatus("waiting", "approval")).toBe("needs-you");
    expect(sessionStatus("waiting", "question")).toBe("needs-you");
    expect(sessionStatus("waiting", "input")).toBe("idle");
    expect(sessionStatus("waiting", "children")).toBe("running");
    expect(sessionStatus("running", null)).toBe("running");
    expect(sessionStatus("ended", null)).toBe("ended");
  });
});

describe("earlierLogs", () => {
  it("keeps the conversations that load and marks the one that did not", async () => {
    const created = (from: string | null): Recorded => ({ seq: 1, at: 0, event: { type: "TaskCreated", agent: "main", config: {}, history_from: from } as Event });
    const logs: Record<string, Recorded[]> = { t2: [created("t1")] };
    const api = {
      events: async (id: string) => {
        if (!logs[id]) throw new Error("gone");
        return logs[id];
      },
    } as unknown as Api;
    expect(await earlierLogs(api, created("t2"))).toEqual([
      ["t1", null],
      ["t2", logs.t2],
    ]);
  });
});
