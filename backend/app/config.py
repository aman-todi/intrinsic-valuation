"""Runtime settings loaded from environment / .env (spec §12)."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    # Auth: AWS Cognito user pool. The API accepts access tokens issued to COGNITO_APP_CLIENT_ID.
    COGNITO_REGION: str = "us-east-2"
    COGNITO_USER_POOL_ID: str = ""
    COGNITO_APP_CLIENT_ID: str = ""

    # Postgres (AWS RDS in production; add ?sslmode=require there). Used by the app AND Alembic.
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"

    # Redis
    REDIS_URL: str = "redis://localhost:6379"

    # Anthropic
    ANTHROPIC_API_KEY: str = ""
    ANTHROPIC_MODEL: str = "claude-sonnet-5"

    # SEC EDGAR
    SEC_EDGAR_USER_AGENT: str = "DCF-Valuation-App contact@example.com"

    # FRED
    FRED_API_KEY: str = ""

    # AWS
    AWS_REGION: str = "us-east-2"
    S3_BUCKET_NAME: str = "dcf-app-artifacts"

    # App/runtime
    ENGINE_VERSION: str = "v1"
    PROMPT_VERSION: str = "v1"
    CACHE_TTL_DAYS: int = 30
    CACHE_EXPIRY_BACKSTOP_DAYS: int = 35
    BUILD_LOCK_TTL_SECONDS: int = 120
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = "http://localhost:3000"

    # Artifact storage (§11.5). "s3" in every deployed environment; "local" is for dev without AWS:
    # files live under LOCAL_STORAGE_DIR and download links are short-lived HMAC-signed URLs served
    # by the API's GET /api/files/{path} route (PUBLIC_API_BASE_URL is the browser-facing API origin).
    STORAGE_BACKEND: Literal["s3", "local"] = "s3"
    LOCAL_STORAGE_DIR: str = ".local-storage"
    STORAGE_SIGNING_SECRET: str = ""  # HMAC key for local signed URLs; empty -> fixed dev key
    PUBLIC_API_BASE_URL: str = "http://localhost:8000"
    PRESIGNED_URL_TTL_SECONDS: int = 3600

    # Job queue (SAQ on Redis)
    SAQ_QUEUE_NAME: str = "dcf"
    WORKER_CONCURRENCY: int = 4
    # SIGTERM drain for in-flight jobs (§8.3). Must stay below the worker container's stop grace
    # (docker compose stop_grace_period: 90s in infra/deploy/docker-compose.prod.yml) so the cancel +
    # cleanup path runs before SIGKILL.
    WORKER_SHUTDOWN_GRACE_SECONDS: int = 60

    # Local dev only: used as the risk-free rate when FRED_API_KEY is empty (decimal, e.g. 0.042).
    # Runs using it carry a data-confidence flag. Leave unset in any deployed environment.
    RISK_FREE_RATE_OVERRIDE: float | None = None

    # Local dev / demos only: "fixtures" serves SEC EDGAR data from the bundled fixture set
    # (app/data/demo/edgar), fixed per-ticker prices and a fixed risk-free rate, with no network calls
    # to SEC / Yahoo / FRED. Every run is flagged "DEMO DATA". NEVER enable in a deployed environment.
    DATA_SOURCE_MODE: Literal["live", "fixtures"] = "live"

    # Local dev only: accept "Bearer dev-bypass-token" (the frontend's no-Cognito mode) as a fixed
    # dev user. NEVER enable against a shared/production database.
    DEV_AUTH_BYPASS: bool = False

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
