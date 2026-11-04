// noeta serve's HTTP API (noeta/host/http.py), the same calls RemoteClient makes.
import type {
  Command,
  Delta,
  Input,
  ModelInfo,
  Pending,
  Recorded,
  RewindPoint,
  TaskSummary,
  TreeUsage,
} from "./types";

/** Where the server is and its bearer token. `server` "" is this page's own origin. */
export interface Connection {
  server: string;
  token: string;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
  }
}

const enc = encodeURIComponent;

function query(params: Record<string, string | number | boolean | undefined | null>): string {
  const kept = Object.entries(params).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return kept.length ? "?" + kept.map(([k, v]) => `${k}=${enc(String(v))}`).join("&") : "";
}

/** One server-sent-events message: its event name and its data. */
export interface SseMessage {
  event: string;
  data: string;
}

/** Splits a text/event-stream body into messages; returns what is left unparsed. */
export function parseSse(buffer: string, emit: (m: SseMessage) => void): string {
  const parts = buffer.split(/\r?\n\r?\n/);
  const rest = parts.pop() ?? "";
  for (const part of parts) {
    let event = "message";
    const data: string[] = [];
    for (const line of part.split(/\r?\n/)) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
    }
    if (data.length) emit({ event, data: data.join("\n") });
  }
  return rest;
}

export class Api {
  constructor(readonly conn: Connection) {}

  private url(path: string): string {
    return this.conn.server.replace(/\/$/, "") + path;
  }

  private headers(json: boolean): HeadersInit {
    const h: Record<string, string> = {};
    if (this.conn.token) h.authorization = `Bearer ${this.conn.token}`;
    if (json) h["content-type"] = "application/json";
    return h;
  }

  async call<T>(method: string, path: string, body?: unknown): Promise<T> {
    const resp = await fetch(this.url(path), {
      method,
      headers: this.headers(body !== undefined),
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    if (!resp.ok) {
      let message = resp.statusText;
      try {
        message = ((await resp.json()) as { error?: string }).error ?? message;
      } catch {
        // not JSON
      }
      throw new ApiError(resp.status, message);
    }
    return (await resp.json()) as T;
  }

  /** The root tasks; `archived`: only the archived ones, or only the others. */
  summaries(limit = 50, archived?: boolean): Promise<TaskSummary[]> {
    return this.call("GET", `/summaries${query({ limit, archived })}`);
  }

  /** Archive a session (lists leave it out), or bring it back. */
  archive(taskId: string, archived = true): Promise<unknown> {
    // The list reads it from the inbox: an idle session is not woken.
    return this.signal(taskId, "archive", { archived }, false);
  }

  /** A new task; `historyFrom` continues that task's conversation. */
  async create(input: Input | null, config: Record<string, unknown>, agent = "main", historyFrom?: string): Promise<string> {
    const body: Record<string, unknown> = { agent, input, config };
    if (historyFrom) body.history_from = historyFrom;
    const got = await this.call<{ task_id: string }>("POST", "/tasks", body);
    return got.task_id;
  }

  /** `{inbox_seq}`, or `{output}` when a local slash command answered. */
  send(taskId: string, input: Input, when: "now" | "next_turn" = "next_turn"): Promise<{ inbox_seq?: number; output?: string }> {
    return this.call("POST", `/tasks/${enc(taskId)}/messages`, { input, when });
  }

  answer(taskId: string, requestId: string, decision: "allow" | "deny" | null, opts: { always?: boolean; data?: unknown } = {}): Promise<unknown> {
    return this.call("POST", `/tasks/${enc(taskId)}/answers`, {
      request_id: requestId,
      decision,
      always: opts.always ?? false,
      data: opts.data ?? null,
    });
  }

  signal(taskId: string, name: string, data?: unknown, wake = true): Promise<unknown> {
    return this.call("POST", `/tasks/${enc(taskId)}/signals`, { name, data: data ?? null, ...(wake ? {} : { wake }) });
  }

  cancel(taskId: string, turnOnly: boolean): Promise<unknown> {
    return this.call("POST", `/tasks/${enc(taskId)}/cancel`, { turn_only: turnOnly });
  }

  events(taskId: string, after = 0): Promise<Recorded[]> {
    return this.call("GET", `/tasks/${enc(taskId)}/events${query({ after })}`);
  }

  pending(taskId: string): Promise<Pending[]> {
    return this.call("GET", `/tasks/${enc(taskId)}/pending`);
  }

  usage(taskId: string): Promise<TreeUsage> {
    return this.call("GET", `/tasks/${enc(taskId)}/usage`);
  }

  rewindPoints(taskId: string): Promise<RewindPoint[]> {
    return this.call("GET", `/tasks/${enc(taskId)}/rewind`);
  }

  rewind(taskId: string, index: number, conversation: boolean, files: boolean): Promise<{ task_id: string; prompt: string; files: string[] }> {
    return this.call("POST", `/tasks/${enc(taskId)}/rewind`, { index, conversation, files });
  }

  commands(taskId: string | null): Promise<Command[]> {
    return this.call("GET", `/commands${query({ task_id: taskId })}`);
  }

  /** A typed slash command run where a task would run it: its text, `local`
   * when it is shown instead of sent; `null`: no such command. */
  runCommand(text: string, taskId: string | null, cwd?: string): Promise<{ text: string; local: boolean } | null> {
    return this.call("POST", "/commands/run", { text, task_id: taskId, cwd: cwd || null });
  }

  models(): Promise<ModelInfo[]> {
    return this.call("GET", "/models");
  }

  files(q: string, taskId: string | null): Promise<string[]> {
    return this.call("GET", `/files${query({ q, task_id: taskId, limit: 12 })}`);
  }

  /** Follows the task's events from `after` until the server ends the
   * stream or `signal` aborts; a dropped connection picks up from the last
   * event seen. Rejects with the server's error when it ends the stream on one. */
  async watch(
    taskId: string,
    after: number,
    on: { event: (r: Recorded) => void; delta: (d: Delta) => void },
    signal: AbortSignal,
  ): Promise<void> {
    let failures = 0;
    while (!signal.aborted) {
      try {
        const resp = await fetch(this.url(`/tasks/${enc(taskId)}/watch${query({ after })}`), {
          headers: this.headers(false),
          signal,
        });
        if (!resp.ok || !resp.body) throw new ApiError(resp.status, resp.statusText);
        failures = 0;
        const reader = resp.body.pipeThrough(new TextDecoderStream()).getReader();
        let buffer = "";
        let ended = false;
        let error = "";
        while (!ended) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer = parseSse(buffer + value, (m) => {
            if (m.event === "event") {
              const rec = JSON.parse(m.data) as Recorded;
              after = rec.seq;
              on.event(rec);
            } else if (m.event === "delta") {
              on.delta(JSON.parse(m.data) as Delta);
            } else if (m.event === "end") {
              ended = true;
              try {
                error = String((JSON.parse(m.data) as { error?: unknown }).error ?? "");
              } catch {
                // an end without a body
              }
            }
          });
        }
        if (error) throw new ApiError(0, error);
        if (ended) return;
      } catch (err) {
        if (signal.aborted) return;
        if (err instanceof ApiError && err.status < 500) throw err;
      }
      failures += 1;
      if (failures > 5) throw new Error(`lost the event stream of ${taskId}`);
      await new Promise((r) => setTimeout(r, Math.min(200 * 2 ** (failures - 1), 5000)));
    }
  }
}

declare global {
  interface Window {
    /** Set by an embedding shell (the desktop app's preload) before the app loads. */
    NOETA_CONFIG?: Partial<Connection>;
  }
}

const SERVER_KEY = "noeta.server";
const TOKEN_KEY = "noeta.token";

function stored(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

/** The connection: the embedding shell's, else one the URL's fragment
 * (`#token=…&server=…`, taken off the address bar) or an earlier visit saved. */
export function initialConnection(): Connection | null {
  const given = window.NOETA_CONFIG;
  if (given?.token !== undefined) return { server: given.server ?? "", token: given.token };
  const hash = new URLSearchParams(window.location.hash.slice(1));
  if (hash.has("token")) {
    const conn = { server: hash.get("server") ?? "", token: hash.get("token") ?? "" };
    saveConnection(conn);
    history.replaceState(null, "", window.location.pathname + window.location.search);
    return conn;
  }
  const token = stored(TOKEN_KEY);
  return token === null ? null : { server: stored(SERVER_KEY) ?? given?.server ?? "", token };
}

/** Forgets the saved server and token (the page asks for them again). */
export function forgetConnection(): void {
  try {
    localStorage.removeItem(SERVER_KEY);
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    // nothing was saved
  }
}

export function saveConnection(conn: Connection): void {
  try {
    localStorage.setItem(SERVER_KEY, conn.server);
    localStorage.setItem(TOKEN_KEY, conn.token);
  } catch {
    // storage blocked: the connection lasts this page only
  }
}
