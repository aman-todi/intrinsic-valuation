"""Integration-test infrastructure: real Postgres + Redis (shared by all tickets).

Fixtures
--------
``pg_url`` (session)
    ``postgresql+psycopg://`` URL of a database already migrated to ``head``. Uses
    ``TEST_DATABASE_URL`` if set; otherwise spins up a throwaway Postgres 16 cluster (when
    ``/usr/lib/postgresql/16/bin/initdb`` exists) on a free port; otherwise skips.
``redis_url`` (session)
    Uses ``TEST_REDIS_URL`` if set; otherwise starts ``redis-server`` on a free port; otherwise skips.
``db_engine`` (function)
    An ``AsyncEngine`` on ``pg_url`` (NullPool, so it is safe across per-test event loops).
``db_session`` (function)
    An ``AsyncSession``; all app tables (including ``users``) are truncated before each test.
``make_user`` (function)
    Async factory inserting a row into ``users`` (FK target of ``runs.user_id``) and
    returning its UUID; created users are deleted after the test.

Every test under ``tests/integration/`` is automatically marked ``integration``.
"""

import contextlib
import os
import shutil
import socket
import subprocess
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from app.db.base import to_psycopg_url
from app.db.models import ALL_TABLES
from app.deps import reset_user_touch_cache

BACKEND_DIR = Path(__file__).resolve().parents[2]
PG_BIN = Path("/usr/lib/postgresql/16/bin")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    here = Path(__file__).parent
    for item in items:
        if here in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.integration)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def alembic_config(url: str) -> Config:
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", to_psycopg_url(url).replace("%", "%%"))
    cfg.attributes["configure_logger"] = False
    return cfg


def _run_as_pg(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    # initdb/postgres refuse to run as root; drop to the `postgres` OS user when we are root.
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", "postgres", "--", *cmd]
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


@contextlib.contextmanager
def _throwaway_postgres() -> Iterator[str]:
    tmp = Path(tempfile.mkdtemp(prefix="dcf-pg-"))
    if os.geteuid() == 0:
        shutil.chown(tmp, "postgres", "postgres")
    datadir, port = tmp / "data", free_port()
    started = False
    try:
        _run_as_pg(
            [str(PG_BIN / "initdb"), "-D", str(datadir), "-U", "postgres", "-A", "trust", "-E", "UTF8"]
        )
        _run_as_pg(
            [
                str(PG_BIN / "pg_ctl"),
                "-D",
                str(datadir),
                "-l",
                str(tmp / "pg.log"),
                "-w",
                "-o",
                f"-p {port} -k {tmp} -c listen_addresses=127.0.0.1 -c fsync=off",
                "start",
            ]
        )
        started = True
        yield f"postgresql+psycopg://postgres@127.0.0.1:{port}/postgres"
    finally:
        if started:
            with contextlib.suppress(subprocess.CalledProcessError):
                _run_as_pg([str(PG_BIN / "pg_ctl"), "-D", str(datadir), "-m", "immediate", "-w", "stop"])
        shutil.rmtree(tmp, ignore_errors=True)


@pytest.fixture(scope="session")
def pg_url() -> Iterator[str]:
    env_url = os.environ.get("TEST_DATABASE_URL")
    if env_url:
        url = to_psycopg_url(env_url)
        command.upgrade(alembic_config(url), "head")
        yield url
        return
    if not (PG_BIN / "initdb").exists():
        pytest.skip("no TEST_DATABASE_URL and no local Postgres 16 binaries")
    with _throwaway_postgres() as url:
        command.upgrade(alembic_config(url), "head")
        yield url


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    env_url = os.environ.get("TEST_REDIS_URL")
    if env_url:
        yield env_url
        return
    exe = shutil.which("redis-server")
    if exe is None:
        pytest.skip("no TEST_REDIS_URL and no redis-server on PATH")
    port = free_port()
    proc = subprocess.Popen(
        [exe, "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.5) as s:
                    s.sendall(b"PING\r\n")
                    if s.recv(16).startswith(b"+PONG"):
                        break
            except OSError:
                pass
            if proc.poll() is not None or time.monotonic() > deadline:
                pytest.fail("redis-server failed to start")
            time.sleep(0.05)
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
async def db_engine(pg_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        await engine.dispose()


async def truncate_all(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE"))
    reset_user_touch_cache()  # the auth dependency's "already upserted" cache is now stale


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    await truncate_all(db_engine)
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
async def make_user(db_engine: AsyncEngine) -> AsyncIterator[Callable[[], Awaitable[uuid.UUID]]]:
    created: list[uuid.UUID] = []

    async def _make() -> uuid.UUID:
        uid = uuid.uuid4()
        async with db_engine.begin() as conn:
            await conn.execute(text("INSERT INTO users (id) VALUES (:id)"), {"id": uid})
        created.append(uid)
        return uid

    yield _make
    if created:
        async with db_engine.begin() as conn:
            await conn.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": created})
