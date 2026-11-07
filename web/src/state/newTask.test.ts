import { afterEach, describe, expect, it, vi } from "vitest";
import { Api } from "../api/client";
import type { Event, Recorded } from "../api/types";
import { continuationConfig, newSessionConfig } from "./newTask";
import { emptyTask, reduce, type Action } from "./transcript";

afterEach(() => vi.unstubAllGlobals());

let seq = 0;
const ev = (event: Event): Action => ({ type: "event", rec: { seq: ++seq, at: 0, event } as Recorded });

describe("the config a session starts with", () => {
  it("a new one: the mode, model and project picked", () => {
    expect(newSessionConfig({ mode: "plan", model: "m", project: "/p" }, "UTC")).toEqual({ timezone: "UTC", permission_mode: "plan", model: "m", cwd: "/p" });
    expect(newSessionConfig({ mode: "default", model: "", project: "" }, "UTC")).toEqual({ timezone: "UTC", permission_mode: "default" });
  });

  it("a continuation names neither the mode nor the plugins switched off: the server carries the ended session's", async () => {
    // The ended session started in plan mode, a plugin left it in acceptEdits
    // (a plan approved), and it ran without one plugin: the page's fold knows
    // none of that as it is now, so it sends none of it.
    const ended = [
      ev({ type: "TaskCreated", agent: "main", config: { permission_mode: "plan", plugins_off: ["web"], cwd: "/w", model: "m1" } }),
      ev({ type: "ExtensionRecorded", extension: "permissions", kind: "plan_exit", data: { mode: "acceptEdits" } }),
      ev({ type: "TaskEnded", status: "cancelled" }),
    ].reduce(reduce, emptyTask());
    const fetch = vi.fn().mockResolvedValue(new Response(JSON.stringify({ task_id: "t2" }), { status: 201 }));
    vi.stubGlobal("fetch", fetch);
    const api = new Api({ server: "", token: "tok" });
    await api.create(null, continuationConfig({ model: ended.model, cwd: ended.cwd }, "UTC"), "main", "t1");
    expect(JSON.parse(fetch.mock.calls[0][1].body)).toEqual({
      agent: "main",
      input: null,
      history_from: "t1",
      config: { timezone: "UTC", continued_as: "resume", model: "m1", cwd: "/w" },
    });
  });
});
