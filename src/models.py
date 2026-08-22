"""Request/response models for the API.

Deliberately strict: unknown fields are rejected rather than ignored, so a
malformed client request fails at the edge instead of reaching the agent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=1000)


class GuardrailReport(BaseModel):
    """What the guardrail layer decided, surfaced to the caller.

    Populated by src/guardrails.py and src/cost_guard.py (owner: Ajinkya).
    Exposed because the blocking decision *is* the product here — a caller
    should be able to see why a question was refused.
    """

    passed: bool
    checks_run: list[str] = []
    violations: list[str] = []
    estimated_bytes_scanned: int | None = None


class AskResponse(BaseModel):
    question: str
    sql: str | None = None
    rows: list[dict] | None = None
    row_count: int | None = None
    explanation: str | None = None
    guardrails: GuardrailReport
    retries_used: int = 0


class HealthResponse(BaseModel):
    status: str
    bigquery: str
    llm: str
    agent: str
