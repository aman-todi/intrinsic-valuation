import { defineConfig, devices } from "@playwright/test";

const PORT = Number(process.env.E2E_PORT ?? 3100);

/**
 * E2E runs against a production build (`next build && next start`). The API is fully mocked
 * with Playwright route interception (tests/e2e/mock-api.ts) — the app is built with an empty
 * NEXT_PUBLIC_API_BASE_URL so API calls are same-origin (/api/...) and intercepted before they
 * reach Next, and with NEXT_PUBLIC_COGNITO_CLIENT_ID unset so the dev auth bypass signs us in.
 */
export default defineConfig({
  testDir: "./tests/e2e",
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : "list",
  timeout: 45_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: `http://localhost:${PORT}`,
    trace: "retain-on-failure",
    serviceWorkers: "block",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `npm run build && npx next start -p ${PORT}`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: !process.env.CI,
    timeout: 240_000,
    env: {
      NEXT_PUBLIC_API_BASE_URL: "",
      NEXT_PUBLIC_COGNITO_DOMAIN: "",
      NEXT_PUBLIC_COGNITO_CLIENT_ID: "",
      NEXT_PUBLIC_COGNITO_USER_POOL_ID: "",
      NEXT_PUBLIC_COGNITO_REGION: "",
      NEXT_PUBLIC_API_MOCKING: "",
      NEXT_TELEMETRY_DISABLED: "1",
    },
  },
});
