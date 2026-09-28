/**
 * TypeScript mirrors of the backend API contracts.
 *
 * Source of truth:
 *   - backend/app/schemas/run.py              (RunOut, RunEventOut, RunResultOut, ActiveRunConflict, ...)
 *   - backend/app/schemas/valuation_result.py (ValuationResult)
 *   - backend/app/schemas/company.py          (ModelType)
 *   - backend/app/schemas/assumptions.py      (AssumptionField / *Assumptions)
 *
 * Field names are kept identical (snake_case) to the Pydantic models. UUIDs and
 * datetimes arrive as strings over JSON. Rates are decimals (0.042 = 4.2%);
 * money is raw USD.
 */

export const MODEL_TYPES = ["fcff", "fcfe", "excess_return", "nav_reit", "nav_ep", "sotp"] as const;
export type ModelType = (typeof MODEL_TYPES)[number];

export const MODEL_TYPE_LABELS: Record<ModelType, string> = {
  fcff: "FCFF (unlevered DCF)",
  fcfe: "FCFE (levered DCF)",
  excess_return: "Excess Return / DDM",
  nav_reit: "REIT NAV",
  nav_ep: "E&P NAV",
  sotp: "Sum of the Parts",
};

export function modelTypeLabel(model: string | null | undefined): string {
  if (!model) return "—";
  return (MODEL_TYPE_LABELS as Record<string, string>)[model] ?? model;
}

export type RunStatus =
  | "classifying"
  | "proposing"
  | "awaiting_confirm"
  | "building"
  | "complete"
  | "failed"
  | "cancelled";

export type RunMode = "auto" | "custom";

export type AssumptionSource =
  | "historical_trend"
  | "industry_median"
  | "analyst_like_judgment"
  | "risk_free_rate"
  | "regulatory_filing";

/** One numeric assumption line (AssumptionField). */
export interface AssumptionField {
  value: number;
  rationale: string;
  source: AssumptionSource | string;
}

/** The flat shape shared by every model type except SOTP. */
export type FlatAssumptions = Record<string, AssumptionField>;

export interface SotpSegmentAssumption {
  segment_name: string;
  valuation_approach: "fcff" | "ev_ebitda_multiple" | string;
  ev_ebitda_multiple: number;
  fcff_assumptions?: FlatAssumptions | null;
}

export interface SotpAssumptions {
  segments: SotpSegmentAssumption[];
  corporate_overhead_capitalized: AssumptionField;
  conglomerate_discount_note: string;
}

/** Whatever `proposed_assumptions` / `final_assumptions` holds (dict[str, Any] on the backend). */
export type AssumptionsPayload = Record<string, unknown>;

/** JSON Schema (as produced by Pydantic's model_json_schema()). Only the bits the UI reads are typed. */
export interface JsonSchema {
  title?: string;
  description?: string;
  type?: string;
  properties?: Record<string, JsonSchema & { $ref?: string }>;
  required?: string[];
  $defs?: Record<string, JsonSchema>;
  [key: string]: unknown;
}

export interface RunOut {
  id: string;
  ticker: string;
  mode: RunMode;
  status: RunStatus;

  cik: string | null;
  company_name: string | null;
  sic_code: string | null;
  model_type: ModelType | null;
  model_confidence: number | null;
  model_reasons: string[] | null;
  runner_up_model: ModelType | null;
  decline_reason: string | null;
  historical_window_years: number | null;
  window_reason: string | null;

  proposed_assumptions: AssumptionsPayload | null;
  final_assumptions: AssumptionsPayload | null;
  assumptions_edited: boolean;
  assumptions_schema: JsonSchema | null;
  cache_hit_available: boolean;

  cancel_requested: boolean;
  error_message: string | null;
  current_stage: string | null;
  progress_pct: number;

  created_at: string;
  updated_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RunEventOut {
  id: number;
  run_id: string;
  ts: string;
  stage: string;
  message: string;
  progress_pct: number | null;
}

export interface CreateRunRequest {
  ticker: string;
  mode?: RunMode;
}

export interface ConfirmRunRequest {
  model_type_override: ModelType | null;
  assumptions: AssumptionsPayload | null;
  edited: boolean;
}

/** Body of the 409 returned by POST /api/runs when the user already has an active run. */
export interface ActiveRunConflict {
  detail: string;
  active_run_id: string;
}

export interface NonOperatingAdjustment {
  label: string;
  amount: number;
}

export interface ScenarioResult {
  label: string; // "base" | "bull" | "bear"
  value_per_share: number;
  key_assumption_deltas: Record<string, number>;
}

export interface SensitivityCell {
  row_label: string;
  col_label: string;
  value_per_share: number;
}

export interface ValuationResult {
  ticker: string;
  model_type: string;
  run_date: string;
  currency: string;

  operating_value: number;
  cash_and_equivalents: number;
  non_operating_adjustments: NonOperatingAdjustment[];
  enterprise_value: number;
  total_debt: number;
  operating_lease_liability: number;
  preferred_equity: number;
  minority_interest: number;
  pension_deficit: number;
  equity_value: number;

  diluted_shares: number;
  value_per_share: number;
  market_price: number;
  upside_pct: number;

  implied_ev_ebitda: number | null;
  implied_pb: number | null;
  implied_p_ffo: number | null;

  scenarios: ScenarioResult[];
  sensitivity_grid: SensitivityCell[];

  historical_window_years: number;
  data_confidence_flags: string[];
  assumptions_used: AssumptionsPayload;
  sources: string[];

  accession_number: string;
  engine_version: string;

  projection_rows?: Record<string, number>[];
  terminal_value?: number | null;
  discount_rate?: number | null;
}

export interface LivePrice {
  price: number;
  as_of: string;
  upside_pct: number;
}

export interface RunResultOut {
  run_id: string;
  result: ValuationResult;
  xlsx_url: string | null;
  pdf_url: string | null;
  live_price: LivePrice | null;
}
