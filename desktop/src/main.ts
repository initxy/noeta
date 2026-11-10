// The main process: one window over a local `noeta serve` it starts and stops.
import { app, BrowserWindow, dialog, ipcMain, Menu, nativeTheme, Notification, screen, shell } from "electron";
import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join, resolve } from "node:path";
import { findNoeta, freePort, freshToken, INSTALL_LINE, isUnder, saveProject, ServerError, startServer, type PageConfig, type Running } from "./server";
import { addRoot, loadSettings, saveSettings, visibleBounds, type Settings } from "./settings";
import { CHANNELS, errorPage, GRANTED_PERMISSIONS, linkTarget, liveSessions, menuTemplate, type PageCommand } from "./shell";

const DOCS_URL = "https://github.com/initxy/noeta#readme";
const dev = !app.isPackaged;

// Tests and side-by-side runs keep their own profile (and so their own instance lock).
if (process.env.NOETA_DESKTOP_USER_DATA) app.setPath("userData", resolve(process.env.NOETA_DESKTOP_USER_DATA));
const settingsPath = join(app.getPath("userData"), "settings.json");
let settings: Settings = loadSettings(settingsPath);

/** The built web app: next to the app when packaged, else the sibling web/dist. */
function webDir(): string {
  if (process.env.NOETA_DESKTOP_WEB) return resolve(process.env.NOETA_DESKTOP_WEB);
  return app.isPackaged ? join(process.resourcesPath, "web") : resolve(__dirname, "..", "..", "web", "dist");
}

let server: Running | null = null;
let window: BrowserWindow | null = null;

function project(): string {
  return settings.project && existsSync(settings.project) ? settings.project : homedir();
}

function pageConfig(): PageConfig {
  return { server: "", token: server?.token ?? "" };
}

async function launch(): Promise<Running> {
  const configured = process.env.NOETA_DESKTOP_NOETA || settings.noetaPath;
  const noeta = findNoeta(configured);
  if (!noeta) {
    throw new ServerError(
      configured
        ? `noeta was not found at ${configured} (NOETA_DESKTOP_NOETA, or noetaPath in ${settingsPath}). Install it:`
        : "noeta was not found on PATH. Install it, then open the app again:",
    );
  }
  return startServer({
    noeta,
    port: await freePort(),
    token: freshToken(),
    webDir: webDir(),
    cwd: homedir(),
    roots: settings.roots,
  });
}

function send(c: PageCommand): void {
  window?.webContents.send(CHANNELS.command, c);
}

function showError(e: unknown, title = "noeta could not start"): void {
  const err = e instanceof ServerError ? e : new ServerError(String(e));
  const missing = err.message.includes("not found");
  const html = errorPage(
    missing ? "noeta is not installed" : title,
    err.message,
    missing ? INSTALL_LINE : undefined,
    err.output.trim() || undefined,
  );
  void window?.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(html)}`);
}

/** A server that ends without being stopped (stop() clears `server` first):
 * the window says so, with its last output. */
function watchExit(s: Running): void {
  void s.exited.then((how) => {
    if (server !== s) return;
    server = null;
    showError(new ServerError(`noeta serve stopped (${how}). Sessions are kept; they go on once it runs again.`, s.output()), "noeta stopped");
  });
}

async function openPage(): Promise<void> {
  if (!window) return;
  try {
    if (!server) {
      server = await launch();
      watchExit(server);
      // The page opens on the last project: the server lists it (one chosen by an older app too).
      if (settings.project && existsSync(settings.project)) await saveProject(server, settings.project);
    }
    await window.loadURL(`${server.url}/`);
  } catch (e) {
    server = null;
    showError(e);
  }
}

/** View > Reload: the page, or a new server when there is none running. */
function reload(): void {
  if (server) window?.webContents.reload();
  else void openPage();
}

/** Whether to go on with a restart that would interrupt sessions mid-turn. */
async function confirmRestart(): Promise<boolean> {
  if (!window || !server) return true;
  let live = 0;
  try {
    const resp = await fetch(`${server.url}/summaries`, { headers: { authorization: `Bearer ${server.token}` } });
    if (resp.ok) live = liveSessions((await resp.json()) as { status: string; waiting_on: string | null }[]);
  } catch {
    // unreachable: nothing to interrupt
  }
  if (!live) return true;
  const got = await dialog.showMessageBox(window, {
    type: "warning",
    message: "Restart the local server?",
    detail: `A folder outside your home directory needs the server restarted. ${live} session(s) are working or waiting for you; they stop now and go on after the restart.`,
    buttons: ["Restart", "Cancel"],
    defaultId: 1,
    cancelId: 1,
  });
  return got.response === 0;
}

async function restartServer(): Promise<void> {
  const old = server;
  server = null;
  await old?.stop();
  await openPage();
}

async function chooseProject(): Promise<string | null> {
  if (!window) return null;
  const got = await dialog.showOpenDialog(window, {
    title: "Add a project folder",
    defaultPath: project(),
    properties: ["openDirectory", "createDirectory"],
  });
  const dir = got.canceled ? undefined : got.filePaths[0];
  if (!dir) return null;
  // The server runs sessions below its own directory (the home directory) and
  // the roots it was started with: a folder elsewhere needs a restart.
  const outside = !isUnder(dir, homedir()) && !settings.roots.some((r) => isUnder(dir, r));
  if (outside && !(await confirmRestart())) return null;
  settings = { ...settings, project: dir, roots: outside ? addRoot(settings.roots, dir) : settings.roots };
  saveSettings(settingsPath, settings);
  // A restart saves it as the server's project on the way up.
  if (outside) await restartServer();
  else {
    if (server) await saveProject(server, dir);
    send({ name: "project", path: dir });
  }
  return dir;
}

function openSettings(): void {
  if (!existsSync(settingsPath)) saveSettings(settingsPath, settings);
  void shell.openPath(settingsPath);
}

function createWindow(): void {
  const w = visibleBounds(settings.window, screen.getAllDisplays().map((d) => d.workArea));
  window = new BrowserWindow({
    ...w,
    minWidth: 480,
    minHeight: 360,
    title: "noeta",
    show: false,
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#262624" : "#faf9f5",
    webPreferences: {
      preload: join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  if (w.maximized) window.maximize();
  window.once("ready-to-show", () => window?.show());
  const remember = () => {
    if (!window) return;
    const maximized = window.isMaximized();
    const b = maximized ? settings.window : { ...settings.window, ...window.getNormalBounds() };
    settings = { ...settings, window: { ...b, maximized } };
  };
  window.on("resize", remember);
  window.on("move", remember);
  window.on("close", () => {
    remember();
    saveSettings(settingsPath, settings);
  });
  window.on("closed", () => (window = null));

  const contents = window.webContents;
  contents.session.setPermissionRequestHandler((_wc, permission, done) => done(GRANTED_PERMISSIONS.has(permission)));
  contents.session.setPermissionCheckHandler((_wc, permission) => GRANTED_PERMISSIONS.has(permission));
  const route = (url: string): boolean => {
    const target = server ? linkTarget(url, server.url) : "refuse";
    if (target === "browser") void shell.openExternal(url);
    return target === "app";
  };
  contents.setWindowOpenHandler(({ url }) => {
    route(url);
    return { action: "deny" };
  });
  contents.on("will-navigate", (e, url) => {
    if (!route(url)) e.preventDefault();
  });
  void openPage();
}

function wire(): void {
  ipcMain.on(CHANNELS.config, (e) => {
    e.returnValue = pageConfig();
  });
  ipcMain.handle(CHANNELS.project, () => project());
  ipcMain.handle(CHANNELS.chooseProject, () => chooseProject());
  // The project picked in the page: the window opens on it next time.
  ipcMain.on(CHANNELS.setProject, (_e, path: unknown) => {
    if (typeof path !== "string" || !path || path === settings.project || !existsSync(path)) return;
    settings = { ...settings, project: path };
    saveSettings(settingsPath, settings);
  });
  ipcMain.on(CHANNELS.notify, (_e, n: { title?: unknown; body?: unknown; taskId?: unknown }) => {
    if (!window || window.isFocused() || !Notification.isSupported()) return;
    const note = new Notification({ title: String(n.title ?? "noeta"), body: String(n.body ?? "") });
    note.on("click", () => {
      window?.show();
      window?.focus();
      if (typeof n.taskId === "string") send({ name: "open-session", taskId: n.taskId });
    });
    note.show();
  });
  Menu.setApplicationMenu(
    Menu.buildFromTemplate(
      menuTemplate(process.platform, dev, {
        newSession: () => send({ name: "new-session" }),
        reload,
        chooseProject: () => void chooseProject(),
        openSettings,
        openDocs: () => void shell.openExternal(DOCS_URL),
      }),
    ),
  );
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!window) return createWindow();
    if (window.isMinimized()) window.restore();
    window.focus();
  });
  nativeTheme.themeSource = "system";
  void app.whenReady().then(() => {
    wire();
    createWindow();
    app.on("activate", () => {
      if (!window) createWindow();
    });
  });
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") app.quit();
  });
  let stopped = false;
  app.on("before-quit", (e) => {
    if (stopped || !server) return;
    e.preventDefault();
    const s = server;
    server = null;
    void s.stop().finally(() => {
      stopped = true;
      app.quit();
    });
  });
}
