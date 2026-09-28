"use client";

/**
 * SSE progress stream for a building run (§8.6, §10.3).
 *
 * Opens a native EventSource on /api/runs/{id}/events (token passed as `?access_token=`
 * because EventSource cannot send headers). Each `data:` message is a RunEventOut; it is
 * appended to the events cache and folded into the run's cached row via setQueryData, so the
 * rest of the UI stays in sync without a second poll loop. The final `event: done` closes the
 * stream and refetches the run (which is now complete / failed / cancelled).
 */
import { useQuery, useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { queryKeys, runEventsUrl } from "./api-client";
import type { RunEventOut, RunOut } from "./types";

export type StreamState = "idle" | "connecting" | "open" | "done" | "error";

export function useRunEvents(runId: string, enabled: boolean): { events: RunEventOut[]; stream: StreamState } {
  const queryClient = useQueryClient();
  const [stream, setStream] = React.useState<StreamState>("idle");

  const { data: events = [] } = useQuery<RunEventOut[]>({
    queryKey: queryKeys.runEvents(runId),
    queryFn: () => queryClient.getQueryData<RunEventOut[]>(queryKeys.runEvents(runId)) ?? [],
    staleTime: Infinity,
    enabled: false,
  });

  React.useEffect(() => {
    if (!enabled) return;
    let es: EventSource | null = null;
    let closed = false;

    runEventsUrl(runId).then((url) => {
      if (closed) return;
      setStream("connecting");
      es = new EventSource(url);
      es.onopen = () => setStream("open");
      es.onmessage = (msg) => {
        let ev: RunEventOut;
        try {
          ev = JSON.parse(msg.data) as RunEventOut;
        } catch {
          return;
        }
        queryClient.setQueryData<RunEventOut[]>(queryKeys.runEvents(runId), (prev = []) =>
          prev.some((p) => p.id === ev.id) ? prev : [...prev, ev].sort((a, b) => a.id - b.id),
        );
        queryClient.setQueryData<RunOut>(queryKeys.run(runId), (prev) =>
          prev
            ? {
                ...prev,
                current_stage: ev.stage,
                progress_pct: ev.progress_pct ?? prev.progress_pct,
              }
            : prev,
        );
      };
      es.addEventListener("done", () => {
        es?.close();
        setStream("done");
        void queryClient.invalidateQueries({ queryKey: queryKeys.run(runId), exact: true });
        void queryClient.invalidateQueries({ queryKey: queryKeys.activeRun, exact: true });
      });
      es.onerror = () => {
        // EventSource retries on its own while CONNECTING; CLOSED means it gave up.
        if (es?.readyState === EventSource.CLOSED) setStream("error");
      };
    });

    return () => {
      closed = true;
      es?.close();
    };
  }, [runId, enabled, queryClient]);

  return { events, stream };
}
