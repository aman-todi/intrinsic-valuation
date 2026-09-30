"""SQLAlchemy async engine/session (spec §9.2).

The running app (api + worker) and Alembic share one ``DATABASE_URL`` (plain Postgres; AWS RDS in
production with ``?sslmode=require``), used with psycopg3 in async mode. Nothing connects at import
time: the engine and sessionmaker are built lazily on first use.

Pool sizing: production is a single RDS db.t4g.micro (max_connections ~80) shared by 2 uvicorn
workers + 1 SAQ worker process, each with its own engine. ``pool_size=5, max_overflow=5`` caps that
at 3 x 10 = 30 connections, leaving ample headroom for Alembic, psql and RDS's reserved slots.
"""

from collections.abc import AsyncIterator

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def to_psycopg_url(url: str) -> str:
    """Pin the ``postgresql+psycopg://`` dialect for bare ``postgres(ql)://`` URLs."""
    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return "postgresql+psycopg://" + url[len(prefix) :]
    return url


def make_engine(url: str, **kwargs: object) -> AsyncEngine:
    """Build an async engine with the §9.2 pool settings (overridable via kwargs)."""
    opts: dict[str, object] = {
        "pool_size": 5,
        "max_overflow": 5,
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }
    opts.update(kwargs)
    return create_async_engine(to_psycopg_url(url), **opts)


def get_engine() -> AsyncEngine:
    """Process-wide engine on ``settings.DATABASE_URL``, created on first call."""
    global _engine
    if _engine is None:
        _engine = make_engine(settings.DATABASE_URL)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    global _sessionmaker
    if _sessionmaker is None:
        _sessionmaker = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _sessionmaker


async def dispose_engine() -> None:
    """Close pooled connections (call on app/worker shutdown). Safe to call when never created."""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one ``AsyncSession`` per request. Handlers commit explicitly;
    uncommitted work is rolled back when the session closes."""
    async with get_sessionmaker()() as session:
        yield session
