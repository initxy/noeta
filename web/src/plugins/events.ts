import type { PluginRecord } from "@noeta-ai/web-sdk";
import type { Recorded } from "../api/types";

/** The open task's own events as the page folds them, handed on to plugins'
 * web parts (`ctx.client.watch`) and kept per plugin (`ctx.records`), the
 * records of the conversation it continues first. */
export class TaskEvents {
  private listeners = new Set<(r: Recorded) => void>();
  private kept = new Map<string, PluginRecord[]>();
  private last = 0;

  constructor(readonly taskId: string) {}

  /** A record of the conversation the task continues: kept, not handed on. */
  keep(rec: Recorded): void {
    const ev = rec.event;
    if (ev.type !== "ExtensionRecorded") return;
    this.kept.set(ev.extension, [...(this.kept.get(ev.extension) ?? []), { kind: ev.kind, data: ev.data ?? null }]);
  }

  emit(rec: Recorded): void {
    if (rec.seq <= this.last) return; // a stream picked up again
    this.last = rec.seq;
    this.keep(rec);
    for (const l of [...this.listeners]) {
      try {
        l(rec);
      } catch (e) {
        console.error("a plugin's watcher failed", e);
      }
    }
  }

  watch(listener: (r: Recorded) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  records(plugin: string, kind: string): unknown[] {
    return this.all(plugin)
      .filter((r) => r.kind === kind)
      .map((r) => r.data);
  }

  all(plugin: string): PluginRecord[] {
    return [...(this.kept.get(plugin) ?? [])];
  }
}
