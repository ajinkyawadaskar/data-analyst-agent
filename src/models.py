"""Request/response models for the API.

Deliberately strict: unknown fields are rejected rather than ignored, so a
malformed client request fails at the edge instead of reaching the agent.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from src.synthesis import _get


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
    WHAT THIS MODEL ACTUALLY DOES, IN PLAIN LANGUAGE
    ----------------------------------------------------
    This is the trust receipt attached to every answer. Other layers each
    make a claim about themselves along the way -- "this SQL only touched
    certified tables," "this row passed the security filter," "this came
    from a fresh cache entry." Nobody reading the final answer can check any
    of that unless it's written down somewhere alongside the answer itself.
    This object is that write-down: it copies forward what each layer
    already decided, verbatim, so a reader (or a program) can verify what
    ran and why it was trusted without taking anything on faith. It never
    re-checks or recomputes those claims itself -- it's a receipt, not a
    re-audit.

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

    DECISIONS MADE (see the docstring's original DECISIONS TO MAKE section)
    ---------------------------------------------------------------------------

      A. HOW IS `audit_id` GENERATED?
         A random UUID4 by default. Nothing in this system needs the id to
         be reproducible FROM content -- no caller re-derives an id from a
         (question, timestamp) pair to look an envelope up. The one real
         future need named in the spec -- correlating this envelope with an
         external Langfuse trace -- is already solved by the `audit_id`
         parameter itself: the caller passes its own trace id straight
         through and it's used verbatim, no generation involved. A UUID is
         the right default for "no external id was supplied"; the override
         path is the right answer for "yes, correlate this with something
         external."

      B. WHAT DOES A FIELD "NOT APPLYING TO THIS ROUTE" LOOK LIKE?
         Split by field shape, applied consistently everywhere below:
           - List fields (`proven_join_path`, `retrieved_note_ids`): `None`
             means the mechanism never ran at all for this route (e.g. a
             pure "unstructured" answer never touched the compiler, so
             `proven_join_path` is `None`). `[]` means the mechanism DID run
             for this route but produced nothing (e.g. retrieval genuinely
             executed and matched zero notes). These are different facts --
             "we never looked" vs. "we looked and found nothing" -- and
             collapsing them to the same sentinel would lose that.
           - Scalar fields (`compiled_sql`, `semantic_model_version`,
             `cache_hit`): always `None` for "not applicable." There is no
             honest non-null value for "this SQL string doesn't exist" the
             way `[]` honestly represents "this list came back empty."
         One field the original DECISIONS TO MAKE didn't resolve:
         `model_version` is REQUIRED (not Optional) on this model, so some
         value must always exist. It is read from `result` under a few
         candidate keys first; only if genuinely absent does this function
         fall back to a settings-level version stamp for the running system
         -- that is reading configuration about what code is running, not
         re-deriving a claim about what happened in this specific request,
         so it doesn't violate "never recompute a substantive claim."

    WHAT THIS MODEL/BUILDER MUST NOT DO
    --------------------------------------
    - No silently omitting a field for a route where it's inapplicable --
      every field must be present on every response, even if its value is
      the documented "not applicable" sentinel from decision B.
    - No recomputing latency by re-timing anything -- `started_at` is
      passed in by the caller (who owns the actual request boundary);
      this function only computes the elapsed time from it to "now."

    Owner: Ajinkya.
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


# ---------------------------------------------------------------------------
# Internal helpers. Not part of the public contract.
# ---------------------------------------------------------------------------


def _first_present(result: Any, *names: str) -> Any:
    """Try each candidate field name in order, returning the first one that
    is actually present (not just falsy -- an empty list or False is a real
    value, not a missing one). Different pipelines (graph_semantic.py's
    state dict vs. src/stacking.py::answer()'s return shape vs.
    mcp_server/tools.py's QueryResult) may spell the same concept under
    different keys; this tries the plausible spellings rather than assuming
    one exact shape, same spirit as `_get` itself.
    """
    _MISSING = object()
    for name in names:
        value = _get(result, name, _MISSING)
        if value is not _MISSING:
            return value
    return None


def _resolve_model_version(result: Any) -> str:
    """model_version is required on AuditEnvelope, so something must always
    be returned. Prefer whatever the pipeline actually recorded; fall back
    to a running-system version stamp only as a last resort -- this is
    reading configuration about the deployed code, not inventing a claim
    about what happened in this request (see decision B's note on this).
    """
    from_result = _first_present(result, "model_version", "llm_model_version")
    if from_result is not None:
        return from_result

    from src.config import get_settings

    settings = get_settings()
    return getattr(settings, "model_version", "unknown")


def _resolve_retrieved_note_ids(result: Any, route_taken: str) -> list[str] | None:
    """DECISION B: None if retrieval never ran for this route at all
    (a pure "structured" answer never invokes retrieve_notes.py); [] if it
    ran and matched nothing; the actual ids otherwise.
    """
    if route_taken == "structured":
        return None

    direct = _first_present(result, "retrieved_note_ids", "cited_note_ids")
    if direct is not None:
        return list(direct)

    # Some pipelines may carry the raw RetrievedNote objects instead of
    # already-extracted ids (e.g. under a "notes" key) -- derive the ids
    # defensively rather than requiring every caller to pre-flatten them.
    notes = _first_present(result, "notes")
    if notes is not None:
        return [
            note_id
            for note_id in (_get(n, "note_id") for n in notes)
            if note_id is not None
        ]

    # Retrieval-capable route, but the result carried neither key -- treat
    # as "ran, found nothing" rather than "never ran," since route_taken
    # already told us the mechanism applies here.
    return []


def _resolve_compiled_fields(
    result: Any, route_taken: str
) -> tuple[str | None, list[str] | None, str | None, bool | None]:
    """compiled_sql, proven_join_path, semantic_model_version, cache_hit --
    all None together when compilation never happened for this route
    (a pure "unstructured" answer, or a "stack" answer whose retrieval half
    came back empty and therefore never reached the compiler).
    """
    compiled_sql = _first_present(result, "compiled_sql", "sql")
    if compiled_sql is None:
        # No compiled SQL recorded at all -- compilation didn't happen for
        # this response, so every field in this group is "not applicable."
        return None, None, None, None

    proven_join_path = _first_present(result, "proven_join_path")
    if proven_join_path is not None:
        proven_join_path = list(proven_join_path)

    semantic_model_version = _first_present(result, "semantic_model_version")
    cache_hit = _first_present(result, "cache_hit")

    return compiled_sql, proven_join_path, semantic_model_version, cache_hit


# ---------------------------------------------------------------------------
# Public contract
# ---------------------------------------------------------------------------


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
    resolved_audit_id = audit_id if audit_id is not None else str(uuid.uuid4())

    route_taken = _first_present(result, "route_taken", "route") or "structured"

    answer = _first_present(result, "answer", "explanation")

    compiled_sql, proven_join_path, semantic_model_version, cache_hit = (
        _resolve_compiled_fields(result, route_taken)
    )
    retrieved_note_ids = _resolve_retrieved_note_ids(result, route_taken)
    model_version = _resolve_model_version(result)

    # The only computation this function performs itself -- elapsed time
    # from a boundary the caller owns, never a re-timing of anything else.
    latency_ms = (time.time() - started_at) * 1000

    return AuditEnvelope(
        audit_id=resolved_audit_id,
        answer=answer,
        route_taken=route_taken,
        compiled_sql=compiled_sql,
        proven_join_path=proven_join_path,
        semantic_model_version=semantic_model_version,
        cache_hit=cache_hit,
        retrieved_note_ids=retrieved_note_ids,
        model_version=model_version,
        latency_ms=latency_ms,
    )