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
      /** The sub-agent task an `Agent` call started. */
      child?: string;
      childStatus?: string;
    }
  | { kind: "notice"; id: string; text: string; tone: "info" | "error" }
  | { kind: "compaction"; id: string; summary: string };

export interface Todo {
  id: string;
  subject: string;
  activeForm?: string;
  status: string;
}

export interface Modes {
  modes: string[];
  cycle: string[];
  names: Record<string, string>;
}

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
  ended: string | null;
  title: string;
  cwd: string;
  model: string;
  mode: string;
  modes: Modes | null;
  todos: Todo[];
  contextTokens: number;
  /** Inbox seqs of messages sent but not taken yet (queued), with their text. */
  queued: { seq: number; text: string }[];
}

export const emptyTask = (): TaskState => ({
  items: [],
  live: null,
  called: 0,
  replied: 0,
  lastSeq: 0,
  busy: false,
  waitingOn: null,
  ended: null,
  title: "",
  cwd: "",
  model: "",
  mode: "",
  modes: null,
  todos: [],
  contextTokens: 0,
  queued: [],
});

export type Action =
  | { type: "event"; rec: Recorded }
  | { type: "delta"; delta: Delta }
  | { type: "queued"; seq: number; text: string }
  | { type: "notice"; text: string; tone: "info" | "error" }
  | { type: "reset" };

const text = (blocks: Block[]): string =>
  blocks.map((b) => (b.type === "text" ? b.text : "")).join("");

const fromPerson = (m: Message): boolean =>
  m.role === "user" && (m.origin == null || m.origin === "human");

function updateTool(items: Item[], id: string, patch: Partial<Extract<Item, { kind: "tool" }>>): Item[] {
  return items.map((it) => (it.kind === "tool" && it.id === id ? { ...it, ...patch } : it));
}

function updateChild(items: Item[], child: string, status: string): Item[] {
  return items.map((it) => (it.kind === "tool" && it.child === child ? { ...it, childStatus: status } : it));
}

function record(st: TaskState, seq: number, ev: Extract<Event, { type: "ExtensionRecorded" }>): TaskState {
  const data = (ev.data ?? {}) as Record<string, unknown>;
  const key = `${ev.extension}/${ev.kind}`;
  const notice = (t: string, tone: "info" | "error" = "info"): TaskState => ({
    ...st,
    items: [...st.items, { kind: "notice", id: `n${seq}`, text: t, tone }],
  });
  switch (key) {
    case "title/title":
      return { ...st, title: String(data.title ?? "") };
    case "permissions/mode":
    case "permissions/plan_exit":
      return data.mode ? { ...st, mode: String(data.mode) } : st;
    case "permissions/modes":
      return { ...st, modes: data as unknown as Modes };
    case "todos/created":
      return {
        ...st,
        todos: [
          ...st.todos.filter((t) => t.id !== String(data.id)),
          { id: String(data.id), subject: String(data.subject ?? ""), activeForm: data.active_form as string | undefined, status: "pending" },
        ],
      };
    case "todos/updated": {
      const f = (data.fields ?? {}) as Record<string, unknown>;
      return {
        ...st,
        todos: st.todos.map((t) =>
          t.id === String(data.id)
            ? {
                ...t,
                subject: (f.subject as string) ?? t.subject,
                activeForm: (f.active_form as string) ?? t.activeForm,
                status: (f.status as string) ?? t.status,
              }
            : t,
        ),
      };
    }
    case "todos/deleted":
      return { ...st, todos: st.todos.filter((t) => t.id !== String(data.id)) };
    case "subtasks/launched":
      return { ...st, items: updateTool(st.items, String(data.call_id), { child: String(data.task_id) }) };
    case "core/spawned": {
      let items = st.items;
      for (const c of (data.children ?? []) as { id: string; call_id: string }[]) {
        items = updateTool(items, c.call_id, { child: c.id });
      }
      return { ...st, items };
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
      return st;
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
      };
    case "InputTaken": {
      let items = st.items;
      let busy = st.busy;
      const taken = new Set<number>();
      for (const item of ev.items) {
        taken.add(item.seq);
        const body = item.body;
        if (body.kind === "message" && fromPerson(body.message) && text(body.message.content).trim()) {
          const images = body.message.content.flatMap((b) => (b.type === "image" ? [`data:${b.media_type};base64,${b.data}`] : []));
          items = [...items, { kind: "user", id: `u${seq}.${item.seq}`, text: text(body.message.content), images }];
          busy = true;
        } else if (body.kind === "child_ended") {
          items = updateChild(items, body.child_id, body.status);
        } else if (body.kind === "external" && body.name === "config") {
          const model = (body.data as Record<string, unknown> | null)?.model;
          if (typeof model === "string") st = { ...st, model };
        }
      }
      return { ...st, items, busy, queued: st.queued.filter((q) => !taken.has(q.seq)) };
    }
    case "ModelCalled":
      return { ...st, called: seq, busy: true, model: ev.model || st.model };
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
        }),
      };
    case "Compacted":
      return { ...st, items: [...st.items, { kind: "compaction", id: `c${seq}`, summary: ev.summary }] };
    case "Waiting": {
      let items = st.items;
      if (ev.on === "approval" || ev.on === "question") {
        for (const id of ev.ids ?? []) items = updateTool(items, id, { status: "waiting" });
      }
      return { ...st, items, live: null, waitingOn: ev.on, busy: ev.on !== "input" };
    }
    case "TaskEnded":
      return {
        ...st,
        busy: false,
        live: null,
        waitingOn: null,
        ended: ev.status,
        items: [
          ...st.items,
          { kind: "notice", id: `e${seq}`, text: `TaskSummary ${ev.status}${ev.error ? `: ${ev.error}` : ""}`, tone: ev.status === "failed" ? "error" : "info" },
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

export function reduce(st: TaskState, action: Action): TaskState {
  switch (action.type) {
    case "event":
      return event(st, action.rec);
    case "delta":
      return delta(st, action.delta);
    case "queued":
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
