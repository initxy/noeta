import { WAIT_TIMEOUT_MS, type PluginContext, type SlotRegistry, type Theme } from "@initxy/noeta-web-sdk";
import type { Api } from "../api/client";
import { TaskTranscript } from "../components/Transcript";
import { desktop } from "../desktop";
import type { TaskEvents } from "./events";

/** The page's light or dark mode, as plugins follow it. */
export class ThemeSource implements Theme {
  private listeners = new Set<(mode: "light" | "dark") => void>();

  constructor(public mode: "light" | "dark") {}

  set(mode: "light" | "dark"): void {
    if (mode === this.mode) return;
    this.mode = mode;
    for (const l of [...this.listeners]) l(mode);
  }

  onChange(listener: (mode: "light" | "dark") => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

/** `plugin`'s ctx (v1) for the task `events` follows. */
export function pluginContext(plugin: string, deps: { api: Api; events: TaskEvents; registry: SlotRegistry; theme: Theme }): PluginContext {
  const { api, events, registry, theme } = deps;
  const taskId = events.taskId;
  return {
    client: {
      taskId,
      read: (after = 0) => events.read(after, (from) => api.events(taskId, from)),
      watch: (on) => events.watch(on),
      send: (input, when) => api.send(taskId, input, when),
      answer: (requestId, decision, options) => api.answer(taskId, requestId, decision, options),
      signal: (name, data, wake) => api.signal(taskId, name, data, wake),
    },
    records: (kind) => events.records(plugin, kind),
    allRecords: () => events.all(plugin),
    call: <T>(name: string, args?: unknown) => api.pluginCall<T>(taskId, plugin, name, args),
    slots: {
      add: (slot, options, Component) => registry.add(plugin, slot, options, Component),
      wait: (slot, timeoutMs = WAIT_TIMEOUT_MS) => registry.wait(plugin, slot, timeoutMs),
    },
    theme,
    desktop: desktop(),
    Transcript: TaskTranscript,
  };
}
