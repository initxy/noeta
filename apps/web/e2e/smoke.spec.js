import { expect, test } from "@playwright/test";

// Capture uncaught exceptions thrown in the page. Several of the regressions
// these specs guard against surfaced exactly as uncaught errors (e.g. calling
// an undefined `setInput`, or referencing an unimported `getJSON`), so an empty
// list at the end of each test is a real assertion, not decoration.
function trackPageErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

test.describe("chat page", () => {
  test("composer accepts typing and drives the slash menu", async ({ page }) => {
    const errors = trackPageErrors(page);

    // command_in=true enables the composer; a (loaded) empty task list keeps us
    // on the new-session screen where canSend is true.
    await page.route("**/capabilities", (route) =>
      route.fulfill({
        json: {
          command_in: true,
          slash_commands: [
            { name: "help", description: "List commands", kind: "local" },
            { name: "skills", description: "List skills", kind: "local" },
          ],
        },
      }),
    );
    await page.route("**/tasks", (route) => route.fulfill({ json: [] }));

    await page.goto("/chat.html");

    const textarea = page.locator("textarea.ai-prompt-textarea");
    await expect(textarea).toBeEnabled();

    // Regression: the input controller exposed `setValue` while the textarea
    // read `setInput`, so the first keystroke threw "setInput is not a function"
    // and the box was unusable.
    await textarea.pressSequentially("hello world");
    await expect(textarea).toHaveValue("hello world");

    // Typing "/" opens the slash menu; "/ski" narrows it to /skills.
    await textarea.fill("/ski");
    await expect(page.locator(".slash-menu")).toBeVisible();
    await expect(
      page.locator(".slash-option .slash-command", { hasText: "/skills" }),
    ).toBeVisible();

    // Picking an option fills its template back into the box (also exercises
    // setInput from the menu path).
    await page.locator(".slash-option", { hasText: "/skills" }).click();
    await expect(textarea).toHaveValue("/skills ");

    expect(errors).toEqual([]);
  });

  test("slash menu defaults to ten skills and filters the rest", async ({ page }) => {
    const errors = trackPageErrors(page);
    const skills = Array.from({ length: 12 }, (_, index) => ({
      name: `skill-${String(index + 1).padStart(2, "0")}`,
      description: `Skill ${index + 1}`,
    }));

    await page.route("**/capabilities", (route) =>
      route.fulfill({
        json: {
          command_in: true,
          slash_commands: [
            { name: "help", description: "List commands", kind: "local" },
          ],
          skills,
        },
      }),
    );
    await page.route("**/tasks", (route) => route.fulfill({ json: [] }));

    await page.goto("/chat.html");

    const textarea = page.locator("textarea.ai-prompt-textarea");
    await textarea.fill("/");
    await expect(page.locator(".slash-menu")).toBeVisible();
    await expect(page.locator(".slash-option")).toHaveCount(10);
    await expect(page.locator(".slash-option .slash-command", { hasText: "/skill-01" })).toBeVisible();
    await expect(page.locator(".slash-option .slash-command", { hasText: "/skill-12" })).toHaveCount(0);

    await textarea.fill("/skill-12");
    await expect(page.locator(".slash-option")).toHaveCount(1);
    await expect(page.locator(".slash-option .slash-command", { hasText: "/skill-12" })).toBeVisible();

    expect(errors).toEqual([]);
  });

  test("keeps the sidebar collapse control in the brand row", async ({ page }) => {
    await page.route("**/capabilities", (route) =>
      route.fulfill({ json: { command_in: true } }),
    );
    await page.route("**/tasks", (route) => route.fulfill({ json: [] }));

    await page.goto("/chat.html");

    const brandBox = await page.locator(".sidebar-brand").boundingBox();
    const collapseBox = await page.locator(".sidebar-collapse-btn").boundingBox();
    expect(brandBox).not.toBeNull();
    expect(collapseBox).not.toBeNull();
    expect(collapseBox.y).toBeGreaterThanOrEqual(brandBox.y - 1);
    expect(collapseBox.y + collapseBox.height).toBeLessThanOrEqual(
      brandBox.y + brandBox.height + 1,
    );

    await page.locator(".sidebar-collapse-btn").click();
    await expect(page.locator(".session-sidebar .sidebar-collapse-btn")).toBeVisible();
  });

  test("groups the session list by workspace", async ({ page }) => {
    const errors = trackPageErrors(page);

    // /capabilities advertises one registered workspace; the session list groups
    // by the welded workspace_dir (the registry path), mapping it back to the
    // workspace NAME. A bare session (a private session-<uuid> dir not in the
    // registry) lands in the catch-all "Ungrouped" bucket.
    await page.route("**/capabilities", (route) =>
      route.fulfill({
        json: {
          command_in: true,
          workspaces: [{ id: "ws-proj", name: "Project X", path: "/projects/x" }],
        },
      }),
    );
    // Two sessions share /projects/x (one group), one bare session is ungrouped.
    await page.route("**/tasks", (route) =>
      route.fulfill({
        json: [
          {
            task_id: "t-shared-1",
            status: "suspended",
            closed: false,
            last_seq: 3,
            parent_task_id: null,
            workspace_dir: "/projects/x",
          },
          {
            task_id: "t-shared-2",
            status: "running",
            closed: false,
            last_seq: 5,
            parent_task_id: null,
            workspace_dir: "/projects/x",
          },
          {
            task_id: "t-bare",
            status: "suspended",
            closed: false,
            last_seq: 1,
            parent_task_id: null,
            workspace_dir: "/base/session-deadbeef",
          },
        ],
      }),
    );
    await page.route(/\/\/[^/]+\/events$/, (route) =>
      route.fulfill({ status: 204, body: "" }),
    );

    await page.goto("/chat.html");

    // Two groups: the named workspace ("Project X") and "Ungrouped".
    const groups = page.locator(".session-group");
    await expect(groups).toHaveCount(2);
    await expect(page.locator(".session-group__name").nth(0)).toHaveText("Project X");
    await expect(page.locator(".session-group__name").nth(1)).toHaveText("Ungrouped");

    // The named group holds the two shared-workspace sessions; the bare session
    // is alone in Ungrouped.
    await expect(groups.nth(0).locator(".session-row")).toHaveCount(2);
    await expect(groups.nth(1).locator(".session-row")).toHaveCount(1);

    expect(errors).toEqual([]);
  });
});
