"""Runtime configuration, loaded from environment / .env.

Every guardrail threshold lives here rather than inline in the modules
that enforce them, so the limits are auditable in one place and can be
tightened without touching enforcement logic.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # BigQuery
    google_cloud_project: str = ""
    bq_location: str = "US"

    # Corpus. Fully-qualified dataset ids the agent is permitted to touch.
    allowed_datasets: tuple[str, ...] = (
        "bigquery-public-data.thelook_ecommerce",
    )

    # Guardrail thresholds
    max_bytes_billed: int = 1_000_000_000   # 1 GB dry-run ceiling
    max_rows: int = 500
    max_retries: int = 2

    # LLM
    anthropic_api_key: str = ""
    llm_model: str = "claude-opus-5"


@lru_cache
def get_settings() -> Settings:
    return Settings()
