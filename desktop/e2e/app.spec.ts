// The built app against a real `noeta serve` it starts itself, on the offline
// scripted provider (web/e2e/provider). Screenshots go to screenshots/ only
// with NOETA_SCREENSHOTS=1.
import { _electron as electron, expect, test, type ElectronApplication, type Page } from "@playwright/test";
import { execFileSync } from "node:child_process";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

const here = resolve(__dirname, "..");
const SHOTS = process.env.NOETA_SCREENSHOTS ? "screenshots" : "test-results/screenshots";

function profile(settings: Record<string, unknown>): { dir: string; work: string } {
  const dir = mkdtempSync(join(tmpdir(), "noeta-desktop-e2e-"));
  const work = join(dir, "work");
  mkdirSync(work);
  mkdirSync(join(dir, "home"));
  mkdirSync(join(dir, "profile"));
  writeFileSync(join(dir, "profile", "settings.json"), JSON.stringify({ project: work, roots: [work], ...settings }));
  return { dir, work };
}

async function launch(dir: string, work: string, noeta: string): Promise<{ app: ElectronApplication; page: Page }> {
  const app = await electron.launch({
    // No setuid sandbox helper in CI containers; the page keeps its JS sandbox.
    args: [here, "--no-sandbox"],
    env: {
      ...process.env,
      NOETA_DESKTOP_NOETA: noeta,
      NOETA_DESKTOP_USER_DATA: join(dir, "profile"),
      NOETA_HOME: join(dir, "home"),
      NOETA_STORE: `sqlite:///${join(dir, "store.db")}`,
      NOETA_MODEL: "claude-sonnet-4-5",
      NOETA_PROVIDER: "web_e2e",
      NOETA_WEB_E2E_DIR: work,
    },
  });
  const page = await app.firstWindow();
  await page.setViewportSize({ width: 1200, height: 780 }).catch(() => undefined);
  return { app, page };
}

test("the app starts its own server, injects the token, and a session replies", async () => {
  const { dir, work } = profile({});
  const { app, page } = await launch(dir, work, join(here, "e2e", "noeta"));
  try {
    await expect(page.getByRole("navigation", { name: "Sessions" })).toBeVisible({ timeout: 180_000 });
    // The token came from the preload: no connect form, nothing in the URL.
    expect(page.url()).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/$/);
    expect(await page.evaluate(() => typeof (window as { require?: unknown }).require)).toBe("undefined");
    expect(await page.evaluate(() => typeof (window as { noetaDesktop?: unknown }).noetaDesktop)).toBe("object");
    await expect(page.getByRole("button", { name: work })).toBeVisible();
    await page.getByLabel("Permission mode").selectOption("default");
    await page.screenshot({ path: `${SHOTS}/01-new-session.png` });

    const box = page.getByLabel("Message");
    await box.fill("hello there, noeta");
    await box.press("Enter");
    await expect(page.locator(".msg.assistant").last()).toContainText("I am the scripted model", { timeout: 30_000 });
    await expect(page.locator(".cwd")).toHaveText(work);
    await page.screenshot({ path: `${SHOTS}/02-reply.png` });

    // The menu's New Session (Cmd/Ctrl+N) goes back to the welcome screen.
    await clickMenu(app, "New Session");
    await expect(page.getByText("What should we work on?")).toBeVisible();
  } finally {
    await app.close();
  }
});

async function clickMenu(app: ElectronApplication, label: string): Promise<void> {
  await app.evaluate(({ Menu }, name) => {
    const item = Menu.getApplicationMenu()?.items.flatMap((m) => m.submenu?.items ?? []).find((i) => i.label === name);
    item?.click();
  }, label);
}

test("the window keeps to the app's page, grants only notifications, and notices a server that dies", async () => {
  const { dir, work } = profile({});
  const { app, page } = await launch(dir, work, join(here, "e2e", "noeta"));
  try {
    await expect(page.getByRole("navigation", { name: "Sessions" })).toBeVisible({ timeout: 180_000 });
    const root = page.url();
    await app.evaluate(({ shell }) => {
      const opened: string[] = [];
      (globalThis as { opened?: string[] }).opened = opened;
      shell.openExternal = async (url: string) => {
        opened.push(url);
      };
    });
    expect(await page.evaluate(() => Notification.requestPermission())).toBe("granted");
    const geo = await page.evaluate(
      () => new Promise<string>((resolve) => navigator.geolocation.getCurrentPosition(() => resolve("allowed"), (e) => resolve(`refused ${e.code}`))),
    );
    expect(geo).toBe("refused 1");

    // Another path of the same server is refused; the window stays on the app.
    await page.evaluate(() => window.open("/openapi.json"));
    await page.evaluate(() => window.open("https://example.com/docs"));
    await page.evaluate(() => {
      window.location.href = "/tasks";
    });
    await expect.poll(() => app.evaluate(() => (globalThis as { opened?: string[] }).opened)).toEqual(["https://example.com/docs"]);
    const shown = () => app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0].webContents.getURL());
    expect(await shown()).toBe(root);
    expect(app.windows()).toHaveLength(1);

    // The server dies (not stopped by the app): the window says so, with its output.
    const port = new URL(root).port;
    execFileSync("pkill", ["-KILL", "-f", `serve --host 127.0.0.1 --port ${port}`]);
    await expect(page.getByRole("heading", { name: "noeta stopped" })).toBeVisible({ timeout: 30_000 });
    await expect(page.locator("pre.output")).toContainText("Uvicorn");
    await page.screenshot({ path: `${SHOTS}/04-server-stopped.png` });
    // View > Reload starts it again.
    await clickMenu(app, "Reload");
    await expect(page.getByRole("navigation", { name: "Sessions" })).toBeVisible({ timeout: 180_000 });
    expect(page.url()).not.toBe(root);
  } finally {
    await app.close();
  }
});

test("a missing noeta shows the install line", async () => {
  const { dir, work } = profile({});
  const { app, page } = await launch(dir, work, join(dir, "no-such-noeta"));
  try {
    await expect(page.getByRole("heading", { name: "noeta is not installed" })).toBeVisible();
    await expect(page.locator("pre.command")).toContainText("noeta[server]");
    await page.screenshot({ path: `${SHOTS}/03-missing-noeta.png` });
  } finally {
    await app.close();
  }
});
