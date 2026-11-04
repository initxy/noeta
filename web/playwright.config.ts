import { defineConfig } from "@playwright/test";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

// A real `noeta serve` on the offline scripted provider (e2e/provider), in a
// throwaway home and working directory, serving the built bundle (dist/).
const PORT = Number(process.env.NOETA_E2E_PORT ?? 8765);
export const TOKEN = "e2e-token";
const scratch = process.env.NOETA_E2E_DIR ?? mkdtempSync(join(tmpdir(), "noeta-web-e2e-"));
process.env.NOETA_E2E_DIR = scratch; // the workers re-read this config: the same directory
const work = join(scratch, "work");
const repo = resolve(import.meta.dirname, "..");

export default defineConfig({
  testDir: "e2e",
  timeout: 60_000,
  expect: { timeout: 15_000 },
  workers: 1,
  reporter: [["list"]],
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    viewport: { width: 1280, height: 800 },
    trace: "retain-on-failure",
  },
  webServer: {
    command:
      `mkdir -p ${work} ${join(scratch, "home")} && cd ${work} && ` +
      `uv run --project ${repo} --all-extras --with-editable ${join(repo, "web/e2e/provider")} ` +
      `noeta serve --port ${PORT} --web ${join(repo, "web/dist")}`,
    url: `http://127.0.0.1:${PORT}/`,
    timeout: 180_000,
    reuseExistingServer: false,
    stdout: "ignore",
    env: {
      NOETA_HOME: join(scratch, "home"),
      NOETA_STORE: `sqlite:///${join(scratch, "store.db")}`,
      NOETA_MODEL: "claude-sonnet-4-5",
      NOETA_PROVIDER: "web_e2e",
      NOETA_SERVER_TOKEN: TOKEN,
      NOETA_WEB_E2E_DIR: work,
    },
  },
});
