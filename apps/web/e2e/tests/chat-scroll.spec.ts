import { expect, test } from '@playwright/test'
import {
  completeFirstTurn,
  conversationScroller,
  login,
  sendMessage,
  uniqueUser,
} from './helpers'

/** Distance from the bottom of the scroll container, in px. */
async function distanceFromBottom(page: import('@playwright/test').Page): Promise<number> {
  return conversationScroller(page).evaluate(
    (el) => el.scrollHeight - el.scrollTop - el.clientHeight,
  )
}

/**
 * Follow-scroll behavior of the conversation (Conversation.tsx): while
 * "follow" is on, new SSE-driven items pin the view to the bottom; scrolling
 * up switches follow off (no auto-scroll on new events) and surfaces the
 * "Back to bottom" button, which restores follow.
 */
test('autoscroll follows new events; scrolling up disables follow', async ({ page }) => {
  await login(page, uniqueUser('scroll'))

  // Build a conversation tall enough to scroll: the demo chain first, then a
  // few multi-line echo turns (the mock answers later turns briefly).
  await completeFirstTurn(page, 'scroll seed message')
  const filler = (i: number) =>
    `scroll filler ${i}\n` +
    Array.from({ length: 12 }, (_, k) => `filler line ${k + 1}`).join('\n')
  for (const i of [1, 2, 3]) {
    await sendMessage(page, filler(i))
    await expect(
      page.getByText(new RegExp(`Received your message: "scroll filler ${i}`)),
    ).toBeVisible()
  }

  const convo = conversationScroller(page)
  // The conversation actually overflows (otherwise this test asserts nothing).
  const overflow = await convo.evaluate((el) => el.scrollHeight - el.clientHeight)
  expect(overflow).toBeGreaterThan(200)

  // Follow is on: the last reply pinned the view to the bottom.
  await expect.poll(() => distanceFromBottom(page)).toBeLessThan(80)
  await expect(page.getByRole('button', { name: /Back to bottom/ })).toBeHidden()

  // Scroll to the top: follow switches off and the return button appears.
  await convo.evaluate((el) => {
    el.scrollTop = 0
  })
  await expect(page.getByRole('button', { name: /Back to bottom/ })).toBeVisible()

  // New events must NOT drag the view down while follow is off.
  await sendMessage(page, 'sent while scrolled up')
  await expect(
    page.getByText(/Received your message: "sent while scrolled up/),
  ).toBeVisible() // rendered below the fold; Playwright visibility ignores scroll position
  const scrollTopAfter = await convo.evaluate((el) => el.scrollTop)
  expect(scrollTopAfter).toBeLessThan(100)
  await expect(page.getByRole('button', { name: /Back to bottom/ })).toBeVisible()

  // "Back to bottom" restores follow and pins the view to the bottom again.
  await page.getByRole('button', { name: /Back to bottom/ }).click()
  await expect(page.getByRole('button', { name: /Back to bottom/ })).toBeHidden()
  await expect.poll(() => distanceFromBottom(page)).toBeLessThan(80)
})
