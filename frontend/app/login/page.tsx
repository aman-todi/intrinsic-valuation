"use client";

import Link from "next/link";
import * as React from "react";
import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { signIn } from "@/lib/auth";

export default function LoginPage() {
  const { devBypass } = useAuth();
  const [state, setState] = React.useState<"idle" | "redirecting" | "error">("idle");
  const [error, setError] = React.useState<string | null>(null);

  async function start() {
    setState("redirecting");
    setError(null);
    try {
      await signIn(); // navigates away to Cognito managed login
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not start sign-in.");
      setState("error");
    }
  }

  return (
    <div className="mx-auto w-full max-w-md pt-10">
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">Sign in</CardTitle>
          <CardDescription>You&apos;ll continue to our secure sign-in page and come straight back.</CardDescription>
        </CardHeader>
        <CardContent>
          {devBypass ? (
            <div className="flex flex-col gap-3 text-sm">
              <p className="rounded-md border border-warning/40 bg-warning-bg px-3 py-2 text-warning">
                <strong>Dev auth bypass is on</strong> — <code>NEXT_PUBLIC_COGNITO_CLIENT_ID</code> is not set, so
                you are signed in with a fixed fake token. Never deploy this configuration.
              </p>
              <Link href="/" className="font-medium text-primary underline underline-offset-2">
                Continue to the app
              </Link>
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              <Button type="button" onClick={() => void start()} disabled={state === "redirecting"}>
                {state === "redirecting" ? "Redirecting…" : "Sign in"}
              </Button>
              {error && (
                <p role="alert" className="text-sm text-negative">
                  {error}
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
