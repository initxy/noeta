import { createContext, useContext, useEffect, useRef, useState, useSyncExternalStore } from "react";
import type { Api } from "../api/client";
import type { Recorded } from "../api/types";
import { emptyTask, reduce, type Action, type TaskState } from "./transcript";

export const ApiContext = createContext<Api | null>(null);

export function useApi(): Api {
  const api = useContext(ApiContext);
  if (!api) throw new Error("no API connection");
  return api;
}

/** The conversations a task continues (`history_from`, as a rewind or a
 * continued session makes), oldest first, each up to where it was taken; a
 * log that fails to load is `null`, and what it continued is not known. */
export async function earlierLogs(api: Api, first: Recorded | undefined): Promise<[string, Recorded[] | null][]> {
  const out: [string, Recorded[] | null][] = [];
  let ev = first?.event;
  while (ev?.type === "TaskCreated" && ev.history_from && out.length < 16) {
    const from: string = ev.history_from;
    const through = ev.history_through;
    let log: Recorded[];
    try {
      log = (await api.events(from, 0)).filter((r) => through == null || r.seq <= through);
    } catch {
      out.unshift([from, null]);
      break;
    }
    out.unshift([from, log]);
    ev = log[0]?.event;
  }
  return out;
}

const frame = (run: () => void): (() => void) => {
  // A hidden tab runs no animation frames: the timer stands in.
  const timer = setTimeout(run, 100);
  const id = typeof requestAnimationFrame === "function" ? requestAnimationFrame(run) : 0;
  return () => {
    clearTimeout(timer);
    if (id && typeof cancelAnimationFrame === "function") cancelAnimationFrame(id);
  };
};

/** One task's state, held outside React so each part of the page reads only
 * what it shows (`useTaskView`): streamed text re-renders the streaming row,
 * not the page. Live output is folded once per frame, however many chunks came. */
export class TaskStore {
  private state: TaskState = emptyTask();
  private listeners = new Set<() => void>();
  private deltas: Action[] = [];
  private cancelFrame: (() => void) | null = null;

  get = (): TaskState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  /** Folds `a`: a delta at the next frame, anything else now (after the deltas before it). */
  dispatch = (a: Action): void => {
    if (a.type === "delta") {
      this.deltas.push(a);
      this.cancelFrame ??= frame(() => this.flush());
      return;
    }
    this.apply([a]);
  };

  /** Folds every action in order, then tells the page once. */
  apply(actions: Action[]): void {
    const pending = this.takeDeltas();
    let st = this.state;
    for (const a of pending) st = reduce(st, a);
    for (const a of actions) st = reduce(st, a);
    this.set(st);
  }

  private flush(): void {
    this.apply([]);
  }

  private takeDeltas(): Action[] {
    this.cancelFrame?.();
    this.cancelFrame = null;
    const out = this.deltas;
    this.deltas = [];
    return out;
  }

  private set(st: TaskState): void {
    if (st === this.state) return;
    this.state = st;
    for (const l of [...this.listeners]) l();
  }

  /** Back to empty (another task), dropping the output not folded yet. */
  reset(): void {
    this.takeDeltas();
    this.set(emptyTask());
  }
}

/** Whether two values hold the same fields (each compared with `Object.is`). */
export function shallowEqual<T>(a: T, b: T): boolean {
  if (Object.is(a, b)) return true;
  if (typeof a !== "object" || typeof b !== "object" || a === null || b === null) return false;
  const ka = Object.keys(a);
  if (ka.length !== Object.keys(b).length) return false;
  return ka.every((k) => Object.is((a as Record<string, unknown>)[k], (b as Record<string, unknown>)[k]));
}

/** What `select` picks from the task's state, rendering again only when that changes (`equal`). */
export function useTaskView<T>(store: TaskStore, select: (s: TaskState) => T, equal: (a: T, b: T) => boolean = shallowEqual): T {
  const cache = useRef<{ state: TaskState; value: T } | null>(null);
  const read = () => {
    const state = store.get();
    const was = cache.current;
    if (was?.state === state) return was.value;
    const picked = select(state);
    const value = was && equal(was.value, picked) ? was.value : picked;
    cache.current = { state, value };
    return value;
  };
  return useSyncExternalStore(store.subscribe, read);
}

/** A task's state, followed live: the conversations it continues, its own
 * log (all folded before the page draws it), then what it records and
 * streams from then on. `tap` is handed each event as it is folded, `from`
 * naming the earlier conversation it is of. */
export function useTask(api: Api, taskId: string | null, tap?: (rec: Recorded, from?: string) => void): TaskStore {
  const [store] = useState(() => new TaskStore());
  const tapped = useRef(tap);
  tapped.current = tap;
  useEffect(() => {
    store.reset();
    if (!taskId) return;
    const ctl = new AbortController();
    const live = (a: Action) => {
      if (!ctl.signal.aborted) store.dispatch(a);
    };
    const follow = async () => {
      const own = await api.events(taskId, 0);
      const earlier = await earlierLogs(api, own[0]);
      if (ctl.signal.aborted) return;
      const actions: Action[] = [];
      for (const [from, log] of earlier) {
        if (!log) actions.push({ type: "notice", text: "An earlier part of this conversation could not be loaded.", tone: "error" });
        for (const rec of log ?? []) {
          actions.push({ type: "history", rec, from });
          tapped.current?.(rec, from);
        }
      }
      for (const rec of own) {
        actions.push({ type: "event", rec });
        tapped.current?.(rec);
      }
      store.apply(actions);
      const recorded = (rec: Recorded) => {
        if (ctl.signal.aborted) return;
        store.dispatch({ type: "event", rec });
        tapped.current?.(rec);
      };
      const after = own.length ? own[own.length - 1].seq : 0;
      await api.watch(taskId, after, { event: recorded, delta: (delta) => live({ type: "delta", delta }) }, ctl.signal);
    };
    follow().catch((err: unknown) => {
      if (!ctl.signal.aborted) live({ type: "notice", text: `Lost the session's event stream: ${err instanceof Error ? err.message : String(err)}`, tone: "error" });
    });
    return () => ctl.abort();
  }, [api, taskId, store]);
  return store;
}

export type { TaskState };
