"use client";

/**
 * Schema-driven assumptions form (§10.2).
 *
 * Renders ANY flat `{field: {value, rationale, source}}` payload generically — no per-model
 * form is ever written by hand. Field order follows the JSON Schema's `properties` when one is
 * supplied. Percent fields are shown/edited as percentages but stored as decimals.
 * SOTP's nested payload is rendered read-only (editing it isn't supported).
 */
import * as React from "react";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { checkBounds } from "@/lib/bounds";
import {
  formatAssumptionValue,
  formatSource,
  fromDisplayValue,
  humanizeFieldName,
  inputUnit,
  toDisplayValue,
} from "@/lib/format";
import type { AssumptionField, AssumptionsPayload, JsonSchema, SotpSegmentAssumption } from "@/lib/types";
import { cn } from "@/lib/utils";

export function isAssumptionField(v: unknown): v is AssumptionField {
  return (
    !!v &&
    typeof v === "object" &&
    "value" in v &&
    typeof (v as { value: unknown }).value === "number"
  );
}

export function isSotpPayload(p: AssumptionsPayload | null | undefined): boolean {
  return !!p && Array.isArray((p as { segments?: unknown }).segments);
}

/** A payload the form can edit: flat, non-empty, every entry an AssumptionField. */
export function isEditableAssumptions(p: AssumptionsPayload | null | undefined): boolean {
  if (!p || isSotpPayload(p)) return false;
  const values = Object.values(p);
  return values.length > 0 && values.every(isAssumptionField);
}

/** Field order: schema `properties` order first, then anything else in payload order. */
export function orderedKeys(payload: AssumptionsPayload, schema?: JsonSchema | null): string[] {
  const keys = Object.keys(payload);
  const schemaKeys = schema?.properties ? Object.keys(schema.properties).filter((k) => k in payload) : [];
  return [...schemaKeys, ...keys.filter((k) => !schemaKeys.includes(k))];
}

/** True when any assumption value differs between the two payloads. */
export function assumptionsDiffer(a: AssumptionsPayload, b: AssumptionsPayload): boolean {
  const keys = new Set([...Object.keys(a), ...Object.keys(b)]);
  for (const k of keys) {
    const x = a[k];
    const y = b[k];
    if (isAssumptionField(x) && isAssumptionField(y)) {
      if (Math.abs(x.value - y.value) > 1e-12) return true;
    } else if (JSON.stringify(x) !== JSON.stringify(y)) {
      return true;
    }
  }
  return false;
}

function NumberInput({
  fieldKey,
  value,
  onValue,
  invalid,
  describedBy,
}: {
  fieldKey: string;
  value: number;
  onValue: (v: number) => void;
  invalid: boolean;
  describedBy?: string;
}) {
  const display = toDisplayValue(fieldKey, value);
  const [draft, setDraft] = React.useState(String(display));
  const [synced, setSynced] = React.useState(value);

  // Resync the draft when the value changes from outside (e.g. reset), without clobbering
  // in-progress typing like "4." that parses to the same number.
  if (value !== synced) {
    setSynced(value);
    if (Number(draft) !== display) setDraft(String(display));
  }

  const unit = inputUnit(fieldKey);
  return (
    <div className="flex items-center gap-1.5">
      <Input
        type="number"
        inputMode="decimal"
        step="any"
        value={draft}
        aria-label={humanizeFieldName(fieldKey)}
        aria-invalid={invalid || undefined}
        aria-describedby={describedBy}
        onChange={(e) => {
          const s = e.target.value;
          setDraft(s);
          const n = Number(s);
          if (s.trim() !== "" && Number.isFinite(n)) {
            const stored = fromDisplayValue(fieldKey, n);
            setSynced(stored);
            onValue(stored);
          }
        }}
        className="h-8 w-28 text-right font-mono tabular-nums"
      />
      {unit && <span className="w-6 text-xs text-muted-foreground">{unit}</span>}
    </div>
  );
}

function FlatTable({
  payload,
  schema,
  editable,
  onChange,
  warnings,
  compact,
}: {
  payload: AssumptionsPayload;
  schema?: JsonSchema | null;
  editable: boolean;
  onChange?: (next: AssumptionsPayload) => void;
  warnings: Record<string, string>;
  compact?: boolean;
}) {
  const keys = orderedKeys(payload, schema);
  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="w-[30%]">Assumption</TableHead>
          <TableHead className="w-[18%] text-right">Value</TableHead>
          {!compact && <TableHead>Rationale &amp; source</TableHead>}
        </TableRow>
      </TableHeader>
      <TableBody>
        {keys.map((key) => {
          const field = payload[key];
          const warning = warnings[key];
          const warnId = `warn-${key}`;
          if (!isAssumptionField(field)) {
            // Non-numeric extras (e.g. a note string) — shown read-only.
            return (
              <TableRow key={key}>
                <TableCell className="font-medium">{humanizeFieldName(key)}</TableCell>
                <TableCell colSpan={compact ? 1 : 2} className="text-muted-foreground">
                  {typeof field === "string" ? field : JSON.stringify(field)}
                </TableCell>
              </TableRow>
            );
          }
          return (
            <TableRow key={key} data-testid={`assumption-row-${key}`} className={cn(warning && "bg-warning-bg")}>
              <TableCell className="font-medium">{humanizeFieldName(key)}</TableCell>
              <TableCell className="text-right">
                <div className="flex flex-col items-end gap-1">
                  {editable && onChange ? (
                    <NumberInput
                      fieldKey={key}
                      value={field.value}
                      invalid={!!warning}
                      describedBy={warning ? warnId : undefined}
                      onValue={(v) => onChange({ ...payload, [key]: { ...field, value: v } })}
                    />
                  ) : (
                    <span className="font-mono tabular-nums" data-testid={`assumption-value-${key}`}>
                      {formatAssumptionValue(key, field.value)}
                    </span>
                  )}
                  {warning && (
                    <span id={warnId} role="alert" className="max-w-56 text-left text-xs text-warning">
                      {warning}
                    </span>
                  )}
                </div>
              </TableCell>
              {!compact && (
                <TableCell className="text-sm text-muted-foreground">
                  {field.rationale}
                  {field.source && (
                    <>
                      {" "}
                      — <em className="whitespace-nowrap">{formatSource(String(field.source))}</em>
                    </>
                  )}
                </TableCell>
              )}
            </TableRow>
          );
        })}
      </TableBody>
    </Table>
  );
}

function SotpView({ payload }: { payload: AssumptionsPayload }) {
  const segments = (payload.segments as SotpSegmentAssumption[]) ?? [];
  const rest = Object.fromEntries(Object.entries(payload).filter(([k]) => k !== "segments"));
  return (
    <div className="flex flex-col gap-4">
      <p role="note" className="rounded-md border border-border bg-muted px-3 py-2 text-sm text-muted-foreground">
        Sum-of-the-parts assumptions are shown read-only. Editing SOTP assumptions isn&apos;t supported — each
        segment is proposed separately and assembled server-side.
      </p>
      {segments.map((seg) => (
        <div key={seg.segment_name} className="rounded-md border border-border" data-testid="sotp-segment">
          <div className="flex items-center justify-between gap-2 border-b border-border px-3 py-2">
            <span className="font-medium">{seg.segment_name}</span>
            <Badge variant="outline">
              {seg.valuation_approach === "fcff" ? "FCFF" : `EV/EBITDA ${seg.ev_ebitda_multiple.toFixed(1)}x`}
            </Badge>
          </div>
          {seg.valuation_approach === "fcff" && seg.fcff_assumptions && (
            <FlatTable payload={seg.fcff_assumptions} editable={false} warnings={{}} compact />
          )}
        </div>
      ))}
      <FlatTable payload={rest} editable={false} warnings={{}} />
    </div>
  );
}

export interface AssumptionsFormProps {
  assumptions: AssumptionsPayload;
  editable: boolean;
  onChange?: (next: AssumptionsPayload) => void;
  schema?: JsonSchema | null;
}

export function AssumptionsForm({ assumptions, editable, onChange, schema }: AssumptionsFormProps) {
  if (isSotpPayload(assumptions)) return <SotpView payload={assumptions} />;
  const warnings = checkBounds(assumptions);
  return (
    <FlatTable
      payload={assumptions}
      schema={schema}
      editable={editable && isEditableAssumptions(assumptions)}
      onChange={onChange}
      warnings={warnings}
    />
  );
}

/** The "Editable (Not Recommended)" toggle (§0 decision 4). */
export function EditableToggle({
  checked,
  onCheckedChange,
  disabled,
  disabledReason,
}: {
  checked: boolean;
  onCheckedChange: (v: boolean) => void;
  disabled?: boolean;
  disabledReason?: string;
}) {
  return (
    <div className="flex items-center gap-2">
      <Switch
        id="editable-toggle"
        checked={checked}
        onCheckedChange={onCheckedChange}
        disabled={disabled}
        aria-label="Editable (Not Recommended)"
        aria-describedby="editable-help"
      />
      <label htmlFor="editable-toggle" className="text-sm font-medium">
        Editable <span className="text-muted-foreground">(Not Recommended)</span>
      </label>
      <span id="editable-help" className="sr-only">
        {disabled && disabledReason
          ? disabledReason
          : "Editing forks the run into a private result that bypasses the shared cache."}
      </span>
    </div>
  );
}
