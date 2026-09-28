/**
 * Client-side mirror of the simplest rules in backend/app/assumptions/bounds.py (§5.6).
 * UX sugar only: the server re-validates everything on POST /confirm.
 *
 * Returns human-readable warnings keyed by field name. Works on any flat
 * {field: {value, ...}} payload; rules whose fields are absent are skipped.
 */
import { formatAssumptionValue } from "./format";
import type { AssumptionsPayload } from "./types";

function num(payload: AssumptionsPayload, key: string): number | undefined {
  const f = payload[key];
  if (f && typeof f === "object" && "value" in f) {
    const v = (f as { value: unknown }).value;
    return typeof v === "number" && Number.isFinite(v) ? v : undefined;
  }
  return undefined;
}

const pct = (key: string, v: number) => formatAssumptionValue(key, v);

export function checkBounds(payload: AssumptionsPayload): Record<string, string> {
  const w: Record<string, string> = {};

  const g = num(payload, "terminal_growth_rate");
  const rf = num(payload, "risk_free_rate");
  if (g !== undefined && rf !== undefined && g > rf) {
    w.terminal_growth_rate = `Terminal growth should not exceed the risk-free rate (${pct("risk_free_rate", rf)}).`;
  }

  const coe = num(payload, "cost_of_equity");
  if (coe !== undefined && g !== undefined && coe <= g && !w.terminal_growth_rate) {
    w.cost_of_equity = "Cost of equity must be above terminal growth.";
  }

  const tax = num(payload, "tax_rate");
  if (tax !== undefined && (tax < 0 || tax > 0.5)) {
    w.tax_rate = "Tax rate should be between 0% and 50%.";
  }

  const survival = num(payload, "survival_probability");
  if (survival !== undefined && (survival <= 0 || survival > 1)) {
    w.survival_probability = "Survival probability must be greater than 0% and at most 100%.";
  }

  const cap = num(payload, "cap_rate");
  if (cap !== undefined && (cap < 0.03 || cap > 0.12)) {
    w.cap_rate = "Cap rate should be within a plausible 3%–12% band.";
  }

  const payout = num(payload, "payout_ratio");
  if (payout !== undefined && (payout < 0 || payout > 1)) {
    w.payout_ratio = "Payout ratio should be between 0% and 100%.";
  }

  const s2c = num(payload, "sales_to_capital_ratio");
  if (s2c !== undefined && s2c <= 0) {
    w.sales_to_capital_ratio = "Sales-to-capital ratio must be positive.";
  }

  const conv = num(payload, "margin_convergence_years");
  if (conv !== undefined && conv < 0) {
    w.margin_convergence_years = "Convergence period cannot be negative.";
  }

  return w;
}
