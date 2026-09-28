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

    # Award Engine service (engine-service/, see docs/AWARD_ENGINE_CONTRACT.md).
    # `award_engine_url` unset (the default) means the engine is disabled:
    # ORL keeps placeholder AwardCostMatrix rows, skips post-solve
    # reconciliation, and flags placeholder-costed rosters as such.
    award_engine_url: str | None = None
    award_engine_timeout_s: float = 30.0
    # Employer-level `context` sent with every engine request -- facts about
    # the employer, not about any one worker. MA000016 treats a missing
    # legal employer / work type as a release gap: the engine answers
    # `unresolved` rather than guessing, and ORL surfaces that as-is.
    award_jurisdiction: str = "NSW"
    award_legal_employer: str | None = None
    award_work_type: str | None = None

    # award-intelligence app server (its Express API, not engine-service/),
    # read by the employee sync (POST /workers/sync-employees). Its
    # GET /api/employee-master returns raw payroll IDs and always requires
    # the API token. Unset URL means the sync is disabled.
    award_intelligence_url: str | None = None
    award_intelligence_api_token: str | None = None
    award_intelligence_timeout_s: float = 30.0


settings = Settings()
