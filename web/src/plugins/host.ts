// Plugins' web parts for one task: each loaded (an ES module the server
// serves, or a built-in one of web/src/plugins/), activated with its ctx,
// and disposed (its contributions withdrawn, what it subscribed to through
// its ctx unsubscribed) when the plugin is switched off for the task, its
// web part changes, or the page leaves the task.
import type { PluginContext, PluginModule, SlotRegistry } from "@initxy/web-sdk";

/** A plugin's web part as the page loads it. */
export interface WebPart {
  name: string;
  /** What is loaded (its entry URL, hash and all): another one is a new
   * build, which replaces the running one. */
  source?: string;
  load: () => Promise<PluginModule>;
}

export type PartStatus = { state: "loading" } | { state: "active" } | { state: "failed"; error: string };

const message = (e: unknown): string => (e instanceof Error ? e.message : String(e));

/** What one activation subscribed to: undone together when it is disposed,
 * and refused after. */
class Scope {
  private undo = new Set<() => void>();
  closed = false;

  /** `remove` undone at close (now, if closed already); the returned function undoes it sooner. */
  track(remove: () => void): () => void {
    if (this.closed) {
      remove();
      return () => undefined;
    }
    let done = false;
    const once = () => {
      if (done) return;
      done = true;
      this.undo.delete(once);
      remove();
    };
    this.undo.add(once);
    return once;
  }

  close(plugin: string): void {
    this.closed = true;
    for (const u of [...this.undo]) {
      try {
        u();
      } catch (e) {
        console.error(`plugin ${plugin}: unsubscribing failed`, e);
      }
    }
  }
}

/** `ctx` as one activation uses it: what it adds and subscribes to is
 * tracked in `scope`, and nothing is added once the scope is closed. */
function scoped(ctx: PluginContext, scope: Scope): PluginContext {
  const { client, slots, theme } = ctx;
  return {
    ...ctx,
    client: {
      get taskId() {
        return client.taskId;
      },
      read: (after) => client.read(after),
      watch: (on) => scope.track(client.watch(on)),
      send: (input, when) => client.send(input, when),
      answer: (requestId, decision, options) => client.answer(requestId, decision, options),
      signal: (name, data, wake) => client.signal(name, data, wake),
    },
    slots: {
      add: (slot, options, Component) => (scope.closed ? () => undefined : scope.track(slots.add(slot, options, Component))),
      wait: (slot, timeoutMs) => (scope.closed ? Promise.reject(new Error("the plugin's web part was disposed")) : slots.wait(slot, timeoutMs)),
    },
    theme: {
      get mode() {
        return theme.mode;
      },
      onChange: (listener) => scope.track(theme.onChange(listener)),
    },
  };
}

/** One activation of a plugin's web part. */
interface Run {
  source: string;
  scope: Scope;
  disposer?: () => void;
}

export class PluginHost {
  private runs = new Map<string, Run>();
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

  /** Runs exactly `parts`: disposes the ones not among them (or whose
   * source changed), activates the new ones. */
  async sync(parts: WebPart[]): Promise<void> {
    const next = new Map(parts.map((p) => [p.name, p.source ?? ""]));
    for (const [name, run] of [...this.runs]) if (next.get(name) !== run.source) this.dispose(name);
    await Promise.all(parts.filter((p) => !this.runs.has(p.name)).map((p) => this.activate(p)));
  }

  /** Disposes every part: the page left the task. */
  close(): void {
    for (const name of [...this.runs.keys()]) this.dispose(name);
    this.closed = true;
  }

  private set(name: string, status: PartStatus | null): void {
    if (status) this.states.set(name, status);
    else this.states.delete(name);
    this.onChange();
  }

  private async activate(part: WebPart): Promise<void> {
    if (this.closed) return;
    const run: Run = { source: part.source ?? "", scope: new Scope() };
    const current = () => this.runs.get(part.name) === run;
    this.runs.set(part.name, run);
    this.set(part.name, { state: "loading" });
    let mod: PluginModule;
    try {
      mod = await part.load();
      if (typeof mod?.activate !== "function") throw new Error("its web module exports no activate(ctx)");
    } catch (e) {
      if (current()) this.set(part.name, { state: "failed", error: `could not load: ${message(e)}` });
      return;
    }
    if (!current()) return; // switched off meanwhile
    let disposer: (() => void) | undefined;
    try {
      // An async activate is done once its promise settles; what it resolves to is its disposer.
      const done: unknown = await mod.activate(scoped(this.context(part.name), run.scope));
      disposer = typeof done === "function" ? (done as () => void) : undefined;
    } catch (e) {
      if (!current()) return;
      run.scope.close(part.name);
      this.registry.withdraw(part.name);
      this.set(part.name, { state: "failed", error: `activate failed: ${message(e)}` });
      return;
    }
    if (!current()) {
      // Disposed while it was activating: undo what it set up.
      this.callDisposer(part.name, disposer);
      return;
    }
    run.disposer = disposer;
    this.set(part.name, { state: "active" });
  }

  private callDisposer(name: string, disposer: (() => void) | undefined): void {
    try {
      disposer?.();
    } catch (e) {
      console.error(`plugin ${name}: its disposer failed`, e);
    }
  }

  private dispose(name: string): void {
    const run = this.runs.get(name);
    this.runs.delete(name);
    if (run) {
      this.callDisposer(name, run.disposer);
      run.scope.close(name);
    }
    this.registry.withdraw(name);
    this.set(name, null);
  }
}
