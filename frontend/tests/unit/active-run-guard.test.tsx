import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import * as React from "react";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";
import { ActiveRunGuard, ActiveRunProvider, useActiveRun } from "@/components/active-run-guard";
import { createMockServer } from "@/mocks/node";

const { server, state } = createMockServer();

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  state.reset();
});
afterAll(() => server.close());

function Probe() {
  const { activeRun, locked } = useActiveRun();
  return (
    <span data-testid="probe">
      {activeRun === undefined ? "loading" : activeRun ? `${activeRun.status}:${locked}` : "none"}
    </span>
  );
}

function renderGuard(pollMs = 50) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ActiveRunProvider pollMs={pollMs}>
        <Probe />
        <ActiveRunGuard>
          <input aria-label="Ticker" />
          <button type="submit">Value it</button>
        </ActiveRunGuard>
      </ActiveRunProvider>
    </QueryClientProvider>,
  );
}

describe("ActiveRunGuard", () => {
  it("leaves the form enabled when there is no run (204)", async () => {
    renderGuard();
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("none"));
    expect(screen.getByRole("button", { name: "Value it" })).toBeEnabled();
    expect(screen.getByLabelText("Ticker")).toBeEnabled();
    expect(screen.queryByTestId("active-run-lock")).not.toBeInTheDocument();
  });

  it.each(["classifying", "proposing", "building"] as const)("blocks the form while a run is %s", async (status) => {
    const run = state.seed({ status, ticker: "MSFT" }, { stepMs: 60_000 });
    renderGuard();
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent(`${status}:true`));
    expect(screen.getByRole("button", { name: "Value it" })).toBeDisabled();
    expect(screen.getByLabelText("Ticker")).toBeDisabled();
    const notice = screen.getByTestId("active-run-lock");
    expect(notice).toHaveTextContent("MSFT");
    expect(screen.getByRole("link", { name: "View run" })).toHaveAttribute("href", `/runs/${run.id}`);
  });

  it("does NOT block while the run is awaiting_confirm", async () => {
    state.seed({ status: "awaiting_confirm" });
    renderGuard();
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("awaiting_confirm:false"));
    expect(screen.getByRole("button", { name: "Value it" })).toBeEnabled();
    expect(screen.queryByTestId("active-run-lock")).not.toBeInTheDocument();
  });

  it("unblocks once the active run reaches awaiting_confirm (polling)", async () => {
    // A run started in "another tab" that is still classifying...
    const run = state.seed({ status: "classifying" });
    renderGuard(40);
    await waitFor(() => expect(screen.getByRole("button", { name: "Value it" })).toBeDisabled());

    // ...moves on to awaiting_confirm; the next poll must release the lock.
    state.runs.get(run.id)!.forced = "awaiting_confirm";
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("awaiting_confirm:false"));
    expect(screen.getByRole("button", { name: "Value it" })).toBeEnabled();
  });

  it("locks when a run appears after load (cross-tab)", async () => {
    renderGuard(40);
    await waitFor(() => expect(screen.getByTestId("probe")).toHaveTextContent("none"));
    state.create("NVDA");
    await waitFor(() => expect(screen.getByRole("button", { name: "Value it" })).toBeDisabled());
  });
});
