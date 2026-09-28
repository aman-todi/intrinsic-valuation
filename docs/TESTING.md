# Testing and CI

How the test suite is organized, how to run it locally, and what CI enforces (SPEC §13, §15 Ticket 14).

## Backend (`backend/tests/`)

```bash
cd backend
uv venv -p python3.13 .venv && uv pip install -p .venv -e '.[dev]'
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy app/valuation app/classify app/assumptions app/schemas app/runs app/data/demo
.venv/bin/pytest -q --cov=app --cov-fail-under=95     # unit + integration, overall floor
scripts/check_coverage.sh                             # per-package gates (below)
```

| Layer | Where | Needs |
|---|---|---|
| Unit | `tests/unit/` | nothing: no network, no DB. External HTTP is mocked with `respx`; the Anthropic client is a fake. |
| Integration | `tests/integration/` (auto-marked `integration`) | Postgres 16 + Redis. `TEST_DATABASE_URL` / `TEST_REDIS_URL` if set, otherwise the conftest starts a throwaway cluster from `/usr/lib/postgresql/16/bin` and a `redis-server` on free ports, and skips cleanly if neither is available. `alembic upgrade head` runs first. `soffice` (LibreOffice Calc) and the WeasyPrint libraries make the Excel-recalc and PDF tests run for real; without `soffice` the recalc test skips with a message. |

Run only one layer with `pytest tests/unit` or `pytest -m integration`.

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

| Scope | Gate | Before Ticket 14 | After |
|---|---|---|---|
| `app/valuation/*` | ≥ 90% | 94.2% | 99.0% |
| `app/classify/*` | ≥ 90% | 95.1% | 99.8% |
| `app/assumptions/bounds.py` | ≥ 90% | 99.5% | 100% |
| overall `app` | `--cov-fail-under=95` | 93% | 95.7% |

The "before" figures were measured without greenlet tracing. Part of the overall gain (for example,
`api/routes/runs.py` went from 59% to 86%) comes from measuring correctly rather than from new tests.

`scripts/check_coverage.sh` enforces the per-package floors from the `.coverage` file that
`pytest --cov=app` writes. Coverage runs with `concurrency = ["thread", "greenlet"]`
(`pyproject.toml`), because SQLAlchemy's asyncio layer runs ORM code in greenlets, and route or job
code reached through it would otherwise be under-reported. Raise the overall floor when coverage goes up.
Never lower it to get a PR through.

## Frontend (`frontend/tests/`)

```bash
cd frontend && npm ci
npm run lint        # eslint + tsc --noEmit
npm run test        # Vitest + React Testing Library (tests/unit)
npm run build
npx playwright install --with-deps chromium   # once
npm run test:e2e    # Playwright against `next build && next start`, API mocked via route interception
```

## CI (`.github/workflows/`)

- **`ci-backend.yml`** installs the same apt packages as `infra/docker/Dockerfile.worker` (LibreOffice
  Calc and the WeasyPrint libraries), so the recalc-verification and PDF tests run for real. It then runs
  ruff check, ruff format --check, mypy, pytest (unit and integration) with the overall coverage floor,
  and the per-package gates. Postgres 16 and Redis 7 run as service containers, and `TEST_DATABASE_URL`
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
deployment smoke test (`docs/DEPLOYMENT.md` §13) and by the RLS sanity query in §2. If the project
later needs RLS regression tests, add a separate, manually triggered workflow against a Supabase
branch. Do not make it part of the per-PR gate.

## Offline demo mode

`DATA_SOURCE_MODE=fixtures` runs the whole app on the fixture set with no SEC, Yahoo or FRED access.
See `docs/DEPLOYMENT.md` → "Offline demo mode". `tests/integration/test_demo_mode.py` runs AAPL end to
end in this mode.
