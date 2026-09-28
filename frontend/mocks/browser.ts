/** Browser MSW worker for `npm run dev:mock` (NEXT_PUBLIC_API_MOCKING=enabled). */
import { setupWorker } from "msw/browser";
import { MockApiState, createHandlers } from "./handlers";

export const mockState = new MockApiState();
export const worker = setupWorker(...createHandlers(mockState));
