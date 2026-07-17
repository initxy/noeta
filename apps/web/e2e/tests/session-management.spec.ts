import { expect, test } from '@playwright/test'
import {
  completeFirstTurn,
  conversationScroller,
  deleteSessionViaSidebar,
  login,
  questionCard,
  sendMessage,
  sessionRow,
  uniqueUser,
} from './helpers'

/**
 * Session management through the sidebar: create a second session, switch
 * between the two (each switch re-renders the conversation from SSE replay),
 * and delete one.
 *
 * Rename is not covered: the platform sidebar exposes no rename affordance
 * (titles come from the first message / async title generation), so the old
 * rename scenario has no UI equivalent.
 */
test('switch between sessions re-renders replay; delete removes a session', async ({ page }) => {
  await login(page, uniqueUser('mgmt'))

  // --- Session A: run the full demo chain to a finished conversation ---
  await completeFirstTurn(page, 'alpha topic message')

  // --- Session B: fresh hero, first message sent, question left pending ---
  await page.getByRole('button', { name: 'New session' }).click()
  // Back on the hero empty state (nothing persisted until the first send).
  await expect(page.getByText(/describe the task, paste your docs/)).toBeVisible()
  await sendMessage(page, 'bravo topic message')
  await expect(questionCard(page)).toBeVisible()

  // Both sessions are listed.
  await expect(sessionRow(page, 'alpha topic message')).toBeVisible()
  await expect(sessionRow(page, 'bravo topic message')).toBeVisible()

  // --- Switch to A: the conversation is rebuilt from replay ---
  await sessionRow(page, 'alpha topic message').getByRole('button').first().click()
  const convo = conversationScroller(page)
  await expect(convo.getByText('alpha topic message')).toBeVisible()
  await expect(
    convo.getByText(/Sandbox not enabled, skipping the file write/),
  ).toBeVisible()
  // B's content does not bleed into A's stream.
  await expect(convo.getByText('bravo topic message')).toBeHidden()

  // --- Switch back to B: replay restores the pending question card ---
  await sessionRow(page, 'bravo topic message').getByRole('button').first().click()
  await expect(convo.getByText('bravo topic message')).toBeVisible()
  await expect(questionCard(page)).toBeVisible()
  await expect(convo.getByText('alpha topic message')).toBeHidden()

  // --- Delete B via the sidebar (hover → trash → Confirm) ---
  await deleteSessionViaSidebar(page, 'bravo topic message')
  await expect(sessionRow(page, 'bravo topic message')).toBeHidden()
  // Selection falls back to the remaining session, whose replay renders again.
  await expect(convo.getByText('alpha topic message')).toBeVisible()
  await expect(sessionRow(page, 'alpha topic message')).toBeVisible()
})
