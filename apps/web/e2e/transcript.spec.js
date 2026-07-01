import { expect, test } from "@playwright/test";

// Exercises the composer surface (codex inline chips + working-folder picker),
// the failed-open guard, and the files-panel layout — everything reachable on
// the landing / new-session screen plus the right-dock geometry.
//
// NOTE: the session-render and trace tests that used to live here were removed.
// They modelled the pre-thin-backend REST protocol (GET /tasks/{id} detail,
// /tasks/{id}/events backfill, /tasks/{id}/context, task-scoped
// /tasks/{id}/content/{hash}) which no longer exists: the thin backend (T5/T6/T7)
// replays history over a single GET /stream?task=<id> SSE and derefs blobs from
// the global GET /content/{hash}. Reviving transcript/trace e2e coverage means
// mocking that SSE stream from scratch — a separate task, not a mock patch.

function trackPageErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

test.describe("chat composer", () => {
  test("inline composer chips drive permission, model and effort", async ({ page }) => {
    // codex bottom bar: the old settings gear + read-only tags are replaced by
    // inline chips. /capabilities advertises three permission modes (no plan), a
    // flat model list and the effort list. The access chip lists exactly those
    // modes (flipping to the amber danger style on bypassPermissions); the
    // model·effort chip sets both in one open and its label tracks the pick.
    const errors = trackPageErrors(page);

    await page.route("**/capabilities", (route) =>
      route.fulfill({
        json: {
          command_in: true,
          models: ["gpt-4o", "opus", "sonnet"],
          permission_modes: ["default", "acceptEdits", "bypassPermissions"],
          effort_modes: ["low", "medium", "high", "xhigh", "max"],
        },
      }),
    );
    // Loaded-but-empty task list keeps us on the new-session (landing) screen
    // where the composer is enabled.
    await page.route("**/tasks", (route) => route.fulfill({ json: [] }));
    await page.route(/\/\/[^/]+\/events$/, (route) =>
      route.fulfill({ status: 204, body: "" }),
    );

    await page.goto("/chat.html");

    // The legacy gear menu + read-only tags are gone.
    await expect(page.locator(".settings-trigger")).toHaveCount(0);
    await expect(page.locator(".setting-tag")).toHaveCount(0);

    // Access chip → exactly the three advertised modes (shown by their U3 readable
    // labels), no "plan".
    await page.locator(".composer-chip--access").click();
    await expect(page.locator(".permission-options .menu-option")).toHaveCount(3);
    await expect(page.locator(".permission-options .menu-option", { hasText: "plan" })).toHaveCount(0);
    await page.locator(".permission-options .menu-option", { hasText: "Accept edits" }).click();
    await expect(page.locator(".composer-chip--access")).toContainText("Accept edits");
    await expect(page.locator(".composer-chip--access.composer-chip--danger")).toHaveCount(0);

    // bypassPermissions ("Skip all approvals") flips the chip to the amber danger styling.
    await page.locator(".composer-chip--access").click();
    await page.locator(".permission-options .menu-option", { hasText: "Skip all approvals" }).click();
    await expect(page.locator(".composer-chip--access.composer-chip--danger")).toContainText("Skip all approvals");

    // Model · effort chip → one open sets both; the chip label tracks the pick.
    await page.locator(".composer-chip--model").click();
    await expect(page.locator(".model-options .menu-option")).toHaveCount(3);
    await page.locator(".model-options .menu-option", { hasText: "opus" }).click();
    await page
      .locator(".effort-options")
      .getByRole("button", { name: "high", exact: true })
      .click();
    await expect(page.locator(".composer-chip--model")).toContainText("opus high");

    expect(errors).toEqual([]);
  });

  test("submits the workspace id chosen from the working-folder chip", async ({ page }) => {
    const errors = trackPageErrors(page);
    let createBody = null;

    await page.route("**/capabilities", (route) =>
      route.fulfill({
        json: {
          command_in: true,
          permission_modes: ["default", "acceptEdits", "bypassPermissions"],
          workspaces: [
            { id: "ws-alpha", name: "alpha", path: "/projects/alpha" },
            { id: "ws-beta", name: "beta", path: "/projects/beta" },
          ],
        },
      }),
    );
    await page.route("**/tasks", async (route) => {
      const req = route.request();
      if (req.method() === "POST") {
        createBody = req.postDataJSON();
        await route.fulfill({ status: 201, json: { ok: true, task_id: "new-task" } });
        return;
      }
      await route.fulfill({ json: [] });
    });
    await page.route(/\/\/[^/]+\/events$/, (route) => route.fulfill({ status: 204, body: "" }));

    await page.goto("/chat.html");

    // The working-folder chip lists registered workspaces by NAME; pick "beta".
    await page.locator(".composer-chip--workspace").click();
    await expect(page.locator(".workspace-menu .menu-option")).toHaveCount(2);
    await page.locator(".workspace-menu .menu-option", { hasText: "beta" }).click();
    await expect(page.locator(".composer-chip--workspace .composer-chip__label")).toContainText("beta");

    await page.locator("textarea.ai-prompt-textarea").fill("work in beta");
    await page.locator('button[aria-label="Send"]').click();

    // The chosen workspace rides the opening POST /tasks as the `workspace` key
    // (the create-once binding; the backend resolves an id OR a path). The
    // slash-sniffed name/path keys are gone.
    await expect.poll(() => createBody).toMatchObject({
      goal: "work in beta",
      agent: "main",
      workspace: "ws-beta",
    });
    expect(createBody).not.toHaveProperty("workspace_id");
    expect(createBody).not.toHaveProperty("workspace_path");

    expect(errors).toEqual([]);
  });

  test("a failed open keeps the typed message in the box", async ({ page }) => {
    const errors = trackPageErrors(page);

    await page.route("**/capabilities", (route) =>
      route.fulfill({ json: { command_in: true } }),
    );
    await page.route("**/tasks", async (route) => {
      const req = route.request();
      if (req.method() === "POST") {
        await route.fulfill({
          status: 409,
          json: { ok: false, reason: "not_resumable", message: "boom" },
        });
        return;
      }
      await route.fulfill({ json: [] });
    });
    await page.route(/\/\/[^/]+\/events$/, (route) => route.fulfill({ status: 204, body: "" }));

    await page.goto("/chat.html");

    await page.locator("textarea.ai-prompt-textarea").fill("keep this message");
    await page.locator('button[aria-label="Send"]').click();

    await expect(page.locator(".toast.toast--error")).toContainText("Command failed");
    await expect(page.locator("textarea.ai-prompt-textarea")).toHaveValue("keep this message");

    expect(errors).toEqual([]);
  });

  test("lets the files panel widen after the session sidebar is collapsed", async ({ page }) => {
    const errors = trackPageErrors(page);
    const RUN = "t-files-panel-resize";

    await page.route("**/capabilities", (route) =>
      route.fulfill({ json: { command_in: true } }),
    );
    await page.route("**/tasks", (route) =>
      route.fulfill({
        json: [{ task_id: RUN, status: "running", last_seq: 1, closed: false }],
      }),
    );
    await page.route(`**/tasks/${RUN}/files`, (route) =>
      route.fulfill({ json: { files: [{ path: "src", type: "dir" }, { path: "src/app.js" }] } }),
    );
    await page.route(/\/\/[^/]+\/events$/, (route) =>
      route.fulfill({ status: 204, body: "" }),
    );

    await page.setViewportSize({ width: 1600, height: 900 });
    await page.goto(`/chat.html?task=${RUN}`);
    await page.locator(".file-panel-toggle").click();
    await expect(page.locator(".right-dock")).toBeVisible();

    const before = await page.locator(".right-dock").boundingBox();
    expect(before.width).toBeLessThanOrEqual(1000);

    await page.locator(".sidebar-collapse-btn").click();
    await page.locator(".panel-resizer").dragTo(page.locator(".chat-main"), {
      targetPosition: { x: 46, y: 40 },
    });

    const geometry = await page.evaluate(() => {
      const dock = document.querySelector(".right-dock").getBoundingClientRect();
      const chat = document.querySelector(".chat-main").getBoundingClientRect();
      const shell = document.querySelector(".app-shell");
      return {
        dockWidth: Math.round(dock.width),
        dockLeft: Math.round(dock.left),
        chatWidth: Math.round(chat.width),
        columns: getComputedStyle(shell).gridTemplateColumns,
      };
    });

    expect(geometry.dockWidth).toBeGreaterThan(1000);
    expect(geometry.chatWidth).toBeGreaterThanOrEqual(320);
    expect(geometry.columns.startsWith("44px ")).toBe(true);
    expect(errors).toEqual([]);
  });
});
