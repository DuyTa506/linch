import { existsSync } from "node:fs";
import { resolve } from "node:path";

import { defineConfig } from "@playwright/test";

/**
 * Drives the real `linch-studio serve` against the built SPA in
 * `src/linch_studio/static/`, so these flows exercise the actual FastAPI
 * routes and digest handling rather than a mocked API.
 *
 * The workspace is a throwaway directory under test-results/; `serve` creates
 * it on boot, and each spec creates the projects it needs.
 */
const PORT = Number(process.env.LINCH_STUDIO_PORT ?? "8799");

// Prefer an explicit command, then the repository's standard virtualenv, then
// PATH. GitHub Actions uses PATH after its install step; local contributors can
// simply run `npm run e2e` without first activating .venv.
const localBin = resolve(process.cwd(), "../.venv/bin/linch-studio");
const BIN = process.env.LINCH_STUDIO_BIN ?? (existsSync(localBin) ? localBin : "linch-studio");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  reporter: process.env.CI ? "line" : "list",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "retain-on-failure",
  },
  webServer: {
    command: `${BIN} serve --workspace ./test-results/e2e-workspace --port ${PORT}`,
    url: `http://127.0.0.1:${PORT}/api/v1`,
    reuseExistingServer: false,
    stdout: "ignore",
    stderr: "pipe",
  },
});
