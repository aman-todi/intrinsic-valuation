"""Initial schema: users, runs, run_events, caches, the one-active-run lock (spec §3).

Revision ID: 0001_init
Revises:
Create Date: 2026-09-28

Plain Postgres 16 (AWS RDS in production; a local/CI Postgres otherwise) — nothing provider-specific.

- ``users`` mirrors the Cognito users that have called the API: ``id`` is the Cognito ``sub``. Rows
  are upserted by the API's auth dependency (``app.deps``) on first/periodic use; there is no other
  writer. ``runs.user_id`` references it ``ON DELETE CASCADE``.
- No row-level security: the FastAPI app (api + worker) is the ONLY database client. It connects
  with a single application role and enforces ownership itself (every run query is scoped to the
  authenticated user id). The database is never exposed to browsers or third parties.

The downgrade drops everything this migration created except the ``pgcrypto`` extension.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SCHEMA = r"""
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================
-- users (id = Cognito sub)
-- ============================================================
CREATE TABLE users (
  id                   uuid PRIMARY KEY,
  email                text,
  created_at           timestamptz NOT NULL DEFAULT now(),
  last_seen_at         timestamptz
);

-- ============================================================
-- runs
-- ============================================================
CREATE TYPE run_status AS ENUM (
  'classifying',
  'proposing',
  'awaiting_confirm',
  'building',
  'complete',
  'failed',
  'cancelled'
);

CREATE TYPE run_mode AS ENUM ('auto', 'custom');

CREATE TABLE runs (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id              uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  ticker               text NOT NULL,
  mode                 run_mode NOT NULL DEFAULT 'auto',
  status               run_status NOT NULL DEFAULT 'classifying',

  cik                  text,
  company_name         text,
  sic_code             text,
  model_type           text,
  model_confidence     numeric(4,3),
  model_reasons        jsonb,
  runner_up_model      text,
  decline_reason       text,
  historical_window_years int,
  window_reason        text,

  proposed_assumptions jsonb,
  final_assumptions    jsonb,
  assumptions_edited   boolean NOT NULL DEFAULT false,

  accession_number     text,
  engine_version       text,
  prompt_version       text,
  cache_key            text,

  cancel_requested     boolean NOT NULL DEFAULT false,
  error_message        text,
  current_stage        text,
  progress_pct         int NOT NULL DEFAULT 0,

  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now(),
  started_at           timestamptz,
  finished_at          timestamptz
);

CREATE INDEX runs_user_id_idx ON runs (user_id, created_at DESC);
CREATE INDEX runs_cache_key_idx ON runs (cache_key) WHERE cache_key IS NOT NULL;

-- THE LOCK: one worker-owned ("active") run per user. awaiting_confirm is NOT active.
CREATE UNIQUE INDEX runs_one_active_per_user
  ON runs (user_id)
  WHERE status IN ('classifying', 'proposing', 'building');

CREATE FUNCTION public.runs_set_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  RETURN NEW;
END
$$;

CREATE TRIGGER runs_set_updated_at
  BEFORE UPDATE ON runs
  FOR EACH ROW EXECUTE FUNCTION public.runs_set_updated_at();

-- ============================================================
-- run_events
-- ============================================================
CREATE TABLE run_events (
  id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id       uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  ts           timestamptz NOT NULL DEFAULT now(),
  stage        text NOT NULL,
  message      text NOT NULL,
  progress_pct int
);
CREATE INDEX run_events_run_id_idx ON run_events (run_id, id);

-- ============================================================
-- cached_models
-- ============================================================
CREATE TABLE cached_models (
  cache_key         text PRIMARY KEY,
  ticker            text NOT NULL,
  model_type        text NOT NULL,
  accession_number  text NOT NULL,
  engine_version    text NOT NULL,
  prompt_version    text NOT NULL,
  s3_prefix         text NOT NULL,
  valuation_result  jsonb NOT NULL,
  price_as_of       timestamptz NOT NULL,
  price_used        numeric(18,4) NOT NULL,
  computed_at       timestamptz NOT NULL DEFAULT now(),
  expires_at        timestamptz NOT NULL
);
CREATE INDEX cached_models_ticker_idx ON cached_models (ticker, model_type);
CREATE INDEX cached_models_expires_idx ON cached_models (expires_at);

-- ============================================================
-- cached_proposals
-- ============================================================
CREATE TABLE cached_proposals (
  cache_key         text PRIMARY KEY,
  ticker            text NOT NULL,
  model_type        text NOT NULL,
  accession_number  text NOT NULL,
  prompt_version    text NOT NULL,
  assumptions       jsonb NOT NULL,
  computed_at       timestamptz NOT NULL DEFAULT now()
);

-- ============================================================
-- edgar_filing_cache
-- ============================================================
CREATE TABLE edgar_filing_cache (
  cik               text PRIMARY KEY,
  company_name      text,
  latest_accession  text,
  s3_key            text NOT NULL,
  fetched_at        timestamptz NOT NULL DEFAULT now()
);
"""

DROP_SCHEMA = r"""
DROP TABLE IF EXISTS edgar_filing_cache;
DROP TABLE IF EXISTS cached_proposals;
DROP TABLE IF EXISTS cached_models;
DROP TABLE IF EXISTS run_events;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS users;
DROP FUNCTION IF EXISTS public.runs_set_updated_at();
DROP TYPE IF EXISTS run_mode;
DROP TYPE IF EXISTS run_status;
"""


def upgrade() -> None:
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute(DROP_SCHEMA)
