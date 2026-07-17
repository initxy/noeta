import { expect, type Locator, type Page } from '@playwright/test'

let userCounter = 0

/** Fresh username per test: isolates each test in its own personal space. */
export function uniqueUser(prefix = 'e2e'): string {
  userCounter += 1
  return `${prefix}-${Date.now().toString(36)}-${process.pid}-${userCounter}`
}

/** Dev-login and land in the workbench (sidebar rendered, hero empty state). */
export async function login(page: Page, username: string): Promise<void> {
  await page.goto('/')
  await page.locator('#dev-username').fill(username)
  await page.getByRole('button', { name: 'Enter the workbench' }).click()
  // Landed: the session sidebar with its "New session" button is up.
  await expect(page.getByRole('button', { name: 'New session' })).toBeVisible()
}

/** The single chat composer textarea (hero or bottom — only one at a time). */
export function composer(page: Page): Locator {
  return page.locator('form textarea')
}

/** The composer's send (submit) button; replaced by "Stop" while running. */
export function sendButton(page: Page): Locator {
  return page.locator('form button[type="submit"]')
}

/** Type a message into the composer and send it. */
export async function sendMessage(page: Page, content: string): Promise<void> {
  await composer(page).fill(content)
  await sendButton(page).click()
}

/**
 * The demo chain's clarifying-question card ("Waiting for your input").
 * Note: while suspended on a question the backend emits no turn_finished, so
 * the UI intentionally stays in the running state (Stop button shown).
 */
export function questionCard(page: Page): Locator {
  return page.getByText('Waiting for your input')
}

/** Answer the pending audience question with the given choice label. */
export async function answerQuestion(page: Page, choiceLabel: string): Promise<void> {
  await page.getByRole('button', { name: choiceLabel, exact: true }).click()
  await page.getByRole('button', { name: 'Submit and continue' }).click()
}

/** Wait until the turn is over: the Stop button gives way to the send button. */
export async function expectIdle(page: Page): Promise<void> {
  await expect(page.getByRole('button', { name: 'Stop' })).toBeHidden()
  await expect(sendButton(page)).toBeVisible()
  await expect(composer(page)).toHaveAttribute('placeholder', 'Describe your task…')
}

/**
 * Run the full first-turn demo chain: send the first message, answer the
 * clarifying question with a freeform text, and wait for the final assistant
 * summary. Leaves the session idle.
 */
export async function completeFirstTurn(
  page: Page,
  firstMessage: string,
  freeformAnswer = 'engineers',
): Promise<void> {
  await sendMessage(page, firstMessage)
  await expect(questionCard(page)).toBeVisible()
  await page.getByPlaceholder('Or type a custom answer…').fill(freeformAnswer)
  await page.getByRole('button', { name: 'Submit and continue' }).click()
  await expect(page.getByText(/Sandbox not enabled, skipping the file write/)).toBeVisible()
  await expectIdle(page)
}

/** The conversation's scrollable container (aria-live region). */
export function conversationScroller(page: Page): Locator {
  return page.locator('div[aria-live="polite"]')
}

/** A session row button in the sidebar's session list. */
export function sessionRow(page: Page, title: string | RegExp): Locator {
  return page
    .getByRole('navigation', { name: 'Session list' })
    .locator('li')
    .filter({ hasText: title })
}

/** Delete a session through the sidebar UI: hover row → trash → Confirm. */
export async function deleteSessionViaSidebar(
  page: Page,
  title: string | RegExp,
): Promise<void> {
  const row = sessionRow(page, title)
  await row.hover()
  await row.getByRole('button', { name: 'Delete session' }).click()
  await row.getByRole('button', { name: 'Confirm' }).click()
}
