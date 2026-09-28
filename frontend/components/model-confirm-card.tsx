"use client";

import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Select } from "@/components/ui/input";
import { formatPercent } from "@/lib/format";
import { MODEL_TYPES, modelTypeLabel, type ModelType, type RunOut } from "@/lib/types";

export interface ModelConfirmCardProps {
  run: RunOut;
  /** Selected override, or null to use the recommended model. */
  override?: ModelType | null;
  onOverrideChange?: (model: ModelType | null) => void;
  disabled?: boolean;
}

function confidenceVariant(c: number): "positive" | "warning" | "negative" {
  if (c >= 0.75) return "positive";
  if (c >= 0.5) return "warning";
  return "negative";
}

/** Recommended model, confidence, reasons, runner-up and historical window (§10.1). */
export function ModelConfirmCard({ run, override = null, onOverrideChange, disabled }: ModelConfirmCardProps) {
  const confidence = run.model_confidence ?? 0;
  const reasons = run.model_reasons ?? [];

  return (
    <Card data-testid="model-confirm-card">
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="text-xs font-medium tracking-wide text-muted-foreground uppercase">Recommended model</div>
            <h2 className="text-xl font-semibold tracking-tight" data-testid="recommended-model">
              {modelTypeLabel(run.model_type)}
            </h2>
            <div className="mt-1 text-sm text-muted-foreground">
              <span className="font-mono font-medium text-foreground">{run.ticker}</span>
              {run.company_name && <> · {run.company_name}</>}
              {run.sic_code && <> · SIC {run.sic_code}</>}
            </div>
          </div>
          <div className="flex flex-col items-end gap-1">
            <Badge variant={confidenceVariant(confidence)} data-testid="model-confidence">
              {formatPercent(confidence, 0)} confidence
            </Badge>
            <div
              className="h-1.5 w-32 overflow-hidden rounded-full bg-muted"
              role="meter"
              aria-label="Model confidence"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(confidence * 100)}
            >
              <div className="h-full rounded-full bg-primary" style={{ width: `${Math.round(confidence * 100)}%` }} />
            </div>
          </div>
        </div>
      </CardHeader>
      <CardContent className="grid gap-5 sm:grid-cols-[2fr_1fr]">
        <div>
          <h3 className="mb-2 text-sm font-medium">Why this model</h3>
          {reasons.length > 0 ? (
            <ul className="list-disc space-y-1 pl-5 text-sm text-muted-foreground">
              {reasons.map((r) => (
                <li key={r}>{r}</li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">No reasons provided.</p>
          )}
        </div>
        <dl className="grid content-start gap-3 text-sm">
          <div>
            <dt className="text-muted-foreground">Runner-up</dt>
            <dd className="font-medium" data-testid="runner-up-model">
              {run.runner_up_model ? modelTypeLabel(run.runner_up_model) : "None"}
            </dd>
          </div>
          <div>
            <dt className="text-muted-foreground">Historical window</dt>
            <dd className="font-medium" data-testid="historical-window">
              {run.historical_window_years ? `${run.historical_window_years} years` : "—"}
            </dd>
            {run.window_reason && <dd className="text-xs text-muted-foreground">{run.window_reason}</dd>}
          </div>
          {onOverrideChange && (
            <div className="flex flex-col gap-1">
              <label htmlFor="model-override" className="text-muted-foreground">
                Use a different model
              </label>
              <Select
                id="model-override"
                value={override ?? ""}
                disabled={disabled}
                onChange={(e) => onOverrideChange(e.target.value === "" ? null : (e.target.value as ModelType))}
              >
                <option value="">Recommended ({modelTypeLabel(run.model_type)})</option>
                {MODEL_TYPES.filter((m) => m !== run.model_type).map((m) => (
                  <option key={m} value={m}>
                    {modelTypeLabel(m)}
                  </option>
                ))}
              </Select>
              {override && (
                <p className="text-xs text-warning" data-testid="override-note">
                  Assumptions will be re-proposed for {modelTypeLabel(override)} before building.
                </p>
              )}
            </div>
          )}
        </dl>
      </CardContent>
    </Card>
  );
}
