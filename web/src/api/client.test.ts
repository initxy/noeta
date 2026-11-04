import { afterEach, describe, expect, it, vi } from "vitest";
import { Api, ApiError, forgetConnection, initialConnection, parseSse, type SseMessage } from "./client";

afterEach(() => {
  vi.unstubAllGlobals();
  localStorage.clear();
  delete window.NOETA_CONFIG;
});

describe("parseSse", () => {
  it("splits messages and keeps an unfinished one for the next chunk", () => {
    const got: SseMessage[] = [];
    const rest = parseSse('event: event\ndata: {"seq":1}\n\nevent: delta\ndata: {"kind"', (m) => got.push(m));
    expect(got).toEqual([{ event: "event", data: '{"seq":1}' }]);
    parseSse(rest + ':"text"}\n\n', (m) => got.push(m));
    expect(got[1]).toEqual({ event: "delta", data: '{"kind":"text"}' });
  });
});

describe("Api", () => {
  it("sends the bearer token to the configured server and names the error", async () => {
    const fetch = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ task_id: "t1" }), { status: 201 }));
    vi.stubGlobal("fetch", fetch);
    const api = new Api({ server: "http://host:1/", token: "tok" });
    expect(await api.create("hi", { permission_mode: "plan" })).toBe("t1");
    const [url, init] = fetch.mock.calls[0];
    expect(url).toBe("http://host:1/tasks");
    expect(init.headers.authorization).toBe("Bearer tok");
    expect(JSON.parse(init.body)).toEqual({ agent: "main", input: "hi", config: { permission_mode: "plan" } });

    fetch.mockResolvedValueOnce(new Response(JSON.stringify({ error: "unauthorized" }), { status: 401 }));
    await expect(api.summaries()).rejects.toEqual(new ApiError(401, "unauthorized"));
  });

  it("lists archived sessions apart and archives with a signal", async () => {
    const fetch = vi.fn().mockImplementation(() => Promise.resolve(new Response("[]")));
    vi.stubGlobal("fetch", fetch);
    const api = new Api({ server: "", token: "" });
    await api.summaries(50, true);
    await api.summaries(50, false);
    await api.summaries();
    expect(fetch.mock.calls.map((c) => c[0])).toEqual(["/summaries?limit=50&archived=true", "/summaries?limit=50&archived=false", "/summaries?limit=50"]);
    await api.archive("t 1", false);
    const [url, init] = fetch.mock.calls[3];
    expect(url).toBe("/tasks/t%201/signals");
    expect(JSON.parse(init.body)).toEqual({ name: "archive", data: { archived: false }, wake: false });
  });

  it("watches events and deltas until the server ends the stream", async () => {
    const body = 'event: event\ndata: {"seq":3,"at":0,"event":{"type":"Waiting","on":"input"}}\n\n' + 'event: delta\ndata: {"kind":"text","text":"hi"}\n\nevent: end\ndata: {}\n\n';
    const fetch = vi.fn().mockResolvedValue(new Response(body));
    vi.stubGlobal("fetch", fetch);
    const events: number[] = [];
    const deltas: string[] = [];
    await new Api({ server: "", token: "" }).watch("t1", 0, { event: (r) => events.push(r.seq), delta: (d) => deltas.push(d.text ?? "") }, new AbortController().signal);
    expect(events).toEqual([3]);
    expect(deltas).toEqual(["hi"]);
    expect(fetch.mock.calls[0][0]).toBe("/tasks/t1/watch?after=0");
  });
});

describe("Api.watch reconnecting", () => {
  const event = (seq: number) => `event: event\ndata: {"seq":${seq},"at":0,"event":{"type":"Waiting","on":"input"}}\n\n`;

  it("picks up after the last event seen when the stream drops", async () => {
    const fetch = vi
      .fn()
      .mockResolvedValueOnce(new Response(event(1) + event(2)))
      .mockRejectedValueOnce(new TypeError("network down"))
      .mockResolvedValueOnce(new Response(event(3) + "event: end\ndata: {}\n\n"));
    vi.stubGlobal("fetch", fetch);
    const seen: number[] = [];
    await new Api({ server: "", token: "" }).watch("t1", 0, { event: (r) => seen.push(r.seq), delta: () => undefined }, new AbortController().signal);
    expect(seen).toEqual([1, 2, 3]);
    expect(fetch.mock.calls.map((c) => c[0])).toEqual(["/tasks/t1/watch?after=0", "/tasks/t1/watch?after=2", "/tasks/t1/watch?after=2"]);
  });

  it("counts only failed connections: a stream that answers and drops resets the count", async () => {
    const fetch = vi.fn();
    for (let i = 0; i < 7; i++) fetch.mockResolvedValueOnce(new Response(""));
    fetch.mockResolvedValueOnce(new Response("event: end\ndata: {}\n\n"));
    vi.stubGlobal("fetch", fetch);
    vi.useFakeTimers();
    try {
      const done = new Api({ server: "", token: "" }).watch("t1", 0, { event: () => undefined, delta: () => undefined }, new AbortController().signal);
      await vi.runAllTimersAsync();
      await expect(done).resolves.toBeUndefined();
    } finally {
      vi.useRealTimers();
    }
    expect(fetch).toHaveBeenCalledTimes(8);
  });

  it("rejects with the error the server ended the stream on", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response('event: end\ndata: {"error":"store unreachable"}\n\n')));
    await expect(
      new Api({ server: "", token: "" }).watch("t1", 0, { event: () => undefined, delta: () => undefined }, new AbortController().signal),
    ).rejects.toThrow("store unreachable");
  });
});

describe("initialConnection", () => {
  it("prefers the embedding shell's, then the URL fragment, then what was saved", () => {
    window.NOETA_CONFIG = { server: "http://desk:9", token: "shell" };
    expect(initialConnection()).toEqual({ server: "http://desk:9", token: "shell" });
    delete window.NOETA_CONFIG;
    history.replaceState(null, "", "/#token=frag&server=http://x:1");
    expect(initialConnection()).toEqual({ server: "http://x:1", token: "frag" });
    expect(window.location.hash).toBe("");
    expect(initialConnection()).toEqual({ server: "http://x:1", token: "frag" });
    forgetConnection();
    expect(initialConnection()).toBeNull();
  });
});
