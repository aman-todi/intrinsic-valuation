"use client";

/**
 * App chrome + the global "inert" lock used while a model is building (§10.1): ProgressView
 * calls `useAppLock(true)`, which sets the `inert` attribute on the whole shell (nav, forms,
 * everything). ProgressView itself is portalled to <body>, outside the shell, so its Cancel
 * button stays interactive.
 */
import Link from "next/link";
import * as React from "react";
import { AuthGate, useAuth } from "@/components/auth-provider";
import { useActiveRun } from "@/components/active-run-guard";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

interface AppLockContextValue {
  locked: boolean;
  acquire: () => () => void;
}

const AppLockContext = React.createContext<AppLockContextValue>({
  locked: false,
  acquire: () => () => {},
});

export function AppLockProvider({ children }: { children: React.ReactNode }) {
  const [count, setCount] = React.useState(0);
  const acquire = React.useCallback(() => {
    setCount((c) => c + 1);
    let released = false;
    return () => {
      if (released) return;
      released = true;
      setCount((c) => c - 1);
    };
  }, []);
  const value = React.useMemo(() => ({ locked: count > 0, acquire }), [count, acquire]);
  return <AppLockContext.Provider value={value}>{children}</AppLockContext.Provider>;
}

/** While `active`, the app shell is inert. */
export function useAppLock(active: boolean): void {
  const { acquire } = React.useContext(AppLockContext);
  React.useEffect(() => {
    if (!active) return;
    return acquire();
  }, [active, acquire]);
}

export function useAppLocked(): boolean {
  return React.useContext(AppLockContext).locked;
}

function Header() {
  const { user, devBypass, signOut } = useAuth();
  const { locked } = useActiveRun();

  return (
    <header className="border-b border-border bg-card">
      <div className="mx-auto flex h-14 w-full max-w-6xl items-center justify-between gap-4 px-4 sm:px-6">
        <div className="flex items-center gap-6">
          <Link href="/" className="flex items-center gap-2 font-semibold tracking-tight">
            <span aria-hidden className="inline-flex h-7 w-7 items-center justify-center rounded-md bg-primary text-xs font-bold text-primary-foreground">
              IV
            </span>
            Intrinsic Value
          </Link>
          {user && (
            <nav aria-label="Main" className="text-sm">
              {locked ? (
                <span className="cursor-not-allowed text-muted-foreground" aria-disabled="true" title="A run is in progress">
                  New valuation
                </span>
              ) : (
                <Link href="/?new=1" className="text-muted-foreground hover:text-foreground">
                  New valuation
                </Link>
              )}
            </nav>
          )}
        </div>
        <div className="flex items-center gap-3 text-sm">
          {devBypass && (
            <Badge variant="warning" title="NEXT_PUBLIC_COGNITO_CLIENT_ID is unset — using a fixed fake token">
              Dev auth bypass
            </Badge>
          )}
          {user && <span className="hidden text-muted-foreground sm:inline">{user.email}</span>}
          {user && !devBypass && (
            <Button variant="ghost" size="sm" onClick={() => void signOut()}>
              Sign out
            </Button>
          )}
        </div>
      </div>
    </header>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const locked = useAppLocked();
  return (
    <div id="app-shell" data-testid="app-shell" inert={locked} className="flex min-h-full flex-1 flex-col">
      <Header />
      <main className="mx-auto flex w-full max-w-6xl flex-1 flex-col px-4 py-8 sm:px-6">
        <AuthGate>{children}</AuthGate>
      </main>
      <footer className="border-t border-border py-4 text-center text-xs text-muted-foreground">
        Valuations are estimates for research purposes only — not investment advice.
      </footer>
    </div>
  );
}
