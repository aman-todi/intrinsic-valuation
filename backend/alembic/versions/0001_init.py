"""Initial schema: runs, run_events, caches, the one-active-run lock, RLS (spec §3).

Revision ID: 0001_init
Revises:
Create Date: 2026-09-28

On Supabase the ``auth`` schema, ``auth.users``, ``auth.uid()`` and ``auth.role()`` already exist.
Plain Postgres (CI / local dev) has none of them, so the upgrade first creates minimal compat shims
**only if missing** and never replaces the Supabase originals. The downgrade drops only the objects
this migration owns; the shims (and pgcrypto) are intentionally left in place because on Supabase
they are not ours.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001_init"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


AUTH_SHIMS = r"""
DO $shim$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'auth') THEN
    EXECUTE 'CREATE SCHEMA auth';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'auth' AND c.relname = 'users' AND c.relkind IN ('r', 'p')
  ) THEN
    EXECUTE 'CREATE TABLE auth.users (id uuid PRIMARY KEY)';
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'auth' AND p.proname = 'uid'
  ) THEN
    EXECUTE $fn$
      CREATE FUNCTION auth.uid() RETURNS uuid LANGUAGE sql STABLE AS $body$
        SELECT coalesce(
          nullif(current_setting('request.jwt.claim.sub', true), ''),
          (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'sub')
        )::uuid
      $body$
    $fn$;
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'auth' AND p.proname = 'role'
  ) THEN
    EXECUTE $fn$
      CREATE FUNCTION auth.role() RETURNS text LANGUAGE sql STABLE AS $body$
        SELECT coalesce(
          nullif(current_setting('request.jwt.claim.role', true), ''),
          (nullif(current_setting('request.jwt.claims', true), '')::jsonb ->> 'role')
        )::text
      $body$
    $fn$;
  END IF;
END
$shim$;
"""

SCHEMA = r"""
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

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
  user_id              uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
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

-- ============================================================
-- RLS (defense in depth; the backend uses the service role)
-- ============================================================
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE cached_models ENABLE ROW LEVEL SECURITY;
ALTER TABLE cached_proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE edgar_filing_cache ENABLE ROW LEVEL SECURITY;

CREATE POLICY runs_owner_select ON runs FOR SELECT USING (auth.uid() = user_id);
CREATE POLICY runs_owner_all ON runs FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
CREATE POLICY run_events_owner_select ON run_events FOR SELECT USING (
  EXISTS (SELECT 1 FROM runs r WHERE r.id = run_events.run_id AND r.user_id = auth.uid())
);
CREATE POLICY cached_models_read ON cached_models FOR SELECT USING (auth.role() = 'authenticated');
CREATE POLICY cached_proposals_read ON cached_proposals FOR SELECT USING (auth.role() = 'authenticated');
"""

DROP_SCHEMA = r"""
DROP TABLE IF EXISTS edgar_filing_cache;
DROP TABLE IF EXISTS cached_proposals;
DROP TABLE IF EXISTS cached_models;
DROP TABLE IF EXISTS run_events;
DROP TABLE IF EXISTS runs;
DROP FUNCTION IF EXISTS public.runs_set_updated_at();
DROP TYPE IF EXISTS run_mode;
DROP TYPE IF EXISTS run_status;
"""


def upgrade() -> None:
    op.execute(AUTH_SHIMS)
    op.execute(SCHEMA)


def downgrade() -> None:
    op.execute(DROP_SCHEMA)
