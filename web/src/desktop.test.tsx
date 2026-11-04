import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { TaskSummary } from "./api/types";
import { App } from "./App";
import { attentionChanges, type DesktopBridge, type DesktopCommand } from "./desktop";

const summary = (s: Partial<TaskSummary>): TaskSummary => ({
  task_id: "t1",
  agent: "main",
  status: "running",
  waiting_on: null,
  end_status: null,
  created_at: 0,
  updated_at: 0,
  title: "Fix the build",
  cwd: "/p",
  prompt: "fix it",
  ...s,
});

describe("attentionChanges", () => {
  it("names a session that came to need approval or finished its turn", () => {
    const before = [summary({}), summary({ task_id: "t2" }), summary({ task_id: "t3", status: "waiting", waiting_on: "approval" })];
    const after = [
      summary({ status: "waiting", waiting_on: "approval" }),
      summary({ task_id: "t2", status: "waiting", waiting_on: "input" }),
      summary({ task_id: "t3", status: "waiting", waiting_on: "approval" }),
      summary({ task_id: "t4", status: "waiting", waiting_on: "approval" }),
    ];
    expect(attentionChanges(before, after)).toEqual([
      { taskId: "t1", title: "Fix the build", body: "Needs your approval" },
      { taskId: "t2", title: "Fix the build", body: "Finished" },
    ]);
  });

  it("is quiet while nothing changes, and for a session that was waiting on input", () => {
    const idle = summary({ status: "waiting", waiting_on: "input" });
    expect(attentionChanges([idle], [idle])).toEqual([]);
    expect(attentionChanges([idle], [summary({ status: "ended" })])).toEqual([]);
  });
});

describe("the app inside the desktop app", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    delete window.noetaDesktop;
    delete window.NOETA_CONFIG;
  });

  it("starts a new session in the chosen project folder and follows the menu", async () => {
    const posts: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST" && url === "/tasks") {
          posts.push(JSON.parse(String(init.body)));
          return new Response(JSON.stringify({ task_id: "t9" }), { status: 201 });
        }
        if (url.includes("/watch")) return new Response("event: end\ndata: {}\n\n");
        return new Response("[]");
      }),
    );
    let command: (c: DesktopCommand) => void = () => undefined;
    const bridge: DesktopBridge = {
      platform: "linux",
      project: vi.fn(async () => "/home/me/code"),
      chooseProject: vi.fn(async () => "/home/me/other"),
      notify: vi.fn(),
      onCommand: (cb) => {
        command = cb;
        return () => undefined;
      },
    };
    window.noetaDesktop = bridge;
    window.NOETA_CONFIG = { server: "", token: "desk" };
    render(<App />);
    const pick = await screen.findByRole("button", { name: "/home/me/code" });
    await userEvent.click(pick);
    expect(await screen.findByRole("button", { name: "/home/me/other" })).toBeInTheDocument();
    act(() => command({ name: "project", path: "/srv/repo" }));
    expect(screen.getByRole("button", { name: "/srv/repo" })).toBeInTheDocument();

    const box = screen.getByLabelText("Message");
    await userEvent.type(box, "hello{Enter}");
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ input: "hello", config: { cwd: "/srv/repo" } });

    act(() => command({ name: "new-session" }));
    expect(await screen.findByText("What should we work on?")).toBeInTheDocument();
  });

  it("shows no folder picker in a browser", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("[]")));
    window.NOETA_CONFIG = { server: "", token: "web" };
    render(<App />);
    expect(await screen.findByText(/runs in the server's directory/)).toBeInTheDocument();
    expect(screen.queryByTitle("Choose the project folder")).toBeNull();
  });
});
