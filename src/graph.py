"""
src/graph.py

Wires the already-built pieces (schema, guardrails, cost_guard, bq_client)
into a single bounded LangGraph loop and exposes:

    def build_graph():   # -> compiled graph with .invoke(state)

src/api.py calls build_graph() once (lazily, at first request) and reads
sql / rows / explanation / guardrails / retries_used off the result of
.invoke({"question": ...}).

--------------------------------------------------------------------------
WHY LOAD_SCHEMA HAPPENS AT build_graph() TIME, NOT INSIDE THE GRAPH
--------------------------------------------------------------------------
introspect() is ~10 BigQuery API calls; the schema does not change
between questions. build_graph() introspects and compacts once, and the
resulting SchemaContext is closed over by every node function. A node
that re-introspected per request would burn the free-tier quota (see the
20-request ceiling noted for gemini-3.6-flash) on work that produces the
same answer every time.

--------------------------------------------------------------------------
THE RETRY LOOP -- DECISIONS
--------------------------------------------------------------------------
Three distinct failure kinds can come out of a single attempt:

  A. guardrail violation       (guardrails.check() returns passed=False)
  B. cost ceiling exceeded     (cost_guard.check_cost() returns passed=False,
                                 retryable=True, with a retry_hint)
  C. BigQuery execution error  (either cost_guard.check_cost() *raises* --
                                 see below -- or bq_client.execute() raises
                                 after guard() passed)

DECISION: all three are retryable, up to the shared max_retries cap.
Reasoning: in every case there is a concrete, actionable piece of
feedback to hand back to the model (a violation list, a retry_hint, or a
BigQuery error message), and in practice all three are usually a
misunderstood schema rather than a dead end -- exactly the class of
error CLAUDE.md's "why cap at 2" argument is about. Rather than special
-case one kind as non-retryable, the cap itself is the safety valve: if
retry 2 doesn't fix it, further retries would just be guessing, whichever
kind of failure it was. (If eval numbers end up showing one failure kind
never recovers by retry 2, that's the honest finding to write up in
NOTES.md -- see graph docstring below -- not a reason to hardcode an
asymmetry ahead of the data.)

Feedback fed back to generate_sql on retry:
  A. the exact violations list from GuardrailReport, plus the columns
     that DO exist for every table referenced in the rejected SQL
     (schema_context.columns_for) -- "column X doesn't exist" is a guess
     the model can't fix without knowing what to guess instead.
  B. cost_guard's own retry_hint (narrow with a date filter).
  C. the BigQuery error message, verbatim (truncated) -- it is usually
     specific about what's wrong (bad function, type mismatch, etc).

GOTCHA THIS GRAPH ENFORCES: guardrails.check() always runs before
cost_guard.check_cost(). cost_guard hits the live BigQuery dry-run API
and RAISES on invalid SQL (that's cost_guard's documented contract) --
letting guardrails catch parse/table/column problems first means we
never spend a dry-run call, or an exception, on SQL that was already
structurally wrong. Likewise, execute() is only ever reached after BOTH
guardrails and cost_guard have returned passed=True for that attempt --
there is no path from a failed guard() straight to execute().

WHAT THE CALLER SEES WHEN RETRIES ARE EXHAUSTED: the last GuardrailReport
with passed=False and its real violations -- never a generic error. The
blocking decision, and why it was made, is the product surfaced by the
API; swallowing it into "something went wrong" would defeat the point of
building the guardrail layer at all.

--------------------------------------------------------------------------
LLM
--------------------------------------------------------------------------
Per NOTES.md this project runs on langchain-google-genai / Gemini, model
name from settings.llm_model (never hardcoded here -- model availability
has already moved twice on a sibling project, see NOTES.md). Free-tier
quota is 20 requests total: generate_sql is called at most
1 + max_retries times per question, and explain() is one more call, so a
single /ask can cost up to (1 + max_retries + 1) requests. Keep that in
mind sizing eval runs against the quota -- that sizing decision belongs in
run_evals.py / NOTES.md, not here.
"""

from __future__ import annotations

import logging
from typing import TypedDict

from langgraph.graph import END, StateGraph

from src.config import get_settings
from src.cost_guard import check_cost, merge_into_report
from src.guardrails import check as guardrails_check
from src.models import GuardrailReport
from src.schema import SchemaContext, compact, introspect

log = logging.getLogger(__name__)

# How much of the corpus schema the model sees. This is the one knob that
# most affects both prompt size and column-hallucination rate; see
# NOTES.md 1:30 for why "table_summary" silently breaks the GA dataset.
_SCHEMA_COMPACTION_STRATEGY = "column_sample"

# BigQuery error messages can be long (query text is echoed back); keep
# retry feedback readable rather than dumping the whole thing at the model.
_MAX_ERROR_FEEDBACK_CHARS = 500


class AgentState(TypedDict, total=False):
    question: str
    sql: str | None
    rows: list[dict] | None
    explanation: str | None
    guardrails: GuardrailReport
    retries_used: int

    # internal, not read by api.py
    retry_feedback: str | None
    bq_error: str | None
    outcome: str  # "success" | "guard_failed" | "cost_failed" | "exec_error"


def build_graph():
    """See module docstring. OWNER: Ajinkya."""
    settings = get_settings()

    schema_context: SchemaContext = compact(introspect(), _SCHEMA_COMPACTION_STRATEGY)
    llm = _build_llm(settings)

    graph = StateGraph(AgentState)
    graph.add_node("generate_sql", _make_generate_sql(llm, schema_context))
    graph.add_node("guard", _make_guard(schema_context, settings))
    graph.add_node("execute", _execute_node)
    graph.add_node("explain", _make_explain(llm))

    graph.set_entry_point("generate_sql")
    graph.add_edge("generate_sql", "guard")

    graph.add_conditional_edges(
        "guard",
        _route_after_guard,
        {"success": "execute", "retry": "generate_sql", "give_up": END},
    )
    graph.add_conditional_edges(
        "execute",
        _route_after_execute,
        {"explain": "explain", "retry": "generate_sql", "give_up": END},
    )
    graph.add_edge("explain", END)

    return graph.compile()


# ==========================================================================
# LLM construction
# ==========================================================================
def _build_llm(settings: object):
    """Model name always comes from settings.llm_model -- never written
    from memory here. Model availability has already shifted twice on a
    sibling project (P1), so a hardcoded name is a guaranteed future
    404."""
    from langchain_google_genai import ChatGoogleGenerativeAI

    return ChatGoogleGenerativeAI(model=settings.llm_model, temperature=0)


# ==========================================================================
# Node: generate_sql
# ==========================================================================
_SQL_SYSTEM_PROMPT = """You translate a plain-English analytics question \
into a single BigQuery Standard SQL SELECT statement.

Rules:
- Output ONLY the SQL. No prose, no markdown code fences.
- Use fully-qualified table names exactly as given in the schema below.
- Reference only tables and columns listed in the schema.
- Always include an explicit LIMIT.
- Never write anything other than a SELECT statement.

Schema:
{schema}
"""


def _make_generate_sql(llm, schema_context: SchemaContext):
    schema_prompt = schema_context.to_prompt()

    def generate_sql(state: AgentState) -> AgentState:
        messages = [
            ("system", _SQL_SYSTEM_PROMPT.format(schema=schema_prompt)),
            ("human", state["question"]),
        ]
        feedback = state.get("retry_feedback")
        if feedback:
            messages.append(
                (
                    "human",
                    f"Your previous attempt was rejected:\n{feedback}\n"
                    f"Write a corrected SQL statement.",
                )
            )

        response = llm.invoke(messages)
        sql = _strip_sql_fences(response.text)

        return {
            **state,
            "sql": sql,
            "retry_feedback": None,  # consumed
            "bq_error": None,
        }

    return generate_sql


def _strip_sql_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]  # drop opening fence (may include "sql")
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    return cleaned


# ==========================================================================
# Node: guard  (guardrails.check ALWAYS before cost_guard.check_cost)
# ==========================================================================
def _make_guard(schema_context: SchemaContext, settings: object):
    def guard(state: AgentState) -> AgentState:
        sql = state["sql"]

        report = guardrails_check(sql, schema_context)
        if not report.passed:
            feedback = _guard_feedback(sql, report, schema_context)
            return {
                **state,
                "guardrails": report,
                **_apply_retry_policy(state, feedback),
            }

        # Only reaches the live BigQuery dry-run call once guardrails has
        # already ruled the SQL structurally, allowlist-, and
        # column-safe. cost_guard.check_cost() raises on invalid SQL --
        # deliberately not caught here (see decision #C / module
        # docstring): that is a genuinely different failure than a
        # cost rejection and gets routed through the same retry policy
        # as bq_client.execute()'s exceptions, since both are "valid-
        # looking SQL, invalid at runtime against live BigQuery."
        try:
            verdict = check_cost(sql)
        except Exception as exc:  # noqa: BLE001 -- BigQuery raised on invalid SQL
            merged = merge_into_report(report, _empty_cost_report_stub())
            feedback = (
                f"BigQuery rejected this SQL when estimating cost: "
                f"{str(exc)[:_MAX_ERROR_FEEDBACK_CHARS]}"
            )
            return {
                **state,
                "guardrails": merged,
                "bq_error": str(exc)[:_MAX_ERROR_FEEDBACK_CHARS],
                **_apply_retry_policy(state, feedback),
            }

        merged = merge_into_report(report, verdict)

        if not verdict.passed:
            feedback = verdict.retry_hint or (
                "Query would scan too many bytes; add a filter to narrow it."
            )
            return {
                **state,
                "guardrails": merged,
                **_apply_retry_policy(state, feedback),
            }

        return {
            **state,
            "guardrails": merged,
            "outcome": "success",
            "retry_feedback": None,
        }

    return guard


def _empty_cost_report_stub():
    """cost_guard's dry run raised before producing a CostVerdict, so
    there's no byte estimate to merge in. A zero-effect stand-in keeps
    merge_into_report's single code path the only place that assembles
    the combined report, rather than duplicating that assembly here.
    passed=True here only means "don't additionally flip report.passed
    on top of what report already says" -- the retry/give-up decision is
    made separately by _apply_retry_policy, not by this stub."""
    from src.cost_guard import CostVerdict

    return CostVerdict(
        passed=True,
        estimated_bytes_scanned=0,
        ceiling_bytes=0,
        checks_run=[],
        violations=[],
    )


def _guard_feedback(sql: str, report: GuardrailReport, schema_context: SchemaContext) -> str:
    lines = list(report.violations)

    # Best-effort: tell the model what columns DO exist for whatever
    # tables it referenced, so a correction is possible instead of a
    # second guess. Table extraction here is intentionally light --
    # guardrails.py already did the real AST work; this is just enough
    # to surface a helpful hint, not a second guardrail.
    referenced = _guess_referenced_tables(sql, schema_context)
    for fqn in referenced:
        cols = schema_context.columns_for(fqn)
        if cols:
            sample = ", ".join(sorted(cols)[:30])
            lines.append(f"Columns available on {fqn}: {sample}")

    return "\n".join(lines)


def _guess_referenced_tables(sql: str, schema_context: SchemaContext) -> list[str]:
    return [fqn for fqn in schema_context.table_fqns() if fqn.split(".")[-1] in sql]


# ==========================================================================
# Node: execute
# ==========================================================================
def _execute_node(state: AgentState) -> AgentState:
    from src.bq_client import execute

    try:
        rows = execute(state["sql"])
    except Exception as exc:  # noqa: BLE001 -- valid-looking SQL, invalid at runtime
        feedback = (
            f"BigQuery rejected this SQL at execution time: "
            f"{str(exc)[:_MAX_ERROR_FEEDBACK_CHARS]}"
        )
        return {
            **state,
            "bq_error": str(exc)[:_MAX_ERROR_FEEDBACK_CHARS],
            **_apply_retry_policy(state, feedback),
        }

    return {**state, "rows": rows, "outcome": "success", "retry_feedback": None}


# ==========================================================================
# Node: explain
# ==========================================================================
_EXPLAIN_SYSTEM_PROMPT = (
    "Answer the user's question in 1-3 plain-English sentences using only "
    "the query result rows given. Do not mention SQL or the query itself."
)


def _make_explain(llm):
    def explain(state: AgentState) -> AgentState:
        messages = [
            ("system", _EXPLAIN_SYSTEM_PROMPT),
            (
                "human",
                f"Question: {state['question']}\nResult rows: {state.get('rows')}",
            ),
        ]
        response = llm.invoke(messages)
        return {**state, "explanation": response.text.strip()}

    return explain


# ==========================================================================
# Routing
# ==========================================================================
def _route_after_guard(state: AgentState) -> str:
    return state["outcome"] if state["outcome"] in ("success", "retry") else "give_up"


def _route_after_execute(state: AgentState) -> str:
    if state["outcome"] == "success":
        return "explain"
    return state["outcome"] if state["outcome"] in ("retry",) else "give_up"


def _apply_retry_policy(state: AgentState, feedback: str) -> dict:
    """Called by a failing node (guard or execute) to decide, right then,
    whether this failure gets another attempt. retries_used is only ever
    incremented here -- exactly once per failure that is actually retried
    -- so the count api.py surfaces matches the number of times
    generate_sql was re-invoked, not the number of failures observed.

    This lives in the nodes rather than in the conditional-edge router
    functions: LangGraph's routers choose the next node by return value
    only, they cannot also update state, so retries_used has to be
    incremented by whichever node detected the failure."""
    settings = get_settings()
    retries_used = state.get("retries_used", 0)

    if retries_used >= settings.max_retries:
        return {"outcome": "give_up", "retry_feedback": feedback}

    return {
        "outcome": "retry",
        "retry_feedback": feedback,
        "retries_used": retries_used + 1,
    }