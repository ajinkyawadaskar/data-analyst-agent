"""BigQuery client construction and read-only query execution.

There is no code path here that can write. Every query goes out with
`dry_run` first (see src/cost_guard.py, owner: Ajinkya) and executes with
an explicit `maximum_bytes_billed` so that a query which slips past the
cost check still cannot run away.
"""

from __future__ import annotations

from typing import Any

from google.cloud import bigquery

from src.config import get_settings


def get_client() -> bigquery.Client:
    """Build a BigQuery client from GOOGLE_APPLICATION_CREDENTIALS."""
    settings = get_settings()
    return bigquery.Client(
        project=settings.google_cloud_project or None,
        location=settings.bq_location,
    )


def dry_run(sql: str, client: bigquery.Client | None = None) -> int:
    """Return bytes this query would scan, without executing it.

    Used by the cost guard. Kept here (not in cost_guard.py) because it is
    plumbing; the ceiling *policy* is Ajinkya's module.
    """
    client = client or get_client()
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False),
    )
    return job.total_bytes_processed


def execute(sql: str, client: bigquery.Client | None = None) -> list[dict[str, Any]]:
    """Execute a query that has already passed the guardrails.

    Callers must run guardrails first. The bytes ceiling here is a second
    line of defence, not the primary one.
    """
    settings = get_settings()
    client = client or get_client()
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            maximum_bytes_billed=settings.max_bytes_billed,
            use_query_cache=True,
        ),
    )
    rows = job.result(max_results=settings.max_rows)
    return [dict(row) for row in rows]
