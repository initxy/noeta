import { afterEach, describe, expect, it, vi } from "vitest";
import { Api, ApiError, initialConnection, parseSse, type SseMessage } from "./client";

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

describe("initialConnection", () => {
  it("prefers the embedding shell's, then the URL fragment, then what was saved", () => {
    window.NOETA_CONFIG = { server: "http://desk:9", token: "shell" };
    expect(initialConnection()).toEqual({ server: "http://desk:9", token: "shell" });
    delete window.NOETA_CONFIG;
    history.replaceState(null, "", "/#token=frag&server=http://x:1");
    expect(initialConnection()).toEqual({ server: "http://x:1", token: "frag" });
    expect(window.location.hash).toBe("");
    expect(initialConnection()).toEqual({ server: "http://x:1", token: "frag" });
    localStorage.clear();
    expect(initialConnection()).toBeNull();
  });
});
