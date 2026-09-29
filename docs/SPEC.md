# DCF Valuation Platform — Build Specification

**Audience:** Claude Code (autonomous implementation), orchestrated as multiple parallel sessions from one parent session.
**Status:** Ready to build. Cloud resource *values* (AWS keys, Supabase project, Anthropic key, FRED key) are not yet provisioned — everything reads from environment variables, and `.env.example` documents every one. Runbooks at the end tell a human exactly how to provision each one.
**Owner / primary user:** Aman (+ ~10 friends/family). Small, trusted user base — the app enforces one-run-per-user, not multi-tenant scale.

---

## 0. What this app does (recap of decisions)

Given a US-listed ticker, the app determines the *right* valuation model for that company, builds it with either AI-proposed or user-supplied assumptions, and produces a total equity valuation — usable standalone or in a peer comparison — as a live-formula Excel workbook and a PDF write-up.

### Locked-in decisions from design discussion

1. **Coverage — six model families**, chosen by a rules-first classifier (LLM only as a tiebreaker):
   - **FCFF** (unlevered DCF) — core operating companies, incl. an early-stage-tech variant (margin ramp + survival probability). ~50%+ of names.
   - **FCFE** (levered DCF) — non-financials with a stable target debt ratio. A *variant*, not a separate pipeline; reuses the FCFF projection engine.
   - **Excess Return / DDM** — banks and P&C insurers.
   - **NAV** — REITs (NOI ÷ cap rate) and E&P (SEC standardized measure of reserves, adjusted to strip). Mining NAV is **out of scope** (no standardized data source).
   - **SOTP** — only auto-suggested when a company has ≥2 material, economically-distinct segments; reuses the FCFF engine per segment (a composite of FCFF/multiples runs, not a new pipeline).
   - **Biotech (rNPV) and Real Options are explicitly OUT OF SCOPE** for this app (a separate future app). The classifier must *decline gracefully* for biotech/pre-revenue names, life insurers, SPACs/trusts/MLPs, and mining — never force them into FCFF.
2. **Reverse DCF is excluded entirely.** Only the standard WACC × terminal-growth sensitivity grid remains.
3. **Every model produces a common "value bridge" output** (`ValuationResult`, defined below) so results are comparable across model types.
4. **Default flow is AI-picks-everything.** A single confirmation screen shows the recommended model + AI-proposed assumptions (with rationale + source) read-only, with one toggle — **"Editable (Not Recommended)"** — that switches the assumption fields to editable inputs. Editing forks the run into a private, user-scoped result; leaving it off builds from (and writes back to) a **shared cache**.
5. **Shared 30-day cache, keyed by filing, not just time.** Cache key = `ticker + model_type + latest_filing_accession + engine_version + prompt_version`. A new filing, or a code/prompt change, invalidates it regardless of the 30-day TTL, which is only a cleanup backstop.
6. **One run at a time, per user, enforced server-side** (DB constraint, not just UI). A run in an active (worker-owned) state blocks a new one; a run waiting on user confirmation does not. Cancel is a real worker-side `asyncio` cancellation, not a soft flag the UI ignores.
7. **Historical window: 5 years by default, 10 years when it's actually needed** — banks/insurers, E&P NAV, a cyclical SIC code, or a volatility trigger (margin swing >8–10pts or revenue peak-to-trough >20% within the 5y window) detected in code. Below 5 years of data → low-confidence flag. Below 3 → decline.
8. **Data sourcing:**
   - **SEC EDGAR** (`data.sec.gov`, `companyfacts` + `submissions`) — the great majority of historicals, shares, debt, leases, segments, bank/insurer/E&P line items. No API key; a compliant `User-Agent` header and a 10 req/s budget are mandatory.
   - **Yahoo Finance (`yfinance`)** — live price + market cap only, fetched fresh per run (no price caching, no fallback provider, per explicit decision). Best-effort; a `MarketDataProvider` interface makes swapping providers later a one-file change.
   - **FRED** — risk-free rate (10Y Treasury, `DGS10`). Requires a free API key.
   - **Damodaran datasets** (static `.xls`/`.xlsx` files, no key) — industry betas, ERP, margin/growth benchmarks. Refreshed manually a few times a year, cached in S3.
   - **No paid market-data API, no cross-check provider, no analyst consensus** — explicitly deferred.
9. **AI's job is proposing assumptions + writing narrative text — never arithmetic.** The valuation engine is pure, deterministic, unit-tested Python. Claude structured outputs (Pydantic schema, all-required flat fields — see §5.3 for why) return assumption values + one-line rationale + source per field.
10. **Stack:** FastAPI (Python 3.13) + SAQ/Redis job queue + Postgres via Supabase (session pooler) + S3 (already on free tier) + Next.js 16 frontend on **Vercel**. The backend (API, worker, Redis, Caddy for TLS) runs as a **docker compose stack on a single EC2 instance**, provisioned with **Terraform** — sized for tens of users, not horizontal scale. No LangGraph — the pipeline is a plain state machine; a bounded propose→validate→repair loop handles the one place (assumption proposal) that benefits from an agentic pattern, using the Anthropic SDK directly.

Everything below implements this. Section 12 breaks the build into 15 tickets designed to run in parallel.
## 1. Tech stack & pinned versions

Researched against current state as of **September 2026**. Pin exactly; do not float major versions.

| Layer | Choice | Version | Notes |
|---|---|---|---|
| Backend language | Python | **3.13.x** | 3.14 went stable ~Oct 2026 but SciPy 1.16 doesn't support it yet (`>=3.11,<3.14`); stay on 3.13. |
| Web framework | FastAPI | **>=0.128,<0.140** | Pydantic v2 only; drop any v1 compat code. |
| Validation | Pydantic | **>=2.10,<3.0** | |
| ORM | SQLAlchemy | **>=2.0.35,<2.1** | Async engine. |
| DB driver | **psycopg** (v3), `psycopg[binary,pool]` | **>=3.2,<4.0** | See §9.2 — avoids the `asyncpg` + Supabase-pooler prepared-statement failure mode entirely when run against the *session* pooler. |
| Migrations | Alembic | **>=1.14,<2.0** | Runs against the **direct** (non-pooled) connection string only. |
| Job queue | **SAQ** (`saq[redis]`) | **>=0.26,<0.27** | **Arq is in maintenance-only mode as of 2026 — do not use it.** SAQ is its actively-maintained, faster, async-native successor; supports Redis, has a heartbeat/stuck-job sweeper, and ships a web monitor. |
| Queue backend | Redis | **7.x** (`redis:7-alpine` image) | Also used for the SEC token-bucket limiter, single-flight build locks, and cancel pub/sub. |
| Excel | XlsxWriter | **>=3.2.9,<4.0** | Formulas, not pasted values — see §7. |
| PDF | WeasyPrint | **>=66,<71** | Needs Pango >=1.46 system libs — see Dockerfile in §11. |
| Templating | Jinja2 | **>=3.1,<4.0** | For the PDF HTML template. |
| Charts (for PDF) | matplotlib | **>=3.9,<4.0** | Rendered to PNG and embedded in the PDF. |
| Data | pandas | **>=2.3.3,<3.0** | 2.3.3 is the first release with Python 3.14 wheels; also fine on 3.13. |
| Data | numpy | **>=2.1,<3.0** | |
| Root-finding (sensitivity, WACC solves) | scipy | **>=1.16,<1.17** | Constrains Python to `<3.14`, matching our pin. |
| LLM SDK | `anthropic` (Python) | **>=0.75,<1.0** — pin to the first release with GA `output_config.format` (non-beta) support | Use `client.messages.parse(..., output_format=PydanticModel)` — the SDK still accepts this convenience kwarg and translates it internally; no beta header needed. |
| LLM model | `claude-sonnet-5` (configurable via `ANTHROPIC_MODEL`) | — | Structured outputs supported on Sonnet 4.5/4.6/5, Opus 4.5+, Haiku 4.5. |
| Market data | `yfinance` | **>=1.7,<2.0** | Note: yfinance did a **1.0 rewrite** in Dec 2025 (0.2.66 → 1.0 → 1.7 by Aug 2026). Verify `Ticker.fast_info` / `.info` / `.history()` surface against the pinned version's docs before wiring §4. |
| EDGAR HTTP | plain `httpx` client (hand-rolled, see §3) | — | We do NOT depend on `sec-edgar-api` or `edgartools` as a runtime dependency (adds surface area we don't need for `companyfacts`/`submissions`), but engineers may consult `edgartools`'s source for tag-mapping ideas. |
| Testing | pytest, pytest-asyncio, pytest-cov, respx (HTTP mocking), freezegun | latest compatible with 3.13 | |
| Frontend framework | Next.js | **>=16.3.7,<17.0** | A scheduled security release ships **16.3.7 on 2026-09-30**; do not pin below it. App Router only. |
| Runtime | Node.js | **24.x LTS** | Node 24 is Active LTS as of Sept 2026; Node 26 doesn't hit LTS until Oct 2026 — stay on 24 for a new project needing stability now. |
| UI | React | **19.x** (whatever `create-next-app@latest` pins for Next 16.3) | |
| Styling | Tailwind CSS | **v4** (Oxide engine) | |
| Components | shadcn/ui | latest, Tailwind v4 variant | Copied in, not an npm dependency. |
| Data fetching | TanStack Query | **v5** | Polling + mutation for run lifecycle. |
| Charts (frontend) | Recharts | **v3** | Sensitivity grid, scenario bars. |
| Frontend tests | Vitest + React Testing Library, Playwright (e2e) | latest | |
| Auth | Supabase Auth | — | **New JWT signing keys (asymmetric, JWKS)**, not the legacy shared `JWT_SECRET`. Verify locally via cached JWKS — no per-request round trip to Supabase. |
| DB hosting | Supabase Postgres | — | Session pooler (port 5432 via pooler host) for the app; direct connection for Alembic. |
| Object storage | AWS S3 | — | Already on your free tier. |
| Backend hosting | **One AWS EC2 instance** (Graviton `t4g.small`, arm64, Amazon Linux 2023) running **docker compose** | Compose v2 plugin | Services: `caddy`, `api`, `worker`, `redis`. See §11. |
| Reverse proxy / TLS | **Caddy** | **2.11** | Automatic Let's Encrypt certificates; SSE-safe proxying. |
| Infrastructure as code | **Terraform** | **>=1.10** (S3 backend with native lockfile), AWS provider `~> 6.0` | Everything in AWS is declared under `infra/terraform/`. |
| Container registry | Amazon ECR | — | `dcf-api`, `dcf-worker` (arm64 images). |
| Frontend hosting | **Vercel** | — | Git-integrated deploys from `frontend/`. |
| CI | GitHub Actions | — | |

### Explicitly excluded (per decisions)
LangGraph, Celery, Arq, reverse DCF, biotech/rNPV, mining NAV, cached prices, a market-data fallback provider, a cross-check valuation API, analyst consensus.
## 2. Repository layout

Monorepo, single Git repo, two deployable units (`backend`, `frontend`) plus shared infra config.

```
dcf-app/
├── .env.example
├── .github/workflows/
│   ├── ci-backend.yml
│   ├── ci-frontend.yml
│   └── deploy.yml
├── infra/
│   ├── terraform/                         # all AWS resources (see §11.3)
│   │   ├── bootstrap/                     # one-off: S3 state bucket
│   │   ├── versions.tf  variables.tf  outputs.tf  terraform.tfvars.example  backend.hcl.example
│   │   ├── network.tf  compute.tf  storage.tf  iam.tf  ssm.tf  budget.tf  main.tf
│   │   └── templates/user_data.sh.tftpl   # first-boot host setup (docker, compose, swap, /opt/dcf)
│   ├── deploy/
│   │   ├── docker-compose.prod.yml        # production stack on the EC2 host
│   │   └── Caddyfile
│   ├── docker/
│   │   ├── Dockerfile.api
│   │   ├── Dockerfile.worker
│   │   └── Dockerfile.frontend            # local docker compose only (production frontend is on Vercel)
│   └── scripts/
│       ├── put_ssm_params.sh              # .env -> SSM Parameter Store (secrets never enter Terraform state)
│       ├── deploy.sh                      # build/push images, ship deploy bundle, run remote deploy via SSM
│       ├── deploy_remote.sh               # runs on the host: render .env, migrate, compose up, health-check
│       └── seed_damodaran_cache.py        # one-off: pull Damodaran xls files into S3
├── docker-compose.yml                     # local dev: postgres(optional), redis, api, worker, frontend
├── backend/
│   ├── pyproject.toml
│   ├── alembic.ini
│   ├── alembic/
│   │   ├── env.py
│   │   └── versions/
│   ├── app/
│   │   ├── main.py                        # FastAPI app factory
│   │   ├── config.py                      # pydantic-settings, reads .env
│   │   ├── deps.py                        # FastAPI dependencies (auth, db session)
│   │   ├── db/
│   │   │   ├── base.py                    # SQLAlchemy async engine/session
│   │   │   └── models.py                  # ORM models mirroring §6 DDL
│   │   ├── auth/
│   │   │   ├── jwks.py                    # Supabase JWKS fetch + cache
│   │   │   └── middleware.py
│   │   ├── schemas/                       # Pydantic contracts, shared with jobs
│   │   │   ├── company.py
│   │   │   ├── financials.py
│   │   │   ├── assumptions.py
│   │   │   ├── valuation_result.py
│   │   │   └── run.py
│   │   ├── data/
│   │   │   ├── edgar/
│   │   │   │   ├── client.py              # rate-limited httpx client
│   │   │   │   ├── normalize.py           # tag-mapping, TTM, restatement handling
│   │   │   │   └── segments.py
│   │   │   ├── market/
│   │   │   │   ├── base.py                # MarketDataProvider ABC
│   │   │   │   └── yfinance_provider.py
│   │   │   ├── macro/
│   │   │   │   ├── fred.py
│   │   │   │   └── damodaran.py
│   │   │   └── cache.py                   # S3 raw-JSON cache helper
│   │   ├── classify/
│   │   │   ├── rules.py
│   │   │   ├── llm_tiebreak.py
│   │   │   └── windows.py                 # 5y vs 10y decision
│   │   ├── valuation/
│   │   │   ├── base.py                    # Valuator ABC, ValueBridge helpers
│   │   │   ├── fcff.py                    # incl. FCFE variant, early-stage-tech variant
│   │   │   ├── excess_return.py           # banks + P&C insurers
│   │   │   ├── nav_reit.py
│   │   │   ├── nav_ep.py
│   │   │   └── sotp.py
│   │   ├── assumptions/
│   │   │   ├── proposer.py                # propose -> validate -> repair loop
│   │   │   └── bounds.py                  # sanity-check bounds per field
│   │   ├── export/
│   │   │   ├── excel/
│   │   │   │   ├── builder.py
│   │   │   │   └── sheets/                # one module per sheet
│   │   │   └── pdf/
│   │   │       ├── builder.py
│   │   │       ├── templates/report.html.jinja
│   │   │       └── charts.py
│   │   ├── jobs/
│   │   │   ├── worker_settings.py         # SAQ WorkerSettings
│   │   │   ├── classify_job.py
│   │   │   ├── build_job.py
│   │   │   └── cancel.py                  # cooperative cancellation helpers
│   │   ├── runs/
│   │   │   ├── state_machine.py
│   │   │   ├── lock.py                    # partial-unique-index enforcement + friendly errors
│   │   │   └── cache_key.py
│   │   └── api/
│   │       └── routes/
│   │           ├── runs.py
│   │           └── health.py
│   └── tests/
│       ├── unit/
│       ├── integration/
│       ├── fixtures/                      # recorded EDGAR JSON, Damodaran snapshots
│       └── conftest.py
└── frontend/
    ├── package.json
    ├── next.config.ts
    ├── app/
    │   ├── layout.tsx
    │   ├── page.tsx                       # ticker entry + active-run redirect
    │   ├── login/page.tsx
    │   └── runs/[id]/page.tsx             # confirm / progress / result, by status
    ├── components/
    │   ├── ticker-form.tsx
    │   ├── model-confirm-card.tsx
    │   ├── assumptions-form.tsx           # schema-driven, read-only <-> editable toggle
    │   ├── progress-view.tsx
    │   ├── result-view.tsx
    │   ├── sensitivity-chart.tsx
    │   └── active-run-guard.tsx           # blocks new submissions while a run is active
    ├── lib/
    │   ├── api-client.ts
    │   ├── supabase-client.ts
    │   └── run-status.ts
    └── tests/
        ├── unit/
        └── e2e/
```
## 3. Database schema (Supabase Postgres)

All tables live in the `public` schema and reference `auth.users(id)` for the user FK (Supabase's built-in auth table — we never create our own users table). Row Level Security (RLS) is **on** for every table; the service role (used by the backend) bypasses RLS, the anon/authenticated role (unused directly — the frontend never talks to Postgres, only to our API) would be scoped by these policies if ever needed.

```sql
-- migration 0001_init.sql  (managed by Alembic; shown here as the target schema)

create extension if not exists "pgcrypto";

-- ============================================================
-- runs: one row per user-initiated valuation attempt
-- ============================================================
create type run_status as enum (
  'classifying',
  'proposing',
  'awaiting_confirm',
  'building',
  'complete',
  'failed',
  'cancelled'
);

create type run_mode as enum ('auto', 'custom');

create table runs (
  id                   uuid primary key default gen_random_uuid(),
  user_id              uuid not null references auth.users(id) on delete cascade,
  ticker               text not null,
  mode                 run_mode not null default 'auto',
  status               run_status not null default 'classifying',

  -- classification results (filled after CLASSIFYING)
  cik                  text,
  company_name         text,
  sic_code             text,
  model_type           text,               -- 'fcff' | 'fcfe' | 'excess_return' | 'nav_reit' | 'nav_ep' | 'sotp'
  model_confidence     numeric(4,3),
  model_reasons        jsonb,              -- list[str]
  runner_up_model      text,
  decline_reason       text,               -- set + status='failed' if classifier declines (biotech, SPAC, etc.)
  historical_window_years int,
  window_reason        text,

  -- assumptions (filled after PROPOSING; mutated if user edits under mode='custom')
  proposed_assumptions jsonb,
  final_assumptions    jsonb,
  assumptions_edited   boolean not null default false,

  -- filing/version pins for cache correctness
  accession_number     text,
  engine_version       text,
  prompt_version       text,
  cache_key            text,               -- null for custom/forked runs

  -- lifecycle
  cancel_requested     boolean not null default false,
  error_message        text,
  current_stage        text,               -- human-readable progress label
  progress_pct         int not null default 0,

  created_at           timestamptz not null default now(),
  updated_at           timestamptz not null default now(),
  started_at           timestamptz,
  finished_at          timestamptz
);

create index runs_user_id_idx on runs (user_id, created_at desc);
create index runs_cache_key_idx on runs (cache_key) where cache_key is not null;

-- *** THE LOCK ***
-- one run per user may be in a worker-owned ("active") state at a time.
-- awaiting_confirm is NOT active: a user may sit on a confirmation screen
-- indefinitely without blocking anything.
create unique index runs_one_active_per_user
  on runs (user_id)
  where status in ('classifying', 'proposing', 'building');

-- ============================================================
-- run_events: append-only progress log, replayable over SSE on reconnect
-- ============================================================
create table run_events (
  id          bigint generated always as identity primary key,
  run_id      uuid not null references runs(id) on delete cascade,
  ts          timestamptz not null default now(),
  stage       text not null,
  message     text not null,
  progress_pct int
);
create index run_events_run_id_idx on run_events (run_id, id);

-- ============================================================
-- cached_models: the shared, filing-pinned cache for AUTO-mode builds
-- ============================================================
create table cached_models (
  cache_key         text primary key,
  ticker            text not null,
  model_type        text not null,
  accession_number  text not null,
  engine_version    text not null,
  prompt_version    text not null,
  s3_prefix         text not null,          -- e.g. models/AAPL/fcff/0000320193-26-000010/
  valuation_result  jsonb not null,          -- the full ValuationResult, for fast API reads
  price_as_of       timestamptz not null,
  price_used        numeric(18,4) not null,
  computed_at       timestamptz not null default now(),
  expires_at        timestamptz not null     -- computed_at + 35 days; cleanup backstop only
);
create index cached_models_ticker_idx on cached_models (ticker, model_type);
create index cached_models_expires_idx on cached_models (expires_at);

-- ============================================================
-- cached_proposals: AI-proposed assumptions, cached independently of the
-- final build so the confirmation screen is instant on a repeat ticker
-- ============================================================
create table cached_proposals (
  cache_key         text primary key,        -- ticker+model_type+accession+prompt_version
  ticker            text not null,
  model_type        text not null,
  accession_number  text not null,
  prompt_version    text not null,
  assumptions       jsonb not null,
  computed_at       timestamptz not null default now()
);

-- ============================================================
-- edgar_filing_cache: index of raw EDGAR JSON pulls cached in S3
-- ============================================================
create table edgar_filing_cache (
  cik               text primary key,
  company_name      text,
  latest_accession  text,
  s3_key            text not null,           -- companyfacts JSON blob
  fetched_at        timestamptz not null default now()
);

-- RLS
alter table runs enable row level security;
alter table run_events enable row level security;
alter table cached_models enable row level security;
alter table cached_proposals enable row level security;
alter table edgar_filing_cache enable row level security;

create policy runs_owner_select on runs for select using (auth.uid() = user_id);
create policy runs_owner_all on runs for all using (auth.uid() = user_id) with check (auth.uid() = user_id);
create policy run_events_owner_select on run_events for select using (
  exists (select 1 from runs r where r.id = run_events.run_id and r.user_id = auth.uid())
);
-- cached_models / cached_proposals / edgar_filing_cache: shared read-only for any authenticated user,
-- writes only via service role (the backend), so no `for all` policy is created for them.
create policy cached_models_read on cached_models for select using (auth.role() = 'authenticated');
create policy cached_proposals_read on cached_proposals for select using (auth.role() = 'authenticated');
```

**Notes for the implementing ticket:**
- The backend connects with the Supabase **service role** key for all writes (bypasses RLS by design — our API layer is the actual authorization boundary, checking `run.user_id == current_user.id` in Python before ever touching a row). RLS here is defense-in-depth in case anything ever queries Postgres directly (e.g., a future Supabase client-side read).
- `runs_one_active_per_user` is what makes "one run at a time" a hard guarantee rather than a UI convention. Catch the resulting `UniqueViolation` in the run-creation endpoint and return a `409` with the existing active run's id so the frontend can redirect to it.
- Store `ValuationResult` redundantly in `cached_models.valuation_result` (not just S3) so the API can serve a cache hit without an S3 round trip.
## 4. Core domain schemas (Pydantic)

These are the contracts every other module is built against. Get these right first — tickets for the engine, exporters, proposer, and API can then all proceed in parallel against fixtures.

### 4.1 Company / classification

```python
# backend/app/schemas/company.py
from enum import StrEnum
from pydantic import BaseModel, Field

class ModelType(StrEnum):
    FCFF = "fcff"
    FCFE = "fcfe"
    EXCESS_RETURN = "excess_return"
    NAV_REIT = "nav_reit"
    NAV_EP = "nav_ep"
    SOTP = "sotp"

class DeclineReason(StrEnum):
    BIOTECH_PRECOMMERCIAL = "biotech_precommercial"
    LIFE_INSURER = "life_insurer"
    SPAC_OR_TRUST = "spac_or_trust"
    MLP = "mlp"
    MINING = "mining"
    NON_10K_FILER = "non_10k_filer"          # 20-F/40-F filers etc.
    INSUFFICIENT_DATA = "insufficient_data"   # <3 years of usable history

class CompanySnapshot(BaseModel):
    ticker: str
    cik: str
    name: str
    sic_code: str
    sic_description: str
    market_cap_usd: float
    share_class_note: str | None = None       # e.g. "Class A (GOOGL); Class C (GOOG) also trades"

class ClassificationResult(BaseModel):
    company: CompanySnapshot
    recommended_model: ModelType | None       # None if declined
    confidence: float = Field(ge=0, le=1)
    reasons: list[str]
    runner_up: ModelType | None
    decline_reason: DeclineReason | None
    historical_window_years: int              # 5 or 10 (or fewer, with a confidence flag)
    window_reason: str
    sotp_segments: list[str] | None = None    # populated only when recommended_model == SOTP
```

### 4.2 Normalized financials (EDGAR output → engine input)

```python
# backend/app/schemas/financials.py
from pydantic import BaseModel

class FiscalPeriod(BaseModel):
    fiscal_year: int
    period_end: str            # ISO date
    is_ttm: bool = False

class IncomeStatementLine(BaseModel):
    period: FiscalPeriod
    revenue: float
    cogs: float | None
    gross_profit: float | None
    sga: float | None
    rd: float | None
    operating_income: float
    interest_expense: float
    pretax_income: float
    tax_expense: float
    net_income: float
    diluted_shares: float

class BalanceSheetLine(BaseModel):
    period: FiscalPeriod
    cash_and_equivalents: float
    short_term_investments: float
    total_debt: float                 # incl. current + long-term + finance leases
    operating_lease_liability: float
    total_equity: float
    minority_interest: float
    preferred_equity: float
    pension_deficit: float | None

class CashFlowLine(BaseModel):
    period: FiscalPeriod
    depreciation_amortization: float
    stock_based_comp: float
    capex: float
    change_in_nwc: float

class SegmentLine(BaseModel):
    period: FiscalPeriod
    segment_name: str
    revenue: float
    operating_income: float | None
    depreciation_amortization: float | None
    capex: float | None
    assets: float | None

class BankSpecificLine(BaseModel):
    period: FiscalPeriod
    net_interest_income: float
    provision_for_credit_losses: float
    total_deposits: float
    tangible_book_value: float
    tier1_capital_ratio: float | None

class InsurerSpecificLine(BaseModel):
    period: FiscalPeriod
    net_premiums_earned: float
    loss_and_lae_ratio: float | None
    combined_ratio: float | None          # P&C only
    book_value_per_share: float

class ReitSpecificLine(BaseModel):
    period: FiscalPeriod
    real_estate_investments_gross: float
    accumulated_depreciation: float
    ffo: float | None                     # from 8-K supplement if available; else derived
    affo: float | None

class EpSpecificLine(BaseModel):
    period: FiscalPeriod
    standardized_measure_disc_future_cash_flows: float | None
    proved_reserves_oil_mmbbl: float | None
    proved_reserves_gas_bcf: float | None

class NormalizedFinancials(BaseModel):
    ticker: str
    cik: str
    fiscal_year_end_month: int
    income_statements: list[IncomeStatementLine]     # oldest -> newest, includes TTM as last row
    balance_sheets: list[BalanceSheetLine]
    cash_flows: list[CashFlowLine]
    segments: list[SegmentLine] = []
    bank_data: list[BankSpecificLine] = []
    insurer_data: list[InsurerSpecificLine] = []
    reit_data: list[ReitSpecificLine] = []
    ep_data: list[EpSpecificLine] = []
    data_confidence_flags: list[str] = []            # e.g. "only 4 years available", "revenue jump FY23 (M&A?)"
    accession_number: str                            # latest filing this data reflects — drives the cache key

class MarketSnapshot(BaseModel):
    ticker: str
    price: float
    as_of: str          # ISO datetime
    shares_outstanding: float
    market_cap: float
    risk_free_rate: float          # from FRED DGS10, as of run time
    industry_unlevered_beta: float # from Damodaran, by SIC-mapped industry
    equity_risk_premium: float     # from Damodaran implied ERP dataset
```

### 4.3 Assumptions — one flat, all-required schema per model type

**Why all-required and flat:** Claude's structured-outputs grammar compiler caps a request at **24 optional parameters** and **16 union-typed parameters** combined across all schemas in the call. A schema with `Optional[float]` fields or nested unions burns through both budgets fast. Since every field here always has a value (the proposer either derives it or asks the LLM to fill it — there is no "N/A" case within a single model type), making every field `required` with a concrete type sidesteps the limit entirely. Never add `| None` to these classes. If a model type genuinely doesn't need a field, don't put it in that model's schema — don't make it optional.

Each numeric assumption line carries its rationale and source so the confirmation screen can show them without a second call.

```python
# backend/app/schemas/assumptions.py
from enum import StrEnum
from pydantic import BaseModel, Field

class AssumptionSource(StrEnum):
    HISTORICAL_TREND = "historical_trend"
    INDUSTRY_MEDIAN = "industry_median"       # Damodaran
    ANALYST_LIKE_JUDGMENT = "analyst_like_judgment"  # LLM's own reasoned estimate
    RISK_FREE_RATE = "risk_free_rate"         # FRED
    REGULATORY_FILING = "regulatory_filing"   # e.g. reserve life from 10-K

class AssumptionField(BaseModel):
    value: float
    rationale: str = Field(max_length=240)
    source: AssumptionSource

class FCFFAssumptions(BaseModel):
    """Also used, with different bounds, for the early-stage-tech variant."""
    revenue_growth_y1: AssumptionField
    revenue_growth_y2: AssumptionField
    revenue_growth_y3: AssumptionField
    revenue_growth_y4: AssumptionField
    revenue_growth_y5: AssumptionField
    target_operating_margin: AssumptionField      # margin the company converges to
    margin_convergence_years: AssumptionField     # years to reach target_operating_margin
    tax_rate: AssumptionField
    sales_to_capital_ratio: AssumptionField        # reinvestment efficiency
    risk_free_rate: AssumptionField
    equity_risk_premium: AssumptionField
    levered_beta: AssumptionField
    pretax_cost_of_debt: AssumptionField
    target_debt_to_capital: AssumptionField
    terminal_growth_rate: AssumptionField
    terminal_roic: AssumptionField                 # sanity cross-check vs. WACC
    survival_probability: AssumptionField          # 1.0 for stable co.'s; <1.0 for early-stage-tech variant

class FCFEAssumptions(BaseModel):
    revenue_growth_y1: AssumptionField
    revenue_growth_y2: AssumptionField
    revenue_growth_y3: AssumptionField
    revenue_growth_y4: AssumptionField
    revenue_growth_y5: AssumptionField
    target_net_margin: AssumptionField
    margin_convergence_years: AssumptionField
    tax_rate: AssumptionField
    target_debt_to_capital: AssumptionField
    net_borrowing_as_pct_reinvestment: AssumptionField
    risk_free_rate: AssumptionField
    equity_risk_premium: AssumptionField
    levered_beta: AssumptionField
    terminal_growth_rate: AssumptionField

class ExcessReturnAssumptions(BaseModel):
    """Banks and P&C insurers."""
    roe_y1: AssumptionField
    roe_y2: AssumptionField
    roe_y3: AssumptionField
    roe_y4: AssumptionField
    roe_y5: AssumptionField
    terminal_roe: AssumptionField
    cost_of_equity: AssumptionField                # risk_free + beta * ERP
    book_value_growth_rate: AssumptionField
    payout_ratio: AssumptionField
    terminal_growth_rate: AssumptionField

class ReitNavAssumptions(BaseModel):
    cap_rate: AssumptionField
    noi_growth_rate: AssumptionField
    non_real_estate_asset_adjustment: AssumptionField   # e.g. cash, other assets, as a lump sum
    liability_adjustment: AssumptionField               # debt + preferred, as a lump sum

class EpNavAssumptions(BaseModel):
    price_deck_oil_per_bbl: AssumptionField
    price_deck_gas_per_mcf: AssumptionField
    discount_rate_pv10: AssumptionField
    development_cost_adjustment: AssumptionField

class SotpSegmentAssumption(BaseModel):
    segment_name: str
    valuation_approach: str        # "fcff" or "ev_ebitda_multiple"
    ev_ebitda_multiple: float      # used only when valuation_approach == "ev_ebitda_multiple"
    fcff_assumptions: FCFFAssumptions | None = None   # used only when valuation_approach == "fcff"

class SotpAssumptions(BaseModel):
    segments: list[SotpSegmentAssumption]
    corporate_overhead_capitalized: AssumptionField    # negative EV adjustment
    conglomerate_discount_note: str
```

**On `SotpAssumptions`:** this is the one schema that *can't* be all-required-and-flat (a variable-length list of segments, each optionally carrying a nested `FCFFAssumptions`). Because SOTP is the rarer path (gated, per §5.4) and each segment is proposed **one call per segment** rather than one call for the whole company, each individual call still respects the complexity limits — call the LLM once per segment with the flat `FCFFAssumptions` (or a 2-field multiple schema), then assemble `SotpAssumptions` in Python. Never send the whole `SotpAssumptions` shape to `messages.parse()` in one call.

### 4.4 ValuationResult — the common output shape every model produces

```python
# backend/app/schemas/valuation_result.py
from pydantic import BaseModel

class NonOperatingAdjustment(BaseModel):
    label: str              # "Equity-method investment in X", "Excess cash", "NOL carryforward value"
    amount: float

class ScenarioResult(BaseModel):
    label: str               # "base" | "bull" | "bear"
    value_per_share: float
    key_assumption_deltas: dict[str, float]

class SensitivityCell(BaseModel):
    row_label: str            # e.g. WACC value or cap rate
    col_label: str            # e.g. terminal growth or NOI growth
    value_per_share: float

class ValuationResult(BaseModel):
    ticker: str
    model_type: str
    run_date: str
    currency: str = "USD"

    # value bridge (operating-value models populate all of these;
    # equity-direct models — FCFE, excess return — set operating_value = enterprise_value = equity_value
    # and leave the adjustment fields at 0, since they value equity directly)
    operating_value: float
    cash_and_equivalents: float
    non_operating_adjustments: list[NonOperatingAdjustment]
    enterprise_value: float
    total_debt: float
    operating_lease_liability: float
    preferred_equity: float
    minority_interest: float
    pension_deficit: float
    equity_value: float

    diluted_shares: float
    value_per_share: float
    market_price: float
    upside_pct: float

    implied_ev_ebitda: float | None
    implied_pb: float | None
    implied_p_ffo: float | None

    scenarios: list[ScenarioResult]
    sensitivity_grid: list[SensitivityCell]

    historical_window_years: int
    data_confidence_flags: list[str]
    assumptions_used: dict                 # the resolved FCFFAssumptions/etc. as a dict, for the PDF/Excel
    sources: list[str]                     # "SEC EDGAR 10-K filed 2026-02-14", "FRED DGS10", "Damodaran Jan 2026 betas"

    accession_number: str
    engine_version: str
```

Note: `ValuationResult` is intentionally **not** what gets sent through Claude structured outputs — it's the engine's *output* type, produced by pure Python arithmetic, never by the LLM. Only the `*Assumptions` classes in §4.3 go through `messages.parse()`.
## 5. Data layer

### 5.1 EDGAR client (`backend/app/data/edgar/client.py`)

```python
import asyncio, time, httpx

SEC_RATE_LIMIT_PER_SEC = 10

class EdgarRateLimiter:
    """Redis-backed token bucket shared across ALL API and worker processes —
    the SEC's 10 req/s cap applies per IP across every process we run, not per-process."""
    def __init__(self, redis, key="edgar:ratelimit", rate=SEC_RATE_LIMIT_PER_SEC):
        self.redis, self.key, self.rate = redis, key, rate

    async def acquire(self):
        # Redis Lua script: sliding-window counter, sleep-and-retry if over budget.
        ...

class EdgarClient:
    BASE = "https://data.sec.gov"

    def __init__(self, user_agent: str, limiter: EdgarRateLimiter):
        # user_agent MUST be "<Company/App Name> <contact email>" per SEC fair-access policy,
        # e.g. "DCF-Valuation-App aman.todi01@gmail.com" — read from SEC_EDGAR_USER_AGENT env var.
        self._client = httpx.AsyncClient(
            base_url=self.BASE,
            headers={"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"},
            timeout=30.0,
        )
        self.limiter = limiter

    async def get_submissions(self, cik: str) -> dict:
        await self.limiter.acquire()
        r = await self._client.get(f"/submissions/CIK{cik.zfill(10)}.json")
        r.raise_for_status()
        return r.json()

    async def get_companyfacts(self, cik: str) -> dict:
        await self.limiter.acquire()
        r = await self._client.get(f"/api/xbrl/companyfacts/CIK{cik.zfill(10)}.json")
        r.raise_for_status()
        return r.json()

    async def get_ticker_to_cik_map(self) -> dict[str, str]:
        # https://www.sec.gov/files/company_tickers.json — note: this one is on www.sec.gov, not data.sec.gov
        ...
```

**Caching:** every raw `companyfacts`/`submissions` response is written to S3 at `edgar-raw/{cik}/{fetched_at_iso}.json` and indexed in `edgar_filing_cache`. Before hitting EDGAR, check whether `edgar_filing_cache.latest_accession` still matches the submissions feed's most recent 10-K/10-Q accession; if so, serve the cached blob. This is what makes the cache-key filing check (§8) a single cheap `submissions` call rather than a full re-pull.

### 5.2 Normalization (`normalize.py`)

- **Tag-mapping table**: a dict of `concept -> ordered list of US-GAAP XBRL tags to try`, e.g. `revenue: ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet"]`. Build and unit-test this against ~20 diverse tickers (Apple, a bank e.g. JPM, an insurer e.g. TRV, a REIT e.g. O, an E&P e.g. EOG, a conglomerate e.g. HON, a recent IPO, a loss-making tech co.) before trusting it broadly.
- **TTM derivation**: `TTM = latest_annual + latest_YTD_quarter - same_period_prior_year_YTD`. Handle fiscal-year-end months other than December.
- **Restatements**: when a concept has multiple facts for the same `end` date, take the one with the latest `filed` date.
- **Segments**: `companyfacts` doesn't carry dimensional (segment) data — pull it from the filing's XBRL frames or the SEC's Financial Statement and Notes dataset. This is the one piece of real XBRL-dimension parsing in the app; isolate it behind `segments.py` so it can be tested/iterated independently.
- **Historical window trimming**: `normalize.py` returns the *full* pulled history; `classify/windows.py` decides how many years of it the engine actually uses (§5.4).
- Every unmapped/derived value gets appended to `data_confidence_flags` on the `NormalizedFinancials`.

### 5.3 Market data (`backend/app/data/market/`)

```python
# base.py
from abc import ABC, abstractmethod

class MarketDataProvider(ABC):
    @abstractmethod
    async def get_price_snapshot(self, ticker: str) -> "PriceSnapshot": ...

class PriceSnapshot(BaseModel):
    ticker: str
    price: float
    as_of: str
    shares_outstanding: float
    market_cap: float
```

```python
# yfinance_provider.py
import yfinance as yf
import asyncio

class YFinanceProvider(MarketDataProvider):
    async def get_price_snapshot(self, ticker: str) -> PriceSnapshot:
        # yfinance is sync; run in a thread. Retry once on failure, then raise a
        # typed MarketDataUnavailable error that the run pipeline turns into a
        # user-facing "couldn't fetch a live price — retry" failure (no fallback,
        # no cached price — per explicit decision).
        def _fetch():
            t = yf.Ticker(ticker)
            fi = t.fast_info
            return fi
        fi = await asyncio.to_thread(_fetch)
        ...
```

No price caching, no fallback provider, no daily-movement robustness requirement — this is intentionally the simplest possible implementation. Fetched once when a run starts (feeds the engine) and again whenever a cached result is *viewed* (to show live upside next to the cached fair value) — that second fetch is a pure read, not part of any cache key.

### 5.4 Macro data (`backend/app/data/macro/`)

- **`fred.py`**: `GET https://api.stlouisfed.org/fred/series/observations?series_id=DGS10&api_key=...&file_type=json&sort_order=desc&limit=1`. Requires `FRED_API_KEY` — no keyless tier exists. FRED encodes a missing observation as the literal string `"."`; map that to `None`/error, never to `0`.
- **`damodaran.py`**: downloads the beta-by-industry and implied-ERP `.xls` files from `pages.stern.nyu.edu/~adamodar/pc/datasets/` (no key, but Chrome-style headers sometimes needed — direct `httpx` GET has worked historically; if blocked, this is a manual-download fallback documented in the AWS/ops runbook). Parses to a `{industry_name: {unlevered_beta, ...}}` dict, caches the parsed table (not just the raw xls) to S3 with a `fetched_at` date, and maps our SIC codes to Damodaran's ~90 industry buckets via a hand-maintained lookup table (`damodaran_sic_map.py`) — this mapping is approximate and should ship with a handful of unit tests pinning known tickers to known industries. Refresh cadence: manual, a few times a year (Damodaran republishes in January and periodically through the year) — a `scripts/seed_damodaran_cache.py` script in `infra/scripts/` does the pull; there is no automatic refresh job.

### 5.5 Classifier (`backend/app/classify/`)

**`rules.py`** — the decision tree, evaluated in this order:

1. **Hard declines** (set `decline_reason`, no model recommended):
   - Filer type isn't 10-K/10-Q (i.e., files 20-F/40-F) → `NON_10K_FILER`.
   - SIC code in the life-insurance range (6311) → `LIFE_INSURER`.
   - SIC code indicates a SPAC/blank-check/trust or the entity has no real operating segment → `SPAC_OR_TRUST`.
   - SIC in an MLP-heavy range **and** entity type is a partnership (`entityType` in submissions ≠ `"operating"` corp, or name contains "L.P."/"LP") → `MLP`.
   - SIC in metals-mining ranges (1000–1099, 1200–1299) → `MINING`.
   - **Biotech/pre-revenue signature**: TTM revenue < some small absolute threshold (e.g. <$50M) **and** R&D/revenue far exceeds 1 (or revenue is ~0) **and** the company has been public >2 years with sustained negative operating cash flow → `BIOTECH_PRECOMMERCIAL`. (Note: this checks the *financial signature*, not the SIC/pharma label — a profitable pharma major like Pfizer must NOT be caught here; it should fall through to FCFF.)
   - Fewer than 3 fiscal years of usable normalized data → `INSUFFICIENT_DATA`.
2. **Model selection** (first match wins):
   - XBRL tags for `Deposits` + `InterestIncomeExpenseNet` present and material → `EXCESS_RETURN` (bank).
   - XBRL tags for `PremiumsEarnedNet` present and material → `EXCESS_RETURN` (P&C insurer).
   - `RealEstateInvestmentPropertyNet` material and entity elected REIT status (name/10-K text signal, or SIC 6798) → `NAV_REIT`.
   - `StandardizedMeasureOfDiscountedFutureNetCashFlowsRelatingToProvedOilAndGasReserves` tag present → `NAV_EP`.
   - ≥2 reportable segments, each ≥15–20% of consolidated revenue *or* operating income, with visibly different margins/growth/capital intensity, and segment-level opex data available (ASU 2023-07, so realistically FY2024+ 10-Ks) → `SOTP`.
   - High leverage (`total_debt / (total_debt + market_cap)` above a tunable threshold, e.g. >50%) **and** a stable historical debt ratio → `FCFE` as a candidate, but **default to FCFF** unless FCFE is clearly a better fit (FCFE is offered as the *runner-up*, not auto-selected, unless leverage is extreme and stable — tune this threshold during testing rather than hard-coding a single bright line).
   - Otherwise → `FCFF` (with the early-stage-tech variant flag set if TTM operating margin is negative and revenue is growing >20%/yr).
3. **LLM tiebreak**: only invoked when the top two candidates from step 2 are within a small confidence gap of each other (e.g. SOTP vs. FCFF when segment materiality is borderline). A single Claude call with the company's segment table and financial summary, returning a small structured `{recommended: ModelType, confidence: float, reasoning: str}` — this schema is tiny and fine for optional fields since it's not near the complexity limits.

**`windows.py`** — 5 vs. 10 years:
```python
def historical_window_years(model_type: ModelType, sic_code: str, financials: NormalizedFinancials) -> tuple[int, str]:
    if model_type in (ModelType.EXCESS_RETURN, ModelType.NAV_EP):
        return 10, "model type requires a full cycle"
    if sic_code in CYCLICAL_SIC_RANGES:
        return 10, "cyclical sector"
    margin_swing = max(op_margins[-5:]) - min(op_margins[-5:])
    revenue_dd = 1 - min(revenues[-5:]) / max(revenues[-5:])
    if margin_swing > 0.08 or revenue_dd > 0.20:
        return 10, "volatility trigger: margin swing or revenue drawdown within default window"
    return 5, "default window"
```
Both thresholds (`0.08`, `0.20`) are named constants in `windows.py`, tuned against the 20-ticker spot-check set in testing (§10), not hard-derived — flag them clearly as tunable.

### 5.6 Assumption proposer (`backend/app/assumptions/proposer.py`)

Plain propose → validate → repair loop, no LangGraph:

```python
async def propose_assumptions(model_type: ModelType, financials: NormalizedFinancials,
                               market: MarketSnapshot, industry: DamodaranIndustryData) -> AssumptionsBase:
    schema_cls = ASSUMPTION_SCHEMA_BY_MODEL[model_type]
    prompt = build_prompt(model_type, financials, market, industry)  # includes historical medians as anchors

    for attempt in range(MAX_REPAIR_ATTEMPTS):  # e.g. 3
        response = await claude.messages.parse(
            model=settings.ANTHROPIC_MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": prompt}],
            output_format=schema_cls,
        )
        proposal = response.parsed_output
        violations = check_bounds(model_type, proposal)   # bounds.py — e.g. terminal_growth < risk_free_rate
        if not violations:
            return proposal
        prompt = build_repair_prompt(prompt, proposal, violations)

    raise AssumptionProposalFailed(model_type, violations)
```

`bounds.py` sanity checks (non-exhaustive — extend per model type):
- `terminal_growth_rate <= risk_free_rate`
- WACC (derived from the proposed capital-structure/beta/cost-of-debt fields) `> terminal_growth_rate`
- `sales_to_capital_ratio > 0`
- `0 <= tax_rate <= 0.50`
- `0 < survival_probability <= 1`
- REIT: `cap_rate > 0` and within a plausible band (e.g. 3–12%)
- Excess return: `cost_of_equity > terminal_growth_rate`, `0 <= payout_ratio <= 1`

Both the **proposal itself** (for auto-mode caching) and the **prompt/schema versions** are recorded — `prompt_version` is a manually bumped string constant (`"v1"`, `"v2"`, …) incremented whenever `build_prompt` or an `AssumptionField`'s schema changes, since it's part of the cache key.
## 6. Valuation engine

Pure, deterministic, side-effect-free Python. No network calls, no LLM calls — every function here takes `NormalizedFinancials` + `MarketSnapshot` + an `AssumptionsBase` and returns a `ValuationResult`. This is the most heavily unit-tested part of the app (§10).

### 6.1 Common interface (`backend/app/valuation/base.py`)

```python
from abc import ABC, abstractmethod

class Valuator(ABC):
    model_type: ModelType

    @abstractmethod
    def compute(
        self,
        financials: NormalizedFinancials,
        market: MarketSnapshot,
        assumptions: "AssumptionsBase",
    ) -> ValuationResult: ...

    def compute_scenarios(self, financials, market, base_assumptions) -> list[ScenarioResult]:
        """Default bull/bear: nudge growth/margin/discount-rate assumptions by a
        fixed delta (e.g. ±2pts on growth, ±1pt on margin, ±0.5pt on WACC-driving
        inputs) and re-run .compute(). Model-specific valuators may override this
        with more meaningful bull/bear stories (e.g. cap-rate compression/expansion
        for REIT NAV)."""
        ...

    def compute_sensitivity_grid(self, financials, market, base_assumptions) -> list[SensitivityCell]:
        """Grid over the model's own two key levers, e.g. WACC x terminal growth
        for FCFF/FCFE, cap rate x NOI growth for REIT NAV, cost of equity x
        terminal ROE for excess return. 5x5 grid is the default size."""
        ...


class ValueBridge:
    """Shared math for the operating-value -> equity-value walk (§4.4). Equity-
    direct valuators (FCFE, excess return) skip this and set equity_value directly."""

    @staticmethod
    def bridge(
        operating_value: float,
        cash: float,
        non_operating: list[NonOperatingAdjustment],
        debt: float,
        lease_liability: float,
        preferred: float,
        minority: float,
        pension_deficit: float,
    ) -> tuple[float, float]:
        """Returns (enterprise_value, equity_value)."""
        enterprise_value = operating_value + cash + sum(a.amount for a in non_operating)
        equity_value = enterprise_value - debt - lease_liability - preferred - minority - pension_deficit
        return enterprise_value, equity_value
```

### 6.2 FCFF (`fcff.py`) — also the FCFE and early-stage-tech implementations live here

```python
def wacc(assumptions: FCFFAssumptions) -> float:
    cost_of_equity = (
        assumptions.risk_free_rate.value
        + assumptions.levered_beta.value * assumptions.equity_risk_premium.value
    )
    after_tax_cod = assumptions.pretax_cost_of_debt.value * (1 - assumptions.tax_rate.value)
    d = assumptions.target_debt_to_capital.value
    return cost_of_equity * (1 - d) + after_tax_cod * d

def project_fcff(financials: NormalizedFinancials, assumptions: FCFFAssumptions, years: int = 10) -> list[float]:
    """Two-stage or three-stage depending on margin_convergence_years vs. the
    explicit growth path length (5 years given) plus a fade to terminal growth.
    Reinvestment = revenue_growth / sales_to_capital_ratio. Applies
    survival_probability as a multiplicative haircut on every projected FCFF
    (used for the early-stage-tech variant; 1.0 is a no-op for normal FCFF)."""
    ...

class FCFFValuator(Valuator):
    model_type = ModelType.FCFF

    def compute(self, financials, market, assumptions: FCFFAssumptions) -> ValuationResult:
        w = wacc(assumptions)
        fcffs = project_fcff(financials, assumptions)
        terminal_value = fcffs[-1] * (1 + assumptions.terminal_growth_rate.value) / (w - assumptions.terminal_growth_rate.value)
        operating_value = sum(f / (1 + w) ** (i + 1) for i, f in enumerate(fcffs)) + terminal_value / (1 + w) ** len(fcffs)
        latest_bs = financials.balance_sheets[-1]
        non_operating = infer_non_operating_assets(financials)  # equity-method stakes, excess cash, NOLs — best-effort from tags
        ev, equity_value = ValueBridge.bridge(
            operating_value, latest_bs.cash_and_equivalents, non_operating,
            latest_bs.total_debt, latest_bs.operating_lease_liability,
            latest_bs.preferred_equity, latest_bs.minority_interest,
            latest_bs.pension_deficit or 0.0,
        )
        value_per_share = equity_value / financials.income_statements[-1].diluted_shares
        return build_valuation_result(...)  # fills every ValuationResult field, incl. flags for TV% of EV > 75%
```

`FCFEValuator` reuses `project_fcff`'s revenue/margin projection logic but computes FCFE (`net income + D&A - capex - ΔNWC + net borrowing`) and discounts at cost of equity directly — it sets `equity_value` directly and leaves the bridge fields at 0/pass-through (per the `ValuationResult` note in §4.4).

### 6.3 Excess Return / DDM (`excess_return.py`) — banks and P&C insurers

```python
class ExcessReturnValuator(Valuator):
    model_type = ModelType.EXCESS_RETURN

    def compute(self, financials, market, assumptions: ExcessReturnAssumptions) -> ValuationResult:
        # Value of equity = current book value + PV of excess equity earnings
        # excess_equity_earnings_t = (ROE_t - cost_of_equity) * book_value_{t-1}
        # Terminal value via the same excess-return logic faded to terminal_growth_rate / terminal_roe.
        ...
```

### 6.4 REIT NAV (`nav_reit.py`)

```python
class ReitNavValuator(Valuator):
    model_type = ModelType.NAV_REIT

    def compute(self, financials, market, assumptions: ReitNavAssumptions) -> ValuationResult:
        latest_reit = financials.reit_data[-1]
        noi = derive_noi(latest_reit, financials)   # from FFO/AFFO if disclosed, else derived from GAAP real estate income
        gross_asset_value = noi * (1 + assumptions.noi_growth_rate.value) / assumptions.cap_rate.value
        # operating_value == gross_asset_value here; non_operating carries the lump-sum
        # non_real_estate_asset_adjustment; the bridge subtracts debt + the lump-sum
        # liability_adjustment (folded into `debt` for this model to keep the bridge generic).
        ...
```

### 6.5 E&P NAV (`nav_ep.py`)

```python
class EpNavValuator(Valuator):
    model_type = ModelType.NAV_EP

    def compute(self, financials, market, assumptions: EpNavAssumptions) -> ValuationResult:
        latest_ep = financials.ep_data[-1]
        # Start from the SEC's Standardized Measure (PV-10-like), then adjust for
        # (a) the user's price deck vs. the SEC's trailing-12-month average price
        #     used in the standardized measure, by re-scaling reserve value linearly
        #     as a reasonable approximation (documented as an approximation in the flag list), and
        # (b) discount_rate_pv10 if different from the SEC's mandated 10%.
        ...
```

### 6.6 SOTP composite (`sotp.py`)

```python
class SotpValuator(Valuator):
    model_type = ModelType.SOTP

    def compute(self, financials, market, assumptions: SotpAssumptions) -> ValuationResult:
        segment_evs = []
        for seg_assumption in assumptions.segments:
            seg_financials = slice_financials_to_segment(financials, seg_assumption.segment_name)
            if seg_assumption.valuation_approach == "fcff":
                seg_result = FCFFValuator().compute(seg_financials, market, seg_assumption.fcff_assumptions)
                segment_evs.append((seg_assumption.segment_name, seg_result.enterprise_value))
            else:
                seg_ebitda = latest_segment_ebitda(financials, seg_assumption.segment_name)
                segment_evs.append((seg_assumption.segment_name, seg_ebitda * seg_assumption.ev_ebitda_multiple))
        operating_value = sum(ev for _, ev in segment_evs) + assumptions.corporate_overhead_capitalized.value
        # bridge as usual using consolidated balance sheet data
        ...
        # ALSO run consolidated FCFFValuator().compute(...) with a company-level
        # assumption set (proposed the normal FCFF way) and report the SOTP-vs-
        # consolidated-FCFF gap as `sources`/flags: "implied conglomerate premium/
        # discount vs. consolidated FCFF: +12%" — this satisfies the "run it if
        # we're already computing it anyway" rule from the design discussion.
```

### 6.7 Registry

```python
# backend/app/valuation/__init__.py
VALUATOR_REGISTRY: dict[ModelType, type[Valuator]] = {
    ModelType.FCFF: FCFFValuator,
    ModelType.FCFE: FCFEValuator,
    ModelType.EXCESS_RETURN: ExcessReturnValuator,
    ModelType.NAV_REIT: ReitNavValuator,
    ModelType.NAV_EP: EpNavValuator,
    ModelType.SOTP: SotpValuator,
}
```

`engine_version` (part of the cache key) is a manually bumped constant in this module, incremented whenever any valuator's math changes.
## 7. Export: Excel (live formulas) and PDF

### 7.1 Excel builder (`backend/app/export/excel/builder.py`)

Built with **XlsxWriter**, writing **formulas**, not values, so a user can edit an assumption cell in Excel and watch the model recalculate. Sheets (module per sheet under `sheets/`):

| Sheet | Contents |
|---|---|
| `Cover` | Ticker, company name, model type used + why, run date, price as of, disclaimer text. |
| `Assumptions` | Every `AssumptionField` as an editable cell, with its rationale/source in an adjacent column (as a cell comment or a visible column). Named ranges for each (e.g. `terminal_growth_rate`) so downstream sheets reference names, not raw cell addresses — this is what makes edits actually flow through. |
| `Historicals` | The full pulled history (up to 10 years) even when the model uses a 5-year window; the used window is highlighted. |
| `Projections` | Formula-driven revenue/margin/FCFF (or FCFE/excess-return-earnings/NOI) projection rows referencing the named `Assumptions` ranges. |
| `WACC` (FCFF/FCFE only) | Cost of equity, after-tax cost of debt, weights, WACC — all formulas. |
| `DCF` / `Valuation` | Discounting, terminal value, the value bridge (§4.4 fields as formula rows), value per share, upside vs. the price cell. |
| `Sensitivity` | A `DATA TABLE`-free 5×5 grid built as plain formulas (re-deriving the DCF at each row/col combination) — Excel data tables don't survive XlsxWriter round-trips reliably, so compute the grid as independent formula blocks instead. |
| `SOTP` (SOTP only) | One block per segment plus the consolidated-vs-SOTP comparison. |

```python
# builder.py (skeleton)
def build_workbook(result: ValuationResult, financials: NormalizedFinancials,
                    market: MarketSnapshot, assumptions: AssumptionsBase, out_path: str) -> None:
    wb = xlsxwriter.Workbook(out_path)
    formats = build_formats(wb)   # currency, percent, header, warning
    write_cover_sheet(wb, formats, result)
    write_assumptions_sheet(wb, formats, assumptions)   # defines named ranges
    write_historicals_sheet(wb, formats, financials, result.historical_window_years)
    write_projections_sheet(wb, formats, financials, assumptions, result.model_type)
    if result.model_type in ("fcff", "fcfe"):
        write_wacc_sheet(wb, formats, assumptions)
    write_valuation_sheet(wb, formats, result, assumptions)
    write_sensitivity_sheet(wb, formats, result)
    if result.model_type == "sotp":
        write_sotp_sheet(wb, formats, result, assumptions)
    wb.close()
```

**Mandatory verification step** (ticket 9 + tested in ticket 14): after `build_workbook`, run the workbook through **headless LibreOffice** to force a real recalculation, read back the computed values with `openpyxl(data_only=True)`, and assert they match the Python engine's `ValuationResult.value_per_share` within a small tolerance (e.g. 0.5%). This catches formula bugs that only show up when Excel actually evaluates them.

```python
# backend/app/export/excel/recalc_verify.py
import subprocess, tempfile, shutil, os
from openpyxl import load_workbook

def recalc_and_read(xlsx_path: str, timeout: int = 30) -> dict[str, float]:
    """Forces LibreOffice to recalculate (not just re-render) by seeding a throwaway
    profile with OOXMLRecalcMode=1 (Always recalculate), then round-tripping the
    file through a headless convert-to-xlsx pass, which re-saves with fresh values."""
    profile_dir = tempfile.mkdtemp()
    _seed_recalc_profile(profile_dir)   # writes registrymodifications.xcu with OOXMLRecalcMode=1
    out_dir = tempfile.mkdtemp()
    try:
        subprocess.run(
            [
                "soffice", "--headless", "--norestore",
                f"-env:UserInstallation=file://{profile_dir}",
                "--convert-to", "xlsx:Calc MS Excel 2007 XML",
                "--outdir", out_dir, xlsx_path,
            ],
            timeout=timeout, check=True, capture_output=True,
        )
        recalced_path = os.path.join(out_dir, os.path.basename(xlsx_path))
        wb = load_workbook(recalced_path, data_only=True)
        return {"value_per_share": wb["Valuation"]["B2"].value, ...}  # read the named cells you need
    finally:
        shutil.rmtree(profile_dir, ignore_errors=True)
        shutil.rmtree(out_dir, ignore_errors=True)
```

This binary (`soffice`) and its dependent Pango/Cairo libs only need to exist in the **worker** container (§11), not the API container.

### 7.2 PDF builder (`backend/app/export/pdf/builder.py`)

Jinja2 → HTML → WeasyPrint. Structure of `report.html.jinja`:

1. Title page: ticker, company, model used + one-paragraph "why this model" explanation.
2. Executive summary: value per share, upside, key drivers (2–3 bullets from the AI narrative).
3. Business/historical overview (pulled from EDGAR MD&A text where available, else derived from the numbers).
4. Model mechanics: which model, the resolved assumptions table (value + rationale + source, straight from `assumptions_used`), and — for FCFF/FCFE — a plain-English explanation of the projection.
5. Valuation walk: the value bridge, rendered as a small waterfall chart (matplotlib PNG, see `charts.py`).
6. Sensitivity: the 5×5 grid as a styled HTML table, plus a heatmap PNG.
7. Scenarios: base/bull/bear bar chart.
8. Limitations & disclaimers: terminal-value-as-%-of-EV flag if high, data confidence flags, the "not investment advice" boilerplate, every source cited.

```python
def build_pdf(result: ValuationResult, narrative: "ReportNarrative", out_path: str) -> None:
    charts = render_charts(result)   # dict of {name: base64_png}
    html = jinja_env.get_template("report.html.jinja").render(result=result, narrative=narrative, charts=charts)
    HTML(string=html).write_pdf(out_path)
```

`ReportNarrative` is a **small**, mostly-optional-is-fine Pydantic schema (a handful of string fields: `why_this_model`, `executive_summary`, `key_drivers: list[str]`, `limitations_note`) produced by a *separate*, simple `messages.parse()` call — this one is fine with a couple of optional fields since it's nowhere near the complexity limits. It is explicitly **prose generation**, not numeric — the numbers it discusses come straight from `ValuationResult`, never invented.

**Worker container system dependencies for WeasyPrint** (Debian/Ubuntu base):
```
apt-get install -y --no-install-recommends \
  libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 \
  libcairo2 libgdk-pixbuf2.0-0 libharfbuzz-subset0 \
  fonts-liberation fonts-dejavu-core shared-mime-info
```
## 8. Run lifecycle & job orchestration

### 8.1 State machine

```
PENDING(created) -> CLASSIFYING -> PROPOSING -> AWAITING_CONFIRM -> BUILDING -> COMPLETE
                                                       |                 |
                                                       v                 v
                                                  (discarded on      FAILED / CANCELLED
                                                   new ticker,
                                                   not persisted
                                                   as a distinct
                                                   status change)
```

- `CLASSIFYING`, `PROPOSING`, `BUILDING` are **active** states — covered by the `runs_one_active_per_user` DB constraint (§3) and by the frontend's global busy lock.
- `AWAITING_CONFIRM` is a **waiting** state — no worker owns it, nothing is running, the user can sit on it indefinitely or navigate away. If the user starts a *different* ticker while one run sits in `AWAITING_CONFIRM`, the backend simply creates the new run (the old one is left as-is in the DB, just abandoned — no explicit "discard" transition needed since it was never locking anything).
- Two SAQ jobs implement the transitions:
  - `classify_and_propose(run_id)`: `PENDING -> CLASSIFYING -> PROPOSING -> AWAITING_CONFIRM` (or `-> FAILED` on decline/error). Checks the `cached_proposals` table first — on a hit, skips straight to `AWAITING_CONFIRM` with the cached assumptions.
  - `build_model(run_id)`: `AWAITING_CONFIRM -> BUILDING -> COMPLETE` (or `FAILED`/`CANCELLED`). Checks `cached_models` first (auto-mode only) — on a hit, copies the cached result onto the run and finishes immediately without touching the engine.

### 8.2 SAQ worker (`backend/app/jobs/worker_settings.py`)

```python
from saq import Queue

async def startup(ctx):
    ctx["edgar_client"] = build_edgar_client()
    ctx["anthropic_client"] = build_anthropic_client()
    ctx["db_engine"] = build_engine()

async def shutdown(ctx):
    await ctx["db_engine"].dispose()

class WorkerSettings:
    queue = Queue.from_url(settings.REDIS_URL)
    functions = [classify_and_propose, build_model]
    concurrency = 4                 # small — this is a ~10-user app
    startup = startup
    shutdown = shutdown
    timers = []                      # no periodic jobs needed
```

### 8.3 Cancellation — real, not cosmetic

The UI's Cancel button calls `POST /api/runs/{id}/cancel`, which:
1. Sets `runs.cancel_requested = true` immediately (so a reconnecting client sees the intent even before the worker reacts).
2. Publishes to a Redis pub/sub channel `cancel:{run_id}`.

Inside the job, wrap the whole body in a task and race it against a cancellation listener:

```python
# backend/app/jobs/cancel.py
async def run_cancellable(run_id: str, coro_factory, redis):
    task = asyncio.create_task(coro_factory())
    cancel_event = asyncio.Event()

    async def _listen():
        pubsub = redis.pubsub()
        await pubsub.subscribe(f"cancel:{run_id}")
        async for msg in pubsub.listen():
            if msg["type"] == "message":
                cancel_event.set()
                break

    listener = asyncio.create_task(_listen())
    try:
        done, _ = await asyncio.wait(
            [task, asyncio.create_task(cancel_event.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if cancel_event.is_set() and task not in done:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            raise RunCancelled(run_id)
        return task.result()
    finally:
        listener.cancel()
```

Because the pipeline's work is almost entirely `await`-ing I/O (EDGAR, yfinance, Claude, S3) with short CPU bursts (the engine math, Excel/PDF generation), an `asyncio.Task.cancel()` lands within a request cycle or two — effectively immediate. The one exception is the LibreOffice `subprocess.run` in the Excel verification step: run it via `asyncio.create_subprocess_exec` instead of the blocking `subprocess.run` shown in §7.1's skeleton (that skeleton is illustrative; the real implementation must be async so it can be cancelled/timed-out) and `proc.kill()` it on cancellation.

On cancel: write any partial S3 outputs to a run-scoped temp prefix (`runs/{run_id}/_tmp/`) and only copy them to their final `runs/{run_id}/` or `models/{ticker}/{model}/{accession}/` location at the very end of a successful build; on cancellation or failure, delete the temp prefix. This guarantees a cancelled or failed run never contaminates the shared cache.

Also handle **deploy-time SIGTERM** for the worker: `docker compose` sends `SIGTERM`, then `SIGKILL` after the service's `stop_grace_period` (90s for the worker; SAQ's own shutdown grace is 60s so cleanup finishes first). Install a signal handler that sets a "draining" flag causing the worker to stop pulling new jobs and, if a job is in flight, treat it exactly like a user cancel (same partial-output cleanup) if it can't finish within the remaining time budget.

### 8.4 Single-flight build lock (concurrent identical requests)

Two users requesting the same uncached `ticker+model_type` at once must not both trigger a full LLM-proposal + build:

```python
# a Redis SET NX PX lock, acquired at the top of build_model() before checking cached_models
lock_key = f"buildlock:{cache_key}"
acquired = await redis.set(lock_key, run_id, nx=True, px=BUILD_LOCK_TTL_MS)  # e.g. 120_000
if not acquired:
    # someone else is building this exact cache_key right now — poll cached_models
    # every 1-2s up to a timeout, then fall back to building ourselves if it never lands.
    result = await poll_for_cache_hit(cache_key, timeout_s=90)
    if result:
        return result
# else: proceed to build, release the lock in a finally block
```

### 8.5 Cache-key computation (`backend/app/runs/cache_key.py`)

```python
def compute_cache_key(ticker: str, model_type: ModelType, accession_number: str,
                       engine_version: str, prompt_version: str) -> str:
    raw = f"{ticker}|{model_type}|{accession_number}|{engine_version}|{prompt_version}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
```
Only computed (and only used to look up/write `cached_models`/`cached_proposals`) when `run.mode == 'auto'` **and** `run.assumptions_edited is False`. The moment a user flips the "Editable" toggle and changes any value, `assumptions_edited` is set `True`, `cache_key` is left `NULL` on that run, and its outputs are written under a user-scoped S3 prefix (`runs/{run_id}/model.xlsx`) instead of the shared `models/{ticker}/...` prefix — this is the "fork" from §0.4.

### 8.6 SSE progress stream

```python
# GET /api/runs/{id}/events
@router.get("/runs/{run_id}/events")
async def stream_events(run_id: str, user=Depends(current_user), db=Depends(get_db)):
    async def event_gen():
        last_id = 0
        while True:
            rows = await fetch_new_events(db, run_id, after_id=last_id)
            for row in rows:
                last_id = row.id
                yield f"data: {row.model_dump_json()}\n\n"
            run = await fetch_run(db, run_id)
            if run.status in ("complete", "failed", "cancelled"):
                yield f"event: done\ndata: {run.status}\n\n"
                return
            yield ": heartbeat\n\n"   # keeps idle proxies/clients from closing the connection
            await asyncio.sleep(1.5)
    return StreamingResponse(event_gen(), media_type="text/event-stream")
```
Reconnect-safe: the frontend passes `Last-Event-ID` or simply re-requests from `run_events` on reload — either way, replay is just "all events with `id > last_seen`," which is what `run_events` (§3) exists for.
## 9. API surface & auth

### 9.1 REST/SSE routes (`backend/app/api/routes/runs.py`)

| Method & path | Purpose |
|---|---|
| `POST /api/runs` | Body `{ticker, mode: "auto"}`. Creates a `runs` row, enqueues `classify_and_propose`. Returns `409` + the existing run's id if the user already has an active run (caught from the DB unique-violation, not pre-checked — avoids a race). |
| `GET /api/runs/active` | Returns the current user's run in an active or `awaiting_confirm` state, if any (used on page load to restore the in-progress screen). `204` if none. |
| `GET /api/runs/{id}` | Full run row incl. classification + proposed assumptions once available. |
| `POST /api/runs/{id}/confirm` | Body `{model_type_override?, assumptions?: dict, edited: bool}`. Validates `assumptions` against the model's Pydantic schema and the same `bounds.py` checks used server-side for AI proposals (never trust client-submitted numbers unchecked). Transitions to `BUILDING`, enqueues `build_model`. If `model_type_override` differs from `run.model_type`, re-runs proposal for the new model type first (synchronously within this same job, before building) rather than requiring a second round trip. |
| `POST /api/runs/{id}/cancel` | Sets `cancel_requested`, publishes the Redis cancel signal (§8.3). |
| `GET /api/runs/{id}/events` | SSE stream (§8.6). |
| `GET /api/runs/{id}/result` | Once `COMPLETE`: the `ValuationResult` JSON + presigned S3 URLs for the `.xlsx` and `.pdf`, plus a **fresh** live price fetched at read time (not the cached one) so the UI can show current upside next to the cached fair value (§ pricing decision). |
| `GET /api/health` | Liveness/readiness for the compose healthcheck and deploy verification. No auth. |

### 9.2 Database connectivity (Supabase)

Two distinct connection strings, both env vars:

- `DATABASE_URL` — the **direct** (non-pooled, port 5432, direct host) connection, used **only by Alembic** for migrations (DDL needs session-level state pgbouncer-style poolers don't reliably give you).
- `DATABASE_POOLER_URL` — the Supabase **Session Pooler** connection (still port 5432, but through the pooler hostname) used by the running app (`api` and `worker`). Session mode behaves like a normal persistent connection — including full support for prepared statements — so **no `statement_cache_size=0` workaround is needed here**; that workaround is only required for the *transaction*-mode pooler (port 6543), which this app deliberately does not use since it isn't running as ephemeral serverless functions.

```python
# backend/app/db/base.py
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

engine = create_async_engine(
    settings.DATABASE_POOLER_URL,   # postgresql+psycopg://...
    pool_size=10,
    max_overflow=20,
    pool_recycle=300,
    pool_pre_ping=True,
)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
```

Driver: **psycopg (v3)**, async mode, via the `postgresql+psycopg://` SQLAlchemy dialect — chosen specifically to sidestep the whole class of `asyncpg` + PgBouncer-family prepared-statement bugs that come up repeatedly with Supabase's poolers, even though Session Pooler in principle avoids them; psycopg3 is the more forgiving choice if Supabase ever migrates connection behavior under us.

### 9.3 Auth (Supabase JWT, asymmetric/JWKS)

Supabase's **new JWT signing keys** feature (ES256, asymmetric) lets us verify tokens locally without a round trip to Supabase's Auth server. Use this, not the legacy shared-secret HS256 path.

```python
# backend/app/auth/jwks.py
import time, httpx
from jose import jwt

_cache = {"keys": None, "fetched_at": 0}
JWKS_TTL_SECONDS = 3600

async def get_jwks() -> dict:
    if _cache["keys"] is None or time.time() - _cache["fetched_at"] > JWKS_TTL_SECONDS:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{settings.SUPABASE_URL}/auth/v1/.well-known/jwks.json")
            r.raise_for_status()
            _cache["keys"] = r.json()
            _cache["fetched_at"] = time.time()
    return _cache["keys"]

async def verify_supabase_jwt(token: str) -> dict:
    jwks = await get_jwks()
    unverified_header = jwt.get_unverified_header(token)
    key = next((k for k in jwks["keys"] if k["kid"] == unverified_header["kid"]), None)
    if key is None:
        # kid rotated since our cache was populated — refresh once and retry
        jwks = await _force_refresh_jwks()
        key = next((k for k in jwks["keys"] if k["kid"] == unverified_header["kid"]), None)
        if key is None:
            raise AuthError("unknown signing key")
    claims = jwt.decode(token, key, algorithms=["ES256", "RS256"], audience="authenticated")
    return claims   # claims["sub"] is the Supabase user id -> our runs.user_id
```

```python
# backend/app/deps.py
async def current_user(authorization: str = Header(...)) -> AuthenticatedUser:
    token = authorization.removeprefix("Bearer ").strip()
    claims = await verify_supabase_jwt(token)
    return AuthenticatedUser(id=claims["sub"], email=claims.get("email"))
```

The **frontend** authenticates directly against Supabase Auth (email/password or magic link — either is fine for ~10 users; magic link avoids password-reset UX entirely and is the recommended default) using `@supabase/supabase-js`, and sends the resulting access token as `Authorization: Bearer <token>` on every API call. The backend never talks to Supabase Auth for login/signup — only for JWKS verification.

**Authorization boundary**: every route handler that takes a `run_id` must load the run and check `run.user_id == current_user.id`, returning `404` (not `403`, to avoid confirming existence) on mismatch. This is the real access-control layer; Postgres RLS (§3) is defense-in-depth since the backend connects with the service-role key.
## 10. Frontend

### 10.1 Pages & flow

- **`/` (ticker entry)**: On load, calls `GET /api/runs/active`. If one exists, redirects straight to `/runs/{id}` (restores whatever screen that status implies — this is how a page reload or a second device picks up mid-flight work). Otherwise shows the ticker input + submit. The whole form (and any nav to start a new run) is disabled whenever an active run exists — this is `ActiveRunGuard`, a layout-level component that also polls `GET /api/runs/active` on an interval so a run started from one tab locks the form in another.
- **`/runs/{id}`**: A single page whose rendered content is purely a function of `run.status`:
  - `classifying` / `proposing`: a lightweight "Looking up {ticker}…" spinner state (no cancel needed yet — usually <5s given caching, but still SSE-driven so it never feels stuck).
  - `awaiting_confirm`: `ModelConfirmCard` (recommended model, confidence, reasons, runner-up) directly above `AssumptionsForm` (read-only by default, driven by the model's JSON Schema — see 10.2), with the **"Editable (Not Recommended)"** toggle next to a "Build model" primary action. If the run has a cache hit already reflected (i.e., a finished `cached_models` row exists for this exact key), the button reads "View model" and skips straight to `building`→`complete` with no wait.
  - `building`: `ProgressView` — full-screen-ish modal-like state, SSE-driven progress log, and a **Cancel** button that calls `POST /runs/{id}/cancel`. Per the explicit requirement, essentially the whole app UI (nav, ticker form, everything except Cancel) is inert during this state — implemented as a top-level overlay/route guard, not just disabled buttons scattered around.
  - `complete`: `ResultView` — value per share, upside (using the fresh live price from `GET /runs/{id}/result`, not the cached one), the value bridge as a simple stacked bar, `SensitivityChart` (Recharts heatmap-style grid), scenario bars, download buttons for the `.xlsx`/`.pdf` (presigned S3 URLs, direct browser download — the API server is not in the download path).
  - `failed` / `cancelled`: a plain state with `error_message` (or "Cancelled" ) and a "Start over" button.

### 10.2 Schema-driven assumptions form (`components/assumptions-form.tsx`)

The backend exposes each model type's Pydantic schema as JSON Schema (FastAPI does this for free via `AssumptionsBase.model_json_schema()`, surfaced at a small helper endpoint or embedded directly in the `GET /api/runs/{id}` payload alongside `proposed_assumptions`). The form renders one row per field generically:

```tsx
// simplified shape
type AssumptionField = { value: number; rationale: string; source: string };
type AssumptionsPayload = Record<string, AssumptionField>;

function AssumptionsForm({
  assumptions, editable, onChange,
}: { assumptions: AssumptionsPayload; editable: boolean; onChange: (next: AssumptionsPayload) => void }) {
  return (
    <table>
      {Object.entries(assumptions).map(([key, field]) => (
        <tr key={key}>
          <td>{humanizeFieldName(key)}</td>
          <td>
            {editable
              ? <input type="number" step="0.001" value={field.value}
                       onChange={(e) => onChange({ ...assumptions, [key]: { ...field, value: Number(e.target.value) } })} />
              : <span>{formatAssumptionValue(key, field.value)}</span>}
          </td>
          <td className="text-muted">{field.rationale} — <em>{field.source}</em></td>
        </tr>
      ))}
    </table>
  );
}
```

No per-model-type form component is ever written by hand — new model types (should the app ever add one) get a form for free. Client-side bound checks mirror `bounds.py`'s simplest rules (e.g. terminal growth < risk-free rate) for immediate feedback, but the **server re-validates everything** on `POST /confirm` — the client-side check is UX sugar only, never the security boundary.

### 10.3 State/data fetching

- **TanStack Query** for `GET /api/runs/{id}` (poll every 2s while status is `classifying`/`proposing`, stop polling once `awaiting_confirm`/`complete`/`failed`/`cancelled`).
- **SSE** (native `EventSource`) drives the live progress log during `building`, layered on top of the same query cache — an `onmessage` handler calls `queryClient.setQueryData` so the rest of the UI stays in sync without a second poll loop.
- **Supabase JS client** (`lib/supabase-client.ts`) only for auth (sign-in, session/token retrieval) — every other read/write goes through our own API, never directly to Supabase from the browser.

### 10.4 Sensitivity chart

Recharts doesn't have a first-class heatmap; render the 5×5 `sensitivity_grid` as a `<table>` with cell background color interpolated by `value_per_share` relative to the base case (simple CSS, no chart library needed) plus a `ScatterChart`/`BarChart` for the scenario comparison. Keep this simple — it's a small internal tool for ~10 users, not a polished public product.
## 11. Deployment: Docker, EC2, Terraform, Vercel

Target scale is tens of users (≤ 50), so production is deliberately one small host:

```
browser ──HTTPS──> Vercel (Next.js frontend)
   │
   └──HTTPS (CORS)──> EC2 t4g.small, Elastic IP
                       └─ docker compose: caddy :80/:443 ──> api:8000 (uvicorn)
                                                              worker (SAQ) ──> redis:6379 <── api
                          api/worker ──> Supabase Postgres (session pooler) · S3 · ECR (pull)
                                     ──> SEC EDGAR · Yahoo · FRED · Anthropic (egress)
```

### 11.1 Dockerfiles

All images build for **`linux/arm64`** (Graviton).

- **`infra/docker/Dockerfile.api`** — `python:3.13-slim`, the backend package, `alembic.ini` + `alembic/` (the deploy runs migrations from this image), `uvicorn app.main:app --workers 2`, `HEALTHCHECK` on `/api/health`. No LibreOffice/WeasyPrint.
- **`infra/docker/Dockerfile.worker`** — `python:3.13-slim` plus `libreoffice-calc`/`libreoffice-core` (Excel recalc verification) and the WeasyPrint system libraries (`libpango-1.0-0 libpangoft2-1.0-0 libpangocairo-1.0-0 libcairo2 libgdk-pixbuf-2.0-0 libharfbuzz-subset0 fonts-liberation fonts-dejavu-core shared-mime-info`). `CMD ["saq", "app.jobs.worker_settings.settings", "-v"]`.
- **`infra/docker/Dockerfile.frontend`** — Next.js `output: "standalone"` build (`node:24-slim`), used only by the local `docker-compose.yml`; production frontend builds happen on Vercel.

### 11.2 docker-compose

**Local dev (`docker-compose.yml`)**: `redis`, `api`, `worker`, `frontend`, reading `.env`. Postgres is either the dev Supabase project or a local Postgres 16. Offline demo mode (`DATA_SOURCE_MODE=fixtures`, `STORAGE_BACKEND=local`, `DEV_AUTH_BYPASS=true`) runs the full flow with no external services.

**Production (`infra/deploy/docker-compose.prod.yml`)** on the EC2 host, project name `dcf`, all services `restart: unless-stopped` with json-file log rotation (10 MB × 5):

| Service | Image | Notes |
|---|---|---|
| `caddy` | `caddy:2.11-alpine` | Publishes 80/443 (+443/udp). Serves `APP_DOMAIN` with automatic HTTPS and reverse-proxies everything to `api:8000` with `flush_interval -1` (live SSE) and no response compression. No access log (the SSE URL carries `?access_token=`). Certificates persist in the `caddy_data` volume. |
| `api` | `<ECR>/dcf-api:<git-sha>` | Not published; healthcheck on `/api/health`; `stop_grace_period: 30s`. |
| `worker` | `<ECR>/dcf-worker:<git-sha>` | `init: true` (reaps LibreOffice children); `stop_grace_period: 90s`; `WORKER_SHUTDOWN_GRACE_SECONDS=60`; `WORKER_CONCURRENCY=2` on `t4g.small`. |
| `redis` | `redis:7-alpine` | No persistence (`--save "" --appendonly no`), 128 MB, `noeviction`. Queue, locks, rate limiter and cancel pub/sub only — Postgres and S3 are the sources of truth. |

The compose file pins values that must never differ in production regardless of `.env`: `STORAGE_BACKEND=s3`, `DATA_SOURCE_MODE=live`, `DEV_AUTH_BYPASS=false`, `REDIS_URL=redis://redis:6379`.

### 11.3 AWS resources (Terraform, `infra/terraform/`)

State lives in an S3 bucket created once by `infra/terraform/bootstrap/` (versioned, encrypted, TLS-only); the main configuration uses the S3 backend with `use_lockfile = true`.

- **Compute**: one EC2 instance, Amazon Linux 2023 arm64 (AMI from the public SSM parameter), `instance_type` variable (default `t4g.small`; `t4g.medium` when memory is tight), 30 GB encrypted gp3 root volume, IMDSv2 only (hop limit 2 so containers can use the instance role), `cpu_credits` variable (default `unlimited`), 2 GB swapfile. **Elastic IP**. CloudWatch alarms auto-recover on a system status-check failure and reboot on an instance status-check failure.
- **First boot (`user_data`)**: installs Docker, the compose plugin (checksum-verified), AWS CLI, Docker log rotation, the swapfile, and `/opt/dcf`.
- **Network**: default VPC. Security group allows inbound TCP 80/443 and UDP 443 from anywhere (IPv4 + IPv6) and all egress. **No SSH port and no key pair** — shell access is via SSM Session Manager.
- **Storage**: S3 artifact bucket (public access blocked, SSE-S3, TLS-only; lifecycle expires `models/` and `runs/` after 35 days, `deploy/` bundles after 90 days). ECR repositories `dcf-api` and `dcf-worker` (scan on push, keep last 10 images).
- **IAM**:
  - Instance role: `AmazonSSMManagedInstanceCore`, ECR pull, S3 on the artifact bucket, `ssm:GetParameter*` on `/dcf/prod/*`, `kms:Decrypt` via SSM.
  - GitHub Actions OIDC provider + deploy role trusted only for `repo:<owner>/<repo>:ref:refs/heads/main`: ECR push, `s3:PutObject` on `deploy/*`, read of `/dcf/prod/config/*` (never secrets), `ssm:SendCommand` restricted to this instance and `AWS-RunShellScript`.
- **Configuration**: SSM Parameter Store under `/dcf/prod/`. Terraform writes non-secret `config/*` String parameters (`APP_DOMAIN`, `PUBLIC_API_BASE_URL`, `CORS_ORIGINS`, `ACME_EMAIL`, `AWS_REGION`, `S3_BUCKET_NAME`, `ECR_REGISTRY`, `INSTANCE_ID`, `WORKER_CONCURRENCY`). Secrets (`SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, `DATABASE_URL`, `DATABASE_POOLER_URL`, `ANTHROPIC_API_KEY`, `FRED_API_KEY`) are SecureStrings written by `infra/scripts/put_ssm_params.sh` and **never enter Terraform state**.
- **API hostname**: `domain_name` variable (an A record to the Elastic IP); when empty, `<eip-with-dashes>.sslip.io`, which Let's Encrypt accepts, so HTTPS works without owning a domain.
- **Budget**: `aws_budgets_budget` (default $30/month) emailing at 80%/100% actual and 100% forecast.
- **Outputs**: `api_url`, `api_domain`, `elastic_ip`, `instance_id`, SSM session command, ECR URLs, bucket name, `github_deploy_role_arn`, `cors_origins`.

Approximate monthly cost (us-east-1, on-demand): EC2 `t4g.small` ≈ $12.30, 30 GB gp3 ≈ $2.40, public IPv4 ≈ $3.65, S3/ECR/data transfer < $1 — **≈ $19/month**; `t4g.medium` ≈ $31/month. Vercel Hobby: $0 (non-commercial use).

### 11.4 Deploy flow

`.github/workflows/deploy.yml` runs on push to `main` when the repo variable `DEPLOY_ENABLED == 'true'` (otherwise a notice-only job keeps `main` green):

1. **Build** (matrix: api, worker) on an arm64 runner (`ubuntu-24.04-arm`, overridable via `ARM_RUNNER`; QEMU fallback), OIDC into the deploy role, `docker buildx build --platform linux/arm64`, push to ECR tagged with the full git SHA.
2. **Deploy**: `infra/scripts/deploy.sh --skip-build --tag <sha>` uploads a bundle (`docker-compose.prod.yml`, `Caddyfile`, `deploy_remote.sh`) to `s3://<bucket>/deploy/<sha>.tar.gz`, then runs it on the host via **SSM Run Command** and waits for the result.
3. **On the host** (`/opt/dcf/deploy.sh <sha>`, under a lock): render `/opt/dcf/.env` (0600) from every parameter under `/dcf/prod`; ECR login; `docker compose pull`; `alembic upgrade head` in a one-off container of the new api image — **on failure, restore the previous `.env` and leave the running stack untouched**; `docker compose up -d --remove-orphans`; reload Caddy if its config changed; wait for the api healthcheck and all four services; probe HTTPS through Caddy; record current/previous tag; prune old images.

The same `deploy.sh` works from a laptop (builds with buildx unless `--skip-build`); rollback is `deploy.sh --skip-build --tag <previous-sha>`.

The **frontend** deploys independently: Vercel builds `frontend/` on every push (production on `main`, previews on branches) with `NEXT_PUBLIC_API_BASE_URL=https://<api host>`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`. The API allows the Vercel production origin via `CORS_ORIGINS` (explicit origins only; preview URLs must be listed individually to call the API).

### 11.5 S3 layout

```
s3://<bucket>/
  edgar-raw/{cik}/{fetched_at_iso}.json
  damodaran/{dataset_name}/{fetched_at_iso}.json      # parsed, not raw .xls
  models/{ticker}/{model_type}/{accession_number}/
      model.xlsx
      report.pdf
  runs/{run_id}/                                       # custom/forked (edited-assumptions) runs
      model.xlsx
      report.pdf
  runs/{run_id}/_tmp/                                   # in-progress writes; deleted on cancel/failure,
                                                         # promoted to the final prefix on success
  deploy/{git_sha}.tar.gz                               # deploy bundles (compose file, Caddyfile, script)
```

Lifecycle rule: expire `models/` and `runs/` after **35 days** (a backstop past the 30-day cache TTL tracked in Postgres; `cached_models.expires_at` is the real driver of what's servable) and `deploy/` after 90 days. `edgar-raw/` and `damodaran/` are not expired (small, and re-fetching costs SEC rate-limit budget).
## 12. Configuration — `.env.example`

Every value below is a placeholder; nothing here is a real credential. The runbooks in §14 say exactly how to obtain each one.

```dotenv
# ---------- Supabase ----------
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_ANON_KEY=your-anon-or-publishable-key           # safe to ship to the frontend
SUPABASE_SERVICE_ROLE_KEY=your-service-role-or-secret-key # backend only, never exposed to the client
# Direct (non-pooled) connection — Alembic migrations ONLY. Production stores the session-pooler value
# here by default (put_ssm_params.sh): the direct host is IPv6-only and the EC2 host is IPv4-only.
DATABASE_URL=postgresql+psycopg://postgres:PASSWORD@db.your-project-ref.supabase.co:5432/postgres
# Session pooler — used by the running api/worker services
DATABASE_POOLER_URL=postgresql+psycopg://postgres.your-project-ref:PASSWORD@aws-0-us-east-1.pooler.supabase.com:5432/postgres

# ---------- Redis ----------
REDIS_URL=redis://localhost:6379

# ---------- Anthropic ----------
ANTHROPIC_API_KEY=sk-ant-your-key
ANTHROPIC_MODEL=claude-sonnet-5

# ---------- SEC EDGAR ----------
# MUST identify a real app + contact, per SEC fair-access policy, or requests get 403'd/blocked.
SEC_EDGAR_USER_AGENT="DCF-Valuation-App aman.todi01@gmail.com"

# ---------- FRED ----------
FRED_API_KEY=your-fred-api-key

# ---------- AWS ----------
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=your-access-key-id          # local dev only; the EC2 host uses its instance role instead
AWS_SECRET_ACCESS_KEY=your-secret-access-key  # local dev only
S3_BUCKET_NAME=dcf-app-artifacts   # production: set by Terraform (dcf-app-artifacts-<account-id>)

# ---------- App/runtime ----------
ENGINE_VERSION=v1
PROMPT_VERSION=v1
CACHE_TTL_DAYS=30
CACHE_EXPIRY_BACKSTOP_DAYS=35
BUILD_LOCK_TTL_SECONDS=120
LOG_LEVEL=INFO

# ---------- Frontend (Next.js) ----------
NEXT_PUBLIC_SUPABASE_URL=https://your-project-ref.supabase.co
NEXT_PUBLIC_SUPABASE_ANON_KEY=your-anon-or-publishable-key
# Production (Vercel): https://<api host> from `terraform output api_url`.
NEXT_PUBLIC_API_BASE_URL=http://localhost:8000

# ---------- API ----------
# Comma-separated list of allowed browser origins for CORS (exact origins, no wildcards).
# Production: the Vercel URL(s), from the Terraform variable cors_origins.
CORS_ORIGINS=http://localhost:3000

# ---------- Artifact storage ----------
# "s3" everywhere deployed (infra/deploy/docker-compose.prod.yml pins it). "local" = dev without AWS: model.xlsx/report.pdf
# are written under LOCAL_STORAGE_DIR and download links are short-lived HMAC-signed URLs served by the
# API itself at GET /api/files/{path}?exp=..&sig=.. (PUBLIC_API_BASE_URL = the API origin the browser uses).
STORAGE_BACKEND=s3
# LOCAL_STORAGE_DIR=.local-storage
# STORAGE_SIGNING_SECRET=change-me-for-local-links   # empty -> a fixed dev key (local backend only)
# PUBLIC_API_BASE_URL=http://localhost:8000
PRESIGNED_URL_TTL_SECONDS=3600

# ---------- Worker (SAQ) ----------
# SAQ_QUEUE_NAME=dcf
# WORKER_CONCURRENCY=4
# WORKER_SHUTDOWN_GRACE_SECONDS=60    # keep below the worker's docker compose stop_grace_period (90s)

# ---------- Local development ONLY (never set these in a deployed environment) ----------
# Used as the 10y risk-free rate when FRED_API_KEY is empty (decimal); runs get a data-confidence flag.
# RISK_FREE_RATE_OVERRIDE=0.042
# Offline demo: "fixtures" serves SEC EDGAR data from the bundled 20-ticker fixture set, fixed prices and
# a fixed 4.2% risk-free rate (no SEC/Yahoo/FRED calls); every run is flagged "DEMO DATA". Default: live.
# See docs/DEPLOYMENT.md "Offline demo mode". Set it for both the API and the worker.
# DATA_SOURCE_MODE=fixtures
# Accept "Authorization: Bearer dev-bypass-token" (what the frontend sends when Supabase isn't
# configured) as a fixed dev user, auto-inserted into auth.users. Only works on a local Postgres whose
# auth.users is the migration's stub table - never against a shared Supabase project.
DEV_AUTH_BYPASS=false
```

`backend/app/config.py` loads all of this via `pydantic-settings`'s `BaseSettings`, so a missing required var fails fast at startup rather than surfacing as a confusing runtime error mid-run.
## 13. Test suite

### 13.1 Backend (`backend/tests/`)

**Unit (`unit/`)** — no network, no DB, fast:
- `test_valuation_fcff.py`, `test_valuation_fcfe.py`, `test_valuation_excess_return.py`, `test_valuation_nav_reit.py`, `test_valuation_nav_ep.py`, `test_valuation_sotp.py`: hand-computed expected values against small synthetic `NormalizedFinancials`/`AssumptionsBase` fixtures (spreadsheet-verified by hand once, then pinned as regression tests). Cover edge cases: negative FCFF in year 1, terminal growth approaching WACC (near-singularity guard), zero debt, 100% payout, single-segment SOTP degenerating to plain FCFF.
- `test_bounds.py`: every rule in `assumptions/bounds.py` — both the "passes" and "violates" side of each check.
- `test_classify_rules.py`: the full decision tree in `classify/rules.py` against ~20 recorded `CompanySnapshot`/`NormalizedFinancials` fixtures spanning every model type and every decline reason (Apple → FCFF, JPM → excess_return, a P&C insurer → excess_return, O (Realty Income) → nav_reit, EOG → nav_ep, a diversified industrial like HON → sotp-eligible, a pre-revenue biotech → declined, a recent IPO with 2 years of data → declined `INSUFFICIENT_DATA`, a 20-F foreign filer → declined `NON_10K_FILER`).
- `test_windows.py`: the 5-vs-10-year decision, including both trigger paths (cyclical SIC, margin/revenue volatility) and the default.
- `test_cache_key.py`: same inputs → same key; any single input changed → different key.
- `test_edgar_normalize.py`: tag-mapping fallback order, TTM derivation across a non-December fiscal year end, restatement "latest filed wins" logic — all against recorded fixture JSON (§13.3), no live HTTP.

**Integration (`integration/`)** — real Postgres (a disposable Supabase branch or a local Postgres in CI, see below), real Redis (via `testcontainers`), mocked external HTTP:
- `test_run_lifecycle.py`: full state-machine walk for one `auto`-mode run end to end (classify → propose → confirm → build → complete), asserting DB rows and `run_events` at each step, using `respx` to mock EDGAR/yfinance/FRED/Anthropic responses.
- `test_one_active_run_lock.py`: asserts the second `POST /api/runs` for the same user while one is `building` returns `409`, and succeeds once the first reaches `awaiting_confirm`/`complete`.
- `test_cache_hit_and_fork.py`: two different users request the same ticker in `auto` mode → second one gets an instant cache hit with no new LLM/EDGAR calls; a third user edits an assumption → gets a private forked result, and the shared cache is unaffected.
- `test_single_flight_lock.py`: two concurrent uncached requests for the same `cache_key` → only one actually invokes the mocked build pipeline; the other polls and reuses its result.
- `test_cancel.py`: starts a build with an artificially slowed mock step, cancels mid-flight, asserts the job actually stops (via a monkeypatched sentinel the mock step increments — it must NOT increment past the cancel point), the run lands in `cancelled`, and no `_tmp/` S3 objects are promoted.
- `test_auth_jwks.py`: a token signed with a test ES256 key validates against a mocked JWKS endpoint; a token with an unknown `kid` triggers exactly one forced refresh, not a retry loop; an expired/garbage token → 401.
- `test_excel_recalc_verification.py`: **the one integration test that shells out to real `soffice`** (skip with a clear message if `soffice` isn't on the test runner's PATH — CI must install it, see the CI workflow below) — builds a real workbook from a fixture `ValuationResult`, runs `recalc_and_read`, and asserts the recalculated value matches the Python engine's number within tolerance for at least one case per model type.
- `test_pdf_builder.py`: builds a real PDF from a fixture and asserts it's non-empty, valid PDF magic bytes, and (via `pypdf` text extraction) contains the ticker and value-per-share figure somewhere in the text.

**Fixtures (`fixtures/`)**: recorded (anonymization not needed — SEC filings are public) `companyfacts`/`submissions` JSON for the ~20 tickers used in classifier tests, a snapshot of the parsed Damodaran tables, and a couple of hand-built `NormalizedFinancials` objects for pure-math edge cases that don't need to look like a real company.

### 13.2 Frontend (`frontend/tests/`)

- **Unit/component (Vitest + RTL)**: `assumptions-form.test.tsx` (renders read-only vs. editable correctly, edits propagate, client-side bound warnings show), `active-run-guard.test.tsx` (blocks the form when an active run exists, unblocks on `awaiting_confirm`), `model-confirm-card.test.tsx`.
- **E2E (Playwright)**: `run-flow.spec.ts` — sign in (against a test Supabase project or a mocked auth session), enter a ticker, land on the confirm screen (API mocked via Playwright route interception so this doesn't hit real EDGAR/Anthropic), confirm, watch the progress view, land on the result view, download links present. `single-run-lock.spec.ts` — attempting to start a second run while one is active is blocked in the UI.

### 13.3 CI (`.github/workflows/ci-backend.yml`, `ci-frontend.yml`)

Backend CI installs `libreoffice-calc` and the WeasyPrint system libs (same apt list as the worker Dockerfile) so the recalc-verification and PDF tests run for real, runs `ruff check`, `mypy`, then `pytest --cov`, spinning up Postgres and Redis as GitHub Actions **service containers** (not the real Supabase project — CI uses a throwaway local Postgres to avoid touching shared/prod data; `DATABASE_URL` in CI points at that service container, and Alembic runs against it before the integration tests). Frontend CI runs `npm run lint`, `npm run test` (Vitest), `npm run build`, and the Playwright suite against a `next start` instance with the API fully mocked.
## 14. Setup runbooks (manual, human-in-the-loop)

`docs/DEPLOYMENT.md` is the step-by-step operational runbook; this section lists what each external account must provide. Do these once, roughly in this order. Secrets go into SSM Parameter Store via `infra/scripts/put_ssm_params.sh` (never into Terraform, git, or the image).

### 14.1 Supabase

1. Create a project (free tier is enough), ideally in the same region as the EC2 host.
2. **Project Settings → Data API** → **Project URL** → `SUPABASE_URL` / `NEXT_PUBLIC_SUPABASE_URL`.
3. **Project Settings → API Keys** → **anon/publishable** key → `SUPABASE_ANON_KEY` / `NEXT_PUBLIC_SUPABASE_ANON_KEY`; **service_role/secret** key → `SUPABASE_SERVICE_ROLE_KEY` (backend only).
4. **Project Settings → JWT** → confirm **JWT Signing Keys** uses the **asymmetric** key type (migrate + rotate before any real users exist). The JWKS URL is derived from `SUPABASE_URL`.
5. **Project Settings → Database** → copy the **Session pooler** connection string (port 5432, `*.pooler.supabase.com`) → `DATABASE_POOLER_URL`. The direct host (`db.<ref>.supabase.co`) is IPv6-only while the EC2 host is IPv4-only, so production also runs migrations through the session pooler (`put_ssm_params.sh` stores the pooler URL as `DATABASE_URL` unless `--direct-migrations`). Use `postgresql+psycopg://` for both.
6. **Authentication → Providers**: enable email (magic link recommended). **Authentication → URL Configuration**: Site URL and redirect URLs = the Vercel URL.
7. Migrations run automatically on every deploy (`alembic upgrade head`); afterwards `select * from pg_policies where schemaname = 'public';` should show the §3 policies.

### 14.2 AWS (Terraform)

1. Tools: Terraform ≥ 1.10, AWS CLI v2 + Session Manager plugin, Docker with buildx (manual deploys only). An admin identity for the initial `terraform apply`.
2. `infra/terraform/bootstrap/`: `terraform init && terraform apply` → state bucket. Copy `backend.hcl.example` → `backend.hcl`.
3. `infra/terraform/`: copy `terraform.tfvars.example` → `terraform.tfvars` (region, `github_owner`/`github_repo`, `alert_email`, `acme_email`, `cors_origins` = the Vercel URL, optional `domain_name`, `instance_type`, `monthly_budget_usd`); `terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`. Commit `.terraform.lock.hcl`.
4. `infra/scripts/put_ssm_params.sh` with a filled-in `.env` → SecureString secrets + plain `SUPABASE_URL`, `SEC_EDGAR_USER_AGENT`, `ANTHROPIC_MODEL`.
5. Optional custom domain: A record → `elastic_ip` output, set `domain_name`, re-apply, redeploy.
6. Confirm the budget alert email subscription.

### 14.3 Anthropic

1. console.anthropic.com → API key → `ANTHROPIC_API_KEY`; set a monthly spend limit.
2. Confirm `ANTHROPIC_MODEL` (default `claude-sonnet-5`) is enabled and supports structured outputs.

### 14.4 FRED

1. Free account at fred.stlouisfed.org → API key → `FRED_API_KEY`. There is no keyless tier.

### 14.5 SEC EDGAR

No account or key. `SEC_EDGAR_USER_AGENT` must be a real app name + contact email (e.g. `"DCF-Valuation-App aman.todi01@gmail.com"`). The Redis-backed limiter keeps all processes under 10 req/s; don't bypass it when testing against the real endpoint.

### 14.6 Damodaran datasets

`infra/scripts/seed_damodaran_cache.py` pulls the beta-by-industry, margin, and implied-ERP files from `pages.stern.nyu.edu/~adamodar/pc/datasets/`, parses them, and uploads the parsed JSON to `s3://<bucket>/damodaran/`. Run once after the first deploy (e.g. inside the worker container via SSM Session Manager) and a few times a year. If the site blocks the script, download the files in a browser and use `--from-local-dir`. Until seeded, the app uses the bundled snapshot in `app/data/macro/damodaran_snapshot.json`.

### 14.7 Vercel

1. Import the GitHub repo; **Root Directory** = `frontend`; framework preset Next.js.
2. Environment variables (Production, and Preview if used): `NEXT_PUBLIC_API_BASE_URL` = `terraform output api_url`, `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`.
3. Deploys are automatic on push. Add the production Vercel URL to Terraform `cors_origins`, `terraform apply`, and redeploy the API.

### 14.8 GitHub

1. **Settings → Secrets and variables → Actions**: secret `AWS_DEPLOY_ROLE_ARN` (`terraform output github_deploy_role_arn`); variables `DEPLOY_ENABLED=true`, optional `AWS_REGION`, `ARM_RUNNER`.
2. CI (`ci-backend.yml`, `ci-frontend.yml`) needs no secrets; everything external is mocked.
3. `deploy.yml` runs on push to `main` per §11.4.
## 15. Ticket breakdown (15 tickets, designed for parallel execution)

**Orchestration model for the parent session:** run **Ticket 1 alone first** and wait for it to merge — everything else imports its schemas and reads its repo layout. Once it's done, launch the **10 tickets in Batch 1 as parallel sessions/subagents** (they only depend on Ticket 1 and on each other's *interfaces*, which are already fully specified in §§4–10 above, so they can be built against those contracts and fixture data without waiting on one another's actual code). Then run Batch 2, 3, 4 in order. Each ticket's own unit tests are part of its deliverable, not deferred to Ticket 14 — Ticket 14 is integration tests, the 20-ticker fixture set, and CI hardening across everything.

Every ticket should open its own PR/branch against `main`, include tests, and not merge until its tests pass in CI.

---

### Batch 0 — solo, do first

**Ticket 1 — Repo foundation, domain schemas, tooling**
*Depends on: nothing.*
- Create the full repo layout from §2 (empty modules with docstrings/TODOs are fine outside the schemas).
- Implement every Pydantic model in §4 verbatim (`schemas/company.py`, `financials.py`, `assumptions.py`, `valuation_result.py`), plus the `runs` API request/response models.
- `pyproject.toml` (backend) and `package.json` (frontend) with the exact pinned versions from §1.
- `.env.example` from §12.
- `docker-compose.yml` and both backend Dockerfiles from §11 (worker image can be a stub `CMD` until Ticket 11 exists — the point of this ticket is that `docker compose build` succeeds).
- CI workflow skeletons (`ci-backend.yml`, `ci-frontend.yml`) that at minimum run linting and `pytest`/`vitest` against whatever exists — they'll gain real content as later tickets land.
- Unit tests: schema validation round-trips (every schema serializes/deserializes; `AssumptionField`/`*Assumptions` classes reject `None`; JSON Schema generation for each `*Assumptions` class has ≤24 optional fields and ≤16 union-typed fields — write an actual test that introspects `model_json_schema()` and asserts this, since it's a hard external constraint from §4.3).
- **Deliverable other tickets need:** a merged `main` with `backend/app/schemas/*` importable and `docker compose up` at least starting empty containers.

---

### Batch 1 — parallel, each depends only on Ticket 1

**Ticket 2 — Database, migrations, auth**
- Alembic setup; migration implementing the exact DDL in §3, including the `runs_one_active_per_user` partial unique index and all RLS policies.
- `backend/app/db/models.py` SQLAlchemy models mirroring the DDL.
- `backend/app/db/base.py` async engine/session per §9.2 (psycopg3, session pooler).
- `backend/app/auth/jwks.py` + `middleware.py`/`deps.py` per §9.3 (JWKS fetch/cache, ES256 verify, `kid`-miss refresh-once behavior, `current_user` dependency).
- Tests: migration applies cleanly against a throwaway Postgres (CI service container); the unique index actually rejects a second active row via a raw `INSERT`; JWKS verification unit tests with a locally-generated ES256 test keypair (mock the JWKS HTTP call), covering valid token, expired token, wrong audience, unknown `kid` (triggers exactly one refresh).

**Ticket 3 — EDGAR client & normalization**
- `data/edgar/client.py`: rate-limited (Redis token bucket) `httpx` client per §5.1, `get_submissions`, `get_companyfacts`, ticker→CIK map, S3 raw-JSON caching + `edgar_filing_cache` indexing.
- `data/edgar/normalize.py`: the tag-mapping table and fallback logic, TTM derivation, restatement handling, confidence-flag emission — build and validate against **at least 15 of the 20 fixture tickers** named in §13.1 (the remaining 5 can be added by Ticket 14).
- `data/edgar/segments.py`: segment-level extraction (this is the hardest single piece of parsing in the app — budget real time here).
- Tests: `respx`-mocked HTTP tests for the client (rate limiting, retries, caching); normalization tests against recorded fixture JSON for each of the 15+ tickers, asserting specific known figures (e.g. Apple's FY2025 revenue) match within a tight tolerance.
- **Produces the fixture JSON files** other tickets (5, 6, 14) will reuse — commit them to `backend/tests/fixtures/edgar/`.

**Ticket 4 — Market data & macro data**
- `data/market/base.py` (`MarketDataProvider` ABC) + `yfinance_provider.py` per §5.3 — no caching, no fallback, per the explicit decision; a typed `MarketDataUnavailable` exception on failure.
- `data/macro/fred.py`: DGS10 fetch, `"."` → error mapping (never `0`).
- `data/macro/damodaran.py`: `.xls` fetch/parse for beta-by-industry + implied ERP, S3-cached parsed output, the SIC→Damodaran-industry lookup table with unit tests pinning ~10 known tickers to known industries.
- `infra/scripts/seed_damodaran_cache.py` (the one-off seeding script from §14.6, with the `--from-local-dir` manual fallback).
- Tests: `respx`-mocked yfinance/FRED HTTP; a Damodaran parser test against a small recorded sample `.xls`.

**Ticket 5 — Classifier**
- `classify/rules.py`: the full decision tree from §5.5 (hard declines, then model selection, in the specified order), as pure functions over `NormalizedFinancials`/`CompanySnapshot`.
- `classify/llm_tiebreak.py`: the small tiebreak schema + `messages.parse()` call, only invoked on genuine ambiguity.
- `classify/windows.py`: the 5-vs-10-year decision with named, tunable threshold constants.
- Tests: `test_classify_rules.py` and `test_windows.py` from §13.1 against the fixture set (reuse Ticket 3's fixtures once available; stub minimal fixtures in the meantime and reconcile in Ticket 14 if timing doesn't line up).

**Ticket 6 — Valuation engine: FCFF & FCFE**
- `valuation/base.py` (`Valuator` ABC, `ValueBridge`) and `valuation/fcff.py` (FCFF, the FCFE variant, and the early-stage-tech survival-probability variant) exactly per §6.1–6.2.
- `compute_scenarios` and `compute_sensitivity_grid` default implementations.
- Tests: hand-computed expected values for at least 4 scenarios (stable mature co., high-growth co., early-stage-tech with survival probability <1, near-zero WACC-minus-terminal-growth edge case) — verify by an independent spreadsheet calculation once, then pin as a regression test.

**Ticket 7 — Valuation engine: Excess Return, NAV, SOTP**
*Soft-depends on Ticket 6's `FCFFValuator` interface for SOTP, but that interface is fully specified in §6.1/6.2 above — code against the spec, do a 30-minute integration check with Ticket 6's actual branch once both exist, don't block on it.*
- `valuation/excess_return.py`, `nav_reit.py`, `nav_ep.py`, `sotp.py` exactly per §6.3–6.6, plus the `VALUATOR_REGISTRY` in §6.7.
- Tests: hand-computed expected values per model (a bank excess-return case, a REIT NAV case, an E&P NAV case, a 2-segment SOTP case that also emits the consolidated-FCFF comparison flag).

**Ticket 8 — Assumption proposer**
- `assumptions/proposer.py`: the propose→validate→repair loop per §5.6, using `client.messages.parse()` with `output_format=<the appropriate *Assumptions class>`.
- `assumptions/bounds.py`: every check listed in §5.6, as small composable functions returning a list of violation strings.
- Prompt construction: feed historical medians (from `NormalizedFinancials`) and Damodaran industry benchmarks as anchors in the prompt so proposals are usually close on the first attempt.
- Tests: mock the Anthropic client; test the repair loop actually re-prompts on a violation and gives up with a typed exception after `MAX_REPAIR_ATTEMPTS`; test every `bounds.py` rule in isolation (can share fixtures with Ticket 6/7's numeric edge cases).

**Ticket 9 — Excel export**
- `export/excel/builder.py` and every sheet module per §7.1, using named ranges so edited cells actually flow through formulas.
- `export/excel/recalc_verify.py`: the LibreOffice headless recalculation + read-back helper (async subprocess, not blocking — see §8.3's note that this must support cancellation).
- Build against **fixture `ValuationResult`/`NormalizedFinancials`/`AssumptionsBase` objects** (don't wait on Tickets 6–8 to be merged) — one fixture per model type, hand-crafted if needed.
- Tests: `test_excel_recalc_verification.py` from §13.1 (requires `libreoffice-calc` in the test environment — install it in this ticket's dev/CI setup) for at least one fixture per model type; assert named ranges exist and are referenced (not hard-coded cell addresses) in the projection/valuation formulas.

**Ticket 10 — PDF export**
- `export/pdf/builder.py`, `templates/report.html.jinja`, `charts.py` (matplotlib waterfall, heatmap, scenario bar) per §7.2.
- The small `ReportNarrative` schema + its own simple `messages.parse()` call (separate from the assumption proposer).
- Build against the same fixture `ValuationResult` objects as Ticket 9.
- Tests: builds a real PDF from each fixture, asserts non-empty/valid PDF and that key figures (ticker, value per share) appear in extracted text (`pypdf`).

**Ticket 13 — Frontend application**
*Can build entirely against a mocked API (MSW or a small mock Express/JSON server implementing the contract in §9.1) — does not need the real backend to exist yet.*
- All pages/components from §10: ticker entry + `ActiveRunGuard`, `/runs/[id]` with its four rendered states, `ModelConfirmCard`, schema-driven `AssumptionsForm`, `ProgressView` with SSE + Cancel, `ResultView` with `SensitivityChart` and download links.
- Supabase auth wiring (`lib/supabase-client.ts`) for sign-in only.
- Tests: the Vitest/RTL component tests and the Playwright `run-flow.spec.ts`/`single-run-lock.spec.ts` from §13.2, all against the mock API.
- **Deliverable other tickets need:** nothing blocks on this except final integration (Ticket 15).

---

### Batch 2 — integration, after the relevant Batch 1 tickets land

**Ticket 11 — Job orchestration**
*Depends on: 2, 3, 4, 5, 6, 7, 8, 9, 10 (this is where every module gets wired into one pipeline).*
- `jobs/worker_settings.py` (SAQ `WorkerSettings`), `jobs/classify_job.py` (`classify_and_propose`), `jobs/build_job.py` (`build_model`), `jobs/cancel.py` (the cancellable-task wrapper from §8.3).
- `runs/state_machine.py`, `runs/lock.py` (single-flight build lock from §8.4), `runs/cache_key.py` (§8.5).
- Wires: classifier → proposer → cache check → engine → exporters → S3 upload → DB update, with `run_events` rows emitted at each stage transition for the SSE stream to pick up.
- Tests: `test_run_lifecycle.py`, `test_cache_hit_and_fork.py`, `test_single_flight_lock.py`, `test_cancel.py` from §13.1 (integration-level, mocked external HTTP, real Postgres/Redis via `testcontainers`).

---

### Batch 3

**Ticket 12 — FastAPI routes, SSE, wiring**
*Depends on: 2, 11.*
- All routes in §9.1, request validation on `POST /confirm` re-running `bounds.py` server-side on any client-submitted assumption values, the `409`-on-active-run handling, the SSE generator from §8.6, presigned S3 URL generation for `GET /result`.
- `app/main.py` app factory wiring auth middleware, routers, CORS (allow the frontend's origin only).
- Tests: `test_one_active_run_lock.py`, `test_auth_jwks.py` (route-level, using Ticket 2's unit-level JWKS tests as a base) from §13.1; an end-to-end route test that walks a full run through the real (test) DB/Redis with everything else mocked.

---

### Batch 4 — final integration & deployment

**Ticket 14 — Test hardening & CI**
*Depends on: everything (runs last, or continuously alongside others as a living ticket that starts early collecting fixtures and finishes after Batch 3).*
- Fills out the full 20-ticker fixture set referenced throughout §13.1 (recorded EDGAR JSON for every named example ticker across every model type and decline reason).
- Raises backend test coverage on `valuation/`, `classify/`, and `assumptions/bounds.py` specifically (these are the modules where a silent bug is most costly) to a high bar (e.g. >90%) — add missing edge cases found along the way.
- Finalizes `ci-backend.yml`/`ci-frontend.yml` with real coverage gates, the `libreoffice-calc` + WeasyPrint system-dependency install step, and Playwright browser install step.
- Runs the full test suite against a disposable Supabase branch (if using Supabase's branching feature) or documents why CI stays on a local Postgres container instead.

**Ticket 15 — Deployment**
*Depends on: 1, 11, 12, 13.*
- Terraform under `infra/terraform/` per §11.3 (bootstrap state bucket, EC2 host + Elastic IP + security group, S3, ECR, IAM instance role, GitHub OIDC deploy role, SSM config parameters, budget, status-check alarms), with `terraform fmt`/`validate` clean.
- `infra/deploy/docker-compose.prod.yml` + `Caddyfile` per §11.2; arm64 Dockerfiles per §11.1.
- `infra/scripts/put_ssm_params.sh`, `deploy.sh`, `deploy_remote.sh` and `.github/workflows/deploy.yml` per §11.4 (gated on `DEPLOY_ENABLED`).
- `docs/DEPLOYMENT.md`: the full runbook (§14) split into owner-manual vs. scripted steps, operations (SSM shell, logs, restart, rollback, resize, teardown), cost table, and local-dev/offline-demo instructions.
- Runs the §14 runbooks end to end against real values and confirms a real ticker produces a real Excel + PDF through the deployed system — **the steps requiring the account owner (AWS, Supabase, Anthropic, FRED, Vercel, GitHub settings) are flagged explicitly.**
