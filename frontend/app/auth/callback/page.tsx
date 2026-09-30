"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import * as React from "react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { completeSignIn } from "@/lib/auth";

/** OAuth redirect target: exchanges the authorization code (PKCE) for tokens, then goes home. */
export default function AuthCallbackPage() {
  const router = useRouter();
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    let alive = true;
    completeSignIn()
      .then(() => {
        if (alive) router.replace("/");
      })
      .catch((err: unknown) => {
        if (alive) setError(err instanceof Error ? err.message : "Sign-in failed.");
      });
    return () => {
      alive = false;
    };
  }, [router]);

  if (!error) {
    return (
      <div className="flex flex-1 items-center justify-center p-16">
        <Spinner label="Signing you in" />
      </div>
    );
  }
  return (
    <div className="mx-auto w-full max-w-md pt-10">
      <Card>
        <CardHeader>
          <CardTitle className="text-xl">Sign-in failed</CardTitle>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 text-sm">
          <p role="alert" className="text-negative">
            {error}
          </p>
          <Link href="/login" className="font-medium text-primary underline underline-offset-2">
            Try again
          </Link>
        </CardContent>
      </Card>
    </div>
  );
}
