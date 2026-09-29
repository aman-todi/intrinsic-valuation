"""Migration 0001_init against a real Postgres (Ticket 2)."""

import psycopg
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from alembic import command
from app.db.models import ALL_TABLES, ONE_ACTIVE_RUN_INDEX
from tests.integration.conftest import alembic_config

INSERT_RUN = text(
    "INSERT INTO runs (user_id, ticker, status) VALUES (:uid, 'AAPL', CAST(:status AS run_status))"
)


def _sync_engine(pg_url: str):
    return create_engine(pg_url, poolclass=NullPool)


def test_downgrade_then_upgrade_roundtrip(pg_url: str) -> None:
    cfg = alembic_config(pg_url)
    engine = _sync_engine(pg_url)
    try:
        command.downgrade(cfg, "base")
        with engine.connect() as conn:
            remaining = conn.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename = ANY(:t)"),
                {"t": list(ALL_TABLES)},
            ).all()
            assert remaining == []
            types = conn.execute(
                text("SELECT typname FROM pg_type WHERE typname IN ('run_status', 'run_mode')")
            ).all()
            assert types == []
            # compat shims are left alone by the downgrade
            assert conn.execute(text("SELECT to_regclass('auth.users')")).scalar() is not None
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002_run_results"
    engine.dispose()


def test_rls_enabled_on_all_tables(pg_url: str) -> None:
    engine = _sync_engine(pg_url)
    with engine.connect() as conn:
        rows = dict(
            conn.execute(
                text(
                    "SELECT relname, relrowsecurity FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace AND relname = ANY(:t)"
                ),
                {"t": list(ALL_TABLES)},
            ).all()
        )
        policies = {
            r[0]
            for r in conn.execute(
                text("SELECT policyname FROM pg_policies WHERE schemaname = 'public'")
            ).all()
        }
    engine.dispose()
    assert rows == dict.fromkeys(ALL_TABLES, True)
    assert policies == {
        "runs_owner_select",
        "runs_owner_all",
        "run_events_owner_select",
        "cached_models_read",
        "cached_proposals_read",
    }


async def test_one_active_run_per_user_raw_insert(db_session: AsyncSession, make_user) -> None:
    uid = await make_user()
    await db_session.execute(INSERT_RUN, {"uid": uid, "status": "classifying"})
    await db_session.commit()

    # awaiting_confirm is not "active": allowed alongside the active one
    await db_session.execute(INSERT_RUN, {"uid": uid, "status": "awaiting_confirm"})
    await db_session.commit()

    with pytest.raises(IntegrityError) as ei:
        await db_session.execute(INSERT_RUN, {"uid": uid, "status": "building"})
    await db_session.rollback()
    orig = ei.value.orig
    assert isinstance(orig, psycopg.errors.UniqueViolation)
    assert orig.diag.constraint_name == ONE_ACTIVE_RUN_INDEX

    # a different user is unaffected; terminal runs never conflict
    other = await make_user()
    await db_session.execute(INSERT_RUN, {"uid": other, "status": "proposing"})
    await db_session.execute(INSERT_RUN, {"uid": uid, "status": "complete"})
    await db_session.execute(INSERT_RUN, {"uid": uid, "status": "failed"})
    await db_session.commit()
    count = (await db_session.execute(text("SELECT count(*) FROM runs"))).scalar()
    assert count == 5
