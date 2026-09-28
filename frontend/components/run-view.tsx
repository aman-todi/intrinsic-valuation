"use client";

/**
 * /runs/{id}: rendered content is purely a function of `run.status` (§10.1).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import * as React from "react";
import {
  AssumptionsForm,
  EditableToggle,
  assumptionsDiffer,
  isEditableAssumptions,
} from "@/components/assumptions-form";
import { ModelConfirmCard } from "@/components/model-confirm-card";
import { ProgressView } from "@/components/progress-view";
import { ResultView } from "@/components/result-view";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { ApiError, api, queryKeys } from "@/lib/api-client";
import { humanizeFieldName } from "@/lib/format";
import { runRefetchInterval } from "@/lib/run-status";
import type { AssumptionsPayload, ModelType, RunOut } from "@/lib/types";
import { useRunEvents } from "@/lib/use-run-events";

function Centered({ children }: { children: React.ReactNode }) {
  return <div className="flex flex-1 flex-col items-center justify-center gap-4 p-16 text-center">{children}</div>;
}

function LookingUp({ run }: { run: RunOut }) {
  return (
    <Centered>
      <Spinner className="h-8 w-8" label={`Looking up ${run.ticker}`} />
      <div>
        <p className="text-lg font-medium" data-testid="looking-up">
          Looking up {run.ticker}…
        </p>
        <p className="text-sm text-muted-foreground">
          {run.status === "classifying"
            ? "Reading SEC filings and choosing the right valuation model."
            : "Proposing assumptions with rationale and sources."}
        </p>
      </div>
    </Centered>
  );
}

function TerminalState({ run }: { run: RunOut }) {
  const router = useRouter();
  const cancelled = run.status === "cancelled";
  return (
    <Centered>
      <Card className="w-full max-w-lg text-left" data-testid="terminal-state">
        <CardHeader>
          <CardTitle>{cancelled ? "Cancelled" : `Couldn't value ${run.ticker}`}</CardTitle>
          <CardDescription>
            {cancelled
              ? "The build was cancelled. Nothing was saved."
              : run.decline_reason
                ? `Declined: ${humanizeFieldName(run.decline_reason)}`
                : "The run failed."}
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {!cancelled && run.error_message && (
            <p role="alert" className="text-sm">
              {run.error_message}
            </p>
          )}
          <div>
            <Button onClick={() => router.push("/?new=1")}>Start over</Button>
          </div>
        </CardContent>
      </Card>
    </Centered>
  );
}

function ConfirmView({ run }: { run: RunOut }) {
  const queryClient = useQueryClient();
  const proposed = React.useMemo(() => run.proposed_assumptions ?? {}, [run.proposed_assumptions]);
  const [editable, setEditable] = React.useState(false);
  const [values, setValues] = React.useState<AssumptionsPayload>(proposed);
  const [override, setOverride] = React.useState<ModelType | null>(null);

  const canEdit = isEditableAssumptions(proposed) && !override;
  const edited = editable && canEdit && assumptionsDiffer(values, proposed);
  const cacheHit = run.cache_hit_available && !edited && !override;

  const confirm = useMutation({
    mutationFn: () =>
      api.confirmRun(run.id, {
        model_type_override: override,
        // Unedited runs build from the server-side proposal (and the shared cache).
        assumptions: edited ? values : null,
        edited,
      }),
    onSuccess: (updated) => {
      queryClient.setQueryData(queryKeys.run(run.id), updated);
      void queryClient.invalidateQueries({ queryKey: queryKeys.activeRun, exact: true });
    },
  });

  return (
    <div className="flex flex-col gap-6">
      <ModelConfirmCard run={run} override={override} onOverrideChange={setOverride} disabled={confirm.isPending} />
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-4">
          <div>
            <CardTitle>Assumptions</CardTitle>
            <CardDescription>
              {override
                ? "Assumptions will be re-proposed for the selected model."
                : editable
                  ? "Edited assumptions build a private result and skip the shared cache."
                  : "Proposed by AI from filings and industry data. Each line shows its rationale and source."}
            </CardDescription>
          </div>
          <EditableToggle
            checked={editable && canEdit}
            onCheckedChange={(v) => {
              setEditable(v);
              if (!v) setValues(proposed);
            }}
            disabled={!canEdit || confirm.isPending}
            disabledReason={override ? "Not available with a model override." : "These assumptions can't be edited."}
          />
        </CardHeader>
        <CardContent>
          <AssumptionsForm
            assumptions={editable && canEdit ? values : proposed}
            editable={editable && canEdit}
            onChange={setValues}
            schema={run.assumptions_schema}
          />
        </CardContent>
      </Card>
      <div className="sticky bottom-0 -mx-4 flex flex-wrap items-center justify-end gap-3 border-t border-border bg-background/95 px-4 py-3 backdrop-blur sm:-mx-6 sm:px-6">
        {confirm.isError && (
          <p role="alert" className="mr-auto text-sm text-negative">
            {confirm.error instanceof Error ? confirm.error.message : "Could not start the build."}
          </p>
        )}
        {edited && <span className="text-xs text-warning">Edited — this result will be private to you.</span>}
        <Button size="lg" onClick={() => confirm.mutate()} disabled={confirm.isPending}>
          {confirm.isPending ? "Starting…" : cacheHit ? "View model" : "Build model"}
        </Button>
      </div>
    </div>
  );
}

export function RunView({ id }: { id: string }) {
  const router = useRouter();
  const q = useQuery({
    queryKey: queryKeys.run(id),
    queryFn: () => api.getRun(id),
    refetchInterval: (query) => runRefetchInterval(query.state.data?.status),
  });
  const run = q.data;
  const building = run?.status === "building";
  const { events, stream } = useRunEvents(id, building);

  // Keep the global lock (ActiveRunGuard) in step with this run's status transitions, e.g. a
  // cancel that lands before the SSE `done` event, so "/" doesn't bounce back here.
  const queryClient = useQueryClient();
  const status = run?.status;
  React.useEffect(() => {
    if (status) void queryClient.invalidateQueries({ queryKey: queryKeys.activeRun, exact: true });
  }, [status, queryClient]);

  // Fallback if the SSE stream dies: poll the run until it leaves `building`.
  const { refetch } = q;
  React.useEffect(() => {
    if (!building || stream !== "error") return;
    const t = setInterval(() => void refetch(), 3000);
    return () => clearInterval(t);
  }, [building, stream, refetch]);

  if (q.isLoading) {
    return (
      <Centered>
        <Spinner label="Loading run" />
      </Centered>
    );
  }
  if (q.isError || !run) {
    const notFound = q.error instanceof ApiError && q.error.status === 404;
    return (
      <Centered>
        <p role="alert" className="text-negative">
          {notFound ? "Run not found." : `Could not load the run: ${q.error instanceof Error ? q.error.message : ""}`}
        </p>
        <Button variant="outline" onClick={() => router.push("/?new=1")}>
          Back to start
        </Button>
      </Centered>
    );
  }

  switch (run.status) {
    case "classifying":
    case "proposing":
      return <LookingUp run={run} />;
    case "awaiting_confirm":
      return <ConfirmView key={run.id} run={run} />;
    case "building":
      return (
        <>
          <Centered>
            <p className="text-muted-foreground">Building {run.ticker}…</p>
          </Centered>
          <ProgressView run={run} events={events} stream={stream} />
        </>
      );
    case "complete":
      return <ResultView run={run} />;
    case "failed":
    case "cancelled":
      return <TerminalState run={run} />;
  }
}
