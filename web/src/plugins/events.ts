import type { PluginRecord } from "@initxy/noeta-web-sdk";
import type { Recorded } from "../api/types";

/** The open task's own events as the page folds them, handed on to plugins'
 * web parts (`ctx.client.watch`) and kept per plugin (`ctx.records`), the
 * records of the conversation it continues first. */
export class TaskEvents {
  private listeners = new Set<(r: Recorded) => void>();
  private kept = new Map<string, PluginRecord[]>();
  private log: Recorded[] = [];
  private last = 0;

  constructor(readonly taskId: string) {}

  /** A record of the conversation the task continues: kept, not handed on. */
  keep(rec: Recorded): void {
    const ev = rec.event;
    if (ev.type !== "ExtensionRecorded") return;
    const kept = this.kept.get(ev.extension);
    const record = { kind: ev.kind, data: ev.data ?? null };
    if (kept) kept.push(record);
    else this.kept.set(ev.extension, [record]);
  }

  emit(rec: Recorded): void {
    if (rec.seq <= this.last) return; // a stream picked up again
    this.last = rec.seq;
    this.log.push(rec);
    this.keep(rec);
    for (const l of [...this.listeners]) {
      try {
        l(rec);
      } catch (e) {
        console.error("a plugin's watcher failed", e);
      }
    }
  }

  /** The task's events after `after`: those the page holds, then any the
   * server recorded since (`fetch`), so a plugin reading the log does not
   * download again what the page just did. */
  async read(after: number, fetch: (after: number) => Promise<Recorded[]>): Promise<Recorded[]> {
    if (!this.log.length || this.log[0].seq > after + 1) return fetch(after);
    const held = this.log.filter((r) => r.seq > after);
    const newer = await fetch(Math.max(after, this.last));
    return [...held, ...newer.filter((r) => r.seq > this.last)];
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
