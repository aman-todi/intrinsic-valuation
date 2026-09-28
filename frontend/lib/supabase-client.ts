/**
 * Supabase — used ONLY for auth (magic-link sign-in, session, access token), per §10.3.
 * Every other read/write goes through our own API (lib/api-client.ts).
 *
 * DEV AUTH BYPASS: when NEXT_PUBLIC_SUPABASE_URL is unset, there is no Supabase project to
 * sign in against, so the app treats the browser as signed in with a fixed fake token
 * (DEV_BYPASS_TOKEN). This lets the app run locally against the mock API (`npm run dev:mock`)
 * or a dev backend configured to accept that token. It must never be relied on in production:
 * the real backend verifies JWTs against Supabase's JWKS and will reject the fake token.
 */
import { createClient, type Session, type SupabaseClient } from "@supabase/supabase-js";

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL ?? "";
const SUPABASE_ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY ?? "";

export const DEV_BYPASS_TOKEN = "dev-bypass-token";
export const DEV_BYPASS_EMAIL = "dev@localhost";

/** True when no Supabase project is configured — see module docstring. */
export const isDevAuthBypass = SUPABASE_URL === "";

export interface AuthUser {
  id: string;
  email: string | null;
}

let client: SupabaseClient | null = null;

export function getSupabase(): SupabaseClient | null {
  if (isDevAuthBypass) return null;
  if (!client) {
    client = createClient(SUPABASE_URL, SUPABASE_ANON_KEY, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true },
    });
  }
  return client;
}

function toUser(session: Session | null): AuthUser | null {
  if (!session) return null;
  return { id: session.user.id, email: session.user.email ?? null };
}

const DEV_USER: AuthUser = { id: "00000000-0000-0000-0000-000000000dev", email: DEV_BYPASS_EMAIL };

/** Current user, or null when signed out. */
export async function getCurrentUser(): Promise<AuthUser | null> {
  if (isDevAuthBypass) return DEV_USER;
  const { data } = await getSupabase()!.auth.getSession();
  return toUser(data.session);
}

/** Access token to send as `Authorization: Bearer <token>`; null when signed out. */
export async function getAccessToken(): Promise<string | null> {
  if (isDevAuthBypass) return DEV_BYPASS_TOKEN;
  const { data } = await getSupabase()!.auth.getSession();
  return data.session?.access_token ?? null;
}

/** Send a magic link. Resolves on success, throws with Supabase's message on failure. */
export async function signInWithMagicLink(email: string): Promise<void> {
  if (isDevAuthBypass) return;
  const { error } = await getSupabase()!.auth.signInWithOtp({
    email,
    options: { emailRedirectTo: typeof window !== "undefined" ? window.location.origin : undefined },
  });
  if (error) throw new Error(error.message);
}

export async function signOut(): Promise<void> {
  if (isDevAuthBypass) return;
  await getSupabase()!.auth.signOut();
}

/** Subscribe to sign-in/sign-out. Returns an unsubscribe function. */
export function onAuthChange(cb: (user: AuthUser | null) => void): () => void {
  if (isDevAuthBypass) return () => {};
  const { data } = getSupabase()!.auth.onAuthStateChange((_event, session) => cb(toUser(session)));
  return () => data.subscription.unsubscribe();
}
