import { describe, expect, it } from "vitest";
import { checkBounds } from "@/lib/bounds";
import {
  fieldKind,
  formatAssumptionValue,
  formatSignedPercent,
  formatUsdCompact,
  fromDisplayValue,
  humanizeFieldName,
  toDisplayValue,
} from "@/lib/format";
import { isActive, isTerminal, runRefetchInterval, shouldPoll } from "@/lib/run-status";
import { FCFF_ASSUMPTIONS } from "@/mocks/fixtures";

describe("humanizeFieldName", () => {
  it.each([
    ["revenue_growth_y1", "Revenue growth (Y1)"],
    ["roe_y3", "ROE (Y3)"],
    ["terminal_roic", "Terminal ROIC"],
    ["discount_rate_pv10", "Discount rate PV-10"],
    ["price_deck_oil_per_bbl", "Price deck oil per bbl"],
    ["net_borrowing_as_pct_reinvestment", "Net borrowing as % reinvestment"],
  ])("%s -> %s", (key, label) => {
    expect(humanizeFieldName(key)).toBe(label);
  });
});

describe("fieldKind / formatAssumptionValue", () => {
  it.each([
    ["revenue_growth_y1", 0.062, "6.2%"],
    ["tax_rate", 0.16, "16%"],
    ["risk_free_rate", 0.042, "4.2%"],
    ["target_debt_to_capital", 0.08, "8%"],
    ["payout_ratio", 0.35, "35%"],
    ["survival_probability", 1, "100%"],
    ["cap_rate", 0.0575, "5.75%"],
    ["margin_convergence_years", 5, "5 yrs"],
    ["sales_to_capital_ratio", 2.8, "2.80x"],
    ["ev_ebitda_multiple", 14.5, "14.50x"],
    ["levered_beta", 1.12, "1.12"],
    ["price_deck_oil_per_bbl", 72.5, "$72.50/bbl"],
    ["price_deck_gas_per_mcf", 3.1, "$3.10/Mcf"],
    ["liability_adjustment", -4.2e9, "−$4.2B"],
    ["corporate_overhead_capitalized", -350e6, "−$350M"],
  ])("%s = %s -> %s", (key, value, expected) => {
    expect(formatAssumptionValue(key, value)).toBe(expected);
  });

  it("classifies payout_ratio as a percent, not a multiple", () => {
    expect(fieldKind("payout_ratio")).toBe("percent");
    expect(fieldKind("sales_to_capital_ratio")).toBe("multiple");
  });

  it("round-trips percent display values without float noise", () => {
    expect(toDisplayValue("revenue_growth_y1", 0.062)).toBe(6.2);
    expect(fromDisplayValue("revenue_growth_y1", 6.2)).toBe(0.062);
    expect(toDisplayValue("levered_beta", 1.12)).toBe(1.12);
    expect(fromDisplayValue("margin_convergence_years", 7)).toBe(7);
  });
});

describe("money/percent helpers", () => {
  it("formats compact USD and signed percents", () => {
    expect(formatUsdCompact(3.2159e12)).toBe("$3.2T");
    expect(formatUsdCompact(950_000)).toBe("$950K");
    expect(formatSignedPercent(0.123)).toBe("+12.3%");
    expect(formatSignedPercent(-0.04)).toBe("−4.0%");
  });
});

describe("run-status", () => {
  it("treats classifying/proposing/building as active and awaiting_confirm as not", () => {
    expect(isActive("classifying")).toBe(true);
    expect(isActive("building")).toBe(true);
    expect(isActive("awaiting_confirm")).toBe(false);
    expect(isActive("complete")).toBe(false);
    expect(isTerminal("cancelled")).toBe(true);
    expect(isTerminal("building")).toBe(false);
  });

  it("polls only while classifying/proposing", () => {
    expect(shouldPoll("proposing")).toBe(true);
    expect(runRefetchInterval("classifying")).toBe(2000);
    expect(runRefetchInterval("awaiting_confirm")).toBe(false);
    expect(runRefetchInterval("building")).toBe(false);
  });
});

describe("checkBounds", () => {
  it("passes the fixture proposal", () => {
    expect(checkBounds(FCFF_ASSUMPTIONS)).toEqual({});
  });

  it("flags each rule", () => {
    const f = (value: number) => ({ value, rationale: "", source: "analyst_like_judgment" });
    const w = checkBounds({
      terminal_growth_rate: f(0.05),
      risk_free_rate: f(0.04),
      tax_rate: f(0.6),
      survival_probability: f(0),
      cap_rate: f(0.2),
      payout_ratio: f(1.2),
    });
    expect(Object.keys(w).sort()).toEqual(
      ["cap_rate", "payout_ratio", "survival_probability", "tax_rate", "terminal_growth_rate"].sort(),
    );
  });
});
