"""SQLAlchemy async engine/session (spec §9.2).

The running app (api + worker) connects through ``DATABASE_POOLER_URL`` (Supabase Session Pooler)
with psycopg3 in async mode. Nothing connects at import time: the engine and sessionmaker are built
lazily on first use.
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
        "pool_size": 10,
        "max_overflow": 20,
        "pool_recycle": 300,
        "pool_pre_ping": True,
    }
    opts.update(kwargs)
    return create_async_engine(to_psycopg_url(url), **opts)


def get_engine() -> AsyncEngine:
    """Process-wide engine on ``settings.DATABASE_POOLER_URL``, created on first call."""
    global _engine
    if _engine is None:
        _engine = make_engine(settings.DATABASE_POOLER_URL)
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
