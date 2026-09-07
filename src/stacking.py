"""The Layer 5 orchestrator: ties src/router.py, tools/retrieve_notes.py, the
Layer 1-4 compiled pipeline, and src/synthesis.py into one callable.

WHY THIS FILE EXISTS
----------------------
Day 4 proved the stacking mechanism works by calling each piece by hand in
a throwaway script. router.classify(), tools.retrieve_notes.retrieve(),
intent_compiler.compile(), and synthesis.synthesize() all existed and were
each independently tested, but nothing in the actual API surface (src/api.py,
mcp_server/tools.py) called router.classify() at all -- every request still
went straight into the semantic gateway graph, structured-only. This module
is that missing top-level entry point, so a stacking question can be
answered by calling one function instead of hand-assembling the pipeline
again.

Owner: Claude. Every decision-bearing piece this file calls into
(classify, retrieve, compile, inject_security_context, synthesize) is
someone else's already-tested logic; this file only sequences them and adds
tracing spans (src/tracing.py) around each stage, per Layer 6's chain:
router -> retrieval -> intent_extraction -> compile (nested
security_injection) -> guard -> execute -> synthesis.

ONE REAL DESIGN CHOICE MADE HERE, WORTH DEFENDING
----------------------------------------------------
For a "stack" question, WHAT does the structured half actually ask for? The
qualitative half ("accounts complaining about latency") names WHO; the
question as a whole may or may not clearly name WHAT metric to look up
about them. This module extracts intent from the ORIGINAL question text
using the existing extraction prompt (build_prompt/parse_intent, unchanged),
then programmatically APPENDS a `user_id IN (...)` filter to whatever intent
the LLM already produced -- rather than asking the LLM a second, different
question about "these specific users." That keeps the LLM's job identical
to the structured-only path (translate the question into a certified
measure/dimensions) and treats the user-scoping as a mechanical filter
injection this code controls deterministically, the same trust boundary
already established for Layer 2's row-security predicate.
"""

from __future__ import annotations

import logging
from typing import Any

from src.compiler.intent_compiler import (
    SemanticCompileError,
    UnsupportedIntent,
    compile as compile_intent,
)
from src.compiler.security import SecurityContextError, inject_security_context
from src.config import get_settings
from src.cost_guard import check_cost
from src.graph import _build_llm
from src.guardrails import check as check_guardrails
from src.models import OnBehalfOf
from src.router import classify
from src.schema import load_or_introspect
from src.semantic import model as semantic_model
from src.semantic.intent import Filter, build_prompt, parse_intent
from src.synthesis import synthesize
from src.tracing import traced_span
from tools.retrieve_notes import retrieve

log = logging.getLogger(__name__)


class StackingError(Exception):
    """Any stage of the stack/unstructured pipeline refused or failed.
    Carries which stage, so a caller can tell "denied by Layer 2" apart
    from "no matching notes" apart from "compiler refused"."""


def answer(
    question: str,
    on_behalf_of: OnBehalfOf | None = None,
    top_k: int = 5,
) -> dict[str, Any]:
    """Route `question` through structured, unstructured, or stack, and
    return a dict with at least {"route", "answer"} plus whatever
    provenance that route produces.

    Raises:
        StackingError: a pipeline stage refused (denied access, unknown
            measure, etc.) -- the message names which stage and why.
    """
    settings = get_settings()

    with traced_span("router", question=question[:200]):
        route = classify(question)
    log.info("stacking.answer(): route=%s question=%r", route, question)

    if route == "structured":
        # Unchanged: the existing semantic gateway graph owns this path.
        from src.graph_semantic import build_graph

        graph = build_graph()
        state: dict[str, Any] = {"question": question}
        if on_behalf_of is not None:
            state["principal"] = on_behalf_of.model_dump()
        result = graph.invoke(state)
        return {"route": "structured", "answer": result.get("explanation"), "state": result}

    with traced_span("retrieval", question=question[:200], top_k=top_k):
        notes = retrieve(question, top_k=top_k)

    if route == "unstructured":
        with traced_span("synthesis", note_count=len(notes)):
            result = synthesize(question, notes, query_result=None)
        return {
            "route": "unstructured",
            "answer": result.answer,
            "cited_note_ids": result.cited_note_ids,
        }

    # route == "stack"
    if not notes:
        with traced_span("synthesis", note_count=0):
            result = synthesize(question, notes, query_result=None)
        return {"route": "stack", "answer": result.answer, "cited_note_ids": ()}

    user_ids = sorted({n.user_id for n in notes})

    schema = load_or_introspect()
    model = semantic_model.load(settings.semantic_model_path, schema_context=schema)
    llm = _build_llm(settings)

    with traced_span("intent_extraction", question=question[:200]):
        prompt = build_prompt(question, model)
        # The human turn is NOT the raw question verbatim (a real bug found
        # here, live): asking the standard extraction prompt the FULL
        # two-part question caused the LLM to mark it unsupported, because
        # the qualitative clause ("complaining about latency") looks like an
        # ask for something outside the certified model when read alone.
        # This note tells it that half is already resolved by retrieval, so
        # it should extract only the structured half -- still one
        # unmodified extraction prompt (build_prompt is untouched), just a
        # different human message for this specific call site. The
        # human-turn requirement itself is Concept 21's fix, still in force.
        human_message = (
            f"{question}\n\nThe specific accounts this question refers to "
            "have ALREADY been identified by a separate lookup -- you do "
            "not need to identify them, and their identity is not your "
            "concern. Your only job is to say which ONE certified measure "
            "from the list above (e.g. total_revenue, order_count) this "
            "question is asking to compute for those accounts. Do NOT set "
            "unsupported=true because the accounts aren't identifiable to "
            "you -- only set it if none of the certified measures apply to "
            "what's being asked about them at all."
        )
        response = llm.invoke([("system", prompt), ("human", human_message)])
        try:
            intent = parse_intent(response.text)
        except (ValueError, TypeError) as exc:
            raise StackingError(f"intent_extraction refused: {exc}") from exc

    if intent.unsupported:
        raise StackingError(
            f"structured half not expressible against the semantic model: "
            f"{intent.unsupported_reason}"
        )

    # Deterministic filter injection -- see module docstring. Not an LLM
    # decision: these are exactly the user_ids retrieval just found.
    intent = intent.model_copy(
        update={
            "filters": [
                *intent.filters,
                Filter(field="user_id", operator="in", value=user_ids),
            ]
        }
    )

    with traced_span("compile", measure=intent.measure):
        try:
            compiled = compile_intent(intent, model, settings)
        except (UnsupportedIntent, SemanticCompileError) as exc:
            raise StackingError(f"compile refused: {exc}") from exc

        expression = compiled.expression
        sql = compiled.sql
        if on_behalf_of is not None:
            with traced_span("security_injection", region=on_behalf_of.region):
                try:
                    expression = inject_security_context(
                        expression, on_behalf_of.tenant_id, on_behalf_of.region, model
                    )
                except SecurityContextError as exc:
                    raise StackingError(f"security denied: {exc}") from exc
            sql = expression.sql(dialect=model.dialect, pretty=True)

    with traced_span("guard"):
        report = check_guardrails(sql, schema)
        if not report.passed:
            log.error(
                "guardrail rejected compiler-produced SQL in stacking path "
                "-- compiler defect. sql=%r violations=%s", sql, report.violations,
            )
            raise StackingError(f"internal safety check failed: {report.violations}")
        try:
            verdict = check_cost(sql)
        except Exception as exc:  # noqa: BLE001 - BigQuery rejected invalid SQL
            raise StackingError(f"internal safety check failed: {exc}") from exc
        if not verdict.passed:
            raise StackingError(
                f"query exceeds the allowed cost ceiling: {verdict.retry_hint}"
            )

    with traced_span("execution"):
        from src.bq_client import execute

        try:
            rows = execute(sql)
        except Exception as exc:  # noqa: BLE001 - execution errors vary by backend
            raise StackingError(f"execution failed: {exc}") from exc

    query_result = {
        "rows": rows,
        "measure": compiled.measure,
        "dimensions": compiled.dimensions,
    }
    with traced_span("synthesis", note_count=len(notes), row_count=len(rows)):
        result = synthesize(question, notes, query_result)

    return {
        "route": "stack",
        "answer": result.answer,
        "cited_note_ids": result.cited_note_ids,
        "cited_query_fields": result.cited_query_fields,
        "matched_user_ids": result.matched_user_ids,
        "sql": sql,
        "proven_join_path": compiled.proven_join_path(),
        "semantic_model_version": compiled.model_version,
    }
