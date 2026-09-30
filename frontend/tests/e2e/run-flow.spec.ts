import { expect, test } from "@playwright/test";
import { installMockApi } from "./mock-api";

test.describe("run flow", () => {
  test("ticker → confirm (edited) → progress → result with downloads", async ({ page }) => {
    const { requests } = await installMockApi(page);

    // Signed in via the dev auth bypass (no Cognito app client configured for e2e).
    await page.goto("/");
    await expect(page.getByText("Dev auth bypass")).toBeVisible();
    await expect(page.getByRole("heading", { name: "Value a US-listed company" })).toBeVisible();

    await page.getByLabel("Ticker").fill("aapl");
    await page.getByRole("button", { name: "Value it" }).click();

    await expect(page).toHaveURL(/\/runs\/[0-9a-f-]+$/);
    await expect(page.getByTestId("looking-up")).toHaveText("Looking up AAPL…");

    // awaiting_confirm
    await expect(page.getByTestId("recommended-model")).toHaveText("FCFF (unlevered DCF)");
    await expect(page.getByTestId("model-confidence")).toHaveText("92% confidence");
    await expect(page.getByTestId("assumption-value-revenue_growth_y1")).toHaveText("6.2%");
    await expect(page.getByRole("button", { name: "Build model" })).toBeEnabled();

    // Flip to editable and change one assumption (entered as a percent, stored as a decimal).
    await page.getByRole("switch", { name: /Editable/ }).click();
    await page.getByLabel("Revenue growth (Y1)").fill("7.5");
    await expect(page.getByText("Edited — this result will be private to you.")).toBeVisible();
    await page.getByRole("button", { name: "Build model" }).click();

    // building: full-screen progress overlay, rest of the app inert.
    const overlay = page.getByTestId("progress-overlay");
    await expect(overlay).toBeVisible();
    await expect(page.getByTestId("app-shell")).toHaveAttribute("inert", "");
    await expect(overlay.getByRole("button", { name: "Cancel" })).toBeVisible();

    // complete
    await expect(page.getByTestId("result-view")).toBeVisible({ timeout: 15_000 });
    await expect(overlay).toBeHidden();
    await expect(page.getByTestId("app-shell")).not.toHaveAttribute("inert", "");
    await expect(page.getByTestId("value-per-share")).toHaveText("$212.40");
    await expect(page.getByTestId("live-price")).toHaveText("$226.35");
    await expect(page.getByTestId("upside")).toHaveText("−6.2%");
    await expect(page.getByTestId("download-xlsx")).toHaveAttribute("href", /\.xlsx$/);
    await expect(page.getByTestId("download-pdf")).toHaveAttribute("href", /\.pdf$/);
    await expect(page.getByTestId("sensitivity-table")).toBeVisible();
    await expect(page.getByTestId("bridge-chart")).toBeVisible();
    await expect(page.getByTestId("scenario-chart")).toBeVisible();

    // Contract checks on what the frontend sent.
    const create = requests.find((r) => r.method === "POST" && r.url.endsWith("/api/runs"));
    expect(create?.body).toEqual({ ticker: "AAPL", mode: "auto" });
    expect(create?.authorization).toBe("Bearer dev-bypass-token");

    const confirm = requests.find((r) => r.url.endsWith("/confirm"));
    const body = confirm?.body as { edited: boolean; model_type_override: null; assumptions: Record<string, { value: number }> };
    expect(body.edited).toBe(true);
    expect(body.model_type_override).toBeNull();
    expect(body.assumptions.revenue_growth_y1.value).toBeCloseTo(0.075, 10);
    expect(body.assumptions.tax_rate.value).toBe(0.16);

    const events = requests.find((r) => r.url.includes("/events"));
    expect(events?.url).toContain("access_token=dev-bypass-token");
  });

  test("unedited confirm sends no assumptions; cancel lands on the cancelled state", async ({ page }) => {
    const { requests } = await installMockApi(page, { buildStepMs: 2_000 });
    await page.goto("/");
    await page.getByLabel("Ticker").fill("MSFT");
    await page.getByRole("button", { name: "Value it" }).click();
    await page.getByRole("button", { name: "Build model" }).click();

    const overlay = page.getByTestId("progress-overlay");
    await expect(overlay).toBeVisible();
    await overlay.getByRole("button", { name: "Cancel" }).click();

    await expect(page.getByTestId("terminal-state")).toContainText("Cancelled", { timeout: 15_000 });
    await expect(page.getByTestId("app-shell")).not.toHaveAttribute("inert", "");

    const confirm = requests.find((r) => r.url.endsWith("/confirm"));
    expect(confirm?.body).toEqual({ model_type_override: null, assumptions: null, edited: false });
    expect(requests.some((r) => r.method === "POST" && r.url.endsWith("/cancel"))).toBe(true);

    await page.getByRole("button", { name: "Start over" }).click();
    await expect(page.getByLabel("Ticker")).toBeEnabled();
  });
});
