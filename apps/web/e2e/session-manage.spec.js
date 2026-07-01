import { expect, test } from "@playwright/test";

// In-browser coverage for the two session-management features added on top of
// the workspace-grouped sidebar:
//   1. Collapsing/expanding a workspace group hides/shows its rows.
//   2. The per-row delete button's two-step confirm fires DELETE /tasks/{id}
//      and the purged session drops out of the refreshed list.

function trackPageErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

const CAPS = {
  command_in: true,
  workspaces: [{ id: "ws-proj", name: "Project X", path: "/projects/x" }],
};

function sessionRows() {
  return [
    {
      task_id: "t-keep",
      title: "Keep session",
      status: "suspended",
      closed: false,
      last_seq: 3,
      parent_task_id: null,
      workspace_dir: "/projects/x",
    },
    {
      task_id: "t-del",
      title: "Delete session",
      status: "suspended",
      closed: false,
      last_seq: 2,
      parent_task_id: null,
      workspace_dir: "/projects/x",
    },
  ];
}

test.describe("session management", () => {
  test("collapsing a workspace group hides its rows", async ({ page }) => {
    const errors = trackPageErrors(page);
    await page.route("**/capabilities", (route) => route.fulfill({ json: CAPS }));
    await page.route("**/tasks", (route) =>
      route.fulfill({ json: sessionRows() }),
    );
    await page.route(/\/\/[^/]+\/events$/, (route) =>
      route.fulfill({ status: 204, body: "" }),
    );

    await page.goto("/chat.html");

    const group = page.locator(".session-group").first();
    await expect(group.locator(".session-row")).toHaveCount(2);

    // Collapse → rows gone; the chevron flags the collapsed state.
    await group.locator(".session-group__title").click();
    await expect(group.locator(".session-row")).toHaveCount(0);
    await expect(group.locator(".session-group__chevron.is-collapsed")).toBeVisible();

    // Expand again → rows back.
    await group.locator(".session-group__title").click();
    await expect(group.locator(".session-row")).toHaveCount(2);

    expect(errors).toEqual([]);
  });

  test("deleting a session confirms then drops it from the list", async ({ page }) => {
    const errors = trackPageErrors(page);
    const deleted = new Set();

    await page.route("**/capabilities", (route) => route.fulfill({ json: CAPS }));
    await page.route("**/tasks", (route) =>
      route.fulfill({
        json: sessionRows().filter((r) => !deleted.has(r.task_id)),
      }),
    );
    // DELETE /tasks/t-del → record the purge + answer 200, mirroring the server.
    await page.route("**/tasks/t-del", (route) => {
      if (route.request().method() === "DELETE") {
        deleted.add("t-del");
        route.fulfill({ json: { ok: true, task_id: "t-del", deleted: ["t-del"] } });
        return;
      }
      route.continue();
    });
    await page.route(/\/\/[^/]+\/events$/, (route) =>
      route.fulfill({ status: 204, body: "" }),
    );

    await page.goto("/chat.html");

    const rows = page.locator(".session-row");
    await expect(rows).toHaveCount(2);

    const target = page.locator(".session-row", { hasText: "Delete session" });
    // First click on the trash arms the two-step confirm (no request yet).
    await target.locator(".session-row__del").click();
    await expect(target).toHaveClass(/confirming/);

    // Confirming fires DELETE and the refreshed list drops the row.
    const [req] = await Promise.all([
      page.waitForRequest(
        (r) => r.url().endsWith("/tasks/t-del") && r.method() === "DELETE",
      ),
      target.locator(".session-row__del-yes").click(),
    ]);
    expect(req).toBeTruthy();

    await expect(page.locator(".session-row")).toHaveCount(1);
    await expect(page.locator(".session-row", { hasText: "Delete session" })).toHaveCount(0);
    await expect(page.locator(".session-row", { hasText: "Keep session" })).toHaveCount(1);

    expect(errors).toEqual([]);
  });

  test("cancelling the delete confirm keeps the session", async ({ page }) => {
    const errors = trackPageErrors(page);
    await page.route("**/capabilities", (route) => route.fulfill({ json: CAPS }));
    await page.route("**/tasks", (route) =>
      route.fulfill({ json: sessionRows() }),
    );
    await page.route(/\/\/[^/]+\/events$/, (route) =>
      route.fulfill({ status: 204, body: "" }),
    );

    await page.goto("/chat.html");

    const target = page.locator(".session-row", { hasText: "Delete session" });
    await target.locator(".session-row__del").click();
    await expect(target).toHaveClass(/confirming/);

    // ✗ backs out — the row stays, no longer in the confirming state.
    await target.locator(".session-row__del-no").click();
    await expect(target).not.toHaveClass(/confirming/);
    await expect(page.locator(".session-row")).toHaveCount(2);

    expect(errors).toEqual([]);
  });

  test("running sessions offer stop before delete", async ({ page }) => {
    const errors = trackPageErrors(page);
    const rows = [
      ...sessionRows(),
      {
        task_id: "t-run",
        title: "Running session",
        status: "running",
        closed: false,
        last_seq: 4,
        parent_task_id: null,
        workspace_dir: "/projects/x",
      },
    ];
    let cancelled = false;
    let deleted = false;

    await page.route("**/capabilities", (route) => route.fulfill({ json: CAPS }));
    await page.route("**/tasks", (route) => route.fulfill({ json: rows }));
    await page.route("**/tasks/t-run/cancel", (route) => {
      cancelled = true;
      route.fulfill({ json: { ok: true, task_id: "t-run" } });
    });
    await page.route("**/tasks/t-run", (route) => {
      if (route.request().method() === "DELETE") deleted = true;
      route.fulfill({ json: { ok: false, reason: "unexpected" } });
    });
    await page.route(/\/\/[^/]+\/events$/, (route) =>
      route.fulfill({ status: 204, body: "" }),
    );

    await page.goto("/chat.html");

    const target = page.locator(".session-row", { hasText: "Running session" });
    await expect(target.locator(".session-row__stop")).toHaveCount(1);
    await target.locator(".session-row__stop").click();

    expect(cancelled).toBe(true);
    expect(deleted).toBe(false);
    expect(errors).toEqual([]);
  });

});
