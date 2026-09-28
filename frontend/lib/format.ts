/**
 * Display helpers. Everything the backend sends is a decimal rate (0.042 = 4.2%)
 * or raw USD; the UI decides how to present each field purely from its name so the
 * assumptions form stays generic across model types (§10.2).
 */

export type FieldKind = "percent" | "multiple" | "beta" | "years" | "usd" | "usd_unit" | "number";

const ACRONYMS: Record<string, string> = {
  roe: "ROE",
  roic: "ROIC",
  ev: "EV",
  ebitda: "EBITDA",
  noi: "NOI",
  pv10: "PV-10",
  nav: "NAV",
  fcff: "FCFF",
  fcfe: "FCFE",
  ffo: "FFO",
  bbl: "bbl",
  mcf: "Mcf",
  pct: "%",
  sotp: "SOTP",
  usd: "USD",
};

/** `revenue_growth_y1` -> "Revenue growth (Y1)", `roe_y3` -> "ROE (Y3)". */
export function humanizeFieldName(key: string): string {
  const parts = key.split("_").filter(Boolean);
  let yearSuffix = "";
  const last = parts[parts.length - 1];
  if (last && /^y\d+$/i.test(last)) {
    yearSuffix = ` (Y${last.slice(1)})`;
    parts.pop();
  }
  const words = parts.map((p, i) => {
    const lower = p.toLowerCase();
    if (ACRONYMS[lower]) return ACRONYMS[lower];
    if (lower === "per") return "per";
    return i === 0 ? lower.charAt(0).toUpperCase() + lower.slice(1) : lower;
  });
  return words.join(" ") + yearSuffix;
}

/** Classify an assumption field by name. Order matters: the first matching rule wins. */
export function fieldKind(key: string): FieldKind {
  const k = key.toLowerCase();
  if (k.endsWith("_years") || k === "years" || k.endsWith("_life_years")) return "years";
  if (k.includes("_per_bbl") || k.includes("_per_mcf")) return "usd_unit";
  if (k.endsWith("_adjustment") || k.endsWith("_capitalized") || k.endsWith("_usd")) return "usd";
  if (k.includes("beta")) return "beta";
  if (k.includes("multiple") || (k.endsWith("_ratio") && !k.includes("payout"))) return "multiple";
  if (
    /(growth|margin|rate|premium|roe|roic|cost_of|debt_to_capital|payout|probability|pct|yield|discount)/.test(
      k,
    )
  )
    return "percent";
  return "number";
}

export function isPercentField(key: string): boolean {
  return fieldKind(key) === "percent";
}

function trimZeros(s: string): string {
  return s.includes(".") ? s.replace(/\.?0+$/, "") : s;
}

export function formatPercent(value: number, digits = 1): string {
  if (!Number.isFinite(value)) return "—";
  return `${(value * 100).toFixed(digits)}%`;
}

/** Signed percent, e.g. +12.3% / -4.0%. */
export function formatSignedPercent(value: number, digits = 1): string {
  if (!Number.isFinite(value)) return "—";
  const pct = value * 100;
  const sign = pct > 0 ? "+" : pct < 0 ? "−" : "";
  return `${sign}${Math.abs(pct).toFixed(digits)}%`;
}

/** $1.23T / $456.7B / $12.3M / $950K / $12.34 */
export function formatUsdCompact(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  const sign = value < 0 ? "−" : "";
  const units: [number, string][] = [
    [1e12, "T"],
    [1e9, "B"],
    [1e6, "M"],
    [1e3, "K"],
  ];
  for (const [n, suffix] of units) {
    if (abs >= n) return `${sign}$${(abs / n).toFixed(abs / n >= 100 ? 0 : 1)}${suffix}`;
  }
  return `${sign}$${abs.toFixed(2)}`;
}

/** $123.45 (per-share and per-unit prices). */
export function formatUsd(value: number, digits = 2): string {
  if (!Number.isFinite(value)) return "—";
  const sign = value < 0 ? "−" : "";
  return `${sign}$${Math.abs(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  })}`;
}

export function formatNumber(value: number, digits = 2): string {
  if (!Number.isFinite(value)) return "—";
  return value.toLocaleString("en-US", { maximumFractionDigits: digits });
}

/** Format an assumption value based on its field name (percent vs multiple vs years vs USD). */
export function formatAssumptionValue(key: string, value: number): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  switch (fieldKind(key)) {
    case "percent":
      return `${trimZeros((value * 100).toFixed(2))}%`;
    case "years": {
      const v = trimZeros(value.toFixed(1));
      return `${v} ${Math.abs(value) === 1 ? "yr" : "yrs"}`;
    }
    case "usd_unit": {
      const unit = key.toLowerCase().includes("_per_bbl") ? "/bbl" : "/Mcf";
      return `${formatUsd(value)}${unit}`;
    }
    case "usd":
      return formatUsdCompact(value);
    case "multiple":
      return `${value.toFixed(2)}x`;
    case "beta":
      return value.toFixed(2);
    default:
      return formatNumber(value, 4);
  }
}

/** Unit label shown next to an editable input. */
export function inputUnit(key: string): string {
  switch (fieldKind(key)) {
    case "percent":
      return "%";
    case "years":
      return "yrs";
    case "usd":
    case "usd_unit":
      return "$";
    case "multiple":
      return "x";
    default:
      return "";
  }
}

/** Decimal stored value -> value shown in an editable input (percent fields scale by 100). */
export function toDisplayValue(key: string, value: number): number {
  if (isPercentField(key)) return Math.round(value * 100 * 1e6) / 1e6;
  return value;
}

/** Value typed into an editable input -> decimal stored value. */
export function fromDisplayValue(key: string, display: number): number {
  if (isPercentField(key)) return Math.round((display / 100) * 1e10) / 1e10;
  return display;
}

export const SOURCE_LABELS: Record<string, string> = {
  historical_trend: "Historical trend",
  industry_median: "Industry median (Damodaran)",
  analyst_like_judgment: "AI judgment",
  risk_free_rate: "Risk-free rate (FRED)",
  regulatory_filing: "Regulatory filing",
};

export function formatSource(source: string): string {
  return SOURCE_LABELS[source] ?? humanizeFieldName(source);
}

export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
