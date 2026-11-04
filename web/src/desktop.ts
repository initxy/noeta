// The desktop app's hooks (desktop/src/preload.ts), present only inside it.
import type { TaskSummary } from "./api/types";
import { sessionStatus } from "./state/transcript";

export type DesktopCommand = { name: "new-session" } | { name: "open-session"; taskId: string } | { name: "project"; path: string };

export interface DesktopBridge {
  platform: string;
  /** The project folder new sessions run in. */
  project(): Promise<string>;
  /** Asks for a folder; null when the dialog was cancelled. */
  chooseProject(): Promise<string | null>;
  /** A system notification, shown only while the window is not focused. */
  notify(n: { title: string; body: string; taskId?: string }): void;
  onCommand(cb: (c: DesktopCommand) => void): () => void;
}

declare global {
  interface Window {
    noetaDesktop?: DesktopBridge;
  }
}

export function desktop(): DesktopBridge | null {
  return window.noetaDesktop ?? null;
}

export interface Attention {
  taskId: string;
  title: string;
  body: string;
}

/** The sessions that just came to need the user, or just finished a turn,
 * between two listings: what the desktop app notifies about. A session not
 * in the earlier listing is not news. */
export function attentionChanges(before: TaskSummary[], after: TaskSummary[]): Attention[] {
  const was = new Map(before.map((s) => [s.task_id, sessionStatus(s.status, s.waiting_on)]));
  const out: Attention[] = [];
  for (const s of after) {
    const old = was.get(s.task_id);
    if (old === undefined) continue;
    const now = sessionStatus(s.status, s.waiting_on);
    if (now === old) continue;
    const title = s.title || s.prompt || "Untitled session";
    if (now === "needs-you") out.push({ taskId: s.task_id, title, body: s.waiting_on === "question" ? "Has a question for you" : "Needs your approval" });
    else if (old === "running" && (now === "idle" || now === "ended")) out.push({ taskId: s.task_id, title, body: "Finished" });
  }
  return out;
}
