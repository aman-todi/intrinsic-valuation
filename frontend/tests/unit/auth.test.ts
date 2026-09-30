import { User } from "oidc-client-ts";
import { afterEach, describe, expect, it, vi } from "vitest";

const COGNITO_ENV = {
  NEXT_PUBLIC_COGNITO_DOMAIN: "https://dcf-test.auth.us-east-1.amazoncognito.com/",
  NEXT_PUBLIC_COGNITO_CLIENT_ID: "client-123",
  NEXT_PUBLIC_COGNITO_USER_POOL_ID: "us-east-1_Pool",
  NEXT_PUBLIC_COGNITO_REGION: "us-east-1",
};

async function loadAuth(env: Record<string, string>) {
  vi.resetModules();
  for (const [k, v] of Object.entries(env)) vi.stubEnv(k, v);
  return import("@/lib/auth");
}

function user(expiresInS: number, refresh = "rt-1"): User {
  return new User({
    access_token: `at-${expiresInS}`,
    token_type: "Bearer",
    refresh_token: refresh,
    expires_at: Math.floor(Date.now() / 1000) + expiresInS,
    profile: { sub: "sub-1", email: "a@example.com", iss: "x", aud: "client-123", exp: 0, iat: 0 },
  });
}

afterEach(() => {
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
  window.localStorage.clear();
});

describe("lib/auth", () => {
  it("configures Cognito code + PKCE flow and falls back to the dev bypass without a client id", async () => {
    const bypass = await loadAuth({ NEXT_PUBLIC_COGNITO_CLIENT_ID: "" });
    expect(bypass.isDevAuthBypass).toBe(true);
    expect(await bypass.getAccessToken()).toBe("dev-bypass-token");
    expect(bypass.getUserManager()).toBeNull();

    const auth = await loadAuth(COGNITO_ENV);
    expect(auth.isDevAuthBypass).toBe(false);
    const s = auth.getUserManager()!.settings;
    const origin = window.location.origin;
    expect(s.authority).toBe("https://cognito-idp.us-east-1.amazonaws.com/us-east-1_Pool");
    expect(s.client_id).toBe("client-123");
    expect(s.redirect_uri).toBe(`${origin}/auth/callback`);
    expect(s.post_logout_redirect_uri).toBe(`${origin}/`);
    expect(s.response_type).toBe("code");
    expect(s.scope).toBe("openid email");
    expect(s.metadataSeed?.end_session_endpoint).toBe(
      `https://dcf-test.auth.us-east-1.amazoncognito.com/logout?client_id=client-123&logout_uri=${encodeURIComponent(`${origin}/`)}`,
    );
  });

  it("returns the stored access token and renews an expired one with the refresh token", async () => {
    const auth = await loadAuth(COGNITO_ENV);
    const um = auth.getUserManager()!;
    expect(await auth.getAccessToken()).toBeNull(); // signed out

    await um.storeUser(user(3600));
    expect(await auth.getAccessToken()).toBe("at-3600");
    expect(await auth.getCurrentUser()).toEqual({ id: "sub-1", email: "a@example.com" });

    await um.storeUser(user(-10));
    const renewed = user(3600);
    const silent = vi.spyOn(um, "signinSilent").mockResolvedValue(renewed);
    const [a, b] = await Promise.all([auth.getAccessToken(), auth.getAccessToken()]);
    expect([a, b]).toEqual(["at-3600", "at-3600"]);
    expect(silent).toHaveBeenCalledTimes(1); // concurrent callers share one renewal

    // renewal failure (refresh token revoked/expired) signs out locally
    await um.storeUser(user(-10));
    silent.mockRejectedValueOnce(new Error("invalid_grant"));
    expect(await auth.getAccessToken()).toBeNull();
    expect(await um.getUser()).toBeNull();
  });
});
