"""
WHAT THIS FILE ACTUALLY DOES, IN PLAIN LANGUAGE
------------------------------------------------
This is the front door of the whole system, wired up as one tool an AI agent
(via MCP) can call: "answer this plain-English analytics question."

Under the hood it just runs the pieces already built, in a fixed order, and
translates whatever goes wrong into a message the calling agent can actually
read and react to, instead of a raw crash:

  1. Ask the LLM to turn the English question into the small, restricted
     JSON "order form" (which measure, which dimensions, which filters) --
     retrying a few times if the LLM's JSON comes back malformed.
  2. Hand that form to the Layer 1 compiler (intent_compiler.py) to turn it
     into real SQL. If the form named something that doesn't exist, refuse
     with a clear "here's what you could have asked for" message.
  3. If the request came with an identity attached, run it through Layer 2
     (security.py) to bolt on the row-level access filter. If that identity
     isn't allowed to see anything here, refuse -- never quietly run the
     query unfiltered.
  4. Run the existing safety checks (guardrails + cost ceiling) against the
     compiled SQL as a last-resort seatbelt. If compiled SQL fails these, log
     it loudly -- that means the compiler itself has a bug, not that the
     question was bad.
  5. Actually execute the query against BigQuery.
  6. Package the rows plus a paper trail (the SQL that ran, which tables it
     joined and how, which model version, whether an identity's access
     restriction was actually applied) so a human or an agent can audit
     exactly what happened and why it was trusted -- not just get an answer
     back and have to take it on faith.

Every failure along the way becomes a `ToolError` naming which step refused
and why, so an agent calling this tool gets something it can act on (e.g.
retry with a valid measure name) rather than an opaque exception.

WHY THIS MODULE EXISTS
------------------------
mcp_server/server.py wires this file's tool onto an MCP server; the actual
pipeline -- turning a plain-English question into a governed, executed query
-- lives here. This is the one tool the server exposes (see server.py's
module docstring for why one, not several): everything an MCP client can
ask this gateway to do goes through query_semantic_metric.

CONTRACT
--------
    query_semantic_metric(question, on_behalf_of=None) -> QueryResult

The pipeline, in order -- this is the same sequence graph_semantic.py's
LangGraph nodes already implement and test individually; this function's job
is to run them as a plain call chain for an MCP client rather than a graph
runtime, catching each layer's own exception type and re-raising it as a
fastmcp.exceptions.ToolError so a calling agent gets a structured refusal
instead of a stack trace killing the session:

1. EXTRACT INTENT
   Build the extraction prompt from the certified model
   (src/semantic/intent.py::build_prompt) and ask the configured LLM for
   Semantic Intent JSON (src/semantic/intent.py::parse_intent). A malformed
   response is retryable (same reasoning as
   graph_semantic.py::_make_extract_intent's docstring) -- how many times,
   if at all, is yours to decide; see DECISIONS TO MAKE below.

2. LAYER 1 -- COMPILE
   src/compiler/intent_compiler.py::compile(intent, model, settings).
   UnsupportedIntent and SemanticCompileError both mean "refuse," not "crash"
   -- raise ToolError with the underlying message (SemanticCompileError
   already names the allowed set; let that reach the caller, it is exactly
   the kind of thing an agent can act on and retry with corrected intent).

3. LAYER 2 -- ROW SECURITY
   Only when on_behalf_of is supplied (same opt-in rule as
   graph_semantic.py's compile node -- a request with no identity compiles
   exactly as it would without Layer 2). src/compiler/security.py::
   inject_security_context(expression, tenant_id, region, model).
   SecurityContextError means denied -- raise ToolError, never fall through
   to an unfiltered query.

4. GUARD -- the existing five checks, unmodified
   src/guardrails.py::check(sql, schema_context), then (only if that
   passes) src/cost_guard.py::check_cost(sql). A guardrail violation on
   compiled SQL is a COMPILER DEFECT (see graph_semantic.py's module
   docstring, decision 1) -- log at ERROR and raise ToolError; do not retry
   intent extraction to "fix" SQL the model never wrote.

5. EXECUTE
   src/bq_client.py's execute path (or however graph.py's own _execute_node
   does it -- reuse that logic rather than re-deriving it; two independent
   BigQuery-execution code paths in one repo is a bug waiting to diverge).

6. SHAPE THE RESULT
   Return a QueryResult (below) with the rows, the compiled SQL, and enough
   provenance (join path, model version) that a calling agent -- or a human
   reading the MCP transcript -- can audit what actually ran and why it was
   trusted, without re-deriving it from logs.

DECISIONS MADE (see the module docstring's original DECISIONS TO MAKE section)
-------------------------------------------------------------------------------

  A. HOW MANY RETRIES ON A MALFORMED INTENT?
     A small hand-rolled loop, capped at settings.max_retries (same knob
     graph_semantic.py's _apply_retry_policy reads, so the two pipelines
     agree on "how patient are we with the LLM" without a second setting to
     keep in sync). Each attempt re-sends the same extraction prompt; a
     parse failure on the last attempt raises ToolError naming the raw
     response, since "the LLM's JSON didn't parse" is itself useful
     information for whoever is debugging the prompt, not just the caller.

  B. REUSE graph_semantic.py'S GRAPH, OR CALL EACH LAYER DIRECTLY?
     Calling each layer directly, as the numbered steps above describe.
     This file has no LangGraph runtime underneath it and intent_compiler.py
     / security.py were both built as plain functions specifically so they
     have exactly one calling convention to test, not two (a graph node
     wrapper and a direct call). Going through build_graph().invoke() here
     would mean carrying LangGraph as a dependency of the MCP server for a
     five-step linear pipeline with no branching this file needs on its own.
     The tradeoff this accepts: a future fix to graph_semantic.py's pipeline
     (e.g. a new retry heuristic) has to be re-applied here too. That's
     judged acceptable because this file's pipeline is intentionally a
     subset with no branching -- if the two ever need to diverge in more
     than retry count, that's a signal to extract a shared helper, not to
     switch this file over to the graph runtime.

WHAT THIS MODULE MUST NOT DO
------------------------------
- No second implementation of BigQuery execution -- reuse graph.py's
  execution path (whichever way decision B resolves that access).
- No swallowing a layer's specific error into a generic "something went
  wrong" -- ToolError's message should tell an agent which layer refused
  and why, the same way SemanticCompileError already carries the allowed
  set forward.
- No unfiltered query reaching BigQuery when on_behalf_of was supplied but
  Layer 2 has not yet run -- security injection is not optional once an
  identity is on the request.

A NOTE ON INTERFACES THIS FILE ASSUMES
-----------------------------------------
The internal helpers below were written against a few assumed interfaces
that didn't match the real modules once checked -- corrected in place, none
of the pipeline logic or the two decisions above changed:

  - No src/llm_client.py exists. The LLM is built with
    src.graph._build_llm(settings) (a ChatGoogleGenerativeAI, the same
    factory graph.py and graph_semantic.py both already use), called via
    .invoke(messages) -> response with a .text attribute, not
    .complete(prompt) -> str.
  - The message list MUST include a human turn, not system-only. This is
    not a style preference -- graph_semantic.py's extract_intent node hit
    exactly this as a real, live bug on Day 2 (Concept 21 in LEARNING.md):
    the Gemini API rejects a request with no user-role content at all
    ("contents are required"), even when every actual instruction lives in
    the system message. build_prompt() already embeds the question into
    the system text; a second, explicit ("human", question) message is
    still required purely to satisfy the API's contents requirement.
  - src/semantic/model.py exposes module-level load(path, schema_context),
    not a load_semantic_model() singleton -- and it needs a SchemaContext
    (src.schema.load_or_introspect()) passed in, which is also what
    guardrails.check() needs directly. There is no model.schema_context
    attribute, so the schema is loaded once and threaded to both callers
    rather than re-derived from the model.
  - src/guardrails.py::check(sql, schema_context) does not raise -- it
    returns a GuardrailReport with .passed / .violations, same as
    src/cost_guard.py::check_cost(sql) returning a CostVerdict with
    .passed / .retry_hint. Both only raise when BigQuery itself rejects the
    dry-run as structurally invalid SQL (cost_guard.py's own decision #2) --
    that specific exception is still treated as a compiler defect, per this
    module's step 4, but a plain "checks failed" is a `not report.passed`
    check, not a caught exception.
  - src/bq_client.py::execute(sql, client=None) reads settings internally
    (via get_settings()) rather than taking them as a parameter.

Owner: Ajinkya.
"""

from __future__ import annotations

import logging
from typing import Any

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import BaseModel, ConfigDict, Field

from src.compiler.intent_compiler import (
    SemanticCompileError,
    UnsupportedIntent,
    compile as compile_intent,
)
from src.compiler.security import SecurityContextError, inject_security_context
from src.cache.intent_hash import hash_intent
from src.cache.session_store import SessionStore
from src.models import OnBehalfOf

logger = logging.getLogger(__name__)


class QueryResult(BaseModel):
    """What query_semantic_metric returns on success.

    Mirrors AskResponse (src/models.py) in spirit -- the caller should be
    able to see what ran and why it was trusted, not just the answer -- but
    is its own model because an MCP client and the REST API are different
    audiences: an MCP client is a program that may act on `proven_join_path`
    or `route_taken` programmatically, not a human reading a rendered page.
    """

    model_config = ConfigDict(extra="forbid")

    question: str
    sql: str
    rows: list[dict[str, Any]]
    row_count: int
    measure: str
    dimensions: tuple[str, ...] = ()
    proven_join_path: list[str] = Field(default_factory=list)
    semantic_model_version: str
    principal_applied: bool = Field(
        description="True iff on_behalf_of was supplied and Layer 2 ran. "
        "False does not mean 'unrestricted data' -- it means no identity "
        "was presented at all, same as any other request without one."
    )
    cache_hit: bool = Field(
        default=False,
        description="True iff this result came from Layer 4's cache -- "
        "compilation, Layer 2, the guard, and BigQuery were all skipped.",
    )


def register_tools(mcp: FastMCP) -> None:
    """Wire query_semantic_metric onto `mcp`. Called once from
    mcp_server/server.py. Kept separate from module import time so tests can
    build a throwaway FastMCP instance without needing the real one."""
    mcp.tool(query_semantic_metric)


_session_store: SessionStore | None = None


def _get_session_store() -> SessionStore:
    """Layer 4's cache, shared with src/graph_semantic.py -- both point at
    the same data/session_store.db by default, so a question answered
    through the REST/graph path and the identical question asked again
    through this MCP tool can hit the same cache entry. Lazy so importing
    this module never touches disk; built once per process."""
    global _session_store
    if _session_store is None:
        _session_store = SessionStore()
    return _session_store


# ---------------------------------------------------------------------------
# Internal helpers -- not part of the public contract.
# ---------------------------------------------------------------------------


def _extract_intent_with_retries(question: str, model: Any, settings: Any) -> Any:
    """Step 1: ask the LLM for Semantic Intent JSON, retrying malformed
    responses up to settings.max_retries times (DECISION A).

    Raises:
        ToolError: every attempt's response failed to parse.
    """
    from src.semantic.intent import build_prompt, parse_intent

    # src.graph._build_llm is the one LLM factory this repo uses -- reused
    # here rather than configuring a second, independent client.
    from src.graph import _build_llm

    prompt = build_prompt(question, model)
    llm = _build_llm(settings)

    max_retries = getattr(settings, "max_retries", 0)
    last_error: Exception | None = None
    last_raw: str | None = None

    for attempt in range(max_retries + 1):
        # Both a system AND a human message are required -- a system-only
        # message list is rejected outright by the Gemini API ("contents
        # are required") before the model ever sees the question. See the
        # module docstring's interfaces note; this is the same fix
        # graph_semantic.py needed on Day 2.
        messages = [("system", prompt), ("human", question)]
        response = llm.invoke(messages)
        raw_response = response.text
        last_raw = raw_response
        try:
            return parse_intent(raw_response)
        except (ValueError, TypeError) as exc:
            last_error = exc
            logger.warning(
                "intent extraction attempt %d/%d produced unparseable "
                "output: %s",
                attempt + 1,
                max_retries + 1,
                exc,
            )

    raise ToolError(
        "could not extract a valid intent after "
        f"{max_retries + 1} attempt(s): {last_error}. "
        f"Last raw response: {last_raw!r}"
    )


def _load_semantic_model_and_schema(settings: Any) -> tuple[Any, Any]:
    """The loaded, schema-validated certified model AND the raw
    SchemaContext -- guardrails.check() needs the schema directly, and
    SemanticModel does not carry it as an attribute, so both are returned
    from one place rather than the schema being re-derived by two callers.
    """
    from src.schema import load_or_introspect
    from src.semantic import model as semantic_model

    schema = load_or_introspect()
    model = semantic_model.load(settings.semantic_model_path, schema_context=schema)
    return model, schema


def _execute(sql: str) -> list[dict[str, Any]]:
    """Step 5: run the compiled, guarded SQL. Reuses src/bq_client.py's
    execute() -- the same function graph.py's _execute_node calls -- rather
    than re-deriving BigQuery client setup here. It reads settings
    internally (via get_settings()), so it takes no settings parameter.
    """
    from src.bq_client import execute

    return execute(sql)


# ---------------------------------------------------------------------------
# Public contract
# ---------------------------------------------------------------------------


def query_semantic_metric(
    question: str,
    on_behalf_of: OnBehalfOf | None = None,
) -> QueryResult:
    """Answer a plain-English analytics question through the full Semantic
    Execution Gateway pipeline: extract intent, compile against the
    certified model, apply row-level security if an identity is supplied,
    pass the existing guardrails, execute, and return the result with its
    provenance.

    Args:
        question: a plain-English analytics question, e.g. "what's our
            conversion rate by traffic source last week of August 2016?".
        on_behalf_of: a SIMULATED identity (src/models.py::OnBehalfOf) --
            see that class's docstring. None means no row-level restriction
            is applied, identical to every request before Layer 2 existed.

    Returns:
        QueryResult with the executed rows and full provenance.

    Raises:
        fastmcp.exceptions.ToolError: at any pipeline stage that refuses the
            request -- unsupported intent, an unknown measure/dimension/join
            (Layer 1), denied access (Layer 2), a guardrail or cost-ceiling
            rejection, or a BigQuery execution error. The message names
            which layer refused and why; an MCP client should be able to
            adapt from it rather than treating every failure as opaque.

    See the module docstring for the full step-by-step contract and the two
    decisions (A, B) made above.
    """
    from src.config import get_settings

    settings = get_settings()
    model, schema = _load_semantic_model_and_schema(settings)

    # --- 1. Extract intent ---------------------------------------------
    intent = _extract_intent_with_retries(question, model, settings)

    # --- Layer 4: cache check (only meaningful for the compiled path -- an
    # unsupported intent never reaches here with a usable cache entry, since
    # only a successful compile+execute ever writes one; see below) --------
    principal = on_behalf_of.model_dump() if on_behalf_of is not None else None
    cache_key = hash_intent(intent, principal)
    store = _get_session_store()
    cached = store.cache_get(cache_key)
    if cached is not None:
        meta = cached.metadata
        return QueryResult(
            question=question,
            sql=cached.compiled_sql,
            rows=cached.rows,
            row_count=len(cached.rows),
            measure=meta.get("measure", ""),
            dimensions=tuple(meta.get("dimensions", ())),
            proven_join_path=meta.get("proven_join_path", []),
            semantic_model_version=meta.get("semantic_model_version", ""),
            principal_applied=on_behalf_of is not None,
            cache_hit=True,
        )

    # --- 2. Layer 1: compile ---------------------------------------------
    try:
        compiled = compile_intent(intent, model, settings)
    except UnsupportedIntent as exc:
        raise ToolError(
            f"this question isn't expressible against the semantic model: {exc}"
        ) from exc
    except SemanticCompileError as exc:
        raise ToolError(f"Layer 1 (compile) refused: {exc}") from exc

    expression = compiled.expression
    sql = compiled.sql
    principal_applied = False

    # --- 3. Layer 2: row security (opt-in on identity) ----------------------
    if on_behalf_of is not None:
        try:
            expression = inject_security_context(
                expression,
                tenant_id=on_behalf_of.tenant_id,
                region=on_behalf_of.region,
                model=model,
            )
        except SecurityContextError as exc:
            raise ToolError(f"Layer 2 (security) denied: {exc}") from exc
        principal_applied = True
        sql = expression.sql(dialect=model.dialect, pretty=True)

    # --- 4. Guard: guardrails, then cost ------------------------------------
    # Both guardrails.check() and check_cost() RETURN a verdict object
    # (.passed / .violations, .passed / .retry_hint) rather than raising for
    # an ordinary rejection -- see the module docstring's interfaces note.
    # A guardrail or cost-ceiling rejection here means the COMPILER produced
    # SQL that shouldn't have been possible to compile in the first place
    # (see module docstring step 4) -- logged at ERROR, not retried, since
    # retrying intent extraction cannot fix SQL the model never authored.
    from src.cost_guard import check_cost
    from src.guardrails import check as check_guardrails

    report = check_guardrails(sql, schema)
    if not report.passed:
        logger.error(
            "guardrail rejected compiler-produced SQL -- this is a "
            "compiler defect, not a bad question. question=%r sql=%r "
            "violations=%s",
            question,
            sql,
            report.violations,
        )
        raise ToolError(f"internal safety check failed: {report.violations}")

    try:
        verdict = check_cost(sql)
    except Exception as exc:  # noqa: BLE001 - BigQuery rejected invalid SQL
        logger.error(
            "cost dry-run rejected compiler-produced SQL as invalid -- this "
            "is a compiler defect, not a cost issue. question=%r sql=%r "
            "error=%s",
            question,
            sql,
            exc,
        )
        raise ToolError(f"internal safety check failed: {exc}") from exc

    if not verdict.passed:
        raise ToolError(
            "query exceeds the allowed cost ceiling: "
            f"{verdict.retry_hint or f'{verdict.estimated_bytes_scanned} bytes'}"
        )

    # --- 5. Execute -----------------------------------------------------------
    try:
        rows = _execute(sql)
    except Exception as exc:  # noqa: BLE001 - execution errors vary by backend
        raise ToolError(f"query execution failed: {exc}") from exc

    # --- Layer 4: cache write (compiled path only) --------------------------
    store.cache_set(
        cache_key,
        rows=rows,
        compiled_sql=sql,
        ttl_seconds=getattr(settings, "cache_ttl_seconds", 300),
        metadata={
            "measure": compiled.measure,
            "dimensions": list(compiled.dimensions),
            "proven_join_path": compiled.proven_join_path(),
            "semantic_model_version": compiled.model_version,
        },
    )

    # --- 6. Shape the result --------------------------------------------------
    return QueryResult(
        question=question,
        sql=sql,
        rows=rows,
        row_count=len(rows),
        measure=compiled.measure,
        dimensions=compiled.dimensions,
        proven_join_path=compiled.proven_join_path(),
        semantic_model_version=compiled.model_version,
        principal_applied=principal_applied,
        cache_hit=False,
    )