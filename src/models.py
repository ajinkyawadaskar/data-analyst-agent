"""Request/response models for the API.

Deliberately strict: unknown fields are rejected rather than ignored, so a
malformed client request fails at the edge instead of reaching the agent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class OnBehalfOf(BaseModel):
    """A SIMULATED identity for the semantic gateway's row-level security layer.

    This project has no real users, tenants, or auth system -- there is
    nothing behind these fields but what the caller types into the request.
    It exists to demonstrate compile-time security injection (Layer 2:
    src/compiler/security.py) against something request-shaped, not to claim
    real multi-tenancy. Say so in any write-up or demo of this field.

    `region` matches semantic_model.yaml's row_policies `principal_field`
    (e.g. the `users` and `ga_sessions` policies both key off `region`), so
    the compiler can look up which policy applies without a separate mapping
    table living in this file.
    """

    model_config = ConfigDict(extra="forbid")

    tenant_id: str = Field(min_length=1, max_length=100)
    region: str = Field(min_length=1, max_length=100)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=3, max_length=1000)
    # None means "no identity simulated" -- the query compiles with no
    # row-level restriction, same as every request before Layer 2 existed.
    # Only meaningful on the semantic gateway path; the legacy path ignores it.
    on_behalf_of: OnBehalfOf | None = None


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
    # Which path is serving. Surfaced so a deployed instance can be asked
    # what it is running rather than inferred from the dashboard.
    path: str = "legacy"
