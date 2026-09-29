import { expect, test } from "@playwright/test";
import { installMockApi } from "./mock-api";

test.describe("single-run lock", () => {
  test("a building run restores the progress overlay on load and makes the app inert", async ({ page }) => {
    const { state } = await installMockApi(page);
    const run = state.seed({ status: "building", ticker: "NVDA" }, { stepMs: 60_000 });

    await page.goto("/");
    await expect(page).toHaveURL(`/runs/${run.id}`);
    await expect(page.getByTestId("progress-overlay")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Building NVDA model" })).toBeVisible();
    await expect(page.getByTestId("app-shell")).toHaveAttribute("inert", "");

    // Even an explicit "new valuation" navigation goes back to the active run.
    await page.goto("/?new=1");
    await expect(page).toHaveURL(`/runs/${run.id}`);
    await expect(page.getByTestId("progress-overlay")).toBeVisible();
  });

  test("a 409 from POST /api/runs redirects to the existing active run", async ({ page }) => {
    const { state, requests } = await installMockApi(page, { classifyMs: 60_000 });
    await page.goto("/?new=1");
    await expect(page.getByLabel("Ticker")).toBeEnabled();

    const other = state.create("AMZN"); // races us before the guard's next poll
    await page.getByLabel("Ticker").fill("AAPL");
    await page.getByRole("button", { name: "Value it" }).click();

    await expect(page).toHaveURL(`/runs/${other.id}`);
    await expect(page.getByTestId("looking-up")).toHaveText("Looking up AMZN…");
    const post = requests.filter((r) => r.method === "POST" && r.url.endsWith("/api/runs"));
    expect(post).toHaveLength(1);
  });

  test("awaiting_confirm does not lock: user can start a different ticker", async ({ page }) => {
    const { state } = await installMockApi(page);
    const waiting = state.seed({ status: "awaiting_confirm", ticker: "KO" });

    await page.goto("/");
    await expect(page).toHaveURL(`/runs/${waiting.id}`); // restored on load

    await page.goto("/?new=1");
    await expect(page.getByLabel("Ticker")).toBeEnabled();
    await expect(page.getByTestId("active-run-lock")).toHaveCount(0);
    await expect(page.getByText(/KO run is still waiting for confirmation/)).toBeVisible();

    await page.getByLabel("Ticker").fill("PEP");
    await page.getByRole("button", { name: "Value it" }).click();
    await expect(page.getByTestId("looking-up")).toHaveText("Looking up PEP…");
  });
});
