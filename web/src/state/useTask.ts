import { createContext, useContext, useEffect, useReducer } from "react";
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
 * continued session makes), oldest first, each up to where it was taken. */
export async function earlierLogs(api: Api, first: Recorded | undefined): Promise<[string, Recorded[]][]> {
  const out: [string, Recorded[]][] = [];
  let ev = first?.event;
  while (ev?.type === "TaskCreated" && ev.history_from && out.length < 16) {
    const from: string = ev.history_from;
    const through = ev.history_through;
    const log = (await api.events(from, 0)).filter((r) => through == null || r.seq <= through);
    out.unshift([from, log]);
    ev = log[0]?.event;
  }
  return out;
}

/** A task's state, followed live: the conversations it continues, its own
 * log, then what it records and streams from then on. */
export function useTask(api: Api, taskId: string | null): [TaskState, (a: Action) => void] {
  const [state, dispatch] = useReducer(reduce, undefined, emptyTask);
  useEffect(() => {
    dispatch({ type: "reset" });
    if (!taskId) return;
    const ctl = new AbortController();
    const live = (a: Action) => {
      if (!ctl.signal.aborted) dispatch(a);
    };
    const follow = async () => {
      const own = await api.events(taskId, 0);
      for (const [from, log] of await earlierLogs(api, own[0])) for (const rec of log) live({ type: "history", rec, from });
      for (const rec of own) live({ type: "event", rec });
      const after = own.length ? own[own.length - 1].seq : 0;
      await api.watch(taskId, after, { event: (rec) => live({ type: "event", rec }), delta: (delta) => live({ type: "delta", delta }) }, ctl.signal);
    };
    follow().catch((err: unknown) => {
      if (!ctl.signal.aborted) live({ type: "notice", text: `Lost the session's event stream: ${err instanceof Error ? err.message : String(err)}`, tone: "error" });
    });
    return () => ctl.abort();
  }, [api, taskId]);
  return [state, dispatch];
}

export type { TaskState };
