// Hooks for a web part's components: the plugin's records, read again each
// time the task records something.
import { useEffect, useReducer } from "react";
import type { PluginContext, PluginRecord } from "./index";

function useRecorded(ctx: PluginContext): void {
  const [, changed] = useReducer((n: number) => n + 1, 0);
  useEffect(() => ctx.client.watch((r) => r.event.type === "ExtensionRecorded" && changed()), [ctx]);
}

/** `ctx.records(kind)`, kept current. */
export function useRecords(ctx: PluginContext, kind: string): unknown[] {
  useRecorded(ctx);
  return ctx.records(kind);
}

/** `ctx.allRecords()`, kept current. */
export function useAllRecords(ctx: PluginContext): PluginRecord[] {
  useRecorded(ctx);
  return ctx.allRecords();
}
