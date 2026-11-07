import type { Recorded } from "../api/types";

/** The open task's own events as the page folds them, handed on to plugins'
 * web parts (`ctx.client.watch`) and kept per plugin and kind (`ctx.records`). */
export class TaskEvents {
  private listeners = new Set<(r: Recorded) => void>();
  private kept = new Map<string, unknown[]>();
  private last = 0;

  constructor(readonly taskId: string) {}

  emit(rec: Recorded): void {
    if (rec.seq <= this.last) return; // a stream picked up again
    this.last = rec.seq;
    const ev = rec.event;
    if (ev.type === "ExtensionRecorded") {
      const key = `${ev.extension}/${ev.kind}`;
      this.kept.set(key, [...(this.kept.get(key) ?? []), ev.data ?? null]);
    }
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
    return [...(this.kept.get(`${plugin}/${kind}`) ?? [])];
  }
}
