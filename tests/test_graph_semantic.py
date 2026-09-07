"""Wiring tests for the Semantic Execution Gateway graph.

Fully offline: a stub LLM returns canned responses, the schema comes from
tests/schema_snapshot.json, and the compiler is monkeypatched. No API key, no
BigQuery, no quota spend -- which matters, because the routing decisions here
are exactly the sort of thing you want to test hundreds of times.

What is being tested is ROUTING, not answers: does an unsupported intent reach
the legacy path, does a compile refusal stop rather than retry, does a guardrail
violation on compiled SQL fail loudly instead of looping.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.schema import Column, SchemaContext, Table
from src.semantic import model as M

SNAPSHOT = Path(__file__).parent / "schema_snapshot.json"


class StubResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class StubLLM:
    """Returns queued responses in order, recording what it was asked."""

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(messages)
        if not self._responses:
            raise AssertionError("StubLLM ran out of queued responses")
        return StubResponse(self._responses.pop(0))


class StubSettings:
    max_rows = 500
    max_retries = 2
    semantic_model_path = "semantic_model.yaml"
    llm_model = "stub"
    cache_ttl_seconds = 300


@pytest.fixture(scope="session")
def schema() -> SchemaContext:
    raw = json.loads(SNAPSHOT.read_text())
    tables = tuple(
        Table(
            project=t["project"],
            dataset=t["dataset"],
            name=t["name"],
            num_rows=t.get("num_rows"),
            columns=tuple(Column(**c) for c in t["columns"]),
        )
        for t in raw["tables"]
    )
    return SchemaContext(tables=tables)


@pytest.fixture(scope="session")
def model(schema) -> M.SemanticModel:
    return M.load(schema_context=schema)


@pytest.fixture(autouse=True)
def _no_real_settings(monkeypatch):
    """_apply_retry_policy calls get_settings() directly, so stub it at source."""
    monkeypatch.setattr("src.graph.get_settings", lambda: StubSettings())


def _build(llm, model, schema, **kw):
    from src.cache.session_store import SessionStore
    from src.graph_semantic import build_graph

    # A fresh, in-memory SessionStore per build -- never the real
    # data/session_store.db. Without this, "fully offline" (per this file's
    # own module docstring) would be untrue the moment any test's run
    # reaches a real cache write, and repeated test runs would silently
    # accumulate real cache entries on disk.
    return build_graph(
        schema_context=schema,
        model=model,
        llm=llm,
        settings=StubSettings(),
        session_store=SessionStore(":memory:"),
        **kw,
    )


# ---- the graph assembles at all

def test_graph_builds_without_bigquery_or_api_key(model, schema):
    assert _build(StubLLM(), model, schema) is not None


def test_building_does_not_touch_the_legacy_graph(model, schema):
    """The old path must stay byte-identical -- that is what makes the feature
    flag an A/B rather than a claim."""
    import src.graph as legacy

    before = legacy.build_graph
    _build(StubLLM(), model, schema)
    assert legacy.build_graph is before


# ---- routing: unsupported intent falls back to the legacy path

def test_unsupported_intent_routes_to_legacy_path(model, schema, monkeypatch):
    from src.graph_semantic import ROUTE_FALLBACK

    llm = StubLLM(
        '{"unsupported": true, "unsupported_reason": "needs a grouped subquery"}',
        "SELECT 1 AS x LIMIT 1",  # the legacy generate_sql response
    )
    seen = {}

    def fake_guard_factory(schema_context, settings):
        def guard(state):
            seen.update(state)
            return {**state, "outcome": "give_up"}
        return guard

    monkeypatch.setattr("src.graph_semantic._make_guard", fake_guard_factory)

    graph = _build(llm, model, schema)
    out = graph.invoke({"question": "What is the average order value?"})

    assert out["route_taken"] == ROUTE_FALLBACK
    assert seen.get("sql") == "SELECT 1 AS x LIMIT 1"


def test_fallback_is_distinguishable_in_the_result(model, schema, monkeypatch):
    """A fallback answer must not be countable as a compiled one -- that would
    inflate the coverage number."""
    from src.graph_semantic import ROUTE_COMPILED

    llm = StubLLM(
        '{"unsupported": true, "unsupported_reason": "out of model"}',
        "SELECT 1 AS x LIMIT 1",
    )
    monkeypatch.setattr(
        "src.graph_semantic._make_guard",
        lambda s, st: (lambda state: {**state, "outcome": "give_up"}),
    )
    out = _build(llm, model, schema).invoke({"question": "q"})
    assert out.get("route_taken") != ROUTE_COMPILED


# ---- routing: malformed intent IS retried (the model got it wrong)

def test_malformed_intent_is_retried(model, schema, monkeypatch):
    llm = StubLLM(
        "I think you want revenue?",           # not JSON
        '{"measure": "total_revenue"}',        # recovers
    )
    monkeypatch.setattr(
        "src.graph_semantic.compile_intent",
        lambda intent, m, s: (_ for _ in ()).throw(NotImplementedError()),
    )
    out = _build(llm, model, schema).invoke({"question": "revenue?"})
    assert len(llm.calls) == 2, "a malformed intent should be re-asked"
    assert out.get("retries_used") == 1


def test_malformed_intent_gives_up_after_max_retries(model, schema, monkeypatch):
    llm = StubLLM("nope", "still nope", "nope again", "and again")
    out = _build(llm, model, schema).invoke({"question": "q"})
    assert out["outcome"] == "give_up"
    assert out["retries_used"] == StubSettings.max_retries


# ---- routing: a compile refusal STOPS, it does not retry

def test_compile_refusal_does_not_retry(model, schema, monkeypatch):
    """The intent was already structurally valid, so re-asking produces the
    same JSON. Retrying would just burn quota."""
    from src.compiler.intent_compiler import SemanticCompileError

    llm = StubLLM('{"measure": "total_revenue"}')

    def refuse(intent, m, s):
        raise SemanticCompileError("unknown measure 'x'", allowed=["total_revenue"])

    monkeypatch.setattr("src.graph_semantic.compile_intent", refuse)

    out = _build(llm, model, schema).invoke({"question": "q"})
    assert out["outcome"] == "give_up"
    assert len(llm.calls) == 1, "a compile refusal must not re-invoke the model"
    assert "unknown measure" in out["compile_error"]


def test_compile_error_is_reported_not_a_crash(model, schema, monkeypatch):
    """A compile refusal should surface as a readable compile_error, not an
    opaque crash -- whether the compiler is still a stub or fully written."""
    from src.compiler.intent_compiler import SemanticCompileError

    llm = StubLLM('{"measure": "not_a_real_measure"}')
    out = _build(llm, model, schema).invoke({"question": "q"})
    assert out["outcome"] == "give_up"
    assert "compile_error" in out
    assert "not_a_real_measure" in out["compile_error"]


# ---- routing: a guardrail violation on COMPILED sql is a defect, not a retry

def test_guardrail_violation_on_compiled_sql_does_not_loop(model, schema, monkeypatch):
    """Decision 1 in the module docstring. The model did not write this SQL,
    so regenerating the intent cannot fix it."""
    from src.compiler.intent_compiler import CompiledQuery
    import sqlglot

    llm = StubLLM('{"measure": "total_revenue"}')

    def fake_compile(intent, m, s):
        sql = "SELECT 1 AS x LIMIT 1"
        return CompiledQuery(
            sql=sql,
            expression=sqlglot.parse_one(sql, dialect="bigquery"),
            base_entity="order_items",
            measure="total_revenue",
        )

    def failing_guard_factory(schema_context, settings):
        def guard(state):
            return {**state, "outcome": "retry", "retry_feedback": "nope"}
        return guard

    monkeypatch.setattr("src.graph_semantic.compile_intent", fake_compile)
    monkeypatch.setattr("src.graph_semantic._make_guard", failing_guard_factory)

    out = _build(llm, model, schema).invoke({"question": "q"})
    assert out["outcome"] == "give_up", "a terminated run must not report as retrying"
    assert out["halt_reason"]
    assert len(llm.calls) == 1, "compiled path must not re-invoke the model"


def test_compiled_path_records_provenance(model, schema, monkeypatch):
    """These fields back the audit envelope in Layer 6."""
    from src.compiler.intent_compiler import CompiledQuery, JoinEdge
    from src.graph_semantic import ROUTE_COMPILED
    import sqlglot

    llm = StubLLM('{"measure": "total_revenue", "dimensions": ["product_category"]}')
    sql = "SELECT 1 AS x LIMIT 1"
    edge = JoinEdge(
        name="order_items_to_products",
        relationship="many_to_one",
        path="order_items.product_id = products.id",
        left_entity="order_items",
        right_entity="products",
    )

    monkeypatch.setattr(
        "src.graph_semantic.compile_intent",
        lambda i, m, s: CompiledQuery(
            sql=sql,
            expression=sqlglot.parse_one(sql, dialect="bigquery"),
            base_entity="order_items",
            measure="total_revenue",
            dimensions=("product_category",),
            join_path=(edge,),
        ),
    )
    monkeypatch.setattr(
        "src.graph_semantic._make_guard",
        lambda s, st: (lambda state: {**state, "outcome": "give_up"}),
    )

    out = _build(llm, model, schema).invoke({"question": "q"})
    assert out["route_taken"] == ROUTE_COMPILED
    assert out["proven_join_path"] == ["order_items.product_id = products.id"]
    assert out["semantic_model_version"] == model.version
    assert out["measure"] == "total_revenue"


# ---- the prompt is built from the model, not hardcoded

def test_extraction_prompt_lists_the_certified_measures(model, schema):
    llm = StubLLM('{"measure": "total_revenue"}')
    _build(llm, model, schema).invoke({"question": "q"})
    system_prompt = llm.calls[0][0][1]
    assert "total_revenue" in system_prompt
    assert "session_count" in system_prompt
    assert "customer_lifetime_value" not in system_prompt
