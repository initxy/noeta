import { createContext, useContext, useEffect, useReducer } from "react";
import type { Api } from "../api/client";
import { emptyTask, reduce, type Action, type TaskState } from "./transcript";

export const ApiContext = createContext<Api | null>(null);

export function useApi(): Api {
  const api = useContext(ApiContext);
  if (!api) throw new Error("no API connection");
  return api;
}

/** A task's state, followed live: its whole log replayed, then what it
 * records and streams from then on (one watch from seq 0). */
export function useTask(api: Api, taskId: string | null): [TaskState, (a: Action) => void] {
  const [state, dispatch] = useReducer(reduce, undefined, emptyTask);
  useEffect(() => {
    dispatch({ type: "reset" });
    if (!taskId) return;
    const ctl = new AbortController();
    api
      .watch(taskId, 0, { event: (rec) => dispatch({ type: "event", rec }), delta: (delta) => dispatch({ type: "delta", delta }) }, ctl.signal)
      .catch((err: unknown) => dispatch({ type: "notice", text: `Lost the session's event stream: ${String(err)}`, tone: "error" }));
    return () => ctl.abort();
  }, [api, taskId]);
  return [state, dispatch];
}

export type { TaskState };
