"use client";

/**
 * Complete state (§10.1): value per share, live-price upside, value bridge (waterfall),
 * sensitivity grid, scenario bars, downloads, flags, sources and the assumptions used.
 */
import { useQuery } from "@tanstack/react-query";
import Link from "next/link";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  LabelList,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { AssumptionsForm } from "@/components/assumptions-form";
import { SensitivityChart } from "@/components/sensitivity-chart";
import { Badge } from "@/components/ui/badge";
import { ButtonLink } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { api, queryKeys } from "@/lib/api-client";
import {
  formatAssumptionValue,
  formatDateTime,
  formatNumber,
  formatSignedPercent,
  formatUsd,
  formatUsdCompact,
  humanizeFieldName,
} from "@/lib/format";
import { modelTypeLabel, type RunOut, type ValuationResult } from "@/lib/types";

interface BridgeStep {
  label: string;
  base: number;
  delta: number;
  amount: number;
  kind: "total" | "up" | "down";
}

/** Operating value → (+cash, ±non-operating) → EV → (−claims) → equity value. */
export function buildBridge(r: ValuationResult): BridgeStep[] {
  const steps: BridgeStep[] = [];
  let running = 0;
  const total = (label: string, amount: number) => {
    running = amount;
    steps.push({ label, base: Math.min(0, amount), delta: Math.abs(amount), amount, kind: "total" });
  };
  const step = (label: string, amount: number) => {
    if (!amount) return;
    const next = running + amount;
    steps.push({ label, base: Math.min(running, next), delta: Math.abs(amount), amount, kind: amount >= 0 ? "up" : "down" });
    running = next;
  };
  total("Operating value", r.operating_value);
  step("Cash", r.cash_and_equivalents);
  for (const adj of r.non_operating_adjustments) step(adj.label, adj.amount);
  total("Enterprise value", r.enterprise_value);
  step("Debt", -r.total_debt);
  step("Leases", -r.operating_lease_liability);
  step("Preferred", -r.preferred_equity);
  step("Minority int.", -r.minority_interest);
  step("Pension deficit", -r.pension_deficit);
  total("Equity value", r.equity_value);
  return steps;
}

const BAR_COLOR: Record<BridgeStep["kind"], string> = {
  total: "var(--chart-total)",
  up: "var(--chart-1)",
  down: "var(--chart-neg)",
};

const shortLabel = (s: string) => (s.length > 16 ? `${s.slice(0, 15)}…` : s);

function Kpi({ label, value, sub, testId, tone }: { label: string; value: string; sub?: string; testId?: string; tone?: "pos" | "neg" }) {
  return (
    <div className="flex flex-col gap-1 rounded-lg border border-border bg-card p-4">
      <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">{label}</span>
      <span
        data-testid={testId}
        className={`text-2xl font-semibold tabular-nums ${tone === "pos" ? "text-positive" : tone === "neg" ? "text-negative" : ""}`}
      >
        {value}
      </span>
      {sub && <span className="text-xs text-muted-foreground">{sub}</span>}
    </div>
  );
}

const axisTick = { fill: "var(--muted-foreground)", fontSize: 11 };
const tooltipStyle = {
  background: "var(--card)",
  border: "1px solid var(--border)",
  borderRadius: 6,
  color: "var(--card-foreground)",
  fontSize: 12,
};

export function ResultView({ run }: { run: RunOut }) {
  const q = useQuery({ queryKey: queryKeys.result(run.id), queryFn: () => api.getResult(run.id) });

  if (q.isLoading) {
    return (
      <div className="flex flex-1 items-center justify-center p-16">
        <Spinner label="Loading result" />
      </div>
    );
  }
  if (q.isError || !q.data) {
    return (
      <p role="alert" className="text-negative">
        Could not load the result: {q.error instanceof Error ? q.error.message : "unknown error"}
      </p>
    );
  }

  const { result: r, live_price, xlsx_url, pdf_url } = q.data;
  const price = live_price?.price ?? r.market_price;
  const upside = live_price?.upside_pct ?? r.upside_pct;
  const bridge = buildBridge(r);
  const equityDirect = r.operating_value === r.enterprise_value && r.enterprise_value === r.equity_value;
  const scenarios = [...r.scenarios].sort(
    (a, b) => ["bear", "base", "bull"].indexOf(a.label) - ["bear", "base", "bull"].indexOf(b.label),
  );

  return (
    <div className="flex flex-col gap-6" data-testid="result-view">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <div className="flex items-center gap-2">
            <h1 className="font-mono text-3xl font-semibold tracking-tight">{r.ticker}</h1>
            <Badge variant="outline">{modelTypeLabel(r.model_type)}</Badge>
            {run.assumptions_edited && <Badge variant="warning">Edited assumptions (private)</Badge>}
          </div>
          <p className="text-sm text-muted-foreground">
            {run.company_name ?? r.ticker} · valued {r.run_date} · {r.historical_window_years}-year history
          </p>
        </div>
        <div className="flex gap-2">
          {xlsx_url ? (
            <ButtonLink href={xlsx_url} download data-testid="download-xlsx">
              Download Excel
            </ButtonLink>
          ) : null}
          {pdf_url ? (
            <ButtonLink href={pdf_url} download data-testid="download-pdf">
              Download PDF
            </ButtonLink>
          ) : null}
          <ButtonLink href="/?new=1" variant="default">
            New valuation
          </ButtonLink>
        </div>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Kpi label="Value per share" value={formatUsd(r.value_per_share)} testId="value-per-share" />
        <Kpi
          label={live_price ? "Live price" : "Market price"}
          value={formatUsd(price)}
          sub={live_price ? `as of ${formatDateTime(live_price.as_of)}` : "live price unavailable — price at build time"}
          testId="live-price"
        />
        <Kpi
          label="Upside"
          value={formatSignedPercent(upside)}
          tone={upside >= 0 ? "pos" : "neg"}
          sub="vs. current price"
          testId="upside"
        />
        <Kpi label="Equity value" value={formatUsdCompact(r.equity_value)} sub={`${formatNumber(r.diluted_shares / 1e6, 0)}M diluted shares`} />
      </div>

      {r.data_confidence_flags.length > 0 && (
        <div className="rounded-md border border-warning/40 bg-warning-bg px-4 py-3 text-sm" role="note">
          <div className="mb-1 font-medium text-warning">⚠ Data confidence flags</div>
          <ul className="list-disc space-y-0.5 pl-5">
            {r.data_confidence_flags.map((f) => (
              <li key={f}>{f}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Value bridge</CardTitle>
            <CardDescription>
              {equityDirect
                ? "This model values equity directly, so there are no bridge adjustments."
                : "From operating value to equity value (USD)."}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-72" data-testid="bridge-chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={bridge} margin={{ top: 20, right: 8, left: 8, bottom: 56 }} barCategoryGap={6}>
                  <CartesianGrid vertical={false} stroke="var(--border)" strokeDasharray="0" />
                  <XAxis dataKey="label" tick={axisTick} tickFormatter={shortLabel} interval={0} angle={-35} textAnchor="end" tickLine={false} axisLine={{ stroke: "var(--border)" }} />
                  <YAxis tick={axisTick} tickFormatter={(v: number) => formatUsdCompact(v)} width={64} tickLine={false} axisLine={false} />
                  <Tooltip
                    cursor={{ fill: "var(--muted)" }}
                    contentStyle={tooltipStyle}
                    formatter={(_v, _n, item) => [formatUsdCompact((item.payload as BridgeStep).amount), (item.payload as BridgeStep).label]}
                    labelFormatter={() => ""}
                  />
                  <Bar dataKey="base" stackId="w" fill="transparent" tooltipType="none" isAnimationActive={false} />
                  <Bar dataKey="delta" stackId="w" radius={[4, 4, 0, 0]} isAnimationActive={false}>
                    {bridge.map((s) => (
                      <Cell key={s.label} fill={BAR_COLOR[s.kind]} />
                    ))}
                    <LabelList
                      dataKey="amount"
                      position="top"
                      formatter={(v) => (typeof v === "number" ? formatUsdCompact(v) : "")}
                      style={{ fill: "var(--muted-foreground)", fontSize: 10 }}
                    />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
            <div className="mt-2 flex gap-4 text-xs text-muted-foreground" aria-hidden>
              <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-sm" style={{ background: BAR_COLOR.total }} />Subtotal</span>
              <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-sm" style={{ background: BAR_COLOR.up }} />Adds value</span>
              <span className="flex items-center gap-1"><span className="h-2.5 w-2.5 rounded-sm" style={{ background: BAR_COLOR.down }} />Claims / deductions</span>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Scenarios</CardTitle>
            <CardDescription>Value per share under bear / base / bull assumptions; dashed line = current price.</CardDescription>
          </CardHeader>
          <CardContent>
            <div className="h-56" data-testid="scenario-chart">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={scenarios} margin={{ top: 20, right: 8, left: 8, bottom: 0 }} barCategoryGap="30%">
                  <CartesianGrid vertical={false} stroke="var(--border)" />
                  <XAxis dataKey="label" tick={axisTick} tickFormatter={(v: string) => humanizeFieldName(v)} tickLine={false} axisLine={{ stroke: "var(--border)" }} />
                  <YAxis tick={axisTick} tickFormatter={(v: number) => `$${v}`} width={48} tickLine={false} axisLine={false} />
                  <Tooltip cursor={{ fill: "var(--muted)" }} contentStyle={tooltipStyle} formatter={(v) => [formatUsd(Number(v)), "Value / share"]} />
                  <ReferenceLine y={price} stroke="var(--muted-foreground)" strokeDasharray="4 4" label={{ value: `Price ${formatUsd(price)}`, fill: "var(--muted-foreground)", fontSize: 10, position: "insideTopRight" }} />
                  <Bar dataKey="value_per_share" fill="var(--chart-1)" radius={[4, 4, 0, 0]} isAnimationActive={false}>
                    <LabelList dataKey="value_per_share" position="top" formatter={(v) => (typeof v === "number" ? formatUsd(v) : "")} style={{ fill: "var(--foreground)", fontSize: 11 }} />
                  </Bar>
                </BarChart>
              </ResponsiveContainer>
            </div>
            <ul className="mt-3 space-y-1 text-xs text-muted-foreground">
              {scenarios
                .filter((s) => Object.keys(s.key_assumption_deltas).length > 0)
                .map((s) => (
                  <li key={s.label}>
                    <span className="font-medium text-foreground">{humanizeFieldName(s.label)}:</span>{" "}
                    {Object.entries(s.key_assumption_deltas)
                      .map(([k, d]) => `${humanizeFieldName(k)} ${d >= 0 ? "+" : "−"}${formatAssumptionValue(k, Math.abs(d))}`)
                      .join(", ")}
                  </li>
                ))}
            </ul>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Sensitivity</CardTitle>
          <CardDescription>Value per share across the discount-rate × growth grid.</CardDescription>
        </CardHeader>
        <CardContent>
          <SensitivityChart grid={r.sensitivity_grid} baseValue={r.value_per_share} modelType={r.model_type} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Assumptions used</CardTitle>
        </CardHeader>
        <CardContent>
          <AssumptionsForm assumptions={r.assumptions_used} editable={false} />
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Sources</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          <ul className="list-disc space-y-1 pl-5">
            {r.sources.map((s) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
          <p className="text-xs text-muted-foreground">
            Filing {r.accession_number} · engine {r.engine_version}
            {r.implied_ev_ebitda != null && <> · implied EV/EBITDA {r.implied_ev_ebitda.toFixed(1)}x</>}
            {r.implied_pb != null && <> · implied P/B {r.implied_pb.toFixed(2)}x</>}
            {r.implied_p_ffo != null && <> · implied P/FFO {r.implied_p_ffo.toFixed(1)}x</>}
          </p>
        </CardContent>
      </Card>

      <div>
        <Link href="/?new=1" className="text-sm font-medium text-primary underline underline-offset-2">
          Start a new valuation
        </Link>
      </div>
    </div>
  );
}
