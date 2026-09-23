"""Application settings, loaded from the environment (see .env.example)."""

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """ORL runtime configuration."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    database_url: str = "postgresql+asyncpg://orl:orl@localhost:5432/orl"
    redis_url: str = "redis://localhost:6379/0"

    # Flat hourly rate used to compute `pay_cost` on auto-generated
    # placeholder AwardCostMatrix rows (see app/services/admin_data/
    # eligibility.py) -- mirrors how scripts/seed_demo_data.py computes
    # `pay_cost = hourly_rate * shift_hours`, just with one flat rate
    # instead of a per-worker one, since a placeholder row has no real
    # award-interpreted rate to draw on.
    placeholder_hourly_rate: float = 40.0


settings = Settings()
