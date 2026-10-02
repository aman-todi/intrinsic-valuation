/**
 * MSW v2 handlers implementing the §9.1 run contract against an in-memory, scripted run.
 *
 *   POST /api/runs             -> classifying  (409 + active_run_id if an active run exists)
 *   (time passes)              -> proposing -> awaiting_confirm
 *   POST /api/runs/{id}/confirm-> building
 *   GET  /api/runs/{id}/events -> SSE: `data: RunEventOut` per stage, then `event: done`
 *   (time passes)              -> complete (or cancelled after POST /cancel)
 *
 * Status is derived lazily from wall-clock time on every read, so the mock behaves the same
 * whether or not the client holds the SSE stream open. Used by `npm run dev:mock` (browser
 * service worker), Vitest (msw/node) and Playwright (via msw's getResponse inside page.route).
 *
 * Special tickers: FAIL -> classifier declines (failed); ERR -> build fails.
 *
 * Relative imports only (no "@/") so Playwright can load this file directly.
 */
import { http, HttpResponse, type RequestHandler } from "msw";
import type { ConfirmRunRequest, RunEventOut, RunOut, RunStatus } from "../lib/types";
import { BUILD_STAGES, blankClassification, makeResultOut, makeRun } from "./fixtures";

export interface MockTimings {
  classifyMs: number;
  proposeMs: number;
  buildStepMs: number;
  cacheHitStepMs: number;
}

export const DEFAULT_TIMINGS: MockTimings = {
  classifyMs: 1500,
  proposeMs: 1500,
  buildStepMs: 900,
  cacheHitStepMs: 60,
};

interface MockRunRecord {
  run: RunOut;
  createdAt: number;
  confirmedAt: number | null;
  stepMs: number;
  /** Terminal status forced by the test/cancel, overrides the time script. */
  forced: RunStatus | null;
  events: RunEventOut[];
}

const ACTIVE: RunStatus[] = ["classifying", "proposing", "building"];

let idCounter = 0;
function newId(): string {
  idCounter += 1;
  const hex = (Date.now().toString(16) + idCounter.toString(16).padStart(4, "0")).slice(-12).padStart(12, "0");
  return `00000000-0000-4000-8000-${hex}`;
}

export class MockApiState {
  runs = new Map<string, MockRunRecord>();
  timings: MockTimings;

  constructor(timings: Partial<MockTimings> = {}) {
    this.timings = { ...DEFAULT_TIMINGS, ...timings };
  }

  reset() {
    this.runs.clear();
  }

  /** Seed a run directly in a given status (used by tests). */
  seed(overrides: Partial<RunOut> & { status: RunStatus }, opts: { stepMs?: number } = {}): RunOut {
    const id = overrides.id ?? newId();
    const now = Date.now();
    const run = makeRun({ ...overrides, id });
    const record: MockRunRecord = {
      run,
      createdAt: now - this.timings.classifyMs - this.timings.proposeMs,
      confirmedAt: overrides.status === "building" ? now : null,
      stepMs: opts.stepMs ?? this.timings.buildStepMs,
      forced: ["complete", "failed", "cancelled", "awaiting_confirm"].includes(overrides.status)
        ? overrides.status
        : null,
      events: [],
    };
    if (overrides.status === "classifying" || overrides.status === "proposing") {
      record.createdAt = now - (overrides.status === "proposing" ? this.timings.classifyMs : 0);
      Object.assign(record.run, blankClassification(), overrides);
    }
    this.runs.set(id, record);
    return this.advance(record);
  }

  create(ticker: string): RunOut {
    const id = newId();
    const now = Date.now();
    const iso = new Date(now).toISOString();
    const run = makeRun({
      id,
      ticker,
      status: "classifying",
      ...blankClassification(),
      created_at: iso,
      updated_at: iso,
      started_at: iso,
    });
    const record: MockRunRecord = {
      run,
      createdAt: now,
      confirmedAt: null,
      stepMs: this.timings.buildStepMs,
      forced: null,
      events: [],
    };
    this.runs.set(id, record);
    return this.advance(record);
  }

  get(id: string): MockRunRecord | undefined {
    const rec = this.runs.get(id);
    if (rec) this.advance(rec);
    return rec;
  }

  activeOrWaiting(): RunOut | null {
    let latest: MockRunRecord | null = null;
    for (const rec of this.runs.values()) {
      this.advance(rec);
      if (ACTIVE.includes(rec.run.status) || rec.run.status === "awaiting_confirm") {
        if (!latest || rec.createdAt >= latest.createdAt) latest = rec;
      }
    }
    return latest?.run ?? null;
  }

  activeRun(): RunOut | null {
    for (const rec of this.runs.values()) {
      this.advance(rec);
      if (ACTIVE.includes(rec.run.status)) return rec.run;
    }
    return null;
  }

  confirm(rec: MockRunRecord, body: ConfirmRunRequest): void {
    const now = Date.now();
    const cacheHit = rec.run.cache_hit_available && !body.edited && !body.model_type_override;
    rec.confirmedAt = now;
    rec.forced = null;
    rec.stepMs = cacheHit ? this.timings.cacheHitStepMs : this.timings.buildStepMs;
    rec.run.assumptions_edited = body.edited;
    rec.run.final_assumptions = body.assumptions ?? rec.run.proposed_assumptions;
    if (body.model_type_override) rec.run.model_type = body.model_type_override;
    rec.run.status = "building";
    rec.run.progress_pct = 0;
    rec.run.current_stage = "queued";
    rec.run.updated_at = new Date(now).toISOString();
  }

  cancel(rec: MockRunRecord): void {
    rec.run.cancel_requested = true;
    if (ACTIVE.includes(rec.run.status)) {
      rec.forced = "cancelled";
      this.advance(rec);
    }
  }

  /** Bring a run's status/events up to date with wall-clock time. */
  advance(rec: MockRunRecord): RunOut {
    const now = Date.now();
    const run = rec.run;
    if (rec.forced) {
      if (run.status !== rec.forced) {
        run.status = rec.forced;
        run.updated_at = new Date(now).toISOString();
        if (rec.forced === "cancelled") run.finished_at = run.updated_at;
      }
      return run;
    }

    if (rec.confirmedAt === null) {
      const elapsed = now - rec.createdAt;
      if (run.ticker === "FAIL" && elapsed >= this.timings.classifyMs) {
        run.status = "failed";
        run.decline_reason = "biotech_precommercial";
        run.error_message =
          "FAIL looks like a pre-commercial biotech. rNPV models are out of scope for this app, so no valuation was built.";
        run.finished_at = new Date(now).toISOString();
        rec.forced = "failed";
      } else if (elapsed < this.timings.classifyMs) {
        run.status = "classifying";
      } else if (elapsed < this.timings.classifyMs + this.timings.proposeMs) {
        run.status = "proposing";
      } else if (run.status === "classifying" || run.status === "proposing") {
        Object.assign(run, makeRun({ ...run, ...pickClassification(run.ticker) }), { status: "awaiting_confirm" });
      }
      return run;
    }

    // building
    const stepsDue = Math.min(BUILD_STAGES.length, Math.floor((now - rec.confirmedAt) / rec.stepMs));
    while (rec.events.length < stepsDue) {
      const i = rec.events.length;
      const stage = BUILD_STAGES[i];
      const ev: RunEventOut = {
        id: i + 1,
        run_id: run.id,
        ts: new Date(rec.confirmedAt + (i + 1) * rec.stepMs).toISOString(),
        stage: stage.stage,
        message: stage.message,
        progress_pct: stage.progress_pct,
      };
      rec.events.push(ev);
      run.current_stage = ev.stage;
      run.progress_pct = ev.progress_pct ?? run.progress_pct;
    }
    if (stepsDue >= BUILD_STAGES.length && run.status === "building") {
      if (run.ticker === "ERR") {
        run.status = "failed";
        run.error_message = "Excel recalculation check failed: workbook value differs from engine by 3.1%.";
      } else {
        run.status = "complete";
      }
      run.finished_at = new Date(now).toISOString();
      rec.forced = run.status;
    }
    return run;
  }
}

function pickClassification(ticker: string): Partial<RunOut> {
  const full = makeRun();
  return {
    cik: full.cik,
    sic_code: full.sic_code,
    model_type: full.model_type,
    model_confidence: full.model_confidence,
    model_reasons: full.model_reasons,
    runner_up_model: full.runner_up_model,
    historical_window_years: full.historical_window_years,
    window_reason: full.window_reason,
    proposed_assumptions: full.proposed_assumptions,
    assumptions_schema: full.assumptions_schema,
    company_name: ticker === "AAPL" ? full.company_name : `${ticker} Holdings Inc.`,
  };
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

function sseStream(state: MockApiState, id: string): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    async start(controller) {
      let sent = 0;
      try {
        for (;;) {
          const rec = state.get(id);
          if (!rec) break;
          while (sent < rec.events.length) {
            controller.enqueue(enc.encode(`data: ${JSON.stringify(rec.events[sent])}\n\n`));
            sent += 1;
          }
          if (["complete", "failed", "cancelled"].includes(rec.run.status)) {
            controller.enqueue(enc.encode(`event: done\ndata: ${rec.run.status}\n\n`));
            break;
          }
          if (rec.run.status !== "building") break;
          controller.enqueue(enc.encode(": heartbeat\n\n"));
          await sleep(Math.max(20, Math.min(rec.stepMs, 250)));
        }
      } finally {
        try {
          controller.close();
        } catch {
          /* already closed by the client */
        }
      }
    },
  });
}

const notFound = () => HttpResponse.json({ detail: "Run not found" }, { status: 404 });

export function createHandlers(state: MockApiState): RequestHandler[] {
  return [
    http.get("*/api/health", () => HttpResponse.json({ status: "ok" })),

    http.delete("*/api/me", () => {
      const run = state.activeOrWaiting();
      if (run && run.status !== "awaiting_confirm") {
        return HttpResponse.json(
          { detail: "A valuation is still being built. Cancel it (or let it finish), then delete your account." },
          { status: 409 },
        );
      }
      state.reset();
      return new HttpResponse(null, { status: 204 });
    }),

    http.get("*/api/runs/active", () => {
      const run = state.activeOrWaiting();
      return run ? HttpResponse.json(run) : new HttpResponse(null, { status: 204 });
    }),

    http.post("*/api/runs", async ({ request }) => {
      const body = (await request.json().catch(() => ({}))) as { ticker?: string };
      const ticker = (body.ticker ?? "").trim().toUpperCase();
      if (!/^[A-Z][A-Z0-9.-]{0,9}$/.test(ticker)) {
        return HttpResponse.json({ detail: [{ msg: "Invalid ticker" }] }, { status: 422 });
      }
      const active = state.activeRun();
      if (active) {
        return HttpResponse.json(
          { detail: "You already have a run in progress.", active_run_id: active.id },
          { status: 409 },
        );
      }
      return HttpResponse.json(state.create(ticker), { status: 201 });
    }),

    http.get("*/api/runs/:id", ({ params }) => {
      const rec = state.get(String(params.id));
      return rec ? HttpResponse.json(rec.run) : notFound();
    }),

    http.post("*/api/runs/:id/confirm", async ({ params, request }) => {
      const rec = state.get(String(params.id));
      if (!rec) return notFound();
      if (rec.run.status !== "awaiting_confirm") {
        return HttpResponse.json({ detail: `Run is ${rec.run.status}, not awaiting_confirm` }, { status: 409 });
      }
      const body = (await request.json()) as ConfirmRunRequest;
      state.confirm(rec, body);
      return HttpResponse.json(rec.run);
    }),

    http.post("*/api/runs/:id/cancel", ({ params }) => {
      const rec = state.get(String(params.id));
      if (!rec) return notFound();
      state.cancel(rec);
      return HttpResponse.json(rec.run);
    }),

    http.get("*/api/runs/:id/events", ({ params }) => {
      const id = String(params.id);
      if (!state.get(id)) return notFound();
      return new HttpResponse(sseStream(state, id), {
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
          Connection: "keep-alive",
        },
      });
    }),

    http.get("*/api/runs/:id/result", ({ params }) => {
      const rec = state.get(String(params.id));
      if (!rec) return notFound();
      if (rec.run.status !== "complete") {
        return HttpResponse.json({ detail: "Result not available yet" }, { status: 409 });
      }
      const out = makeResultOut(rec.run.id);
      return HttpResponse.json({
        ...out,
        result: { ...out.result, ticker: rec.run.ticker, model_type: rec.run.model_type ?? out.result.model_type },
      });
    }),

    http.get("*/mock-files/:runId/:name", ({ params }) => {
      const name = String(params.name);
      const type = name.endsWith(".pdf")
        ? "application/pdf"
        : "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
      return new HttpResponse(`mock ${name}`, {
        headers: { "Content-Type": type, "Content-Disposition": `attachment; filename="${name}"` },
      });
    }),
  ];
}
