import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import * as React from "react";
import { describe, expect, it, vi } from "vitest";
import {
  AssumptionsForm,
  EditableToggle,
  assumptionsDiffer,
  isEditableAssumptions,
  orderedKeys,
} from "@/components/assumptions-form";
import type { AssumptionField, AssumptionsPayload } from "@/lib/types";
import { FCFF_ASSUMPTIONS, FCFF_ASSUMPTIONS_SCHEMA, SOTP_ASSUMPTIONS } from "@/mocks/fixtures";

function Harness({ initial, onChange }: { initial: AssumptionsPayload; onChange?: (p: AssumptionsPayload) => void }) {
  const [editable, setEditable] = React.useState(false);
  const [values, setValues] = React.useState(initial);
  return (
    <>
      <EditableToggle checked={editable} onCheckedChange={setEditable} />
      <AssumptionsForm
        assumptions={values}
        editable={editable}
        onChange={(next) => {
          setValues(next);
          onChange?.(next);
        }}
      />
    </>
  );
}

describe("AssumptionsForm", () => {
  it("renders read-only formatted values with rationale and source", () => {
    render(<AssumptionsForm assumptions={FCFF_ASSUMPTIONS} editable={false} />);
    expect(screen.queryAllByRole("spinbutton")).toHaveLength(0);
    expect(screen.getByText("Revenue growth (Y1)")).toBeInTheDocument();
    expect(screen.getByTestId("assumption-value-revenue_growth_y1")).toHaveTextContent("6.2%");
    expect(screen.getByTestId("assumption-value-levered_beta")).toHaveTextContent("1.12");
    expect(screen.getByTestId("assumption-value-margin_convergence_years")).toHaveTextContent("5 yrs");
    expect(screen.getByTestId("assumption-value-sales_to_capital_ratio")).toHaveTextContent("2.80x");
    const row = screen.getByTestId("assumption-row-risk_free_rate");
    expect(within(row).getByText(/10Y Treasury/)).toBeInTheDocument();
    expect(within(row).getByText("Risk-free rate (FRED)")).toBeInTheDocument();
  });

  it("renders one input per field when editable, percent fields as percentages", () => {
    render(<AssumptionsForm assumptions={FCFF_ASSUMPTIONS} editable onChange={() => {}} />);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(Object.keys(FCFF_ASSUMPTIONS).length);
    expect(screen.getByLabelText("Revenue growth (Y1)")).toHaveValue(6.2);
    expect(screen.getByLabelText("Tax rate")).toHaveValue(16);
    expect(screen.getByLabelText("Levered beta")).toHaveValue(1.12);
  });

  it("toggle switches between read-only and editable, and edits propagate as decimals", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<Harness initial={FCFF_ASSUMPTIONS} onChange={onChange} />);

    const toggle = screen.getByRole("switch", { name: /editable/i });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.queryAllByRole("spinbutton")).toHaveLength(0);

    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-checked", "true");

    const input = screen.getByLabelText("Revenue growth (Y1)");
    fireEvent.change(input, { target: { value: "8.5" } });
    const last = onChange.mock.calls.at(-1)![0] as AssumptionsPayload;
    expect((last.revenue_growth_y1 as AssumptionField).value).toBeCloseTo(0.085, 10);
    // rationale/source preserved
    expect((last.revenue_growth_y1 as AssumptionField).source).toBe("historical_trend");
    expect(input).toHaveValue(8.5);

    fireEvent.change(screen.getByLabelText("Levered beta"), { target: { value: "1.3" } });
    const last2 = onChange.mock.calls.at(-1)![0] as AssumptionsPayload;
    expect((last2.levered_beta as AssumptionField).value).toBe(1.3);
    expect((last2.revenue_growth_y1 as AssumptionField).value).toBeCloseTo(0.085, 10);
  });

  it("shows client-side bound warnings", async () => {
    const user = userEvent.setup();
    render(<Harness initial={FCFF_ASSUMPTIONS} />);
    await user.click(screen.getByRole("switch", { name: /editable/i }));
    expect(screen.queryAllByRole("alert")).toHaveLength(0);

    fireEvent.change(screen.getByLabelText("Terminal growth rate"), { target: { value: "5" } });
    expect(screen.getByRole("alert")).toHaveTextContent(/risk-free rate \(4\.2%\)/);

    fireEvent.change(screen.getByLabelText("Tax rate"), { target: { value: "65" } });
    expect(screen.getAllByRole("alert").map((a) => a.textContent)).toContainEqual(
      expect.stringMatching(/Tax rate should be between 0% and 50%/),
    );
    expect(screen.getByLabelText("Tax rate")).toHaveAttribute("aria-invalid", "true");

    fireEvent.change(screen.getByLabelText("Survival probability"), { target: { value: "0" } });
    expect(screen.getAllByRole("alert")).toHaveLength(3);
  });

  it("renders SOTP read-only with a not-supported note, even when editable is requested", () => {
    render(<AssumptionsForm assumptions={SOTP_ASSUMPTIONS as unknown as AssumptionsPayload} editable onChange={() => {}} />);
    expect(screen.getByRole("note")).toHaveTextContent(/Editing SOTP assumptions isn.t supported/);
    expect(screen.getAllByTestId("sotp-segment")).toHaveLength(2);
    expect(screen.getByText("EV/EBITDA 14.5x")).toBeInTheDocument();
    expect(screen.queryAllByRole("spinbutton")).toHaveLength(0);
    expect(isEditableAssumptions(SOTP_ASSUMPTIONS as unknown as AssumptionsPayload)).toBe(false);
  });

  it("orders fields by the JSON schema and detects edits", () => {
    const reversed = Object.fromEntries(Object.entries(FCFF_ASSUMPTIONS).reverse());
    expect(orderedKeys(reversed, FCFF_ASSUMPTIONS_SCHEMA)[0]).toBe("revenue_growth_y1");
    expect(assumptionsDiffer(FCFF_ASSUMPTIONS, { ...FCFF_ASSUMPTIONS })).toBe(false);
    expect(
      assumptionsDiffer(FCFF_ASSUMPTIONS, {
        ...FCFF_ASSUMPTIONS,
        tax_rate: { ...FCFF_ASSUMPTIONS.tax_rate, value: 0.2 },
      }),
    ).toBe(true);
  });
});
