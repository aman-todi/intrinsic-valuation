/**
 * Fixture data for the mock API (dev:mock, Vitest, Playwright).
 * Shapes mirror backend/app/schemas/{run,valuation_result,assumptions}.py.
 *
 * Relative imports only (no "@/") so Playwright can load this file directly.
 */
import type {
  AssumptionField,
  FlatAssumptions,
  JsonSchema,
  RunOut,
  RunResultOut,
  SensitivityCell,
  SotpAssumptions,
  ValuationResult,
} from "../lib/types";

const f = (value: number, rationale: string, source: AssumptionField["source"]): AssumptionField => ({
  value,
  rationale,
  source,
});

/** A realistic FCFF proposal for a large-cap consumer-hardware company. */
export const FCFF_ASSUMPTIONS: FlatAssumptions = {
  revenue_growth_y1: f(0.062, "Consensus-like near-term growth; services mix offsets flat hardware units.", "historical_trend"),
  revenue_growth_y2: f(0.058, "Gradual deceleration toward the 5y historical median of 5.4%.", "historical_trend"),
  revenue_growth_y3: f(0.052, "Continues fading toward mature-industry growth.", "analyst_like_judgment"),
  revenue_growth_y4: f(0.046, "Fade toward terminal growth.", "analyst_like_judgment"),
  revenue_growth_y5: f(0.04, "Converges near nominal GDP growth by year 5.", "analyst_like_judgment"),
  target_operating_margin: f(0.31, "5y median EBIT margin of 30.2%, modest services-driven expansion.", "historical_trend"),
  margin_convergence_years: f(5, "Margin reaches target over the explicit forecast period.", "analyst_like_judgment"),
  tax_rate: f(0.16, "Effective tax rate averaged 15.8% over the last five 10-Ks.", "regulatory_filing"),
  sales_to_capital_ratio: f(2.8, "Asset-light model; 5y median incremental sales/capital of 2.9x.", "historical_trend"),
  risk_free_rate: f(0.042, "10Y Treasury (FRED DGS10) as of run date.", "risk_free_rate"),
  equity_risk_premium: f(0.046, "Damodaran implied ERP, January 2026 update.", "industry_median"),
  levered_beta: f(1.12, "Damodaran computer-hardware industry beta relevered at target D/C.", "industry_median"),
  pretax_cost_of_debt: f(0.051, "AA+ rated; recent issuance spread ~90bp over Treasuries.", "regulatory_filing"),
  target_debt_to_capital: f(0.08, "Market-value D/(D+E) has averaged 6–9% since 2021.", "historical_trend"),
  terminal_growth_rate: f(0.03, "Below the risk-free rate; long-run nominal growth.", "analyst_like_judgment"),
  terminal_roic: f(0.2, "Durable competitive advantage supports ROIC well above WACC.", "analyst_like_judgment"),
  survival_probability: f(1.0, "Stable, profitable company — no distress adjustment.", "analyst_like_judgment"),
};

/** Pydantic-style JSON Schema for FCFFAssumptions (as model_json_schema() would emit it). */
export const FCFF_ASSUMPTIONS_SCHEMA: JsonSchema = {
  title: "FCFFAssumptions",
  type: "object",
  description: "Also used, with different bounds, for the early-stage-tech variant.",
  properties: Object.fromEntries(
    Object.keys(FCFF_ASSUMPTIONS).map((k) => [k, { $ref: "#/$defs/AssumptionField" }]),
  ),
  required: Object.keys(FCFF_ASSUMPTIONS),
  $defs: {
    AssumptionField: {
      title: "AssumptionField",
      type: "object",
      properties: {
        value: { type: "number", title: "Value" },
        rationale: { type: "string", title: "Rationale" },
        source: { type: "string", title: "Source" },
      },
    },
  },
};

export const SOTP_ASSUMPTIONS: SotpAssumptions = {
  segments: [
    {
      segment_name: "Aerospace",
      valuation_approach: "fcff",
      ev_ebitda_multiple: 0,
      fcff_assumptions: FCFF_ASSUMPTIONS,
    },
    {
      segment_name: "Building Automation",
      valuation_approach: "ev_ebitda_multiple",
      ev_ebitda_multiple: 14.5,
      fcff_assumptions: null,
    },
  ],
  corporate_overhead_capitalized: f(-4.2e9, "Unallocated corporate costs capitalized at 10x.", "regulatory_filing"),
  conglomerate_discount_note: "No explicit conglomerate discount applied; see consolidated FCFF cross-check.",
};

const ISO = "2026-09-28T14:00:00Z";

export function makeRun(overrides: Partial<RunOut> = {}): RunOut {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    ticker: "AAPL",
    mode: "auto",
    status: "awaiting_confirm",
    cik: "0000320193",
    company_name: "Apple Inc.",
    sic_code: "3571",
    model_type: "fcff",
    model_confidence: 0.92,
    model_reasons: [
      "Positive and stable operating cash flow over the 5-year window",
      "Non-financial operating company (SIC 3571 — electronic computers)",
      "No material, economically-distinct segments requiring SOTP",
    ],
    runner_up_model: "fcfe",
    decline_reason: null,
    historical_window_years: 5,
    window_reason: "Default 5-year window; no cyclical SIC or volatility trigger detected.",
    proposed_assumptions: FCFF_ASSUMPTIONS,
    final_assumptions: null,
    assumptions_edited: false,
    assumptions_schema: FCFF_ASSUMPTIONS_SCHEMA,
    cache_hit_available: false,
    cancel_requested: false,
    error_message: null,
    current_stage: null,
    progress_pct: 0,
    created_at: ISO,
    updated_at: ISO,
    started_at: ISO,
    finished_at: null,
    ...overrides,
  };
}

/** Classification/proposal fields are empty until the run reaches awaiting_confirm. */
export function blankClassification(): Partial<RunOut> {
  return {
    cik: null,
    company_name: null,
    sic_code: null,
    model_type: null,
    model_confidence: null,
    model_reasons: null,
    runner_up_model: null,
    historical_window_years: null,
    window_reason: null,
    proposed_assumptions: null,
    assumptions_schema: null,
  };
}

const BASE_VPS = 212.4;
const WACC = [0.075, 0.08, 0.085, 0.09, 0.095];
const TG = [0.02, 0.025, 0.03, 0.035, 0.04];

function sensitivityGrid(): SensitivityCell[] {
  const cells: SensitivityCell[] = [];
  for (const w of WACC) {
    for (const g of TG) {
      // Gordon-growth-shaped surface anchored on the base case (8.5% / 3.0%); the explicit-period
      // term makes WACC matter more than growth, as in a real DCF.
      const value = BASE_VPS * (0.55 * ((0.085 - 0.03) / (w - g)) ** 0.8 + 0.45 * (1 + 6 * (0.085 - w)));
      cells.push({
        row_label: `${(w * 100).toFixed(1)}%`,
        col_label: `${(g * 100).toFixed(1)}%`,
        value_per_share: Math.round(value * 100) / 100,
      });
    }
  }
  return cells;
}

export const VALUATION_RESULT: ValuationResult = {
  ticker: "AAPL",
  model_type: "fcff",
  run_date: "2026-09-28",
  currency: "USD",
  operating_value: 3_171_000_000_000,
  cash_and_equivalents: 65_200_000_000,
  non_operating_adjustments: [
    { label: "Marketable securities (non-current)", amount: 91_500_000_000 },
    { label: "Deferred tax asset valuation", amount: -3_800_000_000 },
  ],
  enterprise_value: 3_323_900_000_000,
  total_debt: 96_700_000_000,
  operating_lease_liability: 11_300_000_000,
  preferred_equity: 0,
  minority_interest: 0,
  pension_deficit: 0,
  equity_value: 3_215_900_000_000,
  diluted_shares: 15_141_000_000,
  value_per_share: BASE_VPS,
  market_price: 228.1,
  upside_pct: BASE_VPS / 228.1 - 1,
  implied_ev_ebitda: 22.4,
  implied_pb: null,
  implied_p_ffo: null,
  scenarios: [
    { label: "bear", value_per_share: 161.3, key_assumption_deltas: { revenue_growth_y1: -0.03, target_operating_margin: -0.03 } },
    { label: "base", value_per_share: BASE_VPS, key_assumption_deltas: {} },
    { label: "bull", value_per_share: 268.9, key_assumption_deltas: { revenue_growth_y1: 0.03, target_operating_margin: 0.02 } },
  ],
  sensitivity_grid: sensitivityGrid(),
  historical_window_years: 5,
  data_confidence_flags: [
    "Segment disclosures changed in FY2024; historical services margin partially reconstructed.",
  ],
  assumptions_used: FCFF_ASSUMPTIONS,
  sources: [
    "SEC EDGAR 10-K filed 2025-10-31 (accession 0000320193-25-000106)",
    "FRED DGS10 as of 2026-09-25",
    "Damodaran Jan 2026 industry betas and implied ERP",
    "Yahoo Finance live price",
  ],
  accession_number: "0000320193-25-000106",
  engine_version: "v1",
  projection_rows: [],
  terminal_value: 2_410_000_000_000,
  discount_rate: 0.085,
};

export function makeResultOut(runId: string, overrides: Partial<RunResultOut> = {}): RunResultOut {
  return {
    run_id: runId,
    result: VALUATION_RESULT,
    xlsx_url: `/mock-files/${runId}/AAPL_fcff.xlsx`,
    pdf_url: `/mock-files/${runId}/AAPL_fcff.pdf`,
    live_price: { price: 226.35, as_of: "2026-09-28T15:42:00Z", upside_pct: BASE_VPS / 226.35 - 1 },
    ...overrides,
  };
}

/** Stage script emitted over SSE while building. */
export const BUILD_STAGES: { stage: string; message: string; progress_pct: number }[] = [
  { stage: "fetching_financials", message: "Loading normalized financials from SEC EDGAR", progress_pct: 10 },
  { stage: "market_data", message: "Fetching live price and FRED 10Y Treasury", progress_pct: 25 },
  { stage: "valuation", message: "Running FCFF engine (5-year explicit forecast)", progress_pct: 45 },
  { stage: "sensitivity", message: "Computing WACC × terminal growth sensitivity grid", progress_pct: 60 },
  { stage: "excel", message: "Building live-formula Excel workbook", progress_pct: 75 },
  { stage: "pdf", message: "Rendering PDF report", progress_pct: 90 },
  { stage: "upload", message: "Uploading artifacts", progress_pct: 100 },
];
