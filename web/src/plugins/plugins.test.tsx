import { SlotRegistry, WAIT_TIMEOUT_MS, type PluginContext } from "@noeta-ai/web-sdk";
import { act, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Api } from "../api/client";
import type { PluginInfo, Recorded } from "../api/types";
import { pluginContext, ThemeSource } from "./context";
import { TaskEvents } from "./events";
import { PluginHost } from "./host";
import { provideSharedModules } from "./shared";
import { uiApiProblem, useUiItems, webParts } from "./usePlugins";

afterEach(() => vi.useRealTimers());

const info = (p: Partial<PluginInfo>): PluginInfo => ({
  name: "p",
  version: "1",
  description: "",
  source: "/x",
  builtin: false,
  enabled: true,
  switchable: true,
  broken: null,
  parts: ["backend"],
  ui_api: null,
  web: null,
  ...p,
});

const flush = () => new Promise((r) => setTimeout(r, 0));

function hostWith(registry = new SlotRegistry()) {
  const contexts: string[] = [];
  const host = new PluginHost(registry, (plugin) => {
    contexts.push(plugin);
    return { slots: { add: (slot: string, o: object, C: () => null) => registry.add(plugin, slot, o, C), wait: (s: string, t = WAIT_TIMEOUT_MS) => registry.wait(plugin, s, t) } } as unknown as PluginContext;
  });
  return { host, registry, contexts };
}

describe("PluginHost", () => {
  it("activates new parts, disposes the ones switched off and withdraws what they added", async () => {
    const { host, registry } = hostWith();
    const disposed = vi.fn();
    const a = { name: "a", load: async () => ({ activate: (ctx: PluginContext) => (ctx.slots.add("sidebar", {}, () => null), disposed) }) };
    const b = { name: "b", load: async () => ({ activate: (ctx: PluginContext) => void ctx.slots.add("statusbar", {}, () => null) }) };
    await host.sync([a, b]);
    expect(host.status("a")).toEqual({ state: "active" });
    expect(registry.contributions("sidebar")).toHaveLength(1);
    await host.sync([b]);
    expect(disposed).toHaveBeenCalledOnce();
    expect(host.status("a")).toBeUndefined();
    expect(registry.contributions("sidebar")).toHaveLength(0);
    expect(registry.contributions("statusbar")).toHaveLength(1);
    host.close();
    expect(registry.contributions("statusbar")).toHaveLength(0);
    await host.sync([a]); // closed: nothing starts again
    expect(registry.contributions("sidebar")).toHaveLength(0);
  });

  it("names a part that fails to load or to activate, and keeps what it added out", async () => {
    const { host, registry } = hostWith();
    await host.sync([
      { name: "missing", load: () => Promise.reject(new Error("404")) },
      { name: "empty", load: async () => ({}) as never },
      {
        name: "throws",
        load: async () => ({
          activate: (ctx: PluginContext) => {
            ctx.slots.add("sidebar", {}, () => null);
            throw new Error("bad ctx");
          },
        }),
      },
    ]);
    expect(host.status("missing")).toEqual({ state: "failed", error: "could not load: 404" });
    expect(host.status("empty")).toEqual({ state: "failed", error: "could not load: its web module exports no activate(ctx)" });
    expect(host.status("throws")).toEqual({ state: "failed", error: "activate failed: bad ctx" });
    expect(registry.contributions("sidebar")).toHaveLength(0);
  });

  it("does not activate a part switched off while it loaded", async () => {
    const { host } = hostWith();
    let release: (m: unknown) => void = () => undefined;
    const activate = vi.fn();
    const slow = { name: "slow", load: () => new Promise<never>((r) => (release = r as never)) };
    const loading = host.sync([slow]);
    await flush();
    await host.sync([]);
    release({ activate });
    await loading;
    expect(activate).not.toHaveBeenCalled();
    expect(host.status("slow")).toBeUndefined();
  });

  it("a disposer that throws still withdraws the plugin", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const { host, registry } = hostWith();
    await host.sync([
      {
        name: "a",
        load: async () => ({
          activate: (ctx: PluginContext) => {
            ctx.slots.add("sidebar", {}, () => null);
            return () => {
              throw new Error("no");
            };
          },
        }),
      },
    ]);
    host.close();
    expect(registry.contributions("sidebar")).toHaveLength(0);
  });

  it("awaits an async activate: active once it resolves, its resolved disposer kept, its rejection shown", async () => {
    const { host, registry } = hostWith();
    const disposed = vi.fn();
    let finish: () => void = () => undefined;
    const slow = {
      name: "slow",
      load: async () => ({
        activate: async (ctx: PluginContext) => {
          await new Promise<void>((r) => (finish = r));
          ctx.slots.add("sidebar", {}, () => null);
          return disposed;
        },
      }),
    };
    const rejects = {
      name: "rejects",
      load: async () => ({
        activate: async (ctx: PluginContext) => {
          ctx.slots.add("statusbar", {}, () => null);
          await flush();
          throw new Error("no server");
        },
      }),
    };
    const synced = host.sync([slow, rejects]);
    await flush();
    await flush();
    expect(host.status("slow")).toEqual({ state: "loading" });
    finish();
    await synced;
    expect(host.status("slow")).toEqual({ state: "active" });
    expect(host.status("rejects")).toEqual({ state: "failed", error: "activate failed: no server" });
    expect(registry.contributions("statusbar")).toHaveLength(0);
    await host.sync([]);
    expect(disposed).toHaveBeenCalledOnce();
    expect(registry.contributions("sidebar")).toHaveLength(0);
  });

  it("an async activate still running when its part is switched off adds nothing and is disposed once done", async () => {
    const { host, registry } = hostWith();
    const disposed = vi.fn();
    let finish: () => void = () => undefined;
    const part = {
      name: "late",
      load: async () => ({
        activate: async (ctx: PluginContext) => {
          await new Promise<void>((r) => (finish = r));
          ctx.slots.add("sidebar", {}, () => null);
          return disposed;
        },
      }),
    };
    const synced = host.sync([part]);
    await flush();
    await host.sync([]);
    finish();
    await synced;
    expect(registry.contributions("sidebar")).toHaveLength(0);
    expect(disposed).toHaveBeenCalledOnce();
    expect(host.status("late")).toBeUndefined();
  });

  it("unsubscribes what a part subscribed to through its ctx, and refuses its ctx after, so off then on never doubles", async () => {
    const registry = new SlotRegistry();
    const events = new TaskEvents("t1");
    const theme = new ThemeSource("light");
    const host = new PluginHost(registry, (plugin) => pluginContext(plugin, { api: {} as Api, events, registry, theme }));
    const seen: string[] = [];
    let kept: PluginContext | null = null;
    const part = {
      name: "p",
      source: "/plugins/p/web/index.js?v=1",
      load: async () => ({
        activate: (ctx: PluginContext) => {
          kept = ctx;
          ctx.client.watch((r) => seen.push(`event ${r.seq}`));
          ctx.theme.onChange((mode) => seen.push(mode));
          ctx.slots.add("sidebar", {}, () => null);
        },
      }),
    };
    await host.sync([part]);
    await host.sync([]);
    await host.sync([part]);
    events.emit({ seq: 1, at: 0, event: { type: "ModelCalled", model: "m" } } as Recorded);
    theme.set("dark");
    expect(seen).toEqual(["event 1", "dark"]);
    expect(registry.contributions("sidebar")).toHaveLength(1);
    const first = kept!;
    await host.sync([]);
    first.slots.add("sidebar", {}, () => null); // a disposed ctx adds nothing
    first.client.watch(() => seen.push("late"));
    events.emit({ seq: 2, at: 0, event: { type: "ModelCalled", model: "m" } } as Recorded);
    await expect(first.slots.wait("statusbar")).rejects.toThrow("disposed");
    expect(registry.contributions("sidebar")).toHaveLength(0);
    expect(seen).toEqual(["event 1", "dark"]);
  });

  it("loads a part again when its web entry changes (a new build), disposing the old", async () => {
    const { host } = hostWith();
    const disposed = vi.fn();
    const loads: string[] = [];
    const build = (v: string) => ({
      name: "p",
      source: `/plugins/p/web/index.js?v=${v}`,
      load: async () => (loads.push(v), { activate: () => disposed }),
    });
    await host.sync([build("1")]);
    await host.sync([build("1")]);
    expect(loads).toEqual(["1"]);
    await host.sync([build("2")]);
    expect(loads).toEqual(["1", "2"]);
    expect(disposed).toHaveBeenCalledOnce();
    expect(host.status("p")).toEqual({ state: "active" });
  });

  it("a wait that times out is reported for the waiting plugin", async () => {
    vi.useFakeTimers();
    const { host, registry } = hostWith();
    let waited: Promise<void> = Promise.resolve();
    await host.sync([{ name: "w", load: async () => ({ activate: (ctx: PluginContext) => void (waited = ctx.slots.wait("other.panel")) }) }]);
    const failed = expect(waited).rejects.toThrow("other.panel");
    vi.advanceTimersByTime(WAIT_TIMEOUT_MS);
    await failed;
    expect(registry.problems("w")[0].kind).toBe("wait-timeout");
  });
});

describe("webParts", () => {
  const load = vi.fn((url: string) => Promise.resolve({ url }));
  const served = info({ name: "panel", web: { entry: "/plugins/panel/web/index.js?v=1" }, ui_api: 1 });

  it("loads the served ones the task runs with and the page can run, and built-in ones by name", async () => {
    const builtin = { activate: () => undefined };
    const parts = webParts(
      [served, info({ name: "off", web: { entry: "/x.js" }, ui_api: 1 }), info({ name: "future", web: { entry: "/y.js" }, ui_api: 9 }), info({ name: "todos", builtin: true }), info({ name: "nothing" })],
      ["off"],
      load,
      { todos: builtin },
    );
    expect(parts.map((p) => p.name)).toEqual(["panel", "todos"]);
    expect(await parts[0].load()).toEqual({ url: "/plugins/panel/web/index.js?v=1" });
    expect(parts[0].source).toBe("/plugins/panel/web/index.js?v=1"); // another build, another source: loaded again
    expect(await parts[1].load()).toBe(builtin);
    expect(webParts([info({ name: "todos", builtin: true })], ["todos"], load, { todos: builtin })).toEqual([]);
  });

  it("names a ui_api the page does not run", () => {
    expect(uiApiProblem(served)).toBeNull();
    expect(uiApiProblem(info({ web: { entry: "/y.js" }, ui_api: 9 }))).toBe("its web part needs ui_api 9; this page runs 1 to 1");
  });
});

describe("ctx", () => {
  it("acts on the current task and hands on its events and the plugin's own records", async () => {
    const api = {
      events: vi.fn().mockResolvedValue([]),
      send: vi.fn().mockResolvedValue({ inbox_seq: 3 }),
      answer: vi.fn().mockResolvedValue({}),
      signal: vi.fn().mockResolvedValue({}),
      pluginCall: vi.fn().mockResolvedValue({ n: 1 }),
    };
    const events = new TaskEvents("t1");
    const theme = new ThemeSource("light");
    const registry = new SlotRegistry();
    const ctx = pluginContext("panel", { api: api as unknown as Api, events, registry, theme });
    expect(ctx.client.taskId).toBe("t1");
    expect(ctx.desktop).toBeNull();
    await ctx.client.send("hi");
    await ctx.client.answer("r1", "allow");
    await ctx.client.signal("rename", { title: "x" });
    expect(await ctx.call("count", { a: 1 })).toEqual({ n: 1 });
    expect(api.send).toHaveBeenCalledWith("t1", "hi", undefined);
    expect(api.answer).toHaveBeenCalledWith("t1", "r1", "allow", undefined);
    expect(api.pluginCall).toHaveBeenCalledWith("t1", "panel", "count", { a: 1 });

    const seen: number[] = [];
    const stop = ctx.client.watch((r) => seen.push(r.seq));
    const rec = (seq: number, extension: string, kind: string, data: unknown): Recorded => ({ seq, at: 0, event: { type: "ExtensionRecorded", extension, kind, data } });
    events.emit(rec(1, "panel", "note", { text: "a" }));
    events.emit(rec(2, "other", "note", { text: "b" }));
    events.emit(rec(2, "panel", "note", { text: "again" })); // a stream picked up again
    stop();
    events.emit(rec(3, "panel", "note", { text: "c" }));
    expect(seen).toEqual([1, 2]);
    expect(ctx.records("note")).toEqual([{ text: "a" }, { text: "c" }]);
    // The conversation the task continues: its records kept, not handed on.
    events.keep(rec(1, "panel", "older", { text: "z" }));
    expect(ctx.allRecords()).toEqual([
      { kind: "note", data: { text: "a" } },
      { kind: "note", data: { text: "c" } },
      { kind: "older", data: { text: "z" } },
    ]);
    expect(seen).toEqual([1, 2]);
    expect(ctx.Transcript).toBeTypeOf("function");

    const modes: string[] = [];
    ctx.theme.onChange((m) => modes.push(m));
    theme.set("dark");
    theme.set("dark");
    expect([ctx.theme.mode, modes]).toEqual(["dark", ["dark"]]);
  });

  it("the shared modules are the page's own", async () => {
    provideSharedModules();
    const shared = globalThis.__noetaShared!;
    expect(Object.keys(shared).sort()).toEqual(["@noeta-ai/ui", "@noeta-ai/web-sdk", "react", "react-dom", "react-dom/client", "react/jsx-runtime"]);
    const sdk = await import("@noeta-ai/web-sdk");
    expect((shared["@noeta-ai/web-sdk"] as typeof sdk).SlotRegistry).toBe(sdk.SlotRegistry);
  });
});

describe("useUiItems", () => {
  it("fetches once the log has settled for a moment, and only the open task's", async () => {
    vi.useFakeTimers();
    const ui = vi.fn().mockResolvedValue([{ plugin: "p", type: "status", key: "k", text: "on", tone: "ok" }]);
    const api = { ui } as unknown as Api;
    const { result, rerender } = renderHook(({ id, seq }) => useUiItems(api, id, seq), { initialProps: { id: "t1" as string | null, seq: 1 } });
    rerender({ id: "t1", seq: 2 });
    rerender({ id: "t1", seq: 3 });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(300);
    });
    expect(ui).toHaveBeenCalledOnce();
    expect(result.current).toHaveLength(1);
    rerender({ id: "t2", seq: 0 });
    expect(result.current).toEqual([]);
  });
});
