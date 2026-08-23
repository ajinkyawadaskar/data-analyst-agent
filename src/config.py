"""Runtime configuration, loaded from environment / .env.

Every guardrail threshold lives here rather than inline in the modules
that enforce them, so the limits are auditable in one place and can be
tightened without touching enforcement logic.
"""

import os
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
    llm_model: str = "gemini-3.1-flash-lite"


@lru_cache
def get_settings() -> Settings:
    """Load settings and export the LLM key to the environment.

    pydantic-settings reads .env into this object, but it does NOT set
    os.environ. Client libraries (langchain-google-genai, deepeval's
    GeminiModel) look for GOOGLE_API_KEY as an environment variable and
    fail construction without it. Exporting here fixes it once for every
    consumer rather than per call site.
    """
    settings = Settings()
    if settings.google_api_key:
        os.environ.setdefault("GOOGLE_API_KEY", settings.google_api_key)
    return settings
