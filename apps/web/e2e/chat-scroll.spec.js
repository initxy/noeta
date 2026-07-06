import { expect, test } from "@playwright/test";

// WS-A scroll regression. The auto-stick-to-bottom logic in ConversationContent
// (conversation.jsx) is the one part of WS-A that unit tests cannot reach (jsdom
// has no layout — scrollHeight/scrollTop are always 0), and the rAF coalescing
// added in WS-A is exactly the kind of change that could silently break it. This
// spec drives the real chromium layout through a CONTROLLABLE fake EventSource
// (the thin backend's single GET /stream?task=<id> has no mock harness yet — see
// the note in transcript.spec.js), so the test can push canonical envelopes on
// demand and assert the three stick behaviours:
//   1. opening a session lands at the bottom (latest message visible);
//   2. once the user scrolls up, a streamed append does NOT yank them back down;
//   3. switching sessions re-sticks to the bottom of the new conversation.

function trackPageErrors(page) {
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  return errors;
}

const CAPS = { command_in: true, workspaces: [{ id: "ws", name: "Proj", path: "/p" }] };

function sessionRows() {
  return [
    {
      task_id: "t1",
      title: "First session",
      status: "suspended",
      closed: false,
      last_seq: 0,
      parent_task_id: null,
      workspace_dir: "/p",
    },
    {
      task_id: "t2",
      title: "Second session",
      status: "suspended",
      closed: false,
      last_seq: 0,
      parent_task_id: null,
      workspace_dir: "/p",
    },
  ];
}

// A canonical assistant message blob (what GET /content/{hash} returns), made
// tall enough to overflow the conversation viewport so there is something to
// scroll. The marker text lets the test wait for a specific body to land.
function assistantBlob(marker) {
  const lines = Array.from({ length: 80 }, (_, i) => `${marker} paragraph ${i}`).join(
    "\n\n",
  );
  return JSON.stringify([
    {
      __canonical_tag__: "message",
      role: "assistant",
      content: [{ __canonical_tag__: "text_block", text: lines }],
    },
  ]);
}

// Replace window.EventSource with a fake the test can drive. The app opens one
// per conversation in chat-data.startStream; we keep the latest instance so the
// test can fire its onopen / onmessage handlers from page.evaluate.
async function installFakeSse(page) {
  await page.addInitScript(() => {
    window.__sse = { last: null };
    class FakeEventSource {
      constructor(url) {
        this.url = url;
        this.onopen = null;
        this.onmessage = null;
        this.onerror = null;
        this.readyState = 0;
        this._listeners = {};
        window.__sse.last = this;
      }
      // The app registers named-frame listeners (e.g. the token-streaming
      // "delta" channel) via addEventListener; the stub must carry the real
      // EventSource surface or stream setup throws.
      addEventListener(type, listener) {
        this._listeners[type] = listener;
      }
      removeEventListener(type) {
        delete this._listeners[type];
      }
      close() {
        this.readyState = 2;
      }
    }
    window.EventSource = FakeEventSource;
    window.__sseOpen = () => window.__sse.last && window.__sse.last.onopen?.({});
    window.__sseSend = (env) =>
      window.__sse.last && window.__sse.last.onmessage?.({ data: JSON.stringify(env) });
    window.__sseDelta = (frame) =>
      window.__sse.last &&
      window.__sse.last._listeners.delta?.({ data: JSON.stringify(frame) });
  });
}

// Fold a fresh conversation: lifecycle + one tall assistant message turn whose
// body derefs to `marker`.
async function streamTallConversation(page, taskId, hash, marker) {
  await page.evaluate(() => window.__sseOpen());
  for (const env of [
    { task_id: taskId, seq: 0, type: "TaskCreated", payload: {}, occurred_at: 1 },
    { task_id: taskId, seq: 1, type: "TaskStarted", payload: {}, occurred_at: 2 },
    {
      task_id: taskId,
      seq: 2,
      type: "MessagesAppended",
      payload: { count: 1, messages_ref: { hash } },
      occurred_at: 3,
    },
  ]) {
    await page.evaluate((e) => window.__sseSend(e), env);
  }
  // Wait for the deref'd body to render.
  await expect(page.locator(".ai-conversation-content")).toContainText(
    `${marker} paragraph 0`,
  );
}

function scrollMetrics(page) {
  return page.evaluate(() => {
    const node = document.querySelector(".ai-conversation-content");
    if (!node) return null;
    return {
      scrollTop: Math.round(node.scrollTop),
      distanceFromBottom: Math.round(
        node.scrollHeight - node.scrollTop - node.clientHeight,
      ),
      scrollable: node.scrollHeight - node.clientHeight > 50,
      visibility: getComputedStyle(node).visibility,
    };
  });
}

test("conversation sticks to bottom, respects scroll-up, re-sticks on switch", async ({
  page,
}) => {
  const errors = trackPageErrors(page);
  await installFakeSse(page);
  await page.route("**/capabilities", (route) => route.fulfill({ json: CAPS }));
  await page.route("**/tasks", (route) => route.fulfill({ json: sessionRows() }));
  await page.route(/\/content\/h1\b.*/, (route) =>
    route.fulfill({ body: assistantBlob("ALPHA"), contentType: "application/json" }),
  );
  await page.route(/\/content\/h1b\b.*/, (route) =>
    route.fulfill({ body: assistantBlob("BETA"), contentType: "application/json" }),
  );
  await page.route(/\/content\/h2\b.*/, (route) =>
    route.fulfill({ body: assistantBlob("GAMMA"), contentType: "application/json" }),
  );

  await page.goto("/chat.html");

  // 1) Open session t1 → lands at the bottom once history is laid out + revealed.
  await page.getByText("First session").click();
  await streamTallConversation(page, "t1", "h1", "ALPHA");

  await expect
    .poll(async () => (await scrollMetrics(page))?.visibility)
    .toBe("visible");
  await expect
    .poll(async () => (await scrollMetrics(page))?.scrollable)
    .toBe(true);
  await expect
    .poll(async () => (await scrollMetrics(page))?.distanceFromBottom, {
      message: "opening a session should land at the bottom",
    })
    .toBeLessThanOrEqual(180);

  // 2) Scroll up → a streamed append must NOT yank the view back to the bottom.
  await page.evaluate(() => {
    const node = document.querySelector(".ai-conversation-content");
    node.scrollTop = 0;
    node.dispatchEvent(new Event("scroll"));
  });
  await page.evaluate((e) => window.__sseSend(e), {
    task_id: "t1",
    seq: 3,
    type: "MessagesAppended",
    payload: { count: 1, messages_ref: { hash: "h1b" } },
    occurred_at: 4,
  });
  await expect(page.locator(".ai-conversation-content")).toContainText(
    "BETA paragraph 0",
  );
  // Give the MutationObserver's rAF a couple of frames to (not) fire.
  await page.waitForTimeout(120);
  await expect
    .poll(async () => (await scrollMetrics(page))?.scrollTop, {
      message: "a streamed append must not pull a scrolled-up reader to the bottom",
    })
    .toBeLessThanOrEqual(60);

  // 3) Switch to t2 → the new conversation re-sticks to its own bottom.
  await page.getByText("Second session").click();
  await streamTallConversation(page, "t2", "h2", "GAMMA");
  await expect
    .poll(async () => (await scrollMetrics(page))?.visibility)
    .toBe("visible");
  await expect
    .poll(async () => (await scrollMetrics(page))?.distanceFromBottom, {
      message: "switching sessions should land at the new conversation's bottom",
    })
    .toBeLessThanOrEqual(180);

  expect(errors).toEqual([]);
});
