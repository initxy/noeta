// The web app against a real `noeta serve` on the scripted provider
// (e2e/provider: the prompt's keyword picks the script). Screenshots go to
// screenshots/ (the README there) only with NOETA_SCREENSHOTS=1.
import { expect, test, type Page } from "@playwright/test";
import { mkdirSync, writeFileSync } from "node:fs";
import { join } from "node:path";
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

test.beforeEach(async ({ page }) => {
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
