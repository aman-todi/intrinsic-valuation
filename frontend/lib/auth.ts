/**
 * Auth — AWS Cognito via OpenID Connect (Authorization Code + PKCE against Cognito managed login),
 * using `oidc-client-ts`. Used ONLY for sign-in and the access token (§10.3); every other read/write
 * goes through our own API (lib/api-client.ts), which sends `Authorization: Bearer <access token>`.
 *
 * - authority = the user pool issuer `https://cognito-idp.<region>.amazonaws.com/<pool id>`, which
 *   serves `/.well-known/openid-configuration` (authorize/token endpoints live on the managed-login
 *   domain NEXT_PUBLIC_COGNITO_DOMAIN).
 * - redirect_uri = `<origin>/auth/callback`, sign-out redirect = `<origin>/`, scopes `openid email`.
 * - Tokens live in localStorage (survive reloads, shared across tabs). The access token is renewed
 *   with the refresh token automatically before it expires (`automaticSilentRenew`) and on demand in
 *   `getAccessToken()` if it has already expired.
 * - Cognito's discovery document has no `end_session_endpoint`, so sign-out clears the local session
 *   and navigates to `<domain>/logout?client_id=…&logout_uri=<origin>/` (also seeded into the
 *   metadata for completeness).
 *
 * DEV AUTH BYPASS: when NEXT_PUBLIC_COGNITO_CLIENT_ID is unset there is no user pool to sign in
 * against, so the app treats the browser as signed in with a fixed fake token (DEV_BYPASS_TOKEN).
 * This lets the app run locally against the mock API (`npm run dev:mock`) or a dev backend started
 * with DEV_AUTH_BYPASS=true. The real backend verifies Cognito access tokens and rejects it.
 */
import { UserManager, WebStorageStateStore, type User } from "oidc-client-ts";

const COGNITO_DOMAIN = (process.env.NEXT_PUBLIC_COGNITO_DOMAIN ?? "").replace(/\/+$/, "");
const COGNITO_CLIENT_ID = process.env.NEXT_PUBLIC_COGNITO_CLIENT_ID ?? "";
const COGNITO_USER_POOL_ID = process.env.NEXT_PUBLIC_COGNITO_USER_POOL_ID ?? "";
const COGNITO_REGION = process.env.NEXT_PUBLIC_COGNITO_REGION ?? "";

export const DEV_BYPASS_TOKEN = "dev-bypass-token";
export const DEV_BYPASS_EMAIL = "dev@localhost";
export const CALLBACK_PATH = "/auth/callback";

/** True when no Cognito app client is configured — see module docstring. */
export const isDevAuthBypass = COGNITO_CLIENT_ID === "";

export interface AuthUser {
  id: string;
  email: string | null;
}

const DEV_USER: AuthUser = { id: "00000000-0000-4000-8000-00000000d3e7", email: DEV_BYPASS_EMAIL };

/** Renew this many seconds before the access token expires. */
const RENEW_MARGIN_S = 60;

function cognitoLogoutUrl(origin: string): string {
  const qs = new URLSearchParams({ client_id: COGNITO_CLIENT_ID, logout_uri: `${origin}/` });
  return `${COGNITO_DOMAIN}/logout?${qs.toString()}`;
}

let manager: UserManager | null = null;

/** The UserManager singleton; null in the dev bypass and during server rendering. */
export function getUserManager(): UserManager | null {
  if (isDevAuthBypass || typeof window === "undefined") return null;
  if (!manager) {
    const origin = window.location.origin;
    manager = new UserManager({
      authority: `https://cognito-idp.${COGNITO_REGION}.amazonaws.com/${COGNITO_USER_POOL_ID}`,
      client_id: COGNITO_CLIENT_ID,
      redirect_uri: `${origin}${CALLBACK_PATH}`,
      post_logout_redirect_uri: `${origin}/`,
      response_type: "code",
      scope: "openid email",
      metadataSeed: { end_session_endpoint: cognitoLogoutUrl(origin) },
      userStore: new WebStorageStateStore({ store: window.localStorage }),
      automaticSilentRenew: true,
      accessTokenExpiringNotificationTimeInSeconds: RENEW_MARGIN_S,
      loadUserInfo: false,
    });
  }
  return manager;
}

function toUser(user: User | null): AuthUser | null {
  if (!user || (user.expired && !user.refresh_token)) return null;
  const email = user.profile.email;
  return { id: user.profile.sub, email: typeof email === "string" ? email : null };
}

/** Current user, or null when signed out. */
export async function getCurrentUser(): Promise<AuthUser | null> {
  if (isDevAuthBypass) return DEV_USER;
  const um = getUserManager();
  if (!um) return null;
  return toUser(await um.getUser());
}

let renewing: Promise<User | null> | null = null;

/** Refresh-token renewal, de-duplicated across concurrent callers. Signs out locally on failure. */
function renew(um: UserManager): Promise<User | null> {
  renewing ??= um
    .signinSilent()
    .catch(async () => {
      await um.removeUser();
      return null;
    })
    .finally(() => {
      renewing = null;
    });
  return renewing;
}

/** Access token to send as `Authorization: Bearer <token>`; null when signed out. */
export async function getAccessToken(): Promise<string | null> {
  if (isDevAuthBypass) return DEV_BYPASS_TOKEN;
  const um = getUserManager();
  if (!um) return null;
  let user = await um.getUser();
  if (!user) return null;
  if ((user.expires_in ?? 0) < 5) {
    if (!user.refresh_token) {
      await um.removeUser();
      return null;
    }
    user = await renew(um);
  }
  return user?.access_token ?? null;
}

/** Redirect to Cognito managed login. */
export async function signIn(): Promise<void> {
  const um = getUserManager();
  if (!um) return;
  await um.signinRedirect();
}

let callbackResult: Promise<AuthUser | null> | null = null;

/** Complete the redirect sign-in on /auth/callback (idempotent: the code can be redeemed only once). */
export function completeSignIn(): Promise<AuthUser | null> {
  const um = getUserManager();
  if (!um) return Promise.resolve(isDevAuthBypass ? DEV_USER : null);
  callbackResult ??= um.signinRedirectCallback().then(toUser);
  return callbackResult;
}

/** Clear the local session and end the Cognito managed-login session (redirects to `<origin>/`). */
export async function signOut(): Promise<void> {
  const um = getUserManager();
  if (!um) return;
  await um.removeUser();
  window.location.assign(cognitoLogoutUrl(window.location.origin));
}

/** Subscribe to sign-in/sign-out/renewal. Returns an unsubscribe function. */
export function onAuthChange(cb: (user: AuthUser | null) => void): () => void {
  const um = getUserManager();
  if (!um) return () => {};
  const loaded = (u: User) => cb(toUser(u));
  const unloaded = () => cb(null);
  const unsubs = [
    um.events.addUserLoaded(loaded),
    um.events.addUserUnloaded(unloaded),
    um.events.addUserSignedOut(unloaded),
  ];
  return () => unsubs.forEach((u) => u());
}
