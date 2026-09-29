# DCF Valuation Platform

Full build spec: `docs/SPEC.md` (sections referenced as §N below and in code docstrings).

## Layout
- `backend/` — FastAPI + SAQ worker, Python 3.13. Package `app`.
- `frontend/` — Next.js 16 App Router, Tailwind v4, TanStack Query.
- `infra/` — Dockerfiles, Terraform (single EC2 host running docker compose), deploy scripts. Frontend on Vercel.
  Runbook: `docs/DEPLOYMENT.md`.

## Backend conventions
- Venv: `cd backend && uv venv -p python3.13 .venv && uv pip install -p .venv -e '.[dev]'`
- Test: `cd backend && .venv/bin/pytest -q`; lint: `.venv/bin/ruff check . && .venv/bin/ruff format --check .`
- Schemas in `app/schemas/` are the contracts — do not change field names/types without updating every consumer.
  Adding optional fields to `ValuationResult`/`RunOut` is OK; never add `| None` to the `*Assumptions` classes.
- Rates/percentages are decimals everywhere (0.042 = 4.2%). Money is raw USD (not thousands/millions).
- Valuation engine (`app/valuation/`) is pure: no I/O, no LLM. LLM only proposes assumptions and writes prose.
- Unit tests: no network, no DB. Integration tests (`tests/integration/`, marker `integration`) use real
  Postgres/Redis from `TEST_DATABASE_URL` / `TEST_REDIS_URL` env vars and skip cleanly when unset.
  Locally: Postgres 16 binaries are in `/usr/lib/postgresql/16/bin`, `redis-server` and `soffice` are on PATH.
- External HTTP is mocked with `respx`; the Anthropic client is injected so tests pass a fake.
