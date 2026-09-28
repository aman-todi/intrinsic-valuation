"""Runtime settings loaded from environment / .env (spec §12)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=(".env", "../.env"), extra="ignore")

    # Supabase
    SUPABASE_URL: str = "http://localhost:54321"
    SUPABASE_ANON_KEY: str = ""
    SUPABASE_SERVICE_ROLE_KEY: str = ""
    DATABASE_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
    DATABASE_POOLER_URL: str = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"

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
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = "dcf-app-artifacts"

    # App/runtime
    ENGINE_VERSION: str = "v1"
    PROMPT_VERSION: str = "v1"
    CACHE_TTL_DAYS: int = 30
    CACHE_EXPIRY_BACKSTOP_DAYS: int = 35
    BUILD_LOCK_TTL_SECONDS: int = 120
    LOG_LEVEL: str = "INFO"
    CORS_ORIGINS: str = "http://localhost:3000"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
