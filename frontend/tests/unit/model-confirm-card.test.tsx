import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ModelConfirmCard } from "@/components/model-confirm-card";
import { makeRun } from "@/mocks/fixtures";

describe("ModelConfirmCard", () => {
  it("shows model, confidence, reasons, runner-up and window", () => {
    render(<ModelConfirmCard run={makeRun()} />);
    expect(screen.getByTestId("recommended-model")).toHaveTextContent("FCFF (unlevered DCF)");
    expect(screen.getByTestId("model-confidence")).toHaveTextContent("92% confidence");
    expect(screen.getByRole("meter", { name: "Model confidence" })).toHaveAttribute("aria-valuenow", "92");
    expect(screen.getAllByRole("listitem")).toHaveLength(3);
    expect(screen.getByText(/Positive and stable operating cash flow/)).toBeInTheDocument();
    expect(screen.getByTestId("runner-up-model")).toHaveTextContent("FCFE (levered DCF)");
    expect(screen.getByTestId("historical-window")).toHaveTextContent("5 years");
    expect(screen.getByText(/no cyclical SIC/)).toBeInTheDocument();
    expect(screen.getByText("Apple Inc.", { exact: false })).toBeInTheDocument();
    // No override select unless a handler is passed.
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("offers a model override that excludes the recommended model", async () => {
    const user = userEvent.setup();
    const onOverride = vi.fn();
    const { rerender } = render(<ModelConfirmCard run={makeRun()} onOverrideChange={onOverride} />);
    const select = screen.getByLabelText("Use a different model");
    const options = Array.from((select as HTMLSelectElement).options).map((o) => o.value);
    expect(options).toEqual(["", "fcfe", "excess_return", "nav_reit", "nav_ep", "sotp"]);

    await user.selectOptions(select, "fcfe");
    expect(onOverride).toHaveBeenLastCalledWith("fcfe");

    rerender(<ModelConfirmCard run={makeRun()} override="fcfe" onOverrideChange={onOverride} />);
    expect(screen.getByTestId("override-note")).toHaveTextContent(/re-proposed for FCFE/);

    await user.selectOptions(select, "");
    expect(onOverride).toHaveBeenLastCalledWith(null);
  });
});
