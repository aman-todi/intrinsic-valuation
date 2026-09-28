"""FastAPI app factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import files, health, runs
from app.config import settings
from app.db.base import dispose_engine
from app.jobs.queue import close_queue
from app.redis_client import close_redis


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    await close_queue()
    await close_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    app = FastAPI(title="DCF Valuation API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origin_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Last-Event-ID"],
    )
    app.include_router(health.router)  # unauthenticated
    app.include_router(runs.router)
    app.include_router(files.router)  # signed URLs (local storage backend only)
    return app


app = create_app()
