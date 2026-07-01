import { expect, test } from "@playwright/test";

// U12 — transient notices render as a dismissable toast stack (was an inline
// Banner). P1/P2 acceptance: the stack overlays ABOVE the composer as an absolute
// element (no in-flow height), so it never shifts the composer on the landing
// hero, never overlaps it in an active chat, and a tall toast never pushes the
// composer out of the viewport.

function trackPageErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

// A fake EventSource that never fires onopen, so the "Loading session..." notice
// toast (shown by selectTask, cleared on SSE open) stays up while we measure.
async function stubEventSource(page) {
  await page.addInitScript(() => {
    class FakeEventSource {
      constructor(url) {
        this.url = url;
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
      }
      close() {}
    }
    window.EventSource = FakeEventSource;
  });
}

const ONE_SESSION = [
  {
    task_id: "t1",
    title: "Active session",
    status: "suspended",
    closed: false,
    last_seq: 0,
    parent_task_id: null,
    workspace_dir: "",
  },
];

test("a failed task list shows an error toast that the × dismisses", async ({ page }) => {
  const errors = trackPageErrors(page);
  await page.route("**/capabilities", (route) =>
    route.fulfill({ json: { command_in: true } }),
  );
  // /tasks network-fails (throws) → loadTaskList catch → showNotice("error", …).
  await page.route("**/tasks", (route) => route.abort());

  await page.goto("/chat.html");

  const toast = page.locator(".toast.toast--error");
  await expect(toast).toBeVisible();
  await expect(toast).toContainText("Failed to load session list");
  // error toasts do NOT auto-dismiss — still there after a beat.
  await page.waitForTimeout(300);
  await expect(toast).toBeVisible();
  // the × dismisses it.
  await toast.locator(".toast__close").click();
  await expect(page.locator(".toast.toast--error")).toHaveCount(0);

  expect(errors).toEqual([]);
});

test("landing: a toast overlays the composer without shifting it (displacement 0)", async ({
  page,
}) => {
  const errors = trackPageErrors(page);
  await page.route("**/capabilities", (route) =>
    route.fulfill({ json: { command_in: true } }),
  );
  await page.route("**/tasks", (route) => route.fulfill({ json: [] }));
  // A long error body → a tall, wrapping toast (stands in for "many / long toasts").
  await page.route("**/workspaces", (route) =>
    route.fulfill({ status: 400, json: { error: "x".repeat(600) } }),
  );

  await page.goto("/chat.html");
  await expect(page.locator(".chat-hero")).toBeVisible();
  const before = await page.locator(".composer-block").boundingBox();

  // trigger a toast that stays on the landing screen (New project → 400).
  await page.locator(".new-project-btn").click();
  await page.locator(".new-project-input").first().fill("relative/path");
  await page.locator(".new-project-form__add").click();
  await expect(page.locator(".toast.toast--error")).toBeVisible();

  const after = await page.locator(".composer-block").boundingBox();
  const shift = after.y - before.y;
  expect(Math.abs(shift), `composer shifted by ${shift}px on landing`).toBeLessThanOrEqual(0.5);
  // even a tall toast keeps the composer fully within the viewport.
  const vh = page.viewportSize().height;
  expect(after.y + after.height, "composer bottom in viewport").toBeLessThanOrEqual(vh);

  expect(errors).toEqual([]);
});

test("active chat: a toast sits ABOVE the composer, never overlapping it", async ({
  page,
}) => {
  const errors = trackPageErrors(page);
  await stubEventSource(page);
  await page.route("**/capabilities", (route) =>
    route.fulfill({ json: { command_in: true } }),
  );
  await page.route("**/tasks", (route) => route.fulfill({ json: ONE_SESSION }));

  await page.goto("/chat.html");
  await page.getByText("Active session").click();

  const toast = page.locator(".toast").first();
  await expect(toast).toBeVisible();

  const t = await toast.boundingBox();
  const c = await page.locator(".composer-block").boundingBox();
  expect(
    t.y + t.height,
    `toast bottom (${t.y + t.height}) must be <= composer top (${c.y})`,
  ).toBeLessThanOrEqual(c.y + 1);
  // composer stays fully within the viewport.
  expect(c.y + c.height).toBeLessThanOrEqual(page.viewportSize().height);

  expect(errors).toEqual([]);
});

test("no toast renders on a clean load", async ({ page }) => {
  const errors = trackPageErrors(page);
  await page.route("**/capabilities", (route) =>
    route.fulfill({ json: { command_in: true } }),
  );
  await page.route("**/tasks", (route) => route.fulfill({ json: [] }));

  await page.goto("/chat.html");
  await expect(page.locator(".chat-hero")).toBeVisible();
  await expect(page.locator(".toast")).toHaveCount(0);

  expect(errors).toEqual([]);
});
