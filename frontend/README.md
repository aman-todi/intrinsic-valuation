# Frontend (Next.js 16, App Router)

Spec: `docs/SPEC.md` §10. Node >= 22 locally (CI and Docker use Node 24).

Production: Vercel (Root Directory `frontend`), calling the API cross-origin at `NEXT_PUBLIC_API_BASE_URL`
(the EC2 host's `https://<api host>`). See `docs/DEPLOYMENT.md` → "A5. Vercel (frontend)".

| Script | What it does |
|---|---|
| `npm run dev` | Dev server against `NEXT_PUBLIC_API_BASE_URL` |
| `npm run dev:mock` | Dev server fully against the in-browser MSW mock API (`mocks/`), dev auth bypass on |
| `npm run lint` | ESLint + `tsc --noEmit` |
| `npm run test` | Vitest + React Testing Library (`tests/unit`) |
| `npm run build` | Production build (`output: "standalone"`, used by `infra/docker/Dockerfile.frontend` for local docker compose; harmless on Vercel) |
| `npm run test:e2e` | Playwright (`tests/e2e`) against `next build && next start`, API mocked via `page.route` |

## Layout
- `app/` — `/` (ticker entry, restores an in-progress run on load), `/login` (magic link), `/runs/[id]` (renders purely by `run.status`).
- `components/` — `active-run-guard`, `model-confirm-card`, `assumptions-form` (generic, schema-ordered), `progress-view` (SSE overlay; makes the app shell `inert`), `result-view`, `sensitivity-chart`, `ui/` (shadcn-style primitives).
- `lib/` — `types.ts` (mirrors `backend/app/schemas/run.py` + `valuation_result.py`), `api-client.ts`, `supabase-client.ts`, `run-status.ts`, `format.ts`, `bounds.ts`, `use-run-events.ts`.
- `mocks/` — MSW v2 handlers implementing the §9.1 contract with a scripted run, plus fixtures. Tickers `FAIL` (declined) and `ERR` (build failure) exercise the error paths.

## Auth
Supabase is used only for sign-in. With `NEXT_PUBLIC_SUPABASE_URL` unset the app runs with a
**dev auth bypass**: every request carries `Authorization: Bearer dev-bypass-token`. The real
backend rejects that token; never deploy without Supabase configured.

## What the backend must support
- `GET /api/runs/{id}/events?access_token=<jwt>` — `EventSource` cannot send headers, so the SSE
  route must accept the Supabase access token as a query parameter (in addition to the header).
- CORS for the frontend origin including the `Authorization` header (when not same-origin).
- `RunOut.assumptions_schema` / `cache_hit_available` populated at `awaiting_confirm`.
- `POST /confirm` with `assumptions: null` (and `edited: false`) means "build from the proposal".
