import { defineConfig } from "@playwright/test";

// The app itself is launched by each test (e2e/app.spec.ts), under xvfb-run.
export default defineConfig({
  testDir: "e2e",
  timeout: 240_000,
  expect: { timeout: 20_000 },
  workers: 1,
  reporter: [["list"]],
  use: { trace: "retain-on-failure" },
});
