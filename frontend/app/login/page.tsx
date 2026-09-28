"use client";

import Link from "next/link";
import * as React from "react";
import { useAuth } from "@/components/auth-provider";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { signInWithMagicLink } from "@/lib/supabase-client";

export default function LoginPage() {
  const { devBypass } = useAuth();
  const [email, setEmail] = React.useState("");
  const [state, setState] = React.useState<"idle" | "sending" | "sent" | "error">("idle");
  const [error, setError] = React.useState<string | null>(null);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setState("sending");
    setError(null);
    try {
      await signInWithMagicLink(email.trim());
      setState("sent");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not send the link.");
      setState("error");
    }
  }

  return (
    <div className="mx-auto w-full max-w-md pt-10">
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">Sign in</CardTitle>
          <CardDescription>We&apos;ll email you a one-time magic link. No password needed.</CardDescription>
        </CardHeader>
        <CardContent>
          {devBypass ? (
            <div className="flex flex-col gap-3 text-sm">
              <p className="rounded-md border border-warning/40 bg-warning-bg px-3 py-2 text-warning">
                <strong>Dev auth bypass is on</strong> — <code>NEXT_PUBLIC_SUPABASE_URL</code> is not set, so you are
                signed in with a fixed fake token. Never deploy this configuration.
              </p>
              <Link href="/" className="font-medium text-primary underline underline-offset-2">
                Continue to the app
              </Link>
            </div>
          ) : state === "sent" ? (
            <p role="status" className="text-sm">
              Check <strong>{email}</strong> for a sign-in link. You can close this tab.
            </p>
          ) : (
            <form onSubmit={submit} className="flex flex-col gap-3">
              <Label htmlFor="email">Email</Label>
              <Input
                id="email"
                type="email"
                required
                autoComplete="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="you@example.com"
              />
              <Button type="submit" disabled={state === "sending" || email.trim() === ""}>
                {state === "sending" ? "Sending…" : "Send magic link"}
              </Button>
              {error && (
                <p role="alert" className="text-sm text-negative">
                  {error}
                </p>
              )}
            </form>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
