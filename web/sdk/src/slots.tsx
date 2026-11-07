// The slots of a page: which plugin put what where, and the <Slot> that
// shows it. The page keeps one SlotRegistry and provides it (SlotsContext);
// a plugin's ctx.slots is the same registry, bound to the plugin's name.
import { Component, createContext, useContext, useEffect, useSyncExternalStore, type ComponentType, type ErrorInfo, type ReactNode } from "react";
import type { Mount, SlotOptions } from "./index";

export interface Contribution {
  id: number;
  plugin: string;
  slot: string;
  key?: string;
  order: number;
  mount: Mount;
  Component: ComponentType<Record<string, unknown>>;
}

/** Something a plugin's web part did wrong: a slot it waited on never came,
 * or a contribution threw while rendering. */
export interface SlotProblem {
  plugin: string;
  slot: string;
  kind: "wait-timeout" | "render";
  message: string;
}

const FIXED = new Set(["statusbar", "sidebar", "settings", "session.header", "composer", "theme"]);
const FAMILIES = ["tool:", "request:", "record:", "codeblock:"];

/** Whether `slot` is one of the page's own (always there). */
export function isHostSlot(slot: string): boolean {
  return FIXED.has(slot) || FAMILIES.some((f) => slot.startsWith(f) && slot.length > f.length);
}

interface Waiter {
  plugin: string;
  slot: string;
  resolve: () => void;
  reject: (e: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class SlotRegistry {
  private items: Contribution[] = [];
  private nextId = 1;
  private shown = new Map<string, number>();
  private waiters: Waiter[] = [];
  private listeners = new Set<() => void>();
  private problemList: SlotProblem[] = [];
  /** Changes with every addition, removal and problem (a snapshot for React). */
  version = 0;

  constructor(private readonly hostSlot: (slot: string) => boolean = isHostSlot) {}

  /** `Component` in `slot` for `plugin`; the returned function removes it. */
  // `any`: each slot passes its own props.
  add(plugin: string, slot: string, options: SlotOptions, Component: ComponentType<any>): () => void {
    const mount = options.mount ?? "list";
    const item: Contribution = {
      id: this.nextId++,
      plugin,
      slot,
      key: mount === "keyed" ? (options.key ?? plugin) : options.key,
      order: options.order ?? 0,
      mount,
      Component: Component as ComponentType<Record<string, unknown>>,
    };
    this.items = [...this.items, item];
    this.changed();
    return () => {
      if (!this.items.includes(item)) return;
      this.items = this.items.filter((i) => i !== item);
      this.changed();
    };
  }

  /** What `slot` shows, by `order` then the order added. */
  contributions(slot: string): Contribution[] {
    return this.items.filter((i) => i.slot === slot).sort((a, b) => a.order - b.order || a.id - b.id);
  }

  /** Every contribution of `plugin` removed and its waits dropped (it was switched off). */
  withdraw(plugin: string): void {
    for (const w of this.waiters.filter((w) => w.plugin === plugin)) {
      clearTimeout(w.timer);
      w.reject(new Error(`${plugin} was switched off`));
    }
    this.waiters = this.waiters.filter((w) => w.plugin !== plugin);
    const before = this.items.length;
    this.items = this.items.filter((i) => i.plugin !== plugin);
    const problems = this.problemList.length;
    this.problemList = this.problemList.filter((p) => p.plugin !== plugin);
    if (this.items.length !== before || this.problemList.length !== problems) this.changed();
  }

  /** Whether `slot` is on the page. */
  has(slot: string): boolean {
    return this.hostSlot(slot) || (this.shown.get(slot) ?? 0) > 0;
  }

  /** A <Slot name={slot}> is on the page until the returned function is called. */
  provide(slot: string): () => void {
    this.shown.set(slot, (this.shown.get(slot) ?? 0) + 1);
    const ready = this.waiters.filter((w) => w.slot === slot);
    this.waiters = this.waiters.filter((w) => w.slot !== slot);
    for (const w of ready) {
      clearTimeout(w.timer);
      w.resolve();
    }
    return () => this.shown.set(slot, (this.shown.get(slot) ?? 1) - 1);
  }

  /** Resolves once `slot` is on the page, for `plugin`; after `timeoutMs`
   * rejects and keeps the problem for the plugins page. */
  wait(plugin: string, slot: string, timeoutMs: number): Promise<void> {
    if (this.has(slot)) return Promise.resolve();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.waiters = this.waiters.filter((w) => w !== waiter);
        const message = `waited ${timeoutMs} ms for the slot ${slot}, which never came`;
        this.report({ plugin, slot, kind: "wait-timeout", message });
        reject(new Error(message));
      }, timeoutMs);
      const waiter: Waiter = { plugin, slot, resolve, reject, timer };
      this.waiters.push(waiter);
    });
  }

  report(problem: SlotProblem): void {
    this.problemList = [...this.problemList, problem];
    this.changed();
  }

  problems(plugin?: string): SlotProblem[] {
    return plugin === undefined ? this.problemList : this.problemList.filter((p) => p.plugin === plugin);
  }

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private changed(): void {
    this.version += 1;
    for (const l of [...this.listeners]) l();
  }
}

/** The page's registry; without one a <Slot> shows only its own content. */
export const SlotsContext = createContext<SlotRegistry | null>(null);

const noRegistry = new SlotRegistry(() => true);

function useRegistry(given?: SlotRegistry): SlotRegistry {
  const provided = useContext(SlotsContext);
  const registry = given ?? provided ?? noRegistry;
  useSyncExternalStore(registry.subscribe, () => registry.version);
  return registry;
}

/** How many contributions `slot` has now (in `registry`, else the provided one). */
export function useSlotCount(slot: string, registry?: SlotRegistry): number {
  return useRegistry(registry).contributions(slot).length;
}

interface BoundaryProps {
  plugin: string;
  slot: string;
  registry: SlotRegistry;
  /** Shown instead, after the error line. */
  fallback?: ReactNode;
  children: ReactNode;
}

/** One contribution: an error it throws is shown (naming its plugin) and
 * reported, and the rest of the page goes on. */
class Boundary extends Component<BoundaryProps, { error: string | null }> {
  state = { error: null as string | null };

  static getDerivedStateFromError(e: unknown) {
    return { error: e instanceof Error ? e.message : String(e) };
  }

  componentDidCatch(e: unknown, _info: ErrorInfo) {
    const message = e instanceof Error ? e.message : String(e);
    this.props.registry.report({ plugin: this.props.plugin, slot: this.props.slot, kind: "render", message });
  }

  render() {
    if (this.state.error === null) return this.props.children;
    return (
      <>
        <div className="plugin-error" role="alert">
          {this.props.plugin}: {this.state.error}
        </div>
        {this.props.fallback}
      </>
    );
  }
}

/** A place plugins put things in: `children` is its own content (what a
 * `single` contribution replaces and a `wrap` one wraps); `props` go to
 * every contribution. */
export function Slot({ name, props, children }: { name: string; props?: object; children?: ReactNode }) {
  const registry = useRegistry();
  useEffect(() => registry.provide(name), [registry, name]);
  const all = registry.contributions(name);
  if (!all.length) return <>{children}</>;
  const shown = (c: Contribution, extra: { children?: ReactNode } = {}, fallback?: ReactNode) => (
    <Boundary key={c.id} plugin={c.plugin} slot={name} registry={registry} fallback={fallback}>
      <c.Component {...(props as Record<string, unknown>)} {...extra} />
    </Boundary>
  );
  const single = all.filter((c) => c.mount === "single").sort((a, b) => a.order - b.order || a.id - b.id).pop();
  const keyed = new Map<string, Contribution>();
  for (const c of all) if (c.mount === "keyed") keyed.set(c.key ?? c.plugin, c);
  const added = all.filter((c) => c.mount === "list" || (c.mount === "keyed" && keyed.get(c.key ?? c.plugin) === c));
  let out: ReactNode = (
    <>
      {added.filter((c) => c.order < 0).map((c) => shown(c))}
      {single ? shown(single, { children }, children) : children}
      {added.filter((c) => c.order >= 0).map((c) => shown(c))}
    </>
  );
  for (const c of all.filter((c) => c.mount === "wrap").reverse()) {
    const inner = out;
    out = shown(c, { children: inner }, inner);
  }
  return <>{out}</>;
}
