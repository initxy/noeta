// Plugins' web parts for one task: each loaded (an ES module the server
// serves, or a built-in one of web/src/plugins/), activated with its ctx,
// and disposed (its contributions withdrawn) when the plugin is switched off
// for the task or the page leaves the task.
import type { PluginContext, PluginModule, SlotRegistry } from "@noeta-ai/web-sdk";

/** A plugin's web part as the page loads it. */
export interface WebPart {
  name: string;
  load: () => Promise<PluginModule>;
}

export type PartStatus = { state: "loading" } | { state: "active" } | { state: "failed"; error: string };

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e));

export class PluginHost {
  private active = new Map<string, (() => void) | undefined>();
  private wanted = new Set<string>();
  private states = new Map<string, PartStatus>();
  private closed = false;

  constructor(
    private readonly registry: SlotRegistry,
    private readonly context: (plugin: string) => PluginContext,
    private readonly onChange: () => void = () => undefined,
  ) {}

  /** How each plugin's web part is doing. */
  status(name: string): PartStatus | undefined {
    return this.states.get(name);
  }

  /** Runs exactly `parts`: disposes the ones not among them, activates the new ones. */
  async sync(parts: WebPart[]): Promise<void> {
    const names = new Set(parts.map((p) => p.name));
    for (const name of [...this.wanted]) if (!names.has(name)) this.dispose(name);
    await Promise.all(parts.filter((p) => !this.wanted.has(p.name)).map((p) => this.activate(p)));
  }

  /** Disposes every part: the page left the task. */
  close(): void {
    for (const name of [...this.wanted]) this.dispose(name);
    this.closed = true;
  }

  private set(name: string, status: PartStatus | null): void {
    if (status) this.states.set(name, status);
    else this.states.delete(name);
    this.onChange();
  }

  private async activate(part: WebPart): Promise<void> {
    if (this.closed) return;
    this.wanted.add(part.name);
    this.set(part.name, { state: "loading" });
    let mod: PluginModule;
    try {
      mod = await part.load();
      if (typeof mod?.activate !== "function") throw new Error("its web module exports no activate(ctx)");
    } catch (e) {
      if (this.wanted.has(part.name)) this.set(part.name, { state: "failed", error: `could not load: ${message(e)}` });
      return;
    }
    if (!this.wanted.has(part.name) || this.active.has(part.name)) return; // switched off meanwhile
    try {
      const done = mod.activate(this.context(part.name));
      this.active.set(part.name, typeof done === "function" ? done : undefined);
      this.set(part.name, { state: "active" });
    } catch (e) {
      this.registry.withdraw(part.name);
      this.set(part.name, { state: "failed", error: `activate failed: ${message(e)}` });
    }
  }

  private dispose(name: string): void {
    const done = this.active.get(name);
    this.active.delete(name);
    this.wanted.delete(name);
    try {
      done?.();
    } catch (e) {
      console.error(`plugin ${name}: its disposer failed`, e);
    }
    this.registry.withdraw(name);
    this.set(name, null);
  }
}
