import { expect, test } from "@playwright/test";

// U5 — ApprovalPrompt redesign: human-readable summary, collapsed args, batch
// approve/deny, and a deny/batch undo window whose deferred commit is HOISTED to
// useChatData (task-scoped) so a session switch / unmount flushes it to the
// original task instead of dropping (blocker 1), and a failed commit restores the
// card (blocker 2).

function trackPageErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

async function driveableEventSource(page) {
  await page.addInitScript(() => {
    window.__sse = { last: null };
    class FakeEventSource {
      constructor(url) {
        this.url = url;
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
        window.__sse.last = this;
      }
      close() {}
    }
    window.EventSource = FakeEventSource;
    window.__sseOpen = () => window.__sse.last && window.__sse.last.onopen?.({});
    window.__sseSend = (env) =>
      window.__sse.last && window.__sse.last.onmessage?.({ data: JSON.stringify(env) });
  });
}

const session = (id, title) => ({
  task_id: id,
  title,
  status: "suspended",
  closed: false,
  last_seq: 0,
  parent_task_id: null,
  workspace_dir: "",
});

const approvalEvent = (seq, callId, tool, args) => ({
  task_id: "t1",
  seq,
  type: "ToolCallApprovalRequested",
  payload: { call_id: callId, tool_name: tool, arguments: args },
  occurred_at: seq,
});

// Opens t1 with 3 pending approvals; returns a live array recording every
// approve/deny POST so tests can assert exactly when a commit happens.
async function openChatWithApprovals(page, { denyStatus = 202, sessions = [session("t1", "Active")] } = {}) {
  const calls = [];
  await driveableEventSource(page);
  await page.route("**/capabilities", (route) =>
    route.fulfill({ json: { command_in: true } }),
  );
  await page.route("**/tasks", (route) => route.fulfill({ json: sessions }));
  await page.route(/\/tasks\/t1\/(approve|deny)$/, (route) => {
    const verb = route.request().url().endsWith("/deny") ? "deny" : "approve";
    let callId = null;
    try {
      callId = route.request().postDataJSON()?.call_id ?? null;
    } catch {
      callId = null;
    }
    calls.push({ verb, callId });
    route.fulfill({
      status: verb === "deny" ? denyStatus : 202,
      body: JSON.stringify({ message: "boom" }),
    });
  });

  await page.goto("/chat.html");
  await page.getByText("Active", { exact: true }).click();
  await page.evaluate(() => window.__sseOpen());
  for (const env of [
    { task_id: "t1", seq: 0, type: "TaskCreated", payload: {}, occurred_at: 0 },
    { task_id: "t1", seq: 1, type: "TaskStarted", payload: {}, occurred_at: 1 },
    approvalEvent(2, "c1", "edit", { path: "src/app.js", content: "a\nb\nc" }),
    approvalEvent(3, "c2", "shell_run", { command: "npm test" }),
    approvalEvent(4, "c3", "read", { path: "README.md" }),
  ]) {
    await page.evaluate((e) => window.__sseSend(e), env);
  }
  await expect(page.locator(".approval-prompt")).toHaveCount(3);
  return calls;
}

const denyCard = (page, text) =>
  page.locator(".approval-prompt", { hasText: text }).getByRole("button", { name: "Deny" });

test("approval: summary + collapsed args + batch bar render", async ({ page }) => {
  const errors = trackPageErrors(page);
  await openChatWithApprovals(page);

  const group = page.locator(".approval-group");
  await expect(group).toContainText("Edit src/app.js");
  await expect(group).toContainText("Run npm test");
  await expect(group).toContainText("Read README.md");
  await expect(group.locator(".approval-args-details").first()).toHaveJSProperty("open", false);
  await expect(group.locator(".approval-batch")).toContainText("Approve all (3)");

  expect(errors).toEqual([]);
});

test("approval: multiple single denies stack toasts without covering the composer", async ({
  page,
}) => {
  const errors = trackPageErrors(page);
  await openChatWithApprovals(page);

  await denyCard(page, "Edit").click();
  await denyCard(page, "Run npm test").click();

  await expect(page.locator(".toast")).toHaveCount(2);
  await expect(page.locator(".toast").first().locator(".toast__action")).toHaveText("Undo");
  await expect(page.locator(".approval-prompt")).toHaveCount(1);

  const stack = await page.locator(".toast-stack").boundingBox();
  const composer = await page.locator(".composer-block").boundingBox();
  expect(stack.y + stack.height).toBeLessThanOrEqual(composer.y + 1);

  expect(errors).toEqual([]);
});

test("approval: deny COMMITS a POST after the undo window", async ({ page }) => {
  const errors = trackPageErrors(page);
  const calls = await openChatWithApprovals(page);

  await denyCard(page, "Read").click();
  await page.waitForTimeout(300);
  expect(calls, "no POST during the undo window").toEqual([]);

  await page.waitForTimeout(5200);
  expect(calls, "committed after the window").toEqual([{ verb: "deny", callId: "c3" }]);

  expect(errors).toEqual([]);
});

test("approval: Undo cancels the commit (no POST) and restores the card", async ({ page }) => {
  const errors = trackPageErrors(page);
  const calls = await openChatWithApprovals(page);

  await denyCard(page, "Read").click();
  await expect(page.locator(".approval-prompt")).toHaveCount(2);
  await page.locator(".toast__action", { hasText: "Undo" }).first().click();
  await expect(page.locator(".approval-prompt")).toHaveCount(3); // restored

  await page.waitForTimeout(5300);
  expect(calls, "Undo must prevent the POST entirely").toEqual([]);

  expect(errors).toEqual([]);
});

test("approval: switching sessions FLUSHES the deferred commit to the original task", async ({
  page,
}) => {
  const errors = trackPageErrors(page);
  const calls = await openChatWithApprovals(page, {
    sessions: [session("t1", "Active"), session("t2", "Second")],
  });

  await denyCard(page, "Read").click();
  await page.waitForTimeout(150);
  expect(calls, "not yet committed").toEqual([]);

  // switch away BEFORE the 5s window elapses → flush commits it now to t1.
  await page.getByText("Second", { exact: true }).click();
  await page.waitForTimeout(300);
  expect(calls, "flushed on session switch (not dropped)").toEqual([
    { verb: "deny", callId: "c3" },
  ]);

  expect(errors).toEqual([]);
});

test("approval: a failed commit restores the card + shows an error toast", async ({ page }) => {
  const errors = trackPageErrors(page);
  await openChatWithApprovals(page, { denyStatus: 500 });

  await denyCard(page, "Read").click();
  await expect(page.locator(".approval-prompt")).toHaveCount(2);

  // after the window the commit fires, fails (500) → card comes back + error toast.
  await expect(page.locator(".approval-prompt")).toHaveCount(3, { timeout: 8000 });
  await expect(page.locator(".toast.toast--error")).toBeVisible();

  expect(errors).toEqual([]);
});
