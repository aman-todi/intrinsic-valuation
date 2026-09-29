# Testing and CI

How the test suite is organized, how to run it locally, and what CI enforces (SPEC §13, §15 Ticket 14).

The suite is deliberately small: **155 tests** (backend 132 = 90 unit + 42 integration; frontend 18
Vitest + 5 Playwright), each parametrized case counted as one test. The rule is one or two strong tests
per behavior (pinned hand-computed regressions, real LibreOffice / WeasyPrint / Postgres / Redis where it
matters) rather than many near-duplicates. Table-driven loops inside one test are used where the rows are
one logical check (the 20-ticker reconciliation, bounds rules per family, the SIC map). When adding a
test, prefer extending an existing table or strengthening an existing test over adding a new one.

## Backend (`backend/tests/`)

```bash
cd backend
uv venv -p python3.13 .venv && uv pip install -p .venv -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy app/valuation app/classify app/assumptions app/schemas app/runs app/data/demo
.venv/bin/pytest -q --cov=app --cov-fail-under=75     # unit + integration, overall floor
scripts/check_coverage.sh                             # app/valuation gate (below)
```

Run one layer at a time:

```bash
.venv/bin/pytest -q tests/unit            # ~7 s, no services needed
.venv/bin/pytest -q -m integration        # ~2 min: Postgres, Redis, soffice, WeasyPrint
.venv/bin/pytest -q -m "not integration"  # same as tests/unit
```

| Layer | Where | Needs |
|---|---|---|
| Unit | `tests/unit/` | nothing: no network, no DB. External HTTP is mocked with `respx`; the Anthropic client is a fake. |
| Integration | `tests/integration/` (auto-marked `integration`) | Postgres 16 + Redis. `TEST_DATABASE_URL` / `TEST_REDIS_URL` if set, otherwise the conftest starts a throwaway cluster from `/usr/lib/postgresql/16/bin` and a `redis-server` on free ports, and skips cleanly if neither is available. `alembic upgrade head` runs first. `soffice` (LibreOffice Calc) and the WeasyPrint libraries make the Excel-recalc and PDF tests run for real; without `soffice` the recalc test skips with a message. |

### What is covered

| Area | Tests | What they protect |
|---|---|---|
| Valuation engine (`test_valuation_*`, `test_registry`) | 19 | One pinned hand-computed regression per model type: FCFF (mature + early-stage, checked against an independent reference loop), FCFE, excess return, REIT NAV, E&P NAV, SOTP (2-segment case + degenerate single segment == FCFF). FCFF also pins the near-singularity guard, bridge arithmetic, scenario ordering (bull > base > bear) and the 5×5 grid (shape, centre == base, monotone). Three "flag instead of fail" boundary cases. |
| Excel export | 2 unit + 9 integration | Unit: every model type's workbook defines a name for each assumption/output, no formula hard-codes a literal or a raw `Assumptions!` address, and cached values equal the engine. Integration (real headless LibreOffice): recalculated value/equity/scenarios/grid cell match the engine for each of 7 cases (one per model type + FCFF early-stage), `verify_workbook` catches a mismatch, and editing a named input flows through to value per share and the grid centre. |
| PDF report | 3 unit + 6 integration | Real WeasyPrint PDF per model type (text, sources, disclaimer); structured-output narrative call; fallback narrative when the LLM fails or is absent. |
| Classifier (incl. `test_fixture_reconciliation`) | 16 | 20-ticker reconciliation against the EDGAR fixtures (one table-driven test) + HON segments/no-segments + DUK FCFE runner-up + engine on every fixture shape (below); synthetic decision-tree fixtures; mining SIC ranges; MLP by entity type; windows (default / cyclical SIC / margin swing / revenue drawdown); LLM tiebreak called only when the top two are close, can flip the choice, and falls back on failure. |
| Proposer / bounds | 11 | Repair loop (re-prompts with violations, gives up after 3), SOTP one call per segment + consolidated, deterministic fallback passes bounds for every model type at normal/tiny/zero risk-free rates, sparse-data fallback, LLM failure -> fallback. Bounds: table-driven pass/violate per rule family (single-field ranges, cross-field rules, SOTP structure). |
| EDGAR | 13 unit + 1 integration | Structure of every normalized fixture, AAPL annual figures, TTM with a September FYE, restatement (latest filed wins), tag fallback order, 20-F filer helpers, GOOGL multi-class share summing; client User-Agent/gzip, 429/5xx retry with Retry-After, retries exhausted, filing-cache hit; shared Redis rate limiter (fakeredis + real Redis); segment XBRL parsing and fetch order. |
| Market / macro | 10 | FRED "." observations skipped and all-missing never becomes 0; yfinance one retry then a typed error; Damodaran spreadsheet parsing; SIC map (one table-driven test, incl. every mapped name exists in the snapshot); snapshot fallback when S3 is unreachable; S3 save/load-latest; market snapshot assembly. |
| Auth / storage / runs / schemas | 16 | Assumption schemas flat and all-required, missing field rejected; JWKS valid / expired / unknown kid refreshes exactly once, 401s from `current_user`; storage key traversal and signed URLs; cache key; run state machine table + transition side effects; `run_cancellable` pub/sub cancel and protected section; demo providers; SAQ settings. |
| DB + run lifecycle (integration) | 26 | Migration downgrade/upgrade round trip, RLS on every table, one-active-run index; AAPL auto end to end through the API and both jobs (real LibreOffice check); 409 lock (and on confirm); cache hit + private fork, out-of-bounds confirm rejected, model override re-proposes; single-flight (two builds run the pipeline once; stale lock); cancel mid-build (sentinel: nothing promoted), during the LLM proposal, at awaiting_confirm, and on worker shutdown; stale-cancel recovery; route auth (401 on every route, 404 for another user's run, SSE `access_token` + Last-Event-ID resume, live SSE build); life insurer declined; market outage fails the run; no LLM -> deterministic proposal; demo mode AAPL end to end + SOTP build with real recalc. |

### EDGAR fixtures (the 20-ticker set)

`app/data/demo/edgar/` holds hand-built `companyfacts` / `submissions` JSON in SEC's exact formats for
20 tickers, plus one segment XBRL instance (HON). This is the single canonical copy: the tests load it
through `tests/fixtures/edgar/__init__.py`, and the offline demo mode ships it as package data.
Regenerate after editing `tests/fixtures/edgar/build_fixtures.py`:

```bash
.venv/bin/python -m tests.fixtures.edgar.build_fixtures
```

| Expected outcome | Tickers |
|---|---|
| FCFF | AAPL, MSFT, PFE, GOOGL (per-class share summing), WMT (Jan FYE, split recast), NUE (cyclical, 10-year window), DUK (FCFE runner-up only when leverage > 50%) |
| FCFF early-stage | SNOW |
| Excess return | JPM (bank), TRV (P&C) |
| REIT NAV / E&P NAV | O / EOG |
| SOTP | HON: 4 material segments, segment profit disclosed, 23-point growth spread; FCFF without segment data |
| Declines | VKTX biotech, ALAB insufficient data, TSM 20-F filer, MET life insurer, EPD MLP, NEM mining, CVII SPAC |

`tests/unit/test_fixture_reconciliation.py` runs the **real** classify-job data path for every ticker
(`FixtureEdgarClient` → `load_company` → `build_signals` → `classify`) and asserts the table above. For
every ticker that is not declined, it then runs normalize → `deterministic_fallback` →
`get_valuator(...).compute(...)` and asserts that the value per share is finite, that the proposal passes
`bounds.py`, and that the value is within 0.05×–20× of the fixture price. This catches unit or sign
mismatches between the normalizer and the engines.

### Coverage gates

| Scope | Gate | Full suite (810 tests) | Slim suite (132 tests) |
|---|---|---|---|
| overall `app` | `--cov-fail-under=75` | 95.7% | 90.6% |
| `app/valuation/*` | ≥ 85% (`scripts/check_coverage.sh`) | 99.0% | 93.5% |
| `app/classify/*` | not gated | 99.8% | 91.9% |
| `app/assumptions/*` | not gated | 97.7% | 95.7% |
| `app/export/*` | not gated | 96.9% | 94.6% |
| `app/jobs/*` | not gated | 90.9% | 85.0% |
| `app/api/*` | not gated | 85.6% | 84.0% |

The floors are set well below the measured values on purpose: they catch a large untested area
landing, not a single uncovered branch. `scripts/check_coverage.sh` reads the `.coverage` file that
`pytest --cov=app` writes. Coverage runs with `concurrency = ["thread", "greenlet"]`
(`pyproject.toml`), because SQLAlchemy's asyncio layer runs ORM code in greenlets, and route or job
code reached through it would otherwise be under-reported. Never lower a floor to get a PR through.

## Frontend (`frontend/tests/`)

```bash
cd frontend && npm ci
npm run lint        # eslint + tsc --noEmit
npm run test        # Vitest + React Testing Library (tests/unit)
npm run build
npx playwright install --with-deps chromium   # once
npm run test:e2e    # Playwright against `next build && next start`, API mocked via route interception
```

- **Vitest (18):** assumptions form (read-only formatting with rationale/source, editable toggle with
  edits propagated as decimals, client-side bound warnings, SOTP read-only, schema ordering + edit
  detection); active-run guard (no run, building locks, polling unlocks at awaiting_confirm, a run
  appearing in another tab locks); model confirm card (display + override); format helpers (per-kind
  assumption formatting, percent round trip, compact USD), run-status polling rules, client bounds.
- **Playwright (5):** full run flow (ticker -> edited confirm -> progress -> result with downloads);
  cancel mid-build; a building run restores the overlay and makes the app inert; a 409 from
  `POST /api/runs` redirects to the existing run; awaiting_confirm does not lock.

## CI (`.github/workflows/`)

- **`ci-backend.yml`** installs the same apt packages as `infra/docker/Dockerfile.worker` (LibreOffice
  Calc and the WeasyPrint libraries), so the recalc-verification and PDF tests run for real. It then runs
  ruff check, ruff format --check, mypy, pytest (unit and integration) with the overall coverage floor
  (75%), and the `app/valuation` gate (85%). Postgres 16 and Redis 7 run as service containers, and `TEST_DATABASE_URL`
  / `TEST_REDIS_URL` point at them.
- **`ci-frontend.yml`** uses Node 24 and runs `npm ci`, lint, Vitest, build, the Playwright Chromium
  install (`--with-deps`), and the Playwright e2e suite. The HTML report is uploaded when a step fails.

### Why CI uses a local Postgres container instead of a Supabase branch

SPEC §13.1 allows either "a disposable Supabase branch or a local Postgres in CI". CI uses the local
container for these reasons:

1. **Isolation from shared data.** A Supabase branch belongs to the real project: same organization,
   same billing, same dashboard. A misconfigured `DATABASE_URL` in CI could point at production. The
   service container can only ever reach itself.
2. **Nothing the tests need is Supabase-specific.** The schema is plain Postgres 16 plus Alembic. The
   initial migration creates stub `auth.users`, `auth.uid()` and `auth.role()` when the Supabase `auth`
   schema is missing. JWT verification is tested against a respx-mocked JWKS endpoint with a test ES256
   key, not against Supabase Auth.
3. **Cost and quotas.** Supabase branching is a paid feature. Each branch is a billed compute instance
   that takes minutes to provision, and branch creation needs a Supabase access token as a CI secret.
   The container starts in seconds and needs no secrets, so PRs from forks also get full integration
   runs.
4. **Determinism.** Each test truncates every app table (`db_session` fixture), and the migrations run
   from scratch on every CI run. That gives the same guarantee a fresh branch would, without depending
   on the network or on Supabase's availability.

What this does not cover is the behavior of the real Supabase project: its RLS policies under the
`authenticated` role, the session pooler, and the IPv6-only direct host. Those are checked by the
deployment smoke test (`docs/DEPLOYMENT.md` → "A8. Smoke test") and by the RLS sanity query in "A2. Supabase". If the project
later needs RLS regression tests, add a separate, manually triggered workflow against a Supabase
branch. Do not make it part of the per-PR gate.

## Offline demo mode

`DATA_SOURCE_MODE=fixtures` runs the whole app on the fixture set with no SEC, Yahoo or FRED access.
See `docs/DEPLOYMENT.md` → "Offline demo mode". `tests/integration/test_demo_mode.py` runs AAPL end to
end in this mode, and HON (SOTP) through the full build with the real LibreOffice recalc check.
