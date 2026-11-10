import { beforeEach, describe, expect, it, vi } from "vitest";

const exposed: Record<string, unknown> = {};
const ipc = {
  sendSync: vi.fn(() => ({ server: "", token: "fresh" })),
  invoke: vi.fn(async (channel: string) => (channel === "noeta:project" ? "/home/me/code" : null)),
  send: vi.fn(),
  on: vi.fn(),
  removeListener: vi.fn(),
};

vi.mock("electron", () => ({
  contextBridge: { exposeInMainWorld: (key: string, value: unknown) => (exposed[key] = value) },
  ipcRenderer: ipc,
}));

interface Bridge {
  project(): Promise<string>;
  chooseProject(): Promise<string | null>;
  setProject(path: string): void;
  notify(n: { title: string; body: string; taskId?: string }): void;
  onCommand(cb: (c: unknown) => void): () => void;
}

describe("preload", () => {
  beforeEach(async () => {
    vi.resetModules();
    await import("../src/preload");
  });

  it("injects the server and token as window.NOETA_CONFIG", () => {
    expect(ipc.sendSync).toHaveBeenCalledWith("noeta:config");
    expect(exposed.NOETA_CONFIG).toEqual({ server: "", token: "fresh" });
  });

  it("exposes the desktop hooks over IPC only", async () => {
    const desk = exposed.noetaDesktop as Bridge;
    expect(await desk.project()).toBe("/home/me/code");
    await desk.chooseProject();
    expect(ipc.invoke).toHaveBeenCalledWith("noeta:choose-project");
    desk.setProject("/srv/repo");
    expect(ipc.send).toHaveBeenCalledWith("noeta:set-project", "/srv/repo");
    desk.notify({ title: "t", body: "b", taskId: "x" });
    expect(ipc.send).toHaveBeenCalledWith("noeta:notify", { title: "t", body: "b", taskId: "x" });
  });

  it("hands menu commands to the page without the IPC event, and unsubscribes", () => {
    const desk = exposed.noetaDesktop as Bridge;
    const got: unknown[] = [];
    const off = desk.onCommand((c) => got.push(c));
    const [channel, listener] = ipc.on.mock.calls.at(-1) as unknown as [string, (e: unknown, c: unknown) => void];
    expect(channel).toBe("noeta:command");
    listener({ sender: "secret" }, { name: "new-session" });
    expect(got).toEqual([{ name: "new-session" }]);
    off();
    expect(ipc.removeListener).toHaveBeenCalledWith("noeta:command", listener);
  });
});
