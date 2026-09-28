/**
 * Sensitivity grid (§10.4): the 5×5 `sensitivity_grid` as an HTML table whose cell background
 * is a diverging blue↔red scale centred (neutral gray) on the base-case value per share.
 * Values stay in text ink; the color carries only "above / below base".
 */
import { formatSignedPercent, formatUsd } from "@/lib/format";
import type { SensitivityCell } from "@/lib/types";

const AXES: Record<string, [string, string]> = {
  fcff: ["WACC", "Terminal growth"],
  fcfe: ["Cost of equity", "Terminal growth"],
  sotp: ["WACC", "Terminal growth"],
  excess_return: ["Cost of equity", "Terminal growth"],
  nav_reit: ["Cap rate", "NOI growth"],
  nav_ep: ["Discount rate", "Price deck"],
};

/** Saturation reached at ±30% vs base. */
const FULL_SCALE = 0.3;

export function cellBackground(value: number, base: number): string {
  if (!Number.isFinite(value) || !base) return "var(--div-mid)";
  const rel = Math.max(-1, Math.min(1, (value - base) / Math.abs(base) / FULL_SCALE));
  const pct = Math.round(Math.abs(rel) * 70);
  const pole = rel >= 0 ? "var(--div-pos)" : "var(--div-neg)";
  return `color-mix(in oklab, ${pole} ${pct}%, var(--div-mid))`;
}

export function SensitivityChart({
  grid,
  baseValue,
  modelType,
}: {
  grid: SensitivityCell[];
  baseValue: number;
  modelType: string;
}) {
  const rows = [...new Set(grid.map((c) => c.row_label))];
  const cols = [...new Set(grid.map((c) => c.col_label))];
  const lookup = new Map(grid.map((c) => [`${c.row_label}|${c.col_label}`, c.value_per_share]));
  const [rowAxis, colAxis] = AXES[modelType] ?? ["Row", "Column"];

  // Base cell: the one closest to the base value; ties go to the cell nearest the grid centre.
  let baseKey = "";
  let best = [Infinity, Infinity];
  for (const c of grid) {
    const d = Math.abs(c.value_per_share - baseValue);
    const centre =
      Math.abs(rows.indexOf(c.row_label) - (rows.length - 1) / 2) +
      Math.abs(cols.indexOf(c.col_label) - (cols.length - 1) / 2);
    if (d < best[0] - 1e-9 || (Math.abs(d - best[0]) <= 1e-9 && centre < best[1])) {
      best = [d, centre];
      baseKey = `${c.row_label}|${c.col_label}`;
    }
  }

  if (grid.length === 0) return <p className="text-sm text-muted-foreground">No sensitivity grid available.</p>;

  return (
    <div className="flex flex-col gap-2">
      <div className="overflow-x-auto">
        <table className="border-separate border-spacing-0.5 text-sm" data-testid="sensitivity-table">
          <caption className="sr-only">
            Value per share by {rowAxis} (rows) and {colAxis} (columns)
          </caption>
          <thead>
            <tr>
              <th scope="col" className="px-2 py-1 text-left text-xs font-medium text-muted-foreground">
                {rowAxis} ↓ / {colAxis} →
              </th>
              {cols.map((c) => (
                <th key={c} scope="col" className="px-2 py-1 text-center text-xs font-medium text-muted-foreground">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r}>
                <th scope="row" className="px-2 py-1 text-left text-xs font-medium text-muted-foreground">
                  {r}
                </th>
                {cols.map((c) => {
                  const key = `${r}|${c}`;
                  const v = lookup.get(key);
                  const isBase = key === baseKey;
                  return (
                    <td
                      key={c}
                      title={
                        v === undefined
                          ? undefined
                          : `${rowAxis} ${r}, ${colAxis} ${c}: ${formatUsd(v)} (${formatSignedPercent(v / baseValue - 1)} vs base)`
                      }
                      className={`min-w-24 rounded px-3 py-1.5 text-center font-mono tabular-nums ${isBase ? "font-semibold ring-2 ring-foreground" : ""}`}
                      style={{ background: v === undefined ? undefined : cellBackground(v, baseValue) }}
                    >
                      {v === undefined ? "—" : formatUsd(v)}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="flex items-center gap-2 text-xs text-muted-foreground" aria-hidden>
        <span>Below base</span>
        <span
          className="h-2 w-40 rounded-full"
          style={{ background: "linear-gradient(to right, var(--div-neg), var(--div-mid), var(--div-pos))" }}
        />
        <span>Above base</span>
        <span className="ml-3">Outlined cell = base case</span>
      </div>
    </div>
  );
}
