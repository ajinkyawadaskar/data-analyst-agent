"""Tests for semantic_model.yaml loading and validation.

Offline: validates against tests/schema_snapshot.json, a recorded snapshot of
real BigQuery introspection. No credentials, no network, no LLM.

The negative cases matter more than the positive one. A validator that only
ever passes is decoration; these assert it actually fails, and specifically
that it catches the failure that would otherwise punch a hole in the column
guardrail one layer down (see src/semantic/model.py's module docstring).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.schema import Column, SchemaContext, Table
from src.semantic import model as M

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
def model() -> M.SemanticModel:
    return M.load(schema_context=None)


# ---- the shipped model is sound

def test_shipped_model_validates_against_real_schema(model, schema):
    assert M.validate(model, schema) == []


def test_shipped_model_covers_both_domains(model):
    tables = {e.table for e in model.entities}
    assert any("thelook_ecommerce" in t for t in tables)
    assert any("google_analytics_sample" in t for t in tables)


# ---- parsing conventions

def test_source_splits_on_first_dot_only():
    """GA nested paths must survive: ga_sessions.totals.hits is one column."""
    assert M._split_source("users.region") == ("users", "region")
    assert M._split_source("ga_sessions.totals.hits") == ("ga_sessions", "totals.hits")


def test_nested_ga_dimension_keeps_full_path(model):
    d = model.dimension("ga_traffic_source")
    assert d.entity == "ga_sessions"
    assert d.column == "trafficSource.source"


def test_ga_partition_is_required(model):
    """An unbounded GA scan is 5.77 GB against a 1 GB ceiling, so the date
    bound is a compile-time requirement, not a runtime check."""
    ga = model.entity("ga_sessions")
    assert ga.partition is not None
    assert ga.partition.required is True
    assert ga.partition.column == "_TABLE_SUFFIX"


# ---- the validator actually fails

def test_unknown_table_is_rejected(schema, model):
    broken = M.SemanticModel(
        version=model.version,
        dialect=model.dialect,
        entities=(M.Entity(name="ghost", table="bigquery-public-data.nope.missing",
                           primary_key="id"),),
        measures=(),
        dimensions=(),
        joins=(),
    )
    problems = M.validate(broken, schema)
    assert problems
    assert any("not in the introspected schema" in p for p in problems)


def test_typo_in_table_name_does_not_silently_pass(schema, model):
    """The failure this validator exists for.

    SchemaContext.columns_for() returns an EMPTY SET for an unknown table, and
    guardrails._schema_columns_for treats that as "unknown table, skip column
    validation". Without this check a one-character typo here would disable the
    column guardrail for every query compiled against that table.
    """
    users = model.entity("users")
    typo = M.SemanticModel(
        version=model.version,
        dialect=model.dialect,
        entities=(M.Entity(name="users", table=users.table + "s", primary_key="id"),),
        measures=(M.Measure(name="user_count", base_entity="users", formula="COUNT(*)"),),
        dimensions=(),
        joins=(),
    )
    problems = M.validate(typo, schema)
    assert problems, "a typo'd table name must not validate"


def test_hallucinated_measure_column_is_rejected(schema, model):
    broken = M.SemanticModel(
        version=model.version,
        dialect=model.dialect,
        entities=(model.entity("order_items"),),
        measures=(M.Measure(name="bogus", base_entity="order_items",
                            formula="SUM(customer_lifetime_value)"),),
        dimensions=(),
        joins=(),
    )
    problems = M.validate(broken, schema)
    assert any("customer_lifetime_value" in p for p in problems)


def test_hallucinated_dimension_column_is_rejected(schema, model):
    broken = M.SemanticModel(
        version=model.version,
        dialect=model.dialect,
        entities=(model.entity("users"),),
        measures=(),
        dimensions=(M.Dimension(name="region", type="string", entity="users",
                                column="region"),),
        joins=(),
    )
    problems = M.validate(broken, schema)
    assert any("region" in p for p in problems)


def test_join_on_nonexistent_column_is_rejected(schema, model):
    broken = M.SemanticModel(
        version=model.version,
        dialect=model.dialect,
        entities=(model.entity("orders"), model.entity("users")),
        measures=(),
        dimensions=(),
        joins=(M.Join(name="bad", relationship="many_to_one",
                      path="orders.user_id = users.user_id",
                      left_entity="orders", right_entity="users"),),
    )
    problems = M.validate(broken, schema)
    assert any("user_id" in p and "users" in p for p in problems)


def test_unparseable_formula_is_reported(schema, model):
    broken = M.SemanticModel(
        version=model.version,
        dialect=model.dialect,
        entities=(model.entity("orders"),),
        measures=(M.Measure(name="bad", base_entity="orders",
                            formula="SUM(((("),),
        dimensions=(),
        joins=(),
    )
    problems = M.validate(broken, schema)
    assert any("does not parse" in p for p in problems)


def test_error_lists_every_problem_not_just_the_first(schema, model):
    broken = M.SemanticModel(
        version=model.version,
        dialect=model.dialect,
        entities=(model.entity("orders"),),
        measures=(
            M.Measure(name="a", base_entity="orders", formula="SUM(nope_one)"),
            M.Measure(name="b", base_entity="orders", formula="SUM(nope_two)"),
        ),
        dimensions=(),
        joins=(),
    )
    problems = M.validate(broken, schema)
    assert len(problems) >= 2
    err = M.SemanticModelError(problems)
    assert "nope_one" in str(err) and "nope_two" in str(err)


def test_pseudo_columns_are_allowed(schema, model):
    """_TABLE_SUFFIX is legal in a filter but never appears in introspection."""
    ok = M.SemanticModel(
        version=model.version,
        dialect=model.dialect,
        entities=(model.entity("ga_sessions"),),
        measures=(M.Measure(name="sessions", base_entity="ga_sessions",
                            formula="COUNT(*)",
                            filters=("_TABLE_SUFFIX BETWEEN '20160801' AND '20160831'",)),),
        dimensions=(),
        joins=(),
    )
    assert M.validate(ok, schema) == []


# ---- glossary (consumed by the Layer 3 MCP Resource)

def test_glossary_is_serialisable_and_complete(model):
    g = model.glossary()
    assert json.dumps(g)
    assert g["version"] == model.version
    assert len(g["measures"]) == len(model.measures)
    assert len(g["dimensions"]) == len(model.dimensions)
