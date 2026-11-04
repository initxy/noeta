import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import type { MenuItemConstructorOptions } from "electron";
import { describe, expect, it, vi } from "vitest";
import { DEFAULT_WINDOW, loadSettings, parseSettings, saveSettings, visibleBounds } from "../src/settings";
import { errorPage, linkTarget, menuTemplate } from "../src/shell";

function items(menu: MenuItemConstructorOptions[]): MenuItemConstructorOptions[] {
  return menu.flatMap((m) => [m, ...(Array.isArray(m.submenu) ? items(m.submenu) : [])]);
}

const actions = () => ({ newSession: vi.fn(), chooseProject: vi.fn(), openSettings: vi.fn(), openDocs: vi.fn() });

describe("menu", () => {
  it("has New Session on Cmd/Ctrl+N and Settings on Cmd/Ctrl+,", () => {
    const a = actions();
    const all = items(menuTemplate("linux", false, a));
    const byLabel = (l: string) => all.find((m) => m.label === l)!;
    expect(byLabel("New Session").accelerator).toBe("CmdOrCtrl+N");
    expect(byLabel("Settings…").accelerator).toBe("CmdOrCtrl+,");
    expect(byLabel("Open Project Folder…").accelerator).toBe("CmdOrCtrl+O");
    (byLabel("New Session").click as () => void)();
    expect(a.newSession).toHaveBeenCalled();
  });

  it("offers DevTools only in development", () => {
    const roles = (dev: boolean) => items(menuTemplate("linux", dev, actions())).map((m) => m.role);
    expect(roles(true)).toContain("toggleDevTools");
    expect(roles(false)).not.toContain("toggleDevTools");
    expect(roles(false)).toContain("reload");
  });

  it("puts Settings in the app menu on macOS", () => {
    const menu = menuTemplate("darwin", false, actions());
    expect(menu[0].label).toBe("noeta");
    expect(items([menu[0]]).some((m) => m.label === "Settings…")).toBe(true);
    expect(items([menu[1]]).some((m) => m.label === "Settings…")).toBe(false);
  });
});

describe("links", () => {
  const app = "http://127.0.0.1:5000";
  it("keeps the app's own pages in the window", () => {
    expect(linkTarget("http://127.0.0.1:5000/#session=x", app)).toBe("app");
  });
  it("opens other web links in the browser", () => {
    expect(linkTarget("https://github.com/initxy/noeta", app)).toBe("browser");
    expect(linkTarget("http://127.0.0.1:5001/", app)).toBe("browser");
  });
  it("refuses other schemes", () => {
    expect(linkTarget("file:///etc/passwd", app)).toBe("refuse");
    expect(linkTarget("javascript:alert(1)", app)).toBe("refuse");
    expect(linkTarget("not a url", app)).toBe("refuse");
  });
});

describe("error page", () => {
  it("shows the install line and escapes the server's output", () => {
    const html = errorPage("noeta is not installed", "not found", "uv tool install noeta", "<b>boom</b>");
    expect(html).toContain('<pre class="command">uv tool install noeta</pre>');
    expect(html).toContain("&#60;b&#62;boom");
    expect(html).not.toContain("<b>boom");
  });
});

describe("settings", () => {
  it("starts from defaults on a missing or broken file", () => {
    const dir = mkdtempSync(join(tmpdir(), "noeta-desktop-settings-"));
    expect(loadSettings(join(dir, "none.json"))).toEqual({ noetaPath: undefined, project: undefined, roots: [], window: DEFAULT_WINDOW });
    expect(parseSettings("{nope").window).toEqual(DEFAULT_WINDOW);
  });

  it("round-trips and drops bad fields", () => {
    const dir = mkdtempSync(join(tmpdir(), "noeta-desktop-settings-"));
    const path = join(dir, "sub", "settings.json");
    const s = { noetaPath: "/x/noeta", project: "/p", roots: ["/p"], window: { x: 10, y: 20, width: 900, height: 700, maximized: true } };
    saveSettings(path, s);
    expect(loadSettings(path)).toEqual(s);
    writeFileSync(path, JSON.stringify({ roots: ["/a", 3], window: { width: 10, height: "x" } }));
    const got = loadSettings(path);
    expect(got.roots).toEqual(["/a"]);
    expect(got.window).toMatchObject({ width: DEFAULT_WINDOW.width, height: DEFAULT_WINDOW.height });
    expect(readFileSync(path, "utf-8")).toContain("roots");
  });

  it("forgets a position no display shows", () => {
    const displays = [{ x: 0, y: 0, width: 1920, height: 1080 }];
    const w = { x: 100, y: 100, width: 800, height: 600, maximized: false };
    expect(visibleBounds(w, displays)).toEqual(w);
    expect(visibleBounds({ ...w, x: 3000 }, displays)).toMatchObject({ x: undefined, y: undefined, width: 800 });
  });
});
