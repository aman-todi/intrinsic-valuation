/** Node MSW server for Vitest. */
import { setupServer } from "msw/node";
import { MockApiState, createHandlers, type MockTimings } from "./handlers";

export function createMockServer(timings: Partial<MockTimings> = {}) {
  const state = new MockApiState(timings);
  const server = setupServer(...createHandlers(state));
  return { state, server };
}
