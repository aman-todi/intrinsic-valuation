"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import * as React from "react";
import { ActiveRunGuard, useActiveRun } from "@/components/active-run-guard";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { ActiveRunConflictError, api, queryKeys } from "@/lib/api-client";

const TICKER_RE = /^[A-Za-z][A-Za-z0-9.-]{0,9}$/;

export function TickerForm() {
  const [ticker, setTicker] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const router = useRouter();
  const queryClient = useQueryClient();
  const { activeRun, locked } = useActiveRun();

  const create = useMutation({
    mutationFn: (t: string) => api.createRun(t),
    onSuccess: (run) => {
      queryClient.setQueryData(queryKeys.run(run.id), run);
      queryClient.setQueryData(queryKeys.activeRun, run);
      router.push(`/runs/${run.id}`);
    },
    onError: (err) => {
      if (err instanceof ActiveRunConflictError) {
        // Another tab/device started a run first — go to it (§9.1 409 contract).
        void queryClient.invalidateQueries({ queryKey: queryKeys.activeRun });
        router.push(`/runs/${err.activeRunId}`);
        return;
      }
      setError(err instanceof Error ? err.message : "Could not start the run.");
    },
  });

  function submit(e: React.FormEvent) {
    e.preventDefault();
    const t = ticker.trim().toUpperCase();
    if (!TICKER_RE.test(t)) {
      setError("Enter a valid US ticker, e.g. AAPL or BRK.B.");
      return;
    }
    setError(null);
    create.mutate(t);
  }

  const pendingElsewhere = !locked && activeRun?.status === "awaiting_confirm" ? activeRun : null;

  return (
    <ActiveRunGuard>
      <form onSubmit={submit} className="flex flex-col gap-3" aria-label="Start a valuation">
        <Label htmlFor="ticker">Ticker</Label>
        <div className="flex gap-2">
          <Input
            id="ticker"
            name="ticker"
            autoComplete="off"
            autoCapitalize="characters"
            spellCheck={false}
            placeholder="AAPL"
            maxLength={10}
            value={ticker}
            onChange={(e) => setTicker(e.target.value.toUpperCase())}
            className="h-11 font-mono text-base tracking-wider uppercase"
            aria-invalid={error ? true : undefined}
            aria-describedby={error ? "ticker-error" : undefined}
          />
          <Button type="submit" size="lg" disabled={create.isPending || ticker.trim() === ""}>
            {create.isPending ? "Starting…" : "Value it"}
          </Button>
        </div>
        {error && (
          <p id="ticker-error" role="alert" className="text-sm text-negative">
            {error}
          </p>
        )}
        {pendingElsewhere && (
          <p className="text-xs text-muted-foreground">
            Your {pendingElsewhere.ticker} run is still waiting for confirmation; starting a new ticker leaves it
            as-is.
          </p>
        )}
      </form>
    </ActiveRunGuard>
  );
}
