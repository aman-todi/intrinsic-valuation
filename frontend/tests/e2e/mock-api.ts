/**
 * Playwright-side mock of the §9.1 API: reuses the MSW handlers from mocks/handlers.ts, but
 * serves them through `page.route` (no service worker). State lives in the test process, so
 * tests can seed runs directly (e.g. "another tab started a run").
 */
import type { Page } from "@playwright/test";
import { getResponse } from "msw";
import { MockApiState, createHandlers, type MockTimings } from "../../mocks/handlers";

export interface RecordedRequest {
  method: string;
  url: string;
  authorization: string | undefined;
  body: unknown;
}

export async function installMockApi(page: Page, timings: Partial<MockTimings> = {}) {
  const state = new MockApiState({ classifyMs: 700, proposeMs: 700, buildStepMs: 300, ...timings });
  const handlers = createHandlers(state);
  const requests: RecordedRequest[] = [];

  await page.route(/\/(api|mock-files)\//, async (route) => {
    const req = route.request();
    const headers: Record<string, string> = {};
    const auth = await req.headerValue("authorization");
    const ctype = await req.headerValue("content-type");
    if (auth) headers.authorization = auth;
    if (ctype) headers["content-type"] = ctype;
    const post = req.postData();
    requests.push({
      method: req.method(),
      url: req.url(),
      authorization: auth ?? undefined,
      body: post ? JSON.parse(post) : undefined,
    });

    const res = await getResponse(
      handlers,
      new Request(req.url(), { method: req.method(), headers, body: post ?? undefined }),
    );
    if (!res) return route.fallback();
    // SSE streams are fully buffered here; EventSource still sees every event + `done`.
    const body = Buffer.from(await res.arrayBuffer());
    await route.fulfill({ status: res.status, headers: Object.fromEntries(res.headers.entries()), body });
  });

  return { state, requests };
}
