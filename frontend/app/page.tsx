"use client";

import { useRouter, useSearchParams } from "next/navigation";
import * as React from "react";
import { useActiveRun } from "@/components/active-run-guard";
import { TickerForm } from "@/components/ticker-form";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { isActive } from "@/lib/run-status";

const MODELS = [
  ["FCFF / FCFE", "Operating companies — unlevered or levered DCF"],
  ["Excess Return", "Banks and P&C insurers"],
  ["REIT NAV", "NOI capitalized at a market cap rate"],
  ["E&P NAV", "SEC standardized measure, re-priced to your deck"],
  ["Sum of the Parts", "Conglomerates with distinct segments"],
];

function Home() {
  const router = useRouter();
  const params = useSearchParams();
  const { activeRun: cachedActiveRun, refetch } = useActiveRun();
  // Decide from a fresh GET /api/runs/active, never from a possibly stale cache entry.
  const [fresh, setFresh] = React.useState(false);
  React.useEffect(() => {
    let alive = true;
    refetch().finally(() => alive && setFresh(true));
    return () => {
      alive = false;
    };
  }, [refetch]);
  const activeRun = fresh ? cachedActiveRun : undefined;
  // `?new=1` = the user explicitly wants a new ticker; only a *locking* run redirects then.
  const wantsNew = params.get("new") === "1";
  const [checkedOnLoad, setCheckedOnLoad] = React.useState(false);
  const redirectOnLoad = !!activeRun && (isActive(activeRun.status) || !wantsNew);

  // On load only: restore whatever screen the user's run implies (§10.1). After that, a run
  // started elsewhere just locks the form via ActiveRunGuard instead of yanking the page away.
  if (!checkedOnLoad && activeRun !== undefined && !redirectOnLoad) setCheckedOnLoad(true);

  React.useEffect(() => {
    if (!checkedOnLoad && activeRun && redirectOnLoad) router.replace(`/runs/${activeRun.id}`);
  }, [checkedOnLoad, activeRun, redirectOnLoad, router]);

  if (!checkedOnLoad) {
    return (
      <div className="flex flex-1 items-center justify-center p-16">
        <Spinner label="Checking for an in-progress run" />
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-8 pt-6">
      <div className="flex flex-col gap-2">
        <h1 className="text-3xl font-semibold tracking-tight">Value a US-listed company</h1>
        <p className="text-muted-foreground">
          Enter a ticker. We pick the right model from its SEC filings, propose assumptions with rationale and
          sources, and build a live-formula Excel model and PDF write-up.
        </p>
      </div>
      <Card>
        <CardContent className="pt-5">
          <TickerForm />
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Supported models</CardTitle>
          <CardDescription>
            Pre-revenue biotech, life insurers, SPACs/trusts/MLPs and miners are declined rather than forced into a
            DCF.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ul className="grid gap-2 text-sm sm:grid-cols-2">
            {MODELS.map(([name, desc]) => (
              <li key={name} className="rounded-md border border-border px-3 py-2">
                <div className="font-medium">{name}</div>
                <div className="text-muted-foreground">{desc}</div>
              </li>
            ))}
          </ul>
        </CardContent>
      </Card>
    </div>
  );
}

export default function HomePage() {
  return (
    <React.Suspense fallback={null}>
      <Home />
    </React.Suspense>
  );
}
