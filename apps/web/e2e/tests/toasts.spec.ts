import { expect, test } from '@playwright/test'
import {
  deleteSessionViaSidebar,
  login,
  questionCard,
  sendMessage,
  sessionRow,
  uniqueUser,
} from './helpers'

/**
 * Toast surface (state/toast.tsx): toasts render as role=status in the
 * bottom-center stack and auto-dismiss after ~4.2s. Both paths below are real
 * product flows, not injected failures.
 */

test.use({ permissions: ['clipboard-read', 'clipboard-write'] })

test('copying the trace id shows an info toast that auto-dismisses', async ({ page }) => {
  await login(page, uniqueUser('toast'))
  // A session must exist for the top bar to show the Trace ID chip.
  await sendMessage(page, 'toast probe session')
  await expect(questionCard(page)).toBeVisible()

  await page.getByRole('button', { name: /Trace ID/ }).click()
  const toast = page.getByRole('status').filter({ hasText: 'Trace ID copied' })
  await expect(toast).toBeVisible()
  // Auto-dismiss (4.2s timer in ToastProvider).
  await expect(toast).toBeHidden({ timeout: 6_000 })
})

test('deleting an already-deleted session surfaces an error toast', async ({ page, context }) => {
  await login(page, uniqueUser('stale'))
  await sendMessage(page, 'stale delete target')
  await expect(sessionRow(page, 'stale delete target')).toBeVisible()

  // Second tab with the same login: its session list becomes stale once the
  // first tab deletes the session.
  const page2 = await context.newPage()
  await page2.goto('/')
  await expect(sessionRow(page2, 'stale delete target')).toBeVisible()

  await deleteSessionViaSidebar(page, 'stale delete target')
  await expect(sessionRow(page, 'stale delete target')).toBeHidden()

  // The stale tab tries the same delete → backend 404 → error toast.
  await deleteSessionViaSidebar(page2, 'stale delete target')
  const toast = page2.getByRole('status').filter({ hasText: 'session not found' })
  await expect(toast).toBeVisible()
  await expect(toast).toBeHidden({ timeout: 6_000 })
  await page2.close()
})
