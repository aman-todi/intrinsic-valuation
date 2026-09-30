"""Migration 0001_init against a real Postgres (Ticket 2)."""

import uuid

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
    finally:
        command.upgrade(cfg, "head")
        engine.dispose()

    with engine.connect() as conn:
        assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar() == "0002_run_results"
    engine.dispose()


async def test_deleting_user_cascades_to_runs_and_events(db_session: AsyncSession, make_user) -> None:
    uid = await make_user()
    other = await make_user()
    run_id = (
        await db_session.execute(
            text("INSERT INTO runs (user_id, ticker) VALUES (:uid, 'AAPL') RETURNING id"), {"uid": uid}
        )
    ).scalar_one()
    await db_session.execute(
        text("INSERT INTO run_events (run_id, stage, message) VALUES (:r, 'classifying', 'x')"), {"r": run_id}
    )
    await db_session.execute(INSERT_RUN, {"uid": other, "status": "complete"})
    await db_session.commit()

    # runs.user_id must reference an existing user
    with pytest.raises(IntegrityError) as ei:
        await db_session.execute(INSERT_RUN, {"uid": uuid.uuid4(), "status": "complete"})
    await db_session.rollback()
    assert isinstance(ei.value.orig, psycopg.errors.ForeignKeyViolation)

    await db_session.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})
    await db_session.commit()
    assert (await db_session.execute(text("SELECT count(*) FROM runs"))).scalar() == 1
    assert (await db_session.execute(text("SELECT count(*) FROM run_events"))).scalar() == 0
    # plain Postgres: no auth schema, no RLS anywhere
    assert (await db_session.execute(text("SELECT to_regnamespace('auth')"))).scalar() is None
    rls = (
        await db_session.execute(
            text(
                "SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace AND relrowsecurity"
            )
        )
    ).scalar()
    assert rls == 0


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
