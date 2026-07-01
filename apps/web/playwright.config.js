import { defineConfig, devices } from "@playwright/test";

// Browserless pytest smoke (tests/test_spa_console.py) cannot click or render;
// these specs are the in-browser pass it defers to. They build the app and
// drive the real bundle against mocked backend endpoints.
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: "list",
  use: {
    baseURL: "http://127.0.0.1:4173",
    trace: "on-first-retry",
  },
  projects: [
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: {
    command: "npm run build && npm run preview -- --port 4173 --strictPort",
    url: "http://127.0.0.1:4173/chat.html",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
