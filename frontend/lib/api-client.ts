/**
 * Typed client for the backend REST/SSE contract (§9.1).
 *
 * Every request carries `Authorization: Bearer <Cognito access token>`. The base URL comes
 * from NEXT_PUBLIC_API_BASE_URL (inlined at build time); when empty, requests are relative
 * to the current origin (used by the MSW mock in `npm run dev:mock`).
 */
import { getAccessToken } from "./auth";
import type {
  ActiveRunConflict,
  ConfirmRunRequest,
  RunOut,
  RunResultOut,
} from "./types";

export function apiBaseUrl(): string {
  return (process.env.NEXT_PUBLIC_API_BASE_URL ?? "").replace(/\/+$/, "");
}

export class ApiError extends Error {
  readonly status: number;
  readonly body: unknown;

  constructor(status: number, message: string, body: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.body = body;
  }
}

/** 409 from POST /api/runs: the user already has an active run (§8.1). */
export class ActiveRunConflictError extends ApiError {
  readonly activeRunId: string;

  constructor(body: ActiveRunConflict) {
    super(409, body.detail || "You already have an active run.", body);
    this.name = "ActiveRunConflictError";
    this.activeRunId = body.active_run_id;
  }
}

/** 401 — the session is missing or expired. */
export class UnauthorizedError extends ApiError {
  constructor(body: unknown) {
    super(401, "Your session has expired. Please sign in again.", body);
    this.name = "UnauthorizedError";
  }
}

function detailOf(body: unknown, fallback: string): string {
  if (body && typeof body === "object" && "detail" in body) {
    const d = (body as { detail: unknown }).detail;
    if (typeof d === "string") return d;
    if (Array.isArray(d)) {
      // FastAPI validation errors: [{loc, msg, type}, ...]
      return d
        .map((e) => (e && typeof e === "object" && "msg" in e ? String((e as { msg: unknown }).msg) : String(e)))
        .join("; ");
    }
  }
  return fallback;
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = await getAccessToken();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body !== undefined) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const res = await fetch(`${apiBaseUrl()}${path}`, { ...init, headers });

  if (res.status === 204) return null as T;

  const text = await res.text();
  let body: unknown = null;
  if (text) {
    try {
      body = JSON.parse(text);
    } catch {
      body = text;
    }
  }

  if (!res.ok) {
    if (res.status === 409 && body && typeof body === "object" && "active_run_id" in body) {
      throw new ActiveRunConflictError(body as ActiveRunConflict);
    }
    if (res.status === 401) throw new UnauthorizedError(body);
    throw new ApiError(res.status, detailOf(body, `Request failed (${res.status})`), body);
  }
  return body as T;
}

export const api = {
  /** GET /api/runs/active — the user's active or awaiting_confirm run, or null (204). */
  getActiveRun: () => request<RunOut | null>("/api/runs/active"),

  /** POST /api/runs — throws ActiveRunConflictError on 409. */
  createRun: (ticker: string) =>
    request<RunOut>("/api/runs", {
      method: "POST",
      body: JSON.stringify({ ticker: ticker.trim().toUpperCase(), mode: "auto" }),
    }),

  getRun: (id: string) => request<RunOut>(`/api/runs/${encodeURIComponent(id)}`),

  confirmRun: (id: string, body: ConfirmRunRequest) =>
    request<RunOut>(`/api/runs/${encodeURIComponent(id)}/confirm`, {
      method: "POST",
      body: JSON.stringify(body),
    }),

  cancelRun: (id: string) =>
    request<RunOut>(`/api/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),

  getResult: (id: string) => request<RunResultOut>(`/api/runs/${encodeURIComponent(id)}/result`),

  /** DELETE /api/me — the account, its runs and private downloads (409 while a build is running). */
  deleteAccount: () => request<null>("/api/me", { method: "DELETE" }),
};

/**
 * URL for the SSE progress stream. EventSource cannot send headers, so the access token
 * travels as `?access_token=`. The backend's /events route must accept it (see README).
 */
export async function runEventsUrl(id: string): Promise<string> {
  const token = await getAccessToken();
  const qs = token ? `?access_token=${encodeURIComponent(token)}` : "";
  return `${apiBaseUrl()}/api/runs/${encodeURIComponent(id)}/events${qs}`;
}

export const queryKeys = {
  activeRun: ["runs", "active"] as const,
  run: (id: string) => ["runs", id] as const,
  runEvents: (id: string) => ["runs", id, "events"] as const,
  result: (id: string) => ["runs", id, "result"] as const,
};
