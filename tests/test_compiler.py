"""Spec for the Intent -> SQL compiler (src/compiler/intent_compiler.py).

Owner of the module under test: Ajinkya. These tests are the executable spec,
written before the body, so the contract is fixed by something that runs rather
than by a docstring that drifts.

Every test is marked xfail(raises=NotImplementedError). While the compiler is a
stub they report as expected failures and the suite stays green; the moment the
body lands they flip to XPASS, and any that flip to a real FAIL are the ones
worth reading. Remove the marker once the compiler is in.

Offline: schema comes from tests/schema_snapshot.json. No BigQuery, no LLM.

The last test is the one that matters most -- it asserts the compiler's output
survives the five existing guardrails untouched on src/guardrails.py. "The
deterministic output still goes through the same checks" is the answer to "how
do you know your compiler is safe", and it should be enforced, not asserted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import sqlglot
from sqlglot import exp

from src.compiler.intent_compiler import (
    SemanticCompileError,
    UnsupportedIntent,
    compile as compile_intent,
    resolve_join_path,
)
from src.schema import Column, SchemaContext, Table
from src.semantic import model as M
from src.semantic.intent import Intent

SNAPSHOT = Path(__file__).parent / "schema_snapshot.json"


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


class _Settings:
    max_rows = 500


def _parse(sql: str) -> exp.Select:
    return sqlglot.parse_one(sql, dialect="bigquery")


# ---- unsupported is a first-class outcome, not a failure

def test_unsupported_intent_raises_unsupported_not_compile_error(model):
    intent = Intent(unsupported=True, unsupported_reason="needs a grouped subquery")
    with pytest.raises(UnsupportedIntent) as e:
        compile_intent(intent, model, _Settings())
    assert "grouped subquery" in str(e.value)


# ---- the property the whole layer exists for

def test_unknown_measure_fails_to_compile(model):
    """Hallucination is structurally impossible, not caught after the fact."""
    intent = Intent(measure="customer_lifetime_value")
    with pytest.raises(SemanticCompileError):
        compile_intent(intent, model, _Settings())


def test_unknown_measure_error_names_the_allowed_set(model):
    intent = Intent(measure="revenu")  # plausible typo
    with pytest.raises(SemanticCompileError) as e:
        compile_intent(intent, model, _Settings())
    assert "total_revenue" in str(e.value), "the error must show what was available"


def test_unknown_dimension_fails_to_compile(model):
    intent = Intent(measure="total_revenue", dimensions=["acquisition_channel"])
    with pytest.raises(SemanticCompileError):
        compile_intent(intent, model, _Settings())


def test_filter_on_unknown_dimension_fails_to_compile(model):
    intent = Intent(
        measure="total_revenue",
        filters=[{"field": "not_a_dimension", "operator": "=", "value": "x"}],
    )
    with pytest.raises(SemanticCompileError):
        compile_intent(intent, model, _Settings())


# ---- basic compilation

def test_simple_measure_compiles(model):
    out = compile_intent(Intent(measure="total_revenue"), model, _Settings())
    tree = _parse(out.sql)
    assert isinstance(tree, exp.Select)
    assert out.measure == "total_revenue"
    assert out.base_entity == "order_items"


def test_limit_is_always_emitted(model):
    """Non-negotiable: guardrails._check_row_limit REJECTS a missing LIMIT
    rather than injecting one, so a compiled query without one is blocked by
    our own guardrail."""
    out = compile_intent(Intent(measure="total_revenue"), model, _Settings())
    assert _parse(out.sql).args.get("limit") is not None


def test_intent_limit_is_respected(model):
    out = compile_intent(Intent(measure="total_revenue", limit=5), model, _Settings())
    assert "5" in _parse(out.sql).args["limit"].sql()


def test_dimension_produces_group_by(model):
    out = compile_intent(
        Intent(measure="total_revenue", dimensions=["product_category"]),
        model,
        _Settings(),
    )
    assert _parse(out.sql).args.get("group") is not None


def test_dimensions_are_projected_before_the_measure(model):
    """The eval metric compares columns POSITIONALLY, so projection order is
    part of the contract, not a formatting preference."""
    out = compile_intent(
        Intent(measure="total_revenue", dimensions=["product_category"]),
        model,
        _Settings(),
    )
    aliases = [e.alias_or_name for e in _parse(out.sql).expressions]
    assert aliases[0] == "product_category"
    assert aliases[-1] == "total_revenue"


# ---- join resolution from the declared graph

def test_cross_entity_query_resolves_a_join(model):
    """Revenue lives on order_items, category on products."""
    out = compile_intent(
        Intent(measure="total_revenue", dimensions=["product_category"]),
        model,
        _Settings(),
    )
    assert out.join_path, "expected a resolved join edge"
    assert any("products" in e.path for e in out.join_path)
    assert _parse(out.sql).args.get("joins")


def test_join_path_is_reported_for_the_audit_envelope(model):
    out = compile_intent(
        Intent(measure="total_revenue", dimensions=["product_category"]),
        model,
        _Settings(),
    )
    assert out.proven_join_path(), "proven_join_path backs an audit claim"


def test_single_entity_query_needs_no_join(model):
    out = compile_intent(
        Intent(measure="user_count", dimensions=["user_country"]), model, _Settings()
    )
    assert out.join_path == ()


def test_unreachable_entity_raises_rather_than_cross_joining():
    """A cartesian product answers the question with a number that is wrong
    rather than absent, which is strictly worse.

    Uses a hand-built model with a genuinely disconnected entity rather than
    the real semantic_model.yaml -- every entity there is reachable from every
    other one via a chain of joins (products -> order_items -> users ->
    events), so there is no actual unreachable pair to test against it.
    """
    isolated_model = M.SemanticModel(
        version="test",
        dialect="bigquery",
        entities=(
            M.Entity(name="products", table="p.d.products", primary_key="id"),
            M.Entity(name="orphan", table="p.d.orphan", primary_key="id"),
        ),
        measures=(),
        dimensions=(),
        joins=(),
    )
    with pytest.raises(SemanticCompileError):
        resolve_join_path(isolated_model, "products", {"products", "orphan"})


# ---- the mandatory GA partition bound

def test_ga_measure_without_time_range_fails_to_compile(model):
    """Unbounded GA is 5.77 GB against a 1 GB ceiling. The bound is a
    compile-time requirement, not a runtime check."""
    with pytest.raises(SemanticCompileError):
        compile_intent(Intent(measure="session_count"), model, _Settings())


def test_ga_measure_with_time_range_emits_table_suffix(model):
    out = compile_intent(
        Intent(
            measure="session_count",
            time_range={"start": "20160801", "end": "20160831"},
        ),
        model,
        _Settings(),
    )
    assert "_TABLE_SUFFIX" in out.sql
    assert "20160801" in out.sql and "20160831" in out.sql


def test_nested_ga_dimension_keeps_its_path(model):
    out = compile_intent(
        Intent(
            measure="session_count",
            dimensions=["ga_traffic_source"],
            time_range={"start": "20160801", "end": "20160831"},
        ),
        model,
        _Settings(),
    )
    assert "trafficSource.source" in out.sql


# ---- values are literals, not interpolated text

def test_filter_value_is_a_literal_not_string_concatenation(model):
    """A filter value is untrusted input. If this compiles to a broken or
    injected query, the AST work in Layer 2 is protecting nothing."""
    nasty = "x' OR 1=1 --"
    out = compile_intent(
        Intent(
            measure="user_count",
            filters=[{"field": "user_country", "operator": "=", "value": nasty}],
        ),
        model,
        _Settings(),
    )
    tree = _parse(out.sql)  # must still parse as a single SELECT
    assert isinstance(tree, exp.Select)
    literals = [
        lit.this for lit in tree.find_all(exp.Literal) if lit.is_string
    ]
    assert nasty in literals, "value must survive as one literal, not as SQL"


# ---- dialect is configuration

def test_dialect_comes_from_the_model(model):
    assert model.dialect == "bigquery"
    out = compile_intent(Intent(measure="total_revenue"), model, _Settings())
    assert out.sql  # renders under the model's dialect, not a hardcoded one


# ---- the integration that matters

@pytest.mark.parametrize(
    "intent",
    [
        Intent(measure="total_revenue", dimensions=["product_category"], limit=5),
        Intent(measure="user_count", dimensions=["user_traffic_source"]),
        Intent(measure="order_count", dimensions=["order_status"]),
        Intent(
            measure="session_count",
            dimensions=["ga_device_category"],
            time_range={"start": "20160801", "end": "20160831"},
        ),
    ],
    ids=["revenue_by_category", "users_by_source", "orders_by_status", "ga_by_device"],
)
def test_compiled_sql_passes_the_existing_guardrails(intent, model, schema):
    """Compiler output must survive the five checks on src/guardrails.py,
    which this build does not modify.

    Cost dry-run is excluded here -- it needs BigQuery. The other four are pure
    and run offline.
    """
    from src.guardrails import check

    out = compile_intent(intent, model, _Settings())
    report = check(out.sql, schema)
    assert report.passed, report.violations
