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
    localStorage.clear();
  });

  it("starts a new session in the chosen project folder and follows the menu", async () => {
    const posts: unknown[] = [];
    const sent: unknown[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === "POST" && url === "/tasks") {
          posts.push(JSON.parse(String(init.body)));
          return new Response(JSON.stringify({ task_id: "t9" }), { status: 201 });
        }
        if (init?.method === "POST" && url === "/tasks/t9/messages") {
          sent.push(JSON.parse(String(init.body)));
          return new Response(JSON.stringify({ inbox_seq: 1 }));
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
      setProject: vi.fn(),
      notify: vi.fn(),
      onCommand: (cb) => {
        command = cb;
        return () => undefined;
      },
    };
    window.noetaDesktop = bridge;
    window.NOETA_CONFIG = { server: "", token: "desk" };
    render(<App />);
    const where = () => document.querySelector(".welcome-project-path")?.textContent;
    await waitFor(() => expect(where()).toBe("/home/me/code"));
    expect(screen.getByRole("button", { name: "code" })).toHaveAttribute("title", "/home/me/code");
    // Add project is the native picker; the main process saves the folder.
    await userEvent.click(screen.getByRole("button", { name: "Add project" }));
    expect(bridge.chooseProject).toHaveBeenCalled();
    await waitFor(() => expect(where()).toBe("/home/me/other"));
    expect(bridge.setProject).toHaveBeenCalledWith("/home/me/other");
    act(() => command({ name: "project", path: "/srv/repo" }));
    expect(where()).toBe("/srv/repo");

    const box = screen.getByLabelText("Message");
    // The first message goes the way every later one does: the task is
    // made empty, then the message is sent (a slash command expands).
    await userEvent.type(box, "/greet the team{Enter}");
    await waitFor(() => expect(sent).toHaveLength(1));
    expect(posts).toEqual([expect.objectContaining({ input: null, config: expect.objectContaining({ cwd: "/srv/repo" }) })]);
    expect(sent[0]).toMatchObject({ input: "/greet the team" });

    act(() => command({ name: "new-session" }));
    expect(await screen.findByText("What should we work on?")).toBeInTheDocument();
  });

  it("answers a local command as the first message without making a session", async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        calls.push(`${init?.method ?? "GET"} ${url}`);
        if (url === "/commands/run") return new Response(JSON.stringify({ text: "in /srv", local: true }));
        return new Response("[]");
      }),
    );
    window.NOETA_CONFIG = { server: "", token: "web" };
    render(<App />);
    const box = await screen.findByLabelText("Message");
    await userEvent.type(box, "/where{Enter}");
    expect(await screen.findByText("in /srv")).toBeInTheDocument();
    await userEvent.type(box, "/rename later{Enter}");
    expect(await screen.findByText("Nothing to rename yet.")).toBeInTheDocument();
    expect(calls.filter((c) => c.startsWith("POST"))).toEqual(["POST /commands/run"]);
  });

  it("forgets the saved token on Disconnect (a browser only)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("[]")));
    localStorage.setItem("noeta.token", "saved");
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "Disconnect" }));
    expect(await screen.findByRole("button", { name: "Connect" })).toBeInTheDocument();
    expect(localStorage.getItem("noeta.token")).toBeNull();
  });

  it("without a project, a new session runs in the server's directory", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response("[]")));
    window.NOETA_CONFIG = { server: "", token: "web" };
    render(<App />);
    expect(await screen.findByText(/runs in the server's directory/)).toBeInTheDocument();
    expect(document.querySelector(".welcome-project")).toBeNull();
  });
});

describe("projects in a browser", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    delete window.NOETA_CONFIG;
    localStorage.clear();
    history.replaceState(null, "", "/");
  });

  const json = (v: unknown, status = 200) => new Response(JSON.stringify(v), { status });

  it("adds a project through the folder picker, remembers it and starts a session in it", async () => {
    const calls: string[] = [];
    const posts: unknown[] = [];
    let saved = [{ path: "/w/app", name: "app", saved: true, sessions: 1 }];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? "GET";
        calls.push(`${method} ${url}`);
        if (url === "/projects" && method === "GET") return json(saved);
        if (url === "/projects" && method === "POST") {
          const { path } = JSON.parse(String(init?.body)) as { path: string };
          if (path === "/etc") return json({ error: "/etc is outside the server's directories" }, 403);
          const p = { path, name: path.split("/").pop() ?? path, saved: true, sessions: 0 };
          saved = [...saved, p];
          return json(p);
        }
        if (url === "/dirs" || url === "/dirs?path=%2Fw") return json({ path: "/w", parent: "/", dirs: [{ name: "app", path: "/w/app" }, { name: "lib", path: "/w/lib" }] });
        if (url === "/dirs?path=%2Fw%2Flib") return json({ path: "/w/lib", parent: "/w", dirs: [] });
        if (url === "/dirs?path=%2Fetc") return json({ path: "/etc", parent: "/", dirs: [] });
        if (url === "/dirs?path=%2F") return json({ path: "/", parent: null, dirs: [{ name: "w", path: "/w" }] });
        if (url.startsWith("/summaries")) return json([summary({ task_id: "t1", cwd: "/w/app", status: "waiting", waiting_on: "input" })]);
        if (url === "/tasks" && method === "POST") {
          posts.push(JSON.parse(String(init?.body)));
          return json({ task_id: "t9" }, 201);
        }
        if (url === "/tasks/t9/messages") return json({ inbox_seq: 1 });
        if (url.includes("/watch")) return new Response("event: end\ndata: {}\n\n");
        return new Response("[]");
      }),
    );
    window.NOETA_CONFIG = { server: "", token: "web" };
    render(<App />);
    const nav = await screen.findByRole("navigation", { name: "Sessions" });
    expect(await screen.findByRole("button", { name: "app" })).toHaveAttribute("title", "/w/app");
    expect(screen.getByText("Fix the build")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Add project" }));
    const dialog = await screen.findByRole("dialog", { name: "Add project" });
    // A folder the server refuses says why, and the dialog stays.
    await userEvent.clear(screen.getByLabelText("Folder path"));
    await userEvent.type(screen.getByLabelText("Folder path"), "/etc{Enter}");
    await userEvent.click(await screen.findByRole("button", { name: "Add etc" }));
    expect(await screen.findByText(/outside the server's directories/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: ".." }));
    await waitFor(() => expect(calls).toContain("GET /dirs?path=%2F"));
    await userEvent.clear(screen.getByLabelText("Folder path"));
    await userEvent.type(screen.getByLabelText("Folder path"), "/w{Enter}");
    await userEvent.click(await screen.findByRole("button", { name: "lib" }));
    await userEvent.click(await screen.findByRole("button", { name: "Add lib" }));
    await waitFor(() => expect(dialog).not.toBeInTheDocument());
    expect(calls).toContain("POST /projects");
    expect(localStorage.getItem("noeta.project")).toBe("/w/lib");
    await waitFor(() => expect(document.querySelector(".welcome-project-path")?.textContent).toBe("/w/lib"));
    expect(nav.querySelector('[data-project="/w/lib"]')).not.toBeNull();

    await userEvent.type(screen.getByLabelText("Message"), "hello{Enter}");
    await waitFor(() => expect(posts).toHaveLength(1));
    expect(posts[0]).toMatchObject({ config: { cwd: "/w/lib" } });
  });

  it("lists projects from the sessions when the server has no /projects, and a saved one is removed", async () => {
    let has = false;
    const calls: string[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url: string, init?: RequestInit) => {
        const method = init?.method ?? "GET";
        calls.push(`${method} ${url}`);
        if (url === "/projects" && method === "GET") return has ? json([{ path: "/w/saved", name: "saved", saved: true, sessions: 0 }]) : json({ error: "not found" }, 404);
        if (url === "/projects" && method === "DELETE") return new Response(null, { status: 204 });
        if (url.startsWith("/summaries")) return json([summary({ cwd: "/w/app" }), summary({ task_id: "t2", cwd: "/w/lib", title: "Lint" })]);
        return new Response("[]");
      }),
    );
    localStorage.setItem("noeta.project", "/w/lib");
    window.NOETA_CONFIG = { server: "", token: "web" };
    const { unmount } = render(<App />);
    expect(await screen.findByRole("button", { name: "app" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "lib" })).toBeInTheDocument();
    // The remembered project is the one a new session goes to.
    await waitFor(() => expect(document.querySelector(".welcome-project-path")?.textContent).toBe("/w/lib"));
    expect(screen.queryByRole("button", { name: /options/ })).toBeNull(); // nothing saved: nothing to remove
    unmount();

    has = true;
    render(<App />);
    await userEvent.click(await screen.findByRole("button", { name: "saved options" }));
    await userEvent.click(screen.getByRole("menuitem", { name: "Remove project" }));
    await waitFor(() => expect(calls).toContain("DELETE /projects"));
  });
});
