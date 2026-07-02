import { expect, test } from "@playwright/test";

// U2 — the composer bottom bar: submit is the only thing in the right cluster
// (model/effort chip moved left), so it is pinned and never pushed off, even on
// a narrow composer. U3 — the permission chip shows a readable label, not the raw
// API value.

function trackPageErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

async function landing(page, caps = {}) {
  await page.route("**/capabilities", (route) =>
    route.fulfill({
      json: {
        command_in: true,
        permission_modes: ["default", "acceptEdits", "bypassPermissions"],
        models: ["a-fairly-long-model-name-v2", "another-model"],
        effort_modes: ["low", "medium", "high"],
        ...caps,
      },
    }),
  );
  await page.route("**/tasks", (route) => route.fulfill({ json: [] }));
  await page.goto("/chat.html");
  await expect(page.locator(".chat-hero")).toBeVisible();
}

test("U3: the permission chip shows a readable label + tooltip", async ({ page }) => {
  const errors = trackPageErrors(page);
  await landing(page);

  const chip = page.locator(".composer-chip--access");
  await expect(chip).toContainText("Default");
  await expect(chip).toHaveAttribute("title", /ask first/);

  // the dropdown lists the readable names + hints under a section label.
  await chip.click();
  const menu = page.locator(".permission-options");
  await expect(menu.locator(".menu-section-label")).toHaveText("Access");
  await expect(menu).toContainText("Accept edits");
  await expect(menu).toContainText("Bypass");
  await expect(menu.locator(".menu-option__hint").first()).toBeVisible();

  expect(errors).toEqual([]);
});

test("U2: submit stays visible + on the bar row on a narrow composer", async ({ page }) => {
  const errors = trackPageErrors(page);
  await page.setViewportSize({ width: 760, height: 720 });
  await landing(page);

  const submit = page.locator('button[aria-label="Send"]');
  await expect(submit).toBeVisible();

  const s = await submit.boundingBox();
  const chip = await page.locator(".composer-chip--access").boundingBox();
  const composer = await page.locator(".composer-block").boundingBox();

  // submit is within the composer's right edge (not pushed off).
  expect(s.x + s.width, "submit right within composer").toBeLessThanOrEqual(
    composer.x + composer.width + 1,
  );
  // submit is on the SAME row as the left chips (not wrapped to a second row):
  // their vertical ranges overlap.
  const overlap = Math.min(s.y + s.height, chip.y + chip.height) - Math.max(s.y, chip.y);
  expect(overlap, "submit shares the bar row with the chips").toBeGreaterThan(0);

  expect(errors).toEqual([]);
});
