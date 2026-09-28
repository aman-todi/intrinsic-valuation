"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as React from "react";
import { ActiveRunProvider } from "@/components/active-run-guard";
import { AppLockProvider, AppShell } from "@/components/app-shell";
import { AuthProvider, useAuth } from "@/components/auth-provider";
import { Spinner } from "@/components/ui/spinner";
import { ApiError } from "@/lib/api-client";

const MOCKING = process.env.NEXT_PUBLIC_API_MOCKING === "enabled";

// Module-level so React StrictMode's double effect doesn't start the worker twice.
let mockWorkerStarted: Promise<unknown> | null = null;
function startMockWorker(): Promise<unknown> {
  mockWorkerStarted ??= import("@/mocks/browser").then(({ worker }) =>
    worker.start({ onUnhandledRequest: "bypass", quiet: false }),
  );
  return mockWorkerStarted;
}

/** In `npm run dev:mock`, start the MSW service worker before anything fetches. */
function MockGate({ children }: { children: React.ReactNode }) {
  const [ready, setReady] = React.useState(!MOCKING);
  React.useEffect(() => {
    if (!MOCKING) return;
    let alive = true;
    startMockWorker().then(() => alive && setReady(true));
    return () => {
      alive = false;
    };
  }, []);
  if (!ready) {
    return (
      <div className="flex flex-1 items-center justify-center p-16">
        <Spinner label="Starting mock API" />
      </div>
    );
  }
  return <>{children}</>;
}

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        refetchOnWindowFocus: false,
        // Don't retry client errors (404 run not found, 401, 409...) — only transient failures.
        retry: (count, err) => !(err instanceof ApiError && err.status < 500) && count < 2,
      },
    },
  });
}

function AuthedActiveRunProvider({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  return <ActiveRunProvider enabled={!!user}>{children}</ActiveRunProvider>;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = React.useState(makeQueryClient);
  return (
    <MockGate>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <AuthedActiveRunProvider>
            <AppLockProvider>
              <AppShell>{children}</AppShell>
            </AppLockProvider>
          </AuthedActiveRunProvider>
        </AuthProvider>
      </QueryClientProvider>
    </MockGate>
  );
}
