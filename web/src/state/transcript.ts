// What the page shows of a task, folded from its recorded events and the
// live output streamed beside them. Pure: `reduce(state, action)`.
import type { Block, Delta, Event, Message, Recorded } from "../api/types";

export type ToolStatus = "pending" | "running" | "waiting" | "done" | "error";

export type Item =
  | { kind: "user"; id: string; text: string; images: string[] }
  | { kind: "assistant"; id: string; text: string }
  | { kind: "thinking"; id: string; text: string }
  | {
      kind: "tool";
      id: string;
      name: string;
      input: Record<string, unknown>;
      status: ToolStatus;
      result?: string;
      meta?: Record<string, unknown>;
      progress?: string;
      /** What the person added to their yes; the model reads it beside the result. */
      note?: string;
      /** The tasks the call started (the kernel's `spawned` records), each with how it ended. */
      children?: { id: string; status?: string }[];
    }
  | { kind: "notice"; id: string; text: string; tone: "info" | "error" }
  | { kind: "compaction"; id: string; summary: string }
  /** A plugin's record the page has no view of its own for: the `record:<plugin>/<kind>` slot. */
  | { kind: "record"; id: string; seq: number; plugin: string; recordKind: string; data: unknown };

// What plugins record (the task list, the title, the mode a plugin switched
// to …) is theirs to show (web/src/plugins/<name>/): this fold reads the
// kernel's own events and records only.

export interface TaskState {
  items: Item[];
  /** Text and thinking of the model call streaming now (not yet recorded). */
  live: { callSeq: number; attempt: number; text: string; thinking: string } | null;
  /** The seq of the last `ModelCalled` and of the last call that replied. */
  called: number;
  replied: number;
  lastSeq: number;
  busy: boolean;
  waitingOn: string | null;
  /** The calls (approval) or requests (question) the wait is on. */
  waitingIds: string[];
  ended: string | null;
  cwd: string;
  model: string;
  /** The permission mode the task started in or was last sent (a mode a
   * plugin switched to is the plugin's to show). */
  mode: string;
  contextTokens: number;
  /** Inbox seqs of messages sent but not taken yet (queued), with their text. */
  queued: { seq: number; text: string }[];
  /** The highest inbox seq the task has taken: a send receipt at or below it
   * arrived after the stream showed the message taken. */
  taken: number;
  /** Sub-agents the task started whose end it has not taken yet: whatever
   * started them (an Agent call, a workflow's later agents with no call). */
  running: string[];
  /** The plugins the task runs without (its config's `plugins_off`). */
  pluginsOff: string[];
}

export const emptyTask = (): TaskState => ({
  items: [],
  live: null,
  called: 0,
  replied: 0,
  lastSeq: 0,
  busy: false,
  waitingOn: null,
  waitingIds: [],
  ended: null,
  cwd: "",
  model: "",
  mode: "",
  contextTokens: 0,
  queued: [],
  taken: 0,
  running: [],
  pluginsOff: [],
});

export type Action =
  | { type: "event"; rec: Recorded }
  /** An event of a conversation the task continues (`history_from`), shown before its own. */
  | { type: "history"; rec: Recorded; from: string }
  | { type: "delta"; delta: Delta }
  | { type: "queued"; seq: number; text: string }
  | { type: "notice"; text: string; tone: "info" | "error" }
  | { type: "reset" };

/** The note a declined call's result carries (noeta.core.runner.texts.REJECTED_MARKER):
 * the decline already shows as the call's error. */
const REJECTED_MARKER = "[Request interrupted by user for tool use]\n";

/** The signal that switches a task's permission mode (noeta.toolkit.permission_modes.MODE_SIGNAL). */
export const MODE_SIGNAL = "permission_mode";

const text = (blocks: Block[]): string =>
  blocks.map((b) => (b.type === "text" ? b.text : "")).join("");

const fromPerson = (m: Message): boolean =>
  m.role === "user" && (m.origin == null || m.origin === "human");

/** A typed slash command as the person typed it (`/name args`), else the text. */
export function shownPrompt(t: string): string {
  const m = /^<command-message>[^<]*<\/command-message>\s*<command-name>([^<]*)<\/command-name>\s*(?:<command-args>([\s\S]*?)<\/command-args>)?/.exec(t.trim());
  return m ? [m[1], (m[2] ?? "").trim()].filter(Boolean).join(" ") : t;
}

const names = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : []);

function updateTool(items: Item[], id: string, patch: Partial<Extract<Item, { kind: "tool" }>>): Item[] {
  return items.map((it) => (it.kind === "tool" && it.id === id ? { ...it, ...patch } : it));
}

function addChild(items: Item[], callId: string, child: string): Item[] {
  return items.map((it) =>
    it.kind === "tool" && it.id === callId && !it.children?.some((c) => c.id === child) ? { ...it, children: [...(it.children ?? []), { id: child }] } : it,
  );
}

function updateChild(items: Item[], child: string, status: string): Item[] {
  return items.map((it) =>
    it.kind === "tool" && it.children?.some((c) => c.id === child) ? { ...it, children: it.children.map((c) => (c.id === child ? { ...c, status } : c)) } : it,
  );
}

function started(running: string[], ids: string[]): string[] {
  return [...running, ...ids.filter((id) => !running.includes(id))];
}

function record(st: TaskState, seq: number, ev: Extract<Event, { type: "ExtensionRecorded" }>): TaskState {
  const data = (ev.data ?? {}) as Record<string, unknown>;
  const key = `${ev.extension}/${ev.kind}`;
  const notice = (t: string, tone: "info" | "error" = "info"): TaskState => ({
    ...st,
    items: [...st.items, { kind: "notice", id: `n${seq}`, text: t, tone }],
  });
  switch (key) {
    case "core/spawned": {
      let items = st.items;
      const children = (data.children ?? []) as { id: string; call_id: string }[];
      for (const c of children) {
        if (c.call_id) items = addChild(items, c.call_id, c.id);
      }
      return { ...st, items, running: started(st.running, children.map((c) => c.id)) };
    }
    case "core/retry":
      return { ...notice(`Retrying (attempt ${data.attempt ?? "?"}): ${String(data.error ?? "").slice(0, 160)}`), live: null };
    case "core/turn_stopped": {
      // What streamed before the stop stays, as Claude Code keeps it.
      const partial = typeof data.partial === "string" && data.partial.trim() ? data.partial : "";
      if (partial) st = { ...st, items: [...st.items, { kind: "assistant", id: `p${seq}`, text: partial }] };
      return { ...notice("Interrupted by you"), live: null, busy: false };
    }
    case "core/turn_failed":
      return notice(`Turn failed: ${[data.reason, data.detail].filter(Boolean).join(": ")}`, "error");
    case "core/notice":
      return notice(String(data.text ?? ""));
    default:
      if (ev.extension === "core") return st;
      return { ...st, items: [...st.items, { kind: "record", id: `r${seq}`, seq, plugin: ev.extension, recordKind: ev.kind, data: ev.data }] };
  }
}

function event(st: TaskState, rec: Recorded): TaskState {
  const ev = rec.event;
  const seq = rec.seq;
  st = { ...st, lastSeq: Math.max(st.lastSeq, seq) };
  switch (ev.type) {
    case "TaskCreated":
      return {
        ...st,
        cwd: String(ev.config.cwd ?? ""),
        model: String(ev.config.model ?? st.model),
        mode: String(ev.config.permission_mode ?? st.mode),
        pluginsOff: names(ev.config.plugins_off),
      };
    case "InputTaken": {
      let items = st.items;
      let busy = st.busy;
      const taken = new Set<number>();
      let through = Math.max(st.taken, ev.through ?? 0);
      let answered = false;
      let running = st.running;
      for (const item of ev.items) {
        taken.add(item.seq);
        through = Math.max(through, item.seq);
        const body = item.body;
        if (body.kind === "message" && fromPerson(body.message) && text(body.message.content).trim()) {
          const images = body.message.content.flatMap((b) => (b.type === "image" ? [`data:${b.media_type};base64,${b.data}`] : []));
          items = [...items, { kind: "user", id: `u${seq}.${item.seq}`, text: shownPrompt(text(body.message.content)), images }];
          busy = true;
        } else if (body.kind === "cancel" || (body.kind === "answer" && st.waitingIds.includes(body.request_id))) {
          answered = true;
        } else if (body.kind === "child_ended") {
          items = updateChild(items, body.child_id, body.status);
          running = running.filter((id) => id !== body.child_id);
        } else if (body.kind === "external" && body.name === "config") {
          const config = (body.data ?? {}) as Record<string, unknown>;
          if (typeof config.model === "string") st = { ...st, model: config.model };
          if ("plugins_off" in config) st = { ...st, pluginsOff: names(config.plugins_off) };
        } else if (body.kind === "external" && body.name === MODE_SIGNAL) {
          const mode = (body.data as { mode?: unknown } | null)?.mode;
          if (typeof mode === "string" && mode) st = { ...st, mode };
        }
      }
      // Taking input ends a wait, but an approval or a question stands until
      // it is answered: a message taken meanwhile leaves it waiting.
      const asking = st.waitingOn === "approval" || st.waitingOn === "question";
      const waiting = asking && !answered ? { waitingOn: st.waitingOn, waitingIds: st.waitingIds } : { waitingOn: null, waitingIds: [] };
      return { ...st, items, busy, ...waiting, running, taken: through, queued: st.queued.filter((q) => !taken.has(q.seq)) };
    }
    case "ModelCalled":
      return { ...st, called: seq, busy: true, waitingOn: null, waitingIds: [], model: ev.model || st.model };
    case "ModelReplied": {
      const msg = ev.response.message;
      const items = [...st.items];
      msg.content.forEach((b, i) => {
        const id = `m${seq}.${i}`;
        if (b.type === "thinking" && b.text) items.push({ kind: "thinking", id, text: b.text });
        else if (b.type === "text" && b.text.trim()) items.push({ kind: "assistant", id, text: b.text });
        else if (b.type === "tool_use") items.push({ kind: "tool", id: b.id, name: b.name, input: b.input, status: "pending" });
      });
      const u = ev.response.usage ?? {};
      const context = (u.uncached ?? 0) + (u.cache_read ?? 0) + (u.cache_write ?? 0) + (u.output ?? 0);
      return { ...st, items, live: null, replied: st.called, contextTokens: context || st.contextTokens };
    }
    case "ToolStarted":
      return { ...st, items: updateTool(st.items, ev.call.id, { status: "running" }) };
    case "ToolFinished":
      return {
        ...st,
        items: updateTool(st.items, ev.result.call_id, {
          status: ev.result.is_error ? "error" : "done",
          result: text(ev.result.content),
          meta: ev.meta,
          progress: undefined,
          note: ev.note && ev.note !== REJECTED_MARKER ? ev.note : undefined,
        }),
      };
    case "Compacted":
      return { ...st, items: [...st.items, { kind: "compaction", id: `c${seq}`, summary: ev.summary }] };
    case "Waiting": {
      let items = st.items;
      if (ev.on === "approval" || ev.on === "question") {
        for (const id of ev.ids ?? []) items = updateTool(items, id, { status: "waiting" });
      }
      return { ...st, items, live: null, waitingOn: ev.on, waitingIds: ev.ids ?? [], busy: ev.on !== "input" };
    }
    case "TaskEnded":
      return {
        ...st,
        busy: false,
        live: null,
        waitingOn: null,
        waitingIds: [],
        ended: ev.status,
        items: [
          ...st.items,
          { kind: "notice", id: `e${seq}`, text: `Session ${ev.status}${ev.error ? `: ${ev.error}` : ""}`, tone: ev.status === "failed" ? "error" : "info" },
        ],
      };
    case "ExtensionRecorded":
      return record(st, seq, ev);
    default:
      return st;
  }
}

function delta(st: TaskState, d: Delta): TaskState {
  if (d.kind === "tool") {
    return d.call_id ? { ...st, items: updateTool(st.items, d.call_id, { progress: d.text ?? "" }) } : st;
  }
  if (d.kind !== "text" && d.kind !== "thinking") return st;
  const callSeq = d.call_seq ?? 0;
  const attempt = d.attempt ?? 1;
  if (callSeq <= st.replied) return st; // the recorded reply is already shown
  let live = st.live;
  if (live && (live.callSeq !== callSeq || live.attempt !== attempt)) {
    if (callSeq < live.callSeq || (callSeq === live.callSeq && attempt < live.attempt)) return st;
    live = null; // a new call, or a retry starting over
  }
  live = live ?? { callSeq, attempt, text: "", thinking: "" };
  live = d.kind === "text" ? { ...live, text: live.text + (d.text ?? "") } : { ...live, thinking: live.thinking + (d.text ?? "") };
  return { ...st, live, busy: true };
}

/** An earlier conversation's event: its items and what it set (title, mode,
 * todos …), but not the state of its turn (busy, waiting, ended) nor its
 * seqs, which are another log's. */
function history(st: TaskState, rec: Recorded, from: string): TaskState {
  if (rec.event.type === "TaskEnded" || rec.event.type === "Waiting") return st;
  const next = event(st, rec);
  const added = next.items.slice(st.items.length).map((it) => (it.kind === "tool" ? it : { ...it, id: `${from}:${it.id}` }));
  return {
    ...next,
    items: [...next.items.slice(0, st.items.length), ...added],
    lastSeq: st.lastSeq,
    taken: st.taken,
    called: st.called,
    replied: st.replied,
    live: st.live,
    busy: st.busy,
    waitingOn: st.waitingOn,
    waitingIds: st.waitingIds,
    ended: st.ended,
    queued: st.queued,
    running: st.running,
  };
}

export function reduce(st: TaskState, action: Action): TaskState {
  switch (action.type) {
    case "event":
      return event(st, action.rec);
    case "delta":
      return delta(st, action.delta);
    case "history":
      return history(st, action.rec, action.from);
    case "queued":
      // Taken already (the stream outran the receipt): nothing is queued.
      if (action.seq <= st.taken) return st;
      return { ...st, busy: true, queued: [...st.queued, { seq: action.seq, text: action.text }] };
    case "notice":
      return { ...st, items: [...st.items, { kind: "notice", id: `x${st.items.length}`, text: action.text, tone: action.tone }] };
    case "reset":
      return emptyTask();
  }
}

/** The session's status as the sidebar names it. */
export function sessionStatus(status: string, waitingOn: string | null): "running" | "needs-you" | "idle" | "ended" {
  if (status === "ended") return "ended";
  if (status === "waiting") {
    return waitingOn === "approval" || waitingOn === "question" ? "needs-you" : waitingOn === "input" ? "idle" : "running";
  }
  return "running";
}
