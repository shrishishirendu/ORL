"""Application settings, loaded from the environment (see .env.example)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """ORL runtime configuration."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://orl:orl@localhost:5432/orl"
    redis_url: str = "redis://localhost:6379/0"


settings = Settings()
