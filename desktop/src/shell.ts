// The window's pieces that need no running Electron: the menu, the error page,
// which links leave the app, the IPC channel names.
import type { MenuItemConstructorOptions } from "electron";

/** IPC channels between the preload and the main process. */
export const CHANNELS = {
  config: "noeta:config",
  project: "noeta:project",
  chooseProject: "noeta:choose-project",
  setProject: "noeta:set-project",
  notify: "noeta:notify",
  /** main -> page: a menu command or a notification clicked. */
  command: "noeta:command",
} as const;

/** What the menu (or a clicked notification) asks the page to do. */
export type PageCommand = { name: "new-session" } | { name: "open-session"; taskId: string } | { name: "project"; path: string };

export interface MenuActions {
  newSession(): void;
  /** View > Reload: the page, or the server when it has stopped. */
  reload(): void;
  chooseProject(): void;
  openSettings(): void;
  openDocs(): void;
}

/** The app menu, as Claude's desktop app lays it out: the app menu on macOS,
 * File, Edit, View (DevTools only in development), Window, Help. */
export function menuTemplate(platform: string, dev: boolean, a: MenuActions): MenuItemConstructorOptions[] {
  const mac = platform === "darwin";
  const settings: MenuItemConstructorOptions = { label: "Settings…", accelerator: "CmdOrCtrl+,", click: a.openSettings };
  const file: MenuItemConstructorOptions[] = [
    { label: "New Session", accelerator: "CmdOrCtrl+N", click: a.newSession },
    { label: "Add Project…", accelerator: "CmdOrCtrl+O", click: a.chooseProject },
    { type: "separator" },
    ...(mac ? [] : [settings, { type: "separator" } as const]),
    mac ? { role: "close" } : { role: "quit" },
  ];
  const view: MenuItemConstructorOptions[] = [
    { label: "Reload", accelerator: "CmdOrCtrl+R", click: a.reload },
    ...(dev ? [{ role: "forceReload" } as const, { role: "toggleDevTools" } as const] : []),
    { type: "separator" },
    { role: "resetZoom" },
    { role: "zoomIn" },
    { role: "zoomOut" },
    { type: "separator" },
    { role: "togglefullscreen" },
  ];
  return [
    ...(mac
      ? [
          {
            label: "noeta",
            submenu: [
              { role: "about" },
              { type: "separator" },
              settings,
              { type: "separator" },
              { role: "hide" },
              { role: "hideOthers" },
              { role: "unhide" },
              { type: "separator" },
              { role: "quit" },
            ],
          } as MenuItemConstructorOptions,
        ]
      : []),
    { label: "File", submenu: file },
    { role: "editMenu" },
    { label: "View", submenu: view },
    { role: "windowMenu" },
    { role: "help", submenu: [{ label: "noeta Documentation", click: a.openDocs }] },
  ];
}

/** Whether a link stays in the window (the app's page: its server's `/`,
 * any query or fragment), opens in the browser (http/https/mailto elsewhere),
 * or is refused (the server's other paths, other schemes). */
export function linkTarget(url: string, appUrl: string): "app" | "browser" | "refuse" {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return "refuse";
  }
  if (u.origin === new URL(appUrl).origin) return u.pathname === "/" ? "app" : "refuse";
  return u.protocol === "https:" || u.protocol === "http:" || u.protocol === "mailto:" ? "browser" : "refuse";
}

/** The page permissions the window grants: notifications and writing the
 * clipboard (copying a code block); camera, location and the rest are refused. */
export const GRANTED_PERMISSIONS: ReadonlySet<string> = new Set(["notifications", "clipboard-sanitized-write"]);

function escape(s: string): string {
  return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

/** The page shown when the server cannot start or has stopped: what
 * happened, what to run, and that View > Reload starts it again. */
export function errorPage(title: string, message: string, command?: string, output?: string): string {
  return `<!doctype html><html><head><meta charset="utf-8"><title>noeta</title>
<style>
:root{color-scheme:light dark;--bg:#faf9f5;--fg:#1f1e1d;--muted:#6b6a68;--box:#f0eee6}
@media (prefers-color-scheme:dark){:root{--bg:#262624;--fg:#ecebe6;--muted:#a3a29c;--box:#1f1e1d}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;display:grid;place-items:center;min-height:100vh}
main{max-width:640px;padding:32px}h1{font:400 28px ui-serif,Georgia,serif;margin:0 0 12px}
p{color:var(--muted)}pre{background:var(--box);padding:12px 14px;border-radius:8px;white-space:pre-wrap;word-break:break-all;user-select:all}
</style></head><body><main><h1>${escape(title)}</h1><p>${escape(message)}</p>
${command ? `<pre class="command">${escape(command)}</pre>` : ""}
<p class="retry">View &gt; Reload (Ctrl+R, ⌘R on macOS) starts it again.</p>
${output ? `<p>The server said:</p><pre class="output">${escape(output)}</pre>` : ""}
</main></body></html>`;
}
