import type { RunStatus } from "./types";

/** Worker-owned states — covered by the one-active-run-per-user DB constraint (§8.1). */
export const ACTIVE_STATUSES: readonly RunStatus[] = ["classifying", "proposing", "building"];

/** Final states — nothing more will happen to the run. */
export const TERMINAL_STATUSES: readonly RunStatus[] = ["complete", "failed", "cancelled"];

/** Statuses during which the run page polls GET /api/runs/{id} (§10.3). */
export const POLLING_STATUSES: readonly RunStatus[] = ["classifying", "proposing"];

export const RUN_POLL_INTERVAL_MS = 2000;

export function isActive(status: RunStatus | null | undefined): boolean {
  return !!status && ACTIVE_STATUSES.includes(status);
}

export function isTerminal(status: RunStatus | null | undefined): boolean {
  return !!status && TERMINAL_STATUSES.includes(status);
}

/** awaiting_confirm is a waiting state: no worker owns it and it does NOT lock new runs. */
export function isWaiting(status: RunStatus | null | undefined): boolean {
  return status === "awaiting_confirm";
}

/** True while the run page should keep polling the run row. */
export function shouldPoll(status: RunStatus | null | undefined): boolean {
  return !!status && POLLING_STATUSES.includes(status);
}

/** TanStack Query `refetchInterval` value for a run in the given status. */
export function runRefetchInterval(status: RunStatus | null | undefined): number | false {
  return shouldPoll(status) ? RUN_POLL_INTERVAL_MS : false;
}

export const STATUS_LABELS: Record<RunStatus, string> = {
  classifying: "Classifying",
  proposing: "Proposing assumptions",
  awaiting_confirm: "Awaiting confirmation",
  building: "Building",
  complete: "Complete",
  failed: "Failed",
  cancelled: "Cancelled",
};
