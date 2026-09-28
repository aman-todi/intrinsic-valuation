"use client";

/**
 * ActiveRunGuard (§10.1): the app-wide single-run lock on the client.
 *
 * `ActiveRunProvider` (mounted in the root layout) polls GET /api/runs/active so a run started
 * in one tab locks the ticker form in every other tab. `ActiveRunGuard` wraps anything that can
 * start a new run and disables it while the user has an *active* run
 * (classifying / proposing / building). A run sitting in `awaiting_confirm` does NOT lock —
 * no worker owns it, and the backend happily creates a new run alongside it.
 *
 * This is UX only; the real lock is the backend's DB constraint (409 on POST /api/runs).
 */
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import * as React from "react";
import { api, queryKeys } from "@/lib/api-client";
import { isActive, STATUS_LABELS } from "@/lib/run-status";
import type { RunOut } from "@/lib/types";

export const ACTIVE_RUN_POLL_MS = 5000;

interface ActiveRunContextValue {
  /** The user's active or awaiting_confirm run, null when none, undefined while loading. */
  activeRun: RunOut | null | undefined;
  /** True when a worker-owned (active) run exists — new submissions must be blocked. */
  locked: boolean;
  isLoading: boolean;
  refetch: () => Promise<unknown>;
}

const ActiveRunContext = React.createContext<ActiveRunContextValue>({
  activeRun: undefined,
  locked: false,
  isLoading: false,
  refetch: async () => {},
});

export function useActiveRun(): ActiveRunContextValue {
  return React.useContext(ActiveRunContext);
}

export function ActiveRunProvider({
  children,
  enabled = true,
  pollMs = ACTIVE_RUN_POLL_MS,
}: {
  children: React.ReactNode;
  enabled?: boolean;
  pollMs?: number;
}) {
  const query = useQuery({
    queryKey: queryKeys.activeRun,
    queryFn: api.getActiveRun,
    enabled,
    refetchInterval: pollMs,
    refetchIntervalInBackground: true,
    refetchOnWindowFocus: true,
  });

  const { refetch } = query;
  const value = React.useMemo<ActiveRunContextValue>(
    () => ({
      activeRun: query.data,
      locked: isActive(query.data?.status),
      isLoading: query.isLoading,
      refetch,
    }),
    [query.data, query.isLoading, refetch],
  );

  return <ActiveRunContext.Provider value={value}>{children}</ActiveRunContext.Provider>;
}

/**
 * Disables everything inside it (via a disabled <fieldset>) while an active run exists and
 * shows where that run lives.
 */
export function ActiveRunGuard({ children }: { children: React.ReactNode }) {
  const { activeRun, locked } = useActiveRun();

  return (
    <div className="flex flex-col gap-3">
      {locked && activeRun && (
        <div
          role="status"
          data-testid="active-run-lock"
          className="rounded-md border border-warning/40 bg-warning-bg px-4 py-3 text-sm text-warning"
        >
          A run for <strong>{activeRun.ticker}</strong> is in progress ({STATUS_LABELS[activeRun.status].toLowerCase()}).
          You can start a new valuation once it finishes.{" "}
          <Link href={`/runs/${activeRun.id}`} className="font-medium underline underline-offset-2">
            View run
          </Link>
        </div>
      )}
      <fieldset disabled={locked} aria-disabled={locked} className="m-0 min-w-0 border-0 p-0 disabled:opacity-60">
        {children}
      </fieldset>
    </div>
  );
}
