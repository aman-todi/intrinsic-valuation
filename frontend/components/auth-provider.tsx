"use client";

import { usePathname, useRouter } from "next/navigation";
import * as React from "react";
import { Spinner } from "@/components/ui/spinner";
import {
  CALLBACK_PATH,
  getCurrentUser,
  isDevAuthBypass,
  onAuthChange,
  signOut as authSignOut,
  type AuthUser,
} from "@/lib/auth";

interface AuthContextValue {
  /** undefined while the session is loading, null when signed out. */
  user: AuthUser | null | undefined;
  devBypass: boolean;
  signOut: () => Promise<void>;
}

const AuthContext = React.createContext<AuthContextValue>({
  user: undefined,
  devBypass: isDevAuthBypass,
  signOut: async () => {},
});

export function useAuth(): AuthContextValue {
  return React.useContext(AuthContext);
}

const PUBLIC_PATHS = ["/login", CALLBACK_PATH];

/**
 * Loads the Cognito session (lib/auth.ts) and redirects to /login when signed out (see AuthGate).
 * With the dev auth bypass (no NEXT_PUBLIC_COGNITO_CLIENT_ID) the user is always signed in.
 */
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = React.useState<AuthUser | null | undefined>(undefined);
  const router = useRouter();
  const pathname = usePathname();

  React.useEffect(() => {
    let alive = true;
    getCurrentUser()
      .then((u) => alive && setUser(u))
      .catch(() => alive && setUser(null));
    const unsubscribe = onAuthChange((u) => setUser(u));
    return () => {
      alive = false;
      unsubscribe();
    };
  }, []);

  const isPublic = PUBLIC_PATHS.includes(pathname);

  React.useEffect(() => {
    if (user === null && !isPublic) router.replace("/login");
    if (user && pathname === "/login") router.replace("/");
  }, [user, isPublic, pathname, router]);

  const value = React.useMemo<AuthContextValue>(
    () => ({
      user,
      devBypass: isDevAuthBypass,
      signOut: async () => {
        await authSignOut();
        setUser(isDevAuthBypass ? user : null);
      },
    }),
    [user],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** Renders page content only once signed in (or on a public page like /login). */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const { user } = useAuth();
  const pathname = usePathname();
  if (!PUBLIC_PATHS.includes(pathname) && !user) {
    return (
      <div className="flex flex-1 items-center justify-center p-16">
        <Spinner label="Checking session" />
      </div>
    );
  }
  return <>{children}</>;
}
