// The web app against a real `noeta serve` on the scripted provider
// (e2e/provider: the prompt's keyword picks the script). Screenshots go to
// screenshots/ (the README there) only with NOETA_SCREENSHOTS=1.
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";
import { TOKEN } from "../playwright.config";

const SHOTS = process.env.NOETA_SCREENSHOTS ? "screenshots" : "test-results/screenshots";
const shot = (page: Page, name: string) => page.screenshot({ path: `${SHOTS}/${name}.png` });

async function newSession(page: Page, mode = "default") {
  await page.getByRole("button", { name: "+ New session" }).click();
  await page.getByLabel("Permission mode").selectOption(mode);
}

async function say(page: Page, text: string) {
  const box = page.getByLabel("Message");
  await box.fill(text);
  await box.press("Enter");
}

test.describe.configure({ mode: "serial" });

// The server's working directory, saved as a project and picked: the scripted
// model writes there and @ mentions complete from it. A session without a
// project gets a scratch folder of its own (the scratch test).
const WORK = join(process.env.NOETA_E2E_DIR!, "work");
const auth = { authorization: `Bearer ${TOKEN}` };

test.beforeAll(async ({ request }) => {
  expect((await request.post("/projects", { headers: auth, data: { path: WORK } })).ok()).toBe(true);
});

test.beforeEach(async ({ page }) => {
  // Picked on the first load only: a reload keeps what the test picked since.
  await page.addInitScript((work) => {
    if (!sessionStorage.getItem("e2e.started")) localStorage.setItem("noeta.project", work);
    sessionStorage.setItem("e2e.started", "1");
  }, WORK);
  await page.goto(`/#token=${TOKEN}`);
  await expect(page.getByRole("navigation", { name: "Sessions" })).toBeVisible();
});

test("the bundle is served with a content security policy", async ({ page }) => {
  const resp = await page.request.get("/");
  expect(resp.headers()["content-security-policy"]).toContain("default-src 'self'");
  expect(resp.headers()["x-frame-options"]).toBe("DENY");
});

test("a new session streams a markdown reply", async ({ page }) => {
  await newSession(page);
  // Shift+Tab cycles the mode, as in Claude Code.
  await page.getByLabel("Message").press("Shift+Tab");
  await expect(page.getByLabel("Permission mode")).toHaveValue("acceptEdits");
  await page.getByLabel("Permission mode").selectOption("default");
  await shot(page, "01-new-session");
  await say(page, "hello there, noeta");
  await expect(page.locator(".msg.user").first()).toContainText("hello there, noeta");
  await expect(page.locator(".msg.assistant").last()).toContainText("I am the scripted model", { timeout: 20_000 });
  await expect(page.locator(".msg.assistant pre code")).toContainText("print('hello from noeta')");
  await expect(page.getByRole("button", { name: /Thought/ })).toBeVisible();
  await expect(page.locator(".session.active")).toContainText("hello there, noeta");
  await expect(page.locator(".usage")).toContainText("tokens");
  await shot(page, "02-streamed-reply");
});

test("an approval card answered runs the Bash call", async ({ page }) => {
  await newSession(page);
  await say(page, "please run bash for me");
  const card = page.getByRole("dialog", { name: /Bash|command/i });
  await expect(card).toBeVisible();
  await expect(card.getByRole("button", { name: /^1 Yes/ })).toBeVisible();
  await expect(page.locator('.tool[data-tool="Bash"]')).toHaveAttribute("data-status", "waiting");
  await shot(page, "03-approval-card");
  await expect(card).toBeFocused();
  await page.keyboard.press("1");
  const tool = page.locator('.tool[data-tool="Bash"]');
  await expect(tool).toHaveAttribute("data-status", "done");
  await expect(tool.locator(".tool-output")).toContainText("hello-from-bash");
  await expect(page.locator(".msg.assistant").last()).toContainText("The command answered: hello-from-bash");
  await shot(page, "04-bash-card");
});

test("an approval card stays while a message arrives during it", async ({ page }) => {
  await newSession(page);
  await say(page, "run bash once more");
  const card = page.getByRole("dialog", { name: /Bash|command/i });
  await expect(card).toBeVisible();
  const id = /#session=(.+)$/.exec(page.url())![1];
  // Taken at once (as a background job's end or a timer is): the approval still stands.
  const sent = await page.request.post(`/tasks/${id}/messages`, { headers: { authorization: `Bearer ${TOKEN}` }, data: { input: "a note meanwhile", when: "now" } });
  expect(sent.ok()).toBe(true);
  await expect(page.locator(".msg.user").last()).toContainText("a note meanwhile");
  await page.waitForTimeout(2000); // past a poll of the cards
  await expect(card).toBeVisible();
  await card.getByRole("button", { name: /^1 Yes/ }).click();
  await expect(page.locator('.tool[data-tool="Bash"]')).toHaveAttribute("data-status", "done");
});

test("a Write shows its diff, and its card switches the mode", async ({ page }) => {
  await newSession(page);
  await say(page, "write notes-e2e.txt please");
  const card = page.getByRole("dialog", { name: "Create file" });
  await expect(card).toBeVisible();
  await expect(card.locator(".diff-add").first()).toContainText("first line");
  await expect(page.locator('.tool[data-tool="Write"] .diff-add').first()).toContainText("first line");
  await card.getByRole("button", { name: /accept edits/ }).click();
  await expect(page.getByLabel("Permission mode")).toHaveValue("acceptEdits");
  await expect(page.locator(".msg.assistant").last()).toContainText("Wrote notes-e2e.txt.");
  await expect(page.locator('.tool[data-tool="Write"] .diffstat')).toContainText("+2");
  await shot(page, "05-write-diff");
});

test("the stop button interrupts a reply", async ({ page }) => {
  await newSession(page, "acceptEdits");
  await say(page, "be slow please");
  await expect(page.getByTestId("streaming")).toContainText("word3");
  await shot(page, "06-streaming");
  await page.getByRole("button", { name: "Stop" }).click();
  await expect(page.locator(".notice").last()).toContainText("Interrupted");
  await expect(page.getByRole("button", { name: "Stop" })).toBeHidden();
  await shot(page, "07-interrupted");
});

test("the mode picker switches a running session to plan mode, and the plan is reviewed", async ({ page }) => {
  await newSession(page);
  await say(page, "hello again, noeta");
  await expect(page.locator(".msg.assistant").last()).toContainText("scripted model");
  await page.getByLabel("Permission mode").selectOption("plan");
  await expect(page.getByLabel("Permission mode")).toHaveValue("plan");
  await say(page, "make a plan");
  const card = page.getByRole("dialog", { name: "ExitPlanMode" });
  await expect(card).toContainText("Ready to code?");
  await expect(card.locator(".plan h1")).toHaveText("Plan");
  await shot(page, "08-plan-review");
  await card.getByLabel(/Yes, manually approve edits/).check();
  await card.getByRole("button", { name: "Submit" }).click();
  await expect(page.locator(".msg.assistant").last()).toContainText("Following the plan.");
  await expect(page.getByLabel("Permission mode")).toHaveValue("default");
});

test("task list, questions and a sub-agent", async ({ page }) => {
  await newSession(page, "auto");
  await say(page, "todo please");
  await expect(page.getByRole("complementary", { name: "Tasks" })).toContainText("Reading the code");
  await expect(page.getByRole("complementary", { name: "Tasks" })).toContainText("Fix the bug");
  await shot(page, "09-todos");

  await say(page, "ask me something");
  const card = page.getByRole("dialog", { name: "AskUserQuestion" });
  await card.getByLabel(/Blue/).check();
  await card.getByRole("button", { name: "Submit" }).click();
  await expect(page.locator(".msg.assistant").last()).toContainText("You answered");

  await say(page, "start an agent");
  const agent = page.locator('.tool[data-tool="Agent"]');
  await expect(agent).toContainText("Explore");
  await agent.getByRole("button", { name: /Show the sub-agent's work/ }).click();
  await expect(agent.locator(".nested .tool[data-tool='Glob']")).toBeVisible();
  await expect(agent.locator(".nested")).toContainText("explored");
  await shot(page, "10-subagent");

  // The task list is the todos plugin's: switched off for the session, it goes.
  await page.getByRole("button", { name: "Plugins", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Plugins" });
  await dialog.locator('.plugin-row[data-plugin="todos"]').getByRole("switch", { name: "This session" }).uncheck();
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(page.getByRole("complementary", { name: "Tasks" })).toHaveCount(0);
  await expect(agent).toContainText("Explore"); // the other plugins' views stay
});

test("slash commands and file mentions complete in the composer", async ({ page }) => {
  await page.locator(".session").first().click();
  const box = page.getByLabel("Message");
  await box.fill("/");
  const menu = page.getByRole("listbox", { name: "commands" });
  await expect(menu).toContainText("/compact");
  await expect(menu).toContainText("/rename");
  await shot(page, "11-slash-menu");
  await box.fill("look at @notes-e");
  await expect(page.getByRole("listbox", { name: "files" })).toContainText("@notes-e2e.txt");
  await box.press("Enter");
  await expect(box).toHaveValue("look at @notes-e2e.txt ");
});

test("rename, rewind, dark theme and a phone-sized screen", async ({ page }) => {
  await page.locator(".session").first().click();
  page.once("dialog", (d) => d.accept("Renamed in the web app"));
  await page.locator(".header .title").click();
  await expect(page.locator(".session.active")).toContainText("Renamed in the web app");
  await page.getByRole("button", { name: "Rewind" }).click();
  await expect(page.getByRole("dialog", { name: "Rewind" })).toContainText("start an agent");
  await shot(page, "12-rewind");
  await page.getByRole("dialog", { name: "Rewind" }).getByRole("button", { name: "Close" }).click();
  await page.getByRole("button", { name: "Toggle theme" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", /dark|light/);
  await shot(page, "13-theme");
  await page.setViewportSize({ width: 390, height: 780 });
  await shot(page, "14-phone");
  await page.getByRole("button", { name: "Sessions" }).click();
  await expect(page.locator(".nav-open .sidebar")).toBeVisible();
  await shot(page, "15-phone-sessions");
});

test("a new session's first message may be a slash command", async ({ page }) => {
  const commands = join(process.env.NOETA_E2E_DIR!, "home", "commands");
  mkdirSync(commands, { recursive: true });
  writeFileSync(join(commands, "greet.md"), "---\ndescription: Greet the team\n---\nSay hello to the team.\n");
  await newSession(page, "acceptEdits");
  await say(page, "/greet the team");
  await expect(page.locator(".msg.user").first()).toHaveText("/greet the team");
  await expect(page.locator(".msg.assistant").last()).toContainText("Ran the command /greet.");
});

test("links open in a new tab and remote images are not loaded", async ({ page }) => {
  const fetched: string[] = [];
  page.on("request", (r) => fetched.push(r.url()));
  await newSession(page, "acceptEdits");
  await say(page, "show me links");
  const reply = page.locator(".msg.assistant").last();
  await expect(reply.getByRole("link", { name: "the docs" })).toHaveAttribute("target", "_blank");
  await expect(reply.getByRole("link", { name: "the docs" })).toHaveAttribute("rel", "noopener noreferrer");
  await expect(reply.getByRole("link", { name: "Image: chart" })).toBeVisible();
  await expect(reply.locator("img")).toHaveCount(0);
  expect(fetched.filter((u) => u.includes("pixel.png"))).toEqual([]);
});

test("an ended session is continued by sending to it", async ({ page }) => {
  await newSession(page, "acceptEdits");
  await say(page, "hello before the end");
  await expect(page.locator(".msg.assistant").last()).toContainText("scripted model");
  const ended = /#session=(.+)$/.exec(page.url())![1];
  const cancel = await page.request.post(`/tasks/${ended}/cancel`, { headers: { authorization: `Bearer ${TOKEN}` }, data: {} });
  expect(cancel.ok()).toBe(true);
  await expect(page.locator(".notice").last()).toContainText("Session cancelled");
  await expect(page.getByLabel("Message")).toHaveAttribute("placeholder", /continue it/);
  await say(page, "hello after the end");
  await expect(page).not.toHaveURL(new RegExp(ended));
  await expect(page.locator(".msg.user")).toHaveText(["hello before the end", "hello after the end"]);
  await expect(page.locator(".msg.assistant").last()).toContainText("scripted model");
  await expect(page.getByLabel("Message")).not.toHaveAttribute("placeholder", /continue it/);
});

test("a rewind puts the prompt back and the session is replaced in the sidebar", async ({ page }) => {
  await newSession(page, "acceptEdits");
  await say(page, "hello one");
  await expect(page.locator(".msg.assistant")).toHaveCount(1);
  await say(page, "hello two");
  await expect(page.locator(".msg.assistant")).toHaveCount(2);
  const before = /#session=(.+)$/.exec(page.url())![1];
  const rows = await page.locator(".session").count();
  await page.getByRole("button", { name: "Rewind" }).click();
  const dialog = page.getByRole("dialog", { name: "Rewind" });
  await dialog.locator("li", { hasText: "hello two" }).getByRole("button", { name: "Conversation" }).click();
  await expect(page).not.toHaveURL(new RegExp(before));
  await expect(page.getByLabel("Message")).toHaveValue("hello two");
  // The conversation up to the prompt is still shown, in the new session.
  await expect(page.locator(".msg.user")).toHaveText(["hello one"]);
  await expect(page.locator(".session")).toHaveCount(rows);
  await expect(page.locator(".session.active")).toBeVisible();
});

test("sessions are searched, archived and brought back", async ({ page }) => {
  await newSession(page, "acceptEdits");
  await say(page, "hello, archive me later");
  await expect(page.locator(".msg.assistant").last()).toContainText("scripted model");
  const nav = page.getByRole("navigation", { name: "Sessions" });
  const search = nav.getByRole("searchbox", { name: "Search sessions" });
  await search.fill("archive me");
  const row = nav.locator(".session-row");
  await expect(row).toHaveCount(1);
  await row.hover();
  await shot(page, "16-search-archive");
  await row.getByRole("button", { name: "Archive", exact: true }).click();
  await expect(nav.getByText("No sessions match")).toBeVisible();
  await nav.getByRole("button", { name: "Archived" }).click();
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("archive me later");
  await row.getByRole("button", { name: "Unarchive" }).click();
  await expect(nav.getByText("No archived sessions")).toBeVisible();
  await nav.getByRole("button", { name: "Back to sessions" }).click();
  await expect(row).toHaveCount(1);
  await search.press("Escape");
  await expect(nav.locator(".session-row").nth(1)).toBeVisible();
});

test("near the auto-compact threshold the status bar counts down to it", async ({ page }) => {
  await newSession(page, "acceptEdits");
  await say(page, "hello, a crowded context");
  await expect(page.locator(".msg.assistant").last()).toContainText("scripted model");
  // The compaction plugin's web part: its threshold record against the reply's 90,000 tokens.
  await expect(page.locator(".pickers [data-plugin=compaction]")).toHaveText(/^\d+% until auto-compact$/);
  await expect(page.locator(".usage")).not.toContainText("auto-compact");
});

test("a project added from the folder picker gets its own sessions", async ({ page }) => {
  const dir = join(process.env.NOETA_E2E_DIR!, "work", "proj-e2e");
  mkdirSync(dir, { recursive: true });
  const nav = page.getByRole("navigation", { name: "Sessions" });
  await nav.getByRole("button", { name: "Add project" }).click();
  const picker = page.getByRole("dialog", { name: "Add project" });
  await picker.getByRole("button", { name: "proj-e2e", exact: true }).click();
  await expect(picker.getByLabel("Folder path")).toHaveValue(dir);
  await shot(page, "19-add-project");
  await picker.getByRole("button", { name: "Add proj-e2e" }).click();
  await expect(picker).toBeHidden();
  await expect(page.locator(".welcome-project-path")).toHaveText(dir);
  const project = nav.locator(`[data-project="${dir}"]`);
  await expect(project.getByRole("button", { name: "proj-e2e", exact: true })).toHaveAttribute("title", dir);

  await page.getByLabel("Permission mode").selectOption("acceptEdits");
  await say(page, "hello from a project");
  await expect(page.locator(".msg.assistant").last()).toContainText("scripted model");
  await expect(page.locator(".cwd")).toHaveAttribute("title", dir);
  await expect(project.locator(".session")).toContainText("hello from a project");
  await shot(page, "20-projects");
  await page.emulateMedia({ colorScheme: "dark" });
  await shot(page, "21-projects-dark");
  await page.emulateMedia({ colorScheme: "light" });

  // "+" on a project's row starts the next session there.
  await project.locator(".project-row").hover();
  await project.getByRole("button", { name: "New session in proj-e2e" }).click();
  await expect(page.locator(".welcome-project-path")).toHaveText(dir);

  // Removed, it is listed on as its session's folder only, with nothing to remove.
  await project.locator(".project-row").hover();
  await project.getByRole("button", { name: "proj-e2e options" }).click();
  await project.getByRole("menuitem", { name: "Remove project" }).click();
  await expect(project.getByRole("button", { name: "proj-e2e options" })).toHaveCount(0);
  await expect(project.locator(".session")).toHaveCount(1);
});

test("the folder picker goes anywhere, and says why a path is no folder", async ({ page }) => {
  await page.getByRole("navigation", { name: "Sessions" }).getByRole("button", { name: "Add project" }).click();
  const picker = page.getByRole("dialog", { name: "Add project" });
  await picker.getByLabel("Folder path").fill("/");
  await picker.getByLabel("Folder path").press("Enter");
  await expect(picker.getByRole("button", { name: "Add /" })).toBeEnabled();
  await expect(picker.locator(".notice.error")).toHaveCount(0);
  await picker.getByLabel("Folder path").fill(join(WORK, "no-such-folder"));
  await picker.getByLabel("Folder path").press("Enter");
  await expect(picker.locator(".notice.error")).toContainText("no-such-folder");
  await picker.getByRole("button", { name: "Cancel" }).click();
  await expect(picker).toBeHidden();
});

test("a session without a project runs in a scratch folder, listed under Scratch by date", async ({ page }) => {
  const nav = page.getByRole("navigation", { name: "Sessions" });
  await page.getByRole("button", { name: "Use a scratch folder instead" }).click();
  await expect(page.locator(".welcome-scratch")).toContainText("its own scratch folder");
  await page.getByLabel("Permission mode").selectOption("acceptEdits");
  await say(page, "hello from scratch");
  await expect(page.locator(".msg.assistant").last()).toContainText("scripted model");
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, "0");
  const day = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  await expect(page.locator(".cwd")).toHaveText(new RegExp(`/scratch/${day}/[\\w-]{8}$`));
  const scratch = nav.locator("section.scratch");
  await expect(scratch.getByRole("button", { name: "Scratch", exact: true })).toBeVisible();
  await expect(scratch.locator(`[data-day="${day}"] .scratch-day-label`)).toHaveText("Today");
  await expect(scratch.locator(".session.active")).toContainText("hello from scratch");
  // Its folder is not listed as a project; the Scratch row has no "+" and no menu.
  await expect(nav.locator(`section.project[data-project*="/scratch/"]`)).toHaveCount(0);
  await expect(scratch.locator(".project-row .row-action")).toHaveCount(0);
  await shot(page, "22-scratch");
  // A new session from here gets another scratch folder, not this one.
  await page.getByRole("button", { name: "+ New session" }).click();
  await expect(page.locator(".welcome-scratch")).toBeVisible();
  await expect(scratch.locator(".project-row.selected")).toBeVisible();
});

// Last: the plugin it installs stays in the server's home.
test("a plugin installed from the plugins page adds its panel and status item, and is switched off per session", async ({ page }) => {
  const plugin = resolve(import.meta.dirname, "../../examples/web-panel");
  await page.getByRole("button", { name: "Plugins", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Plugins" });
  await expect(dialog.locator('.plugin-row[data-plugin="todos"]')).toBeVisible();
  await dialog.getByRole("tab", { name: "Discover" }).click();
  await dialog.getByLabel("Install from a source").fill(plugin);
  await dialog.getByRole("button", { name: "Review…" }).click();
  const confirm = dialog.getByRole("dialog", { name: "Install web-panel" });
  await expect(confirm).toContainText(plugin);
  await expect(confirm).toContainText("none (a local path)");
  await shot(page, "17-plugin-install");
  await confirm.getByRole("button", { name: "Install", exact: true }).click();
  const row = dialog.locator('.plugin-row[data-plugin="web-panel"]');
  await expect(row).toContainText("web");
  await expect(row.locator(".notice.error")).toHaveCount(0);
  await dialog.getByRole("button", { name: "Close" }).click();

  await newSession(page, "acceptEdits");
  await say(page, "hello, plugins");
  await expect(page.locator(".msg.assistant").last()).toContainText("scripted model");
  const panel = page.getByTestId("web-panel");
  await expect(panel).toBeVisible();
  await expect(panel.getByTestId("web-panel-host")).toHaveText("in a browser");
  await panel.getByRole("button", { name: "Look" }).click();
  await expect(panel.getByTestId("web-panel-count")).toContainText("entr");
  await expect(page.getByLabel("Status")).toContainText("messages");
  await shot(page, "18-plugin-panel");

  await page.getByRole("button", { name: "Plugins", exact: true }).click();
  await row.getByRole("switch", { name: "This session" }).uncheck();
  await expect(row.getByRole("switch", { name: "This session" })).not.toBeChecked();
  await dialog.getByRole("button", { name: "Close" }).click();
  await expect(panel).toHaveCount(0);
  await expect(page.getByLabel("Status")).not.toContainText("messages");
});
