"use client";

/**
 * Building state (§10.1): a full-screen overlay with the SSE-driven progress log and a Cancel
 * button. While mounted, the whole app shell is `inert` (useAppLock) — Cancel, which lives in
 * this portalled overlay outside the shell, is the only interactive control.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { createPortal } from "react-dom";
import { useAppLock } from "@/components/app-shell";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { api, queryKeys } from "@/lib/api-client";
import { formatDateTime, humanizeFieldName } from "@/lib/format";
import type { RunEventOut, RunOut } from "@/lib/types";
import type { StreamState } from "@/lib/use-run-events";

export function ProgressView({
  run,
  events,
  stream,
}: {
  run: RunOut;
  events: RunEventOut[];
  stream: StreamState;
}) {
  useAppLock(true);
  const queryClient = useQueryClient();

  const cancel = useMutation({
    mutationFn: () => api.cancelRun(run.id),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.run(run.id), updated);
    },
  });

  const cancelling = run.cancel_requested || cancel.isPending || cancel.isSuccess;
  const pct = Math.max(0, Math.min(100, run.progress_pct ?? 0));
  const logRef = React.useRef<HTMLOListElement>(null);
  React.useEffect(() => {
    logRef.current?.lastElementChild?.scrollIntoView?.({ block: "nearest" });
  }, [events.length]);

  // Only ever rendered client-side (run data is fetched in the browser), but guard anyway.
  if (typeof document === "undefined") return null;

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-[var(--overlay)] p-4 backdrop-blur-sm"
      data-testid="progress-overlay"
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby="progress-title"
        className="flex max-h-[90vh] w-full max-w-xl flex-col gap-5 rounded-lg border border-border bg-card p-6 text-card-foreground shadow-xl"
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="progress-title" className="text-lg font-semibold">
              Building {run.ticker} model
            </h2>
            <p className="text-sm text-muted-foreground" data-testid="progress-stage">
              {cancelling
                ? "Cancelling…"
                : run.current_stage
                  ? humanizeFieldName(run.current_stage)
                  : "Queued"}
            </p>
          </div>
          <Spinner label="Building" />
        </div>

        <div>
          <div
            className="h-2 w-full overflow-hidden rounded-full bg-muted"
            role="progressbar"
            aria-label="Build progress"
            aria-valuemin={0}
            aria-valuemax={100}
            aria-valuenow={pct}
          >
            <div className="h-full rounded-full bg-primary transition-[width] duration-500" style={{ width: `${pct}%` }} />
          </div>
          <div className="mt-1 text-right text-xs text-muted-foreground tabular-nums">{pct}%</div>
        </div>

        <ol
          ref={logRef}
          aria-label="Progress log"
          aria-live="polite"
          className="max-h-60 min-h-24 overflow-y-auto rounded-md border border-border bg-muted/50 p-3 font-mono text-xs"
        >
          {events.length === 0 && (
            <li className="text-muted-foreground">
              {stream === "error" ? "Live updates unavailable — checking status periodically…" : "Waiting for the worker…"}
            </li>
          )}
          {events.map((e) => (
            <li key={e.id} className="flex gap-3 py-0.5" data-testid="progress-event">
              <span className="shrink-0 text-muted-foreground">{formatDateTime(e.ts)}</span>
              <span>{e.message}</span>
            </li>
          ))}
        </ol>

        {cancel.isError && (
          <p role="alert" className="text-sm text-negative">
            Could not cancel: {cancel.error instanceof Error ? cancel.error.message : "unknown error"}
          </p>
        )}

        <div className="flex items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">The rest of the app is paused until this finishes.</p>
          <Button variant="destructive" onClick={() => cancel.mutate()} disabled={cancelling}>
            {cancelling ? "Cancelling…" : "Cancel"}
          </Button>
        </div>
      </div>
    </div>,
    document.body,
  );
}
