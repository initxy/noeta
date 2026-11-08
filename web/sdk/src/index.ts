// @initxy/noeta-web-sdk: what a plugin's web part is given (`activate(ctx)`)
// and the places it can put things. The page serves this module itself
// (its import map), so a plugin marks it external and gets the page's own.
import type { ComponentType, ReactNode } from "react";

export { Slot, SlotRegistry, SlotsContext, useSlotCount, isHostSlot } from "./slots";
export type { Contribution, SlotProblem } from "./slots";
export { useAllRecords, useRecords } from "./hooks";

/** The `ui_api` this SDK describes: a plugin's manifest names it. */
export const UI_API = 1;

/** How long `ctx.slots.wait` waits by default. */
export const WAIT_TIMEOUT_MS = 5000;

/** The modules the page provides; a plugin's build marks them external. */
export const SHARED_MODULES = [
  "react",
  "react-dom",
  "react-dom/client",
  "react/jsx-runtime",
  "react/jsx-dev-runtime",
  "@initxy/noeta-web-sdk",
  "@initxy/noeta-ui",
] as const;

/** The page's slots with a fixed name. */
export const SLOTS = {
  /** The line under the composer: the model picker, then short items
   * (`StatusbarSlotProps`); a negative `order` puts one before the picker. */
  statusbar: "statusbar",
  /** The column beside the conversation: panels. */
  sidebar: "sidebar",
  /** The plugins dialog's Settings tab: sections. */
  settings: "settings",
  /** The session's header: its own content is the session's title
   * (`SessionHeaderSlotProps`); added ones come after it. */
  sessionHeader: "session.header",
  /** The composer; `wrap` it, add beside it, or (`single`) replace it. */
  composer: "composer",
  /** Rendered once, hidden: stylesheets and CSS variables. */
  theme: "theme",
} as const;

/** A tool call's card body (`tool:Bash`). */
export const toolSlot = (tool: string): string => `tool:${tool}`;
/** Where a plugin's record of `kind` is in the conversation (`record:my-plugin/note`). */
export const recordSlot = (plugin: string, kind: string): string => `record:${plugin}/${kind}`;
/** What an approval or question card about a call of `tool` shows above its
 * choices (`request:Bash`); a `single` contribution replaces the card's own
 * lead (who asks). */
export const requestSlot = (tool: string): string => `request:${tool}`;
/** A fenced code block of `language` in rendered markdown (`codeblock:mermaid`). */
export const codeblockSlot = (language: string): string => `codeblock:${language}`;

/** How a slot shows its contributions: `single` replaces the slot's own
 * content (the highest `order` wins, then the latest), `list` adds beside
 * it (after it; before it with a negative `order`), `keyed` adds one per
 * `key` (a later one replaces an earlier) as `list` does, `wrap` wraps the
 * whole (lowest `order` outermost). */
export type Mount = "single" | "list" | "keyed" | "wrap";

export interface SlotOptions {
  key?: string;
  order?: number;
  /** Default `list`. */
  mount?: Mount;
}

/** A tool call as `tool:<Tool>` contributions get it. */
export interface ToolCall {
  id: string;
  name: string;
  input: Record<string, unknown>;
  status: "pending" | "running" | "waiting" | "done" | "error";
  result?: string;
  meta?: Record<string, unknown>;
  /** What the running call streamed last (a command's output so far). */
  progress?: string;
  /** The tasks the call started (sub-agents), each with how it ended once it has. */
  children?: { id: string; status?: string }[];
}

/** The props each slot passes; `wrap` contributions also get `children`. */
export interface ToolSlotProps {
  call: ToolCall;
  /** Whether the person opened the card (clicked its head). A card whose
   * contributions show nothing while it is closed is drawn as one line. */
  open: boolean;
}
/** An approval or a question the task (or one of its sub-agents) waits on. */
export interface PendingRequest {
  kind: "approval" | "question";
  /** The task that asks: the open one or one of its sub-agents. */
  taskId: string;
  tool: string;
  input: Record<string, unknown>;
  title: string;
  prompt: string;
  /** The agent that asks (`main`: the open task's own). */
  agent: string;
}
export interface RequestSlotProps {
  request: PendingRequest;
}
/** The `statusbar` slot's props: how full the task's context is (the last
 * model call's tokens, and the model's window, 0 when not known). */
export interface StatusbarSlotProps {
  context: { tokens: number; window: number };
}
/** The `session.header` slot's props: the session's title as the server
 * lists it (what the slot's own content shows). */
export interface SessionHeaderSlotProps {
  title: string;
}
export interface RecordSlotProps {
  record: { seq: number; plugin: string; kind: string; data: unknown };
}
export interface CodeBlockSlotProps {
  code: string;
  language: string;
}
export interface WrapProps {
  children?: ReactNode;
}

export interface Slots {
  /** Show `Component` in `slot` until the returned remover is called or
   * the plugin is switched off. */
  add<P extends object = Record<string, never>>(slot: string, options: SlotOptions, Component: ComponentType<P & WrapProps>): () => void;
  /** Resolves once `slot` is on the page (the page's own slots always
   * are; another plugin's once it renders a `<Slot>` of that name);
   * rejects after `timeoutMs`, and the plugins page names the wait. */
  wait(slot: string, timeoutMs?: number): Promise<void>;
}

/** One recorded event of the task's log (noeta's `Recorded`). */
export interface Recorded {
  seq: number;
  at: number;
  event: { type: string; [field: string]: unknown };
}

/** A person's message: text, or text and base64 images. */
export type Input =
  | string
  | { role: "user"; content: ({ type: "text"; text: string } | { type: "image"; media_type: string; data: string })[] };

/** The task the page shows, as the plugin may use it. */
export interface TaskClient {
  readonly taskId: string;
  /** Its recorded events after seq `after`. */
  read(after?: number): Promise<Recorded[]>;
  /** Called with each event recorded from now on; returns the unsubscriber. */
  watch(on: (r: Recorded) => void): () => void;
  send(input: Input, when?: "now" | "next_turn"): Promise<{ inbox_seq?: number; output?: string }>;
  answer(requestId: string, decision: "allow" | "deny" | null, options?: { always?: boolean; data?: unknown }): Promise<unknown>;
  signal(name: string, data?: unknown, wake?: boolean): Promise<unknown>;
}

export interface Theme {
  readonly mode: "light" | "dark";
  onChange(listener: (mode: "light" | "dark") => void): () => void;
}

/** The desktop app's bridge; `ctx.desktop` is `null` in a browser. */
export interface DesktopBridge {
  platform: string;
  project(): Promise<string>;
  chooseProject(): Promise<string | null>;
  notify(n: { title: string; body: string; taskId?: string }): void;
}

/** One of the plugin's records. */
export interface PluginRecord {
  kind: string;
  data: unknown;
}

/** What `activate` is given, for the task the page shows. */
export interface PluginContext {
  client: TaskClient;
  /** The data of this plugin's records of `kind` in the task's log (after
   * those of the conversation it continues, if any), oldest first. */
  records(kind: string): unknown[];
  /** This plugin's records of every kind, in the same order. */
  allRecords(): PluginRecord[];
  /** This plugin's backend `ui_call(name, args)` for the task. */
  call<T = unknown>(name: string, args?: unknown): Promise<T>;
  slots: Slots;
  theme: Theme;
  desktop: DesktopBridge | null;
  /** The page's view of another task's conversation, followed live (a
   * sub-agent's: `<ctx.Transcript taskId={child.id} />`). */
  Transcript: ComponentType<{ taskId: string }>;
}

/** A web part's module. `activate` may be async (the part is active once
 * its promise resolves; a rejection is shown on the plugins page). A
 * returned (or resolved) function is called when the plugin is switched off
 * for the task, its web part is rebuilt, or the page leaves the task; then
 * what it added through `ctx.slots` is withdrawn and what it subscribed to
 * through `ctx` (`client.watch`, `theme.onChange`) is unsubscribed, and that
 * `ctx` adds nothing more. */
export interface PluginModule {
  activate(ctx: PluginContext): void | (() => void) | Promise<void | (() => void)>;
}
