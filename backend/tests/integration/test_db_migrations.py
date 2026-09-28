"""Migration 0001_init against a real Postgres (Ticket 2)."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.pool import NullPool

from alembic import command
from app.db.models import ALL_TABLES, ONE_ACTIVE_RUN_INDEX, CachedModel, Run, RunEvent
from app.schemas.run import RunMode, RunStatus
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


def test_upgrade_is_idempotent_at_head(pg_url: str) -> None:
    command.upgrade(alembic_config(pg_url), "head")  # no-op, must not raise


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


def test_auth_shims_read_jwt_claims(pg_url: str) -> None:
    uid = uuid.uuid4()
    engine = _sync_engine(pg_url)
    with engine.begin() as conn:
        conn.execute(text("SELECT set_config('request.jwt.claim.sub', :s, true)"), {"s": str(uid)})
        conn.execute(text("SELECT set_config('request.jwt.claim.role', 'authenticated', true)"))
        assert conn.execute(text("SELECT auth.uid()")).scalar() == uid
        assert conn.execute(text("SELECT auth.role()")).scalar() == "authenticated"
    engine.dispose()


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


async def test_orm_roundtrip_and_updated_at_trigger(db_session: AsyncSession, make_user) -> None:
    uid = await make_user()
    run = Run(user_id=uid, ticker="MSFT")
    db_session.add(run)
    await db_session.commit()
    assert run.status is RunStatus.CLASSIFYING
    assert run.mode is RunMode.AUTO
    assert run.progress_pct == 0

    created_updated = run.updated_at
    await db_session.execute(text("SELECT pg_sleep(0.01)"))
    run.status = RunStatus.AWAITING_CONFIRM
    run.model_reasons = ["positive FCF"]
    run.model_confidence = Decimal("0.875")
    await db_session.commit()
    await db_session.refresh(run)
    assert run.updated_at > created_updated

    db_session.add(RunEvent(run_id=run.id, stage="classifying", message="done", progress_pct=10))
    db_session.add(
        CachedModel(
            cache_key="k1",
            ticker="MSFT",
            model_type="fcff",
            accession_number="0000789019-26-000001",
            engine_version="v1",
            prompt_version="v1",
            s3_prefix="models/MSFT/fcff/x/",
            valuation_result={"value_per_share": 1.0},
            price_as_of=datetime.now(UTC),
            price_used=Decimal("412.3456"),
            expires_at=datetime.now(UTC) + timedelta(days=35),
        )
    )
    await db_session.commit()
    events = (await db_session.execute(select(RunEvent).where(RunEvent.run_id == run.id))).scalars().all()
    assert [e.message for e in events] == ["done"]
    assert events[0].id >= 1

    # cascade: deleting the auth user removes runs and events
    await db_session.execute(text("DELETE FROM auth.users WHERE id = :id"), {"id": uid})
    await db_session.commit()
    assert (await db_session.execute(text("SELECT count(*) FROM run_events"))).scalar() == 0


async def test_db_session_truncates_between_tests(db_session: AsyncSession) -> None:
    for table in ALL_TABLES:
        assert (await db_session.execute(text(f"SELECT count(*) FROM {table}"))).scalar() == 0


def test_redis_fixture(redis_url: str) -> None:
    import redis

    r = redis.Redis.from_url(redis_url)
    try:
        assert r.ping() is True
    finally:
        r.close()
