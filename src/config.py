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
        "bigquery-public-data.google_analytics_sample",
    )

    # Guardrail thresholds
    max_bytes_billed: int = 1_000_000_000   # 1 GB dry-run ceiling
    max_rows: int = 500
    max_retries: int = 2

    # LLM. gemini-3.6-flash confirmed working in P1 (credit-decision-explainer);
    # gemini-2.0-flash and gemini-2.5-flash were retired / 404 there. Do not
    # write a model name from memory -- probe it (tools/probe_models.py).
    google_api_key: str = ""
    llm_model: str = "gemini-3.6-flash"


@lru_cache
def get_settings() -> Settings:
    return Settings()
