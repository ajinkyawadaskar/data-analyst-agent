"""The Semantic Execution Gateway graph, parallel to src/graph.py.

    question -> extract_intent -> compile -> guard -> execute -> explain
                      |              |
                      |              +-- unsupported --> generate_sql (legacy)
                      +-- unsupported ------------------> generate_sql (legacy)

WHY THIS IS A SEPARATE MODULE RATHER THAN AN EDIT TO graph.py
-------------------------------------------------------------
The point of the feature flag is a genuine A/B: same questions, same model, one
variable. That only holds if the old path is byte-identical while the new one
runs. Editing graph.py to add a branch would make "the old path still works" a
claim rather than a fact.

It is also free to do it this way. graph.py's node builders are already pure
factories with no module-level state -- _make_generate_sql, _make_guard,
_execute_node and _make_explain are imported and reused verbatim below. The
guardrail layer on the compiled path is not a reimplementation; it is literally
the same function object.

TWO ROUTING DECISIONS WORTH DEFENDING
-------------------------------------
1. The compiled path does NOT retry on a guardrail failure.
   On the legacy path a guard rejection means the model wrote bad SQL, and
   regenerating with the violation as feedback is reasonable. On the compiled
   path the model did not write the SQL -- the compiler did, from a validated
   intent against a schema-checked model. A guardrail violation there is a
   COMPILER DEFECT. Retrying would burn quota re-deriving the same intent and
   producing the same SQL, and would hide the defect behind an eventual
   give_up. So it routes to a terminal `halt` node and logs at ERROR. The
   failure should be loud, because it means one of two things we assert is
   false.

2. The compiled path keeps the guardrails.
   It would be defensible to argue the compiler makes them redundant -- the SQL
   is built from a certified model, so it cannot reference an unknown column.
   We keep them anyway, because "the deterministic output still passes the same
   five checks" is the evidence for that argument rather than a restatement of
   it. Belt and braces, where the braces are the thing being tested.

Owner: Claude. The compiler this calls into is Ajinkya's
(src/compiler/intent_compiler.py).
"""

from __future__ import annotations

import logging

from langgraph.graph import END, StateGraph

from src.compiler.intent_compiler import (
    CompiledQuery,
    SemanticCompileError,
    UnsupportedIntent,
    compile as compile_intent,
)
from src.compiler.security import SecurityContextError, inject_security_context
from src.config import get_settings
from src.graph import (
    AgentState,
    _apply_retry_policy,
    _build_llm,
    _execute_node,
    _make_explain,
    _make_generate_sql,
    _make_guard,
)
from src.schema import SchemaContext, compact, load_or_introspect
from src.semantic import model as semantic_model
from src.semantic.intent import Intent, build_prompt, parse_intent

log = logging.getLogger(__name__)

# Matches graph.py. The legacy fallback node shares the compaction strategy so
# that a question routed to it behaves exactly as it would on main.
_SCHEMA_COMPACTION_STRATEGY = "column_sample"

ROUTE_COMPILED = "compiled"
ROUTE_FALLBACK = "fallback"


class SemanticAgentState(AgentState, total=False):
    """Extends AgentState rather than widening it.

    graph.py's nodes all return `{**state, ...}`, so the extra keys below flow
    through the reused nodes untouched. Widening the original TypedDict would
    have meant editing the module we are deliberately not touching.
    """

    # Simulated identity (see src/models.py::OnBehalfOf). None means no
    # identity was supplied and no row-level restriction applies -- Layer 2
    # is opt-in per request, not a mode switch on the whole gateway.
    principal: dict | None
    intent: dict | None
    intent_raw: str | None
    route_taken: str
    compiled_sql: str | None
    proven_join_path: list[str]
    base_entity: str | None
    measure: str | None
    semantic_model_version: str
    unsupported_reason: str | None
    compile_error: str | None
    halt_reason: str | None


# ==========================================================================
# Node: extract_intent
# ==========================================================================
def _make_extract_intent(llm, model: semantic_model.SemanticModel):
    """Ask the model for Intent JSON instead of SQL.

    A malformed response IS retryable -- unlike a compile failure, this is the
    model getting something wrong, and the violation makes useful feedback. So
    this node uses the same _apply_retry_policy as the legacy path.
    """

    def extract_intent(state: SemanticAgentState) -> SemanticAgentState:
        prompt = build_prompt(state["question"], model)
        # The question is already embedded in `prompt`, but the Gemini API
        # requires at least one non-system turn in `contents` -- a
        # system-only message list is rejected outright ("contents are
        # required") before the model ever sees the question. Mirrors
        # graph.py's generate_sql, which sends the same system/human split.
        messages = [("system", prompt), ("human", state["question"])]
        feedback = state.get("retry_feedback")
        if feedback:
            messages.append(
                (
                    "human",
                    f"Your previous response was rejected:\n{feedback}\n"
                    f"Return only a valid Semantic Intent JSON object.",
                )
            )

        response = llm.invoke(messages)
        raw = response.text.strip()

        try:
            intent = parse_intent(raw)
        except Exception as exc:  # noqa: BLE001 - malformed intent is retryable
            return {
                **state,
                "intent_raw": raw[:1000],
                **_apply_retry_policy(
                    state, f"That was not a valid Semantic Intent: {exc}"
                ),
            }

        return {
            **state,
            "intent": intent.model_dump(exclude_none=True, mode="json"),
            "intent_raw": raw[:1000],
            "semantic_model_version": model.version,
            "outcome": "success",
            "retry_feedback": None,
        }

    return extract_intent


def _route_after_extract(state: SemanticAgentState) -> str:
    if state.get("outcome") == "success":
        intent = state.get("intent") or {}
        if intent.get("unsupported"):
            # Not a failure. Two questions in the eval set need an aggregate
            # over a grouped subquery and are honestly out of model.
            return "fallback"
        return "compile"
    return state.get("outcome") if state.get("outcome") == "retry" else "give_up"


# ==========================================================================
# Node: compile
# ==========================================================================
def _make_compile(model: semantic_model.SemanticModel, settings: object):
    def compile_node(state: SemanticAgentState) -> SemanticAgentState:
        intent = Intent.model_validate(state["intent"])

        try:
            out: CompiledQuery = compile_intent(intent, model, settings)
        except UnsupportedIntent as exc:
            return {
                **state,
                "unsupported_reason": str(exc),
                "outcome": "fallback",
            }
        except NotImplementedError:
            # The compiler body is Ajinkya's and not written yet. Reported the
            # same way src/api.py reports a missing graph: distinctly, so it
            # reads as "not built" rather than "broken".
            log.error("intent_compiler.compile() is still a stub")
            return {
                **state,
                "compile_error": "compiler not implemented",
                "outcome": "give_up",
            }
        except SemanticCompileError as exc:
            # A name outside the certified model, an unreachable join, or a
            # missing mandatory partition bound. Not retryable: the intent was
            # already structurally valid, so re-asking the model produces the
            # same JSON. This is the layer working as intended.
            log.info("compilation refused: %s", exc)
            return {
                **state,
                "compile_error": str(exc),
                "outcome": "give_up",
            }

        expression = out.expression
        principal = state.get("principal")
        if principal is not None:
            # Opt-in: a request with no on_behalf_of compiles exactly as it
            # did before Layer 2 existed. See src/compiler/security.py's
            # module docstring for the full contract.
            try:
                expression = inject_security_context(
                    expression, principal.get("tenant_id"), principal.get("region"), model
                )
            except NotImplementedError:
                log.error("security.inject_security_context() is still a stub")
                return {
                    **state,
                    "compile_error": "security layer not implemented",
                    "outcome": "give_up",
                }
            except SecurityContextError as exc:
                # Denied, not emptied -- see security.py step 4. Not
                # retryable: re-asking the model cannot change who is asking.
                log.info("access denied by row policy: %s", exc)
                return {
                    **state,
                    "compile_error": f"access denied: {exc}",
                    "outcome": "give_up",
                }
            out.sql = expression.sql(dialect=model.dialect, pretty=True)

        return {
            **state,
            "sql": out.sql,
            "compiled_sql": out.sql,
            "proven_join_path": out.proven_join_path(),
            "base_entity": out.base_entity,
            "measure": out.measure,
            "route_taken": ROUTE_COMPILED,
            "outcome": "success",
        }

    return compile_node


def _route_after_compile(state: SemanticAgentState) -> str:
    outcome = state.get("outcome")
    if outcome == "success":
        return "guard"
    if outcome == "fallback":
        return "fallback"
    return "give_up"


# ==========================================================================
# Node: legacy fallback marker
# ==========================================================================
def _make_fallback(generate_sql):
    """Wraps graph.py's generate_sql so the audit envelope can say the answer
    came from the legacy path.

    Without this marker a fallback answer would be indistinguishable from a
    compiled one in the response, which would quietly inflate the coverage
    number -- the exact thing the coverage/accuracy split exists to prevent.
    """

    def fallback(state: SemanticAgentState) -> SemanticAgentState:
        log.info("falling back to the legacy LLM path: %s",
                 state.get("unsupported_reason") or "intent marked unsupported")
        out = generate_sql(state)
        return {**out, "route_taken": ROUTE_FALLBACK}

    return fallback


# ==========================================================================
# Routing after the shared guard / execute nodes
# ==========================================================================
def _halt(state: SemanticAgentState) -> SemanticAgentState:
    """Terminal node that stamps why the run stopped.

    Needed because a LangGraph router chooses the next node by return value and
    CANNOT also update state (the same constraint graph.py documents in
    _apply_retry_policy). So when the router below decides a compiled-path
    guard failure is fatal, the state it inherits still says outcome="retry"
    from the guard node -- and a run that has actually terminated would report
    itself as mid-retry to anything reading the result, including Layer 6's
    audit envelope. This node exists to make the recorded outcome match what
    happened.
    """
    if state.get("compile_error"):
        reason = f"compilation refused: {state['compile_error']}"
    elif state.get("bq_error"):
        # Checked before the compiled-route guardrails branch below: a live
        # BigQuery dry-run exception is merged into the same `guardrails`
        # report shape (via cost_guard's caught-exception path in graph.py's
        # guard node) with an EMPTY violations list, since nothing in
        # guardrails.py itself rejected the SQL. Checking route+guardrails
        # first would misreport a genuine BigQuery-side rejection (e.g. a
        # malformed nested-field reference) as "compiled SQL failed
        # guardrails: []" -- true about the empty list, false about the cause.
        reason = f"BigQuery rejected the query: {state['bq_error']}"
    elif state.get("route_taken") == ROUTE_COMPILED and state.get("guardrails"):
        violations = getattr(state["guardrails"], "violations", None)
        reason = f"compiler defect -- compiled SQL failed guardrails: {violations}"
    elif state.get("guardrails") is not None:
        reason = f"guardrails blocked the query: " \
                 f"{getattr(state['guardrails'], 'violations', None)}"
    else:
        reason = state.get("retry_feedback") or "retries exhausted"
    return {**state, "outcome": "give_up", "halt_reason": reason}


def _route_after_guard(state: SemanticAgentState) -> str:
    """Same guard node, different retry policy per route.

    See decision 1 in the module docstring: a guardrail violation on compiled
    SQL is a compiler defect, and retrying would hide it.
    """
    outcome = state.get("outcome")
    if outcome == "success":
        return "execute"
    if outcome == "retry":
        if state.get("route_taken") == ROUTE_COMPILED:
            log.error(
                "COMPILER DEFECT: compiled SQL failed guardrails -- %s | sql=%s",
                getattr(state.get("guardrails"), "violations", None),
                state.get("sql"),
            )
            return "halt"
        return "fallback"
    return "halt"


def _route_after_execute(state: SemanticAgentState) -> str:
    outcome = state.get("outcome")
    if outcome == "success":
        return "explain"
    if outcome == "retry" and state.get("route_taken") != ROUTE_COMPILED:
        return "fallback"
    return "halt"


# ==========================================================================
# Build
# ==========================================================================
def build_graph(
    *,
    schema_context: SchemaContext | None = None,
    model: semantic_model.SemanticModel | None = None,
    llm=None,
    settings: object | None = None,
):
    """Build the gateway graph.

    Every dependency is injectable so tests can build the graph without
    BigQuery or an API key, and so the caller decides when introspection
    happens rather than having it fire as an import side effect.

    Note this uses schema.load_or_introspect() -- the DISK-CACHED loader --
    where graph.py calls introspect() directly and pays ~10 BigQuery calls per
    build. That is a live bug on main (see LEARNING.md); this module does not
    inherit it.

    Raises:
        SemanticModelError: semantic_model.yaml does not match the live schema.
            Deliberately fatal. Failing to start is correct here -- a model
            that references a column the warehouse does not have would silently
            disable column validation downstream.
    """
    settings = settings or get_settings()

    # TWO schema contexts, and the split is load-bearing.
    #
    # `schema_full` is the real thing: 338 GA columns. `schema_prompt` is it
    # compacted to fit a context window -- "column_sample" keeps 25 columns per
    # table and drops RECORDs, so GA loses 313 of them.
    #
    # graph.py uses the compacted one for BOTH the prompt and the guardrail,
    # and that is correct there: the legacy model can only see compacted
    # columns, so a reference to anything outside that set really is a
    # hallucination.
    #
    # It is NOT correct here. The compiler is bounded by the semantic model,
    # not by the prompt, and the semantic model legitimately references columns
    # compaction dropped -- device.deviceCategory and geoNetwork.country among
    # them. Validating against the compacted context rejected them as
    # hallucinated, which is how this was found: /health went degraded and the
    # loader named all three. Compaction is a prompt-budget concern; it is not
    # a statement about what exists.
    schema_full = schema_context or load_or_introspect()
    schema_prompt = compact(schema_full, _SCHEMA_COMPACTION_STRATEGY)

    model = model or semantic_model.load(
        settings.semantic_model_path, schema_context=schema_full
    )
    llm = llm or _build_llm(settings)

    # The legacy fallback gets the compacted schema, so a question routed to it
    # behaves exactly as it would on main. The guard gets the full one, so it
    # judges compiled SQL against what the warehouse actually has.
    generate_sql = _make_generate_sql(llm, schema_prompt)

    graph = StateGraph(SemanticAgentState)
    graph.add_node("extract_intent", _make_extract_intent(llm, model))
    graph.add_node("compile", _make_compile(model, settings))
    graph.add_node("fallback", _make_fallback(generate_sql))
    graph.add_node("guard", _make_guard(schema_full, settings))
    graph.add_node("execute", _execute_node)
    graph.add_node("explain", _make_explain(llm))
    graph.add_node("halt", _halt)

    graph.set_entry_point("extract_intent")

    graph.add_conditional_edges(
        "extract_intent",
        _route_after_extract,
        {
            "compile": "compile",
            "fallback": "fallback",
            "retry": "extract_intent",
            "give_up": END,
        },
    )
    graph.add_conditional_edges(
        "compile",
        _route_after_compile,
        {"guard": "guard", "fallback": "fallback", "give_up": END},
    )
    graph.add_edge("fallback", "guard")
    graph.add_conditional_edges(
        "guard",
        _route_after_guard,
        {"execute": "execute", "fallback": "fallback", "halt": "halt"},
    )
    graph.add_conditional_edges(
        "execute",
        _route_after_execute,
        {"explain": "explain", "fallback": "fallback", "halt": "halt"},
    )
    graph.add_edge("explain", END)
    graph.add_edge("halt", END)

    return graph.compile()
