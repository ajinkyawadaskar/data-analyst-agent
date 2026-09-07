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

    # Semantic Execution Gateway (feature/semantic-gateway).
    # Off by default: the prompt-based path on main stays the one that serves
    # traffic until the compiled path is measured at or above it. Note that
    # get_settings() is lru_cached, so flipping this at runtime does nothing --
    # read it at the call site and pass it into build_graph() instead.
    use_semantic_gateway: bool = False
    semantic_model_path: str = "semantic_model.yaml"

    # Layer 4 semantic cache. 5 minutes: long enough that a burst of
    # paraphrased repeats of one question (the case this cache targets) hits
    # every time, short enough that the public datasets' own slow drift
    # (thelook is a live-generated sample, not a frozen snapshot) can't go
    # stale for long without anyone asking again.
    cache_ttl_seconds: int = 300

    # Layer 6 observability. Langfuse's OTel-native ingestion endpoint --
    # see src/tracing.py. Empty keys mean tracing is a documented no-op
    # (init_tracing() logs once and returns), not a crash: this project
    # must run identically with or without an observability backend
    # configured, the same principle /health already follows.
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_host: str = "https://cloud.langfuse.com"


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
