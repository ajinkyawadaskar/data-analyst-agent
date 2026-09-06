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
    # None until src/api.py is wired to call build_audit_envelope() (Day 5,
    # once AuditEnvelope's body exists) -- additive, does not change the
    # shape of any existing response.
    audit: "AuditEnvelope | None" = None


class HealthResponse(BaseModel):
    status: str
    bigquery: str
    llm: str
    agent: str
    # Which path is serving. Surfaced so a deployed instance can be asked
    # what it is running rather than inferred from the dashboard.
    path: str = "legacy"


class AuditEnvelope(BaseModel):
    """
    WHY THIS MODEL EXISTS
    -----------------------
    Every other layer in this build makes a claim about itself: the
    compiler claims a query is certified, Layer 2 claims a row is
    permitted, Layer 4 claims a result is fresh enough to reuse. None of
    those claims are worth anything to a caller who can't independently
    check them after the fact. This envelope is that check -- a single,
    always-present object bundling exactly what ran and why it was
    trusted, attached to every response regardless of which route answered
    it, so "trust me" is never the only option a reader has.

    FIELDS, PER THE PLAN'S OWN LIST
    ---------------------------------
    answer, compiled_sql, proven_join_path, semantic_model_version,
    cache_hit, retrieved_note_ids, route_taken, audit_id, model_version,
    latency_ms -- see the field definitions below for what "n/a" means
    for a route where a given field doesn't apply (e.g. compiled_sql on a
    pure "unstructured" route that never touched the compiler at all).

    CONTRACT
    --------
        build_audit_envelope(result, *, started_at, audit_id=None) -> AuditEnvelope

    `result` is whatever src/stacking.py::answer() or src/graph_semantic.py's
    invoked state already produced -- this function's only job is to read
    the right fields out of whichever shape it was given (see
    src/synthesis.py's `_get()` helper for the same dict-or-object problem,
    already solved there) and stamp an id + elapsed time on top. It must
    NOT recompute or re-derive any of the substantive claims (join path,
    model version, cache status) -- those come from whichever layer already
    computed them, verbatim, or this envelope becomes one more thing that
    could silently drift from what actually happened.

    DECISIONS TO MAKE BEFORE WRITING THE BODY:

      A. HOW IS `audit_id` GENERATED?
         A random UUID is simplest and always unique, but carries no
         information. A content hash (e.g. of question + timestamp) is
         reproducible but not obviously more useful here than a UUID would
         be. Pick one and say why -- and if a caller ever needs to look up
         a specific past answer by this id later (Langfuse trace
         correlation is the obvious use), that requirement should drive
         the choice, not novelty.

      B. WHAT DOES A FIELD "NOT APPLYING TO THIS ROUTE" LOOK LIKE?
         A pure "unstructured" answer has no compiled_sql, no
         proven_join_path, no cache_hit (nothing was compiled to check the
         cache against). `None` is the honest value for "not applicable
         here", but decide whether that should be `None` uniformly or
         whether some fields make more sense as an empty list/string for a
         route where the concept exists but produced nothing (e.g.
         `retrieved_note_ids: []` for a "structured" route that never
         retrieved anything, vs `None` for a route where retrieval was
         never even attempted). Be consistent, and say which choice you
         made and why -- an inconsistent envelope is worse than a
         type-annotated one.

    WHAT THIS MODEL/BUILDER MUST NOT DO
    --------------------------------------
    - No silently omitting a field for a route where it's inapplicable --
      every field must be present on every response, even if its value is
      the documented "not applicable" sentinel from decision B.
    - No recomputing latency by re-timing anything -- `started_at` is
      passed in by the caller (who owns the actual request boundary);
      this function only computes the elapsed time from it to "now."

    Owner: Ajinkya. Scaffolding (fields, docstring contract) only below.
    """

    model_config = ConfigDict(extra="forbid")

    audit_id: str
    answer: str | None = None
    route_taken: str
    compiled_sql: str | None = None
    proven_join_path: list[str] | None = None
    semantic_model_version: str | None = None
    cache_hit: bool | None = None
    retrieved_note_ids: list[str] | None = None
    model_version: str
    latency_ms: float


def build_audit_envelope(
    result: dict,
    *,
    started_at: float,
    audit_id: str | None = None,
) -> AuditEnvelope:
    """Assemble an AuditEnvelope from a pipeline result. See AuditEnvelope's
    docstring for the full contract and the two decisions (A, B) that have
    to be made before this can be written.

    Args:
        result: whatever src/stacking.py::answer() returned, or a
            graph_semantic.py invoked state dict -- read defensively, do
            not assume one exact shape (see AuditEnvelope's docstring).
        started_at: time.time() (or equivalent) captured by the CALLER at
            the actual start of the request -- this function only computes
            elapsed time from it, never re-times anything itself.
        audit_id: caller-supplied id (e.g. to correlate with an existing
            Langfuse trace id), or None to generate one -- see decision A.
    """
    raise NotImplementedError("TODO: Ajinkya writes this")
