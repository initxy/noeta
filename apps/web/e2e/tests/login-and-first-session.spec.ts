import { expect, test } from '@playwright/test'
import {
  answerQuestion,
  composer,
  expectIdle,
  questionCard,
  sendButton,
  sendMessage,
  sessionRow,
  uniqueUser,
} from './helpers'

/**
 * The core happy path on the mock demo chain (see mock_llm.py):
 * dev-login → personal space → implicit session creation on first send →
 * ask_user_question card → answer → demo-skill activation → final assistant
 * summary → back to idle.
 *
 * Replaces the old approval + question/answer smoke: per-call approval no
 * longer exists in the sandbox-only product, so only the question/answer
 * interaction is covered.
 */
test('dev-login, first session, question/answer, skill activation, idle', async ({ page }) => {
  // --- Login page ---
  await page.goto('/')
  await expect(page.getByText('dev login')).toBeVisible()
  const username = uniqueUser('first')
  await page.locator('#dev-username').fill(username)
  await page.getByRole('button', { name: 'Enter the workbench' }).click()

  // --- Landed in the workbench, personal space selected, hero empty state ---
  await expect(page.getByRole('button', { name: 'New session' })).toBeVisible()
  await expect(page.getByText('Personal space')).toBeVisible()
  await expect(page.getByText(username).first()).toBeVisible() // user chip in the sidebar
  await expect(composer(page)).toHaveAttribute('placeholder', 'Describe your task…')

  // --- First message: creates the session implicitly and starts the turn ---
  await sendMessage(page, 'Write me a product report')
  // Optimistic user bubble in the conversation.
  await expect(
    page.locator('div[aria-live="polite"]').getByText('Write me a product report'),
  ).toBeVisible()
  // The session row shows up in the sidebar with the first-message title.
  await expect(sessionRow(page, 'Write me a product report')).toBeVisible()

  // --- Clarifying question card arrives over SSE ---
  await expect(questionCard(page)).toBeVisible()
  await expect(
    page.getByText('Who is the primary audience for this report?'),
  ).toBeVisible()
  await expect(page.getByRole('button', { name: 'Engineer', exact: true })).toBeVisible()
  await expect(
    page.getByRole('button', { name: 'Product manager', exact: true }),
  ).toBeVisible()
  // While suspended on a question no turn_finished is emitted: the composer
  // deliberately stays in the running state (Stop shown, textarea locked copy).
  await expect(page.getByRole('button', { name: 'Stop' })).toBeVisible()

  // --- Answer: pick a choice and submit ---
  await answerQuestion(page, 'Engineer')

  // --- The turn resumes and finishes: final assistant summary lands ---
  await expect(
    page.getByText(/Sandbox not enabled, skipping the file write/),
  ).toBeVisible()

  // --- Status returns to idle: Stop is gone, send is back ---
  await expectIdle(page)

  // --- Process rail: the answered question and the skill activation are on
  // record. The container auto-collapses shortly after the turn ends, so
  // expand it explicitly before asserting. ---
  const processToggle = page.getByRole('button', { name: /Process details/ })
  await expect(processToggle).toBeVisible()
  if ((await processToggle.getAttribute('aria-expanded')) !== 'true') {
    await processToggle.click()
  }
  await expect(page.getByText('demo-skill')).toBeVisible()
  await expect(page.getByText('Answered', { exact: true })).toBeVisible()

  // Sending is possible again on the same session (second turn is a brief echo).
  await sendMessage(page, 'thanks, looks good')
  await expect(
    page.getByText(/Received your message: "thanks, looks good/),
  ).toBeVisible()
  await expectIdle(page)
  await expect(sendButton(page)).toBeVisible()
})
