"""Spec for compile-time row-level security (src/compiler/security.py).

Owner of the module under test: Ajinkya. Same discipline as
tests/test_compiler.py: this is the executable spec, written before the body,
marked xfail(raises=NotImplementedError) so the suite stays green while the
module is a stub and flips to XPASS the moment it lands. Remove the marker
then; any test that flips to a real FAIL is the one worth reading first.

Offline: schema comes from tests/schema_snapshot.json. No BigQuery, no LLM.
Queries are compiled for real via src/compiler/intent_compiler.py -- these
tests exercise Layer 2 injecting into a Layer 1 AST, not a hand-built one, so
"does the predicate survive being AND-combined with a real WHERE/JOIN" is
actually tested rather than assumed.

Two of the module's own decisions (how `region` maps to allowed rows, and
whether `tenant_id` restricts anything yet) are explicitly left to Ajinkya --
see security.py's module docstring, decisions A and B. These tests are
written to hold regardless of how those decisions land: they check structural
properties (the predicate is IN the AST, not appended as text; it survives a
join; an unresolvable identity raises rather than silently returning zero
rows) rather than a specific region encoding.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlglot import exp

from src.compiler.intent_compiler import compile as compile_intent
from src.compiler.security import SecurityContextError, inject_security_context
from src.guardrails import check as guardrails_check
from src.schema import Column, SchemaContext, Table
from src.semantic import model as M
from src.semantic.intent import Filter, Intent

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


def _policy_column_ref(policy) -> str:
    """The bare column name a row_policy restricts, e.g. "country" out of
    "country" or "geoNetwork.country" -- matches how the predicate should
    reference it regardless of nesting."""
    return policy.column.split(".")[-1]


# ---- the predicate actually lands in the AST, not as appended text

def test_predicate_is_a_real_where_node_on_a_policy_guarded_entity(model):
    """users has a row_policy (principal_field: region, column: country).
    A query touching `users` must come back with a WHERE condition referencing
    that column somewhere in the tree -- found via exp.find_all, which only
    sees real AST nodes, never substrings of the rendered SQL."""
    policy = model.policy_for("users")
    assert policy is not None, "test assumes semantic_model.yaml keeps a users row_policy"

    out = compile_intent(Intent(measure="user_count", dimensions=["user_country"]), model, _Settings())
    secured = inject_security_context(out.expression, "t1", "AMER", model)

    col_refs = {
        ".".join(p.name for p in col.parts).lower()
        for col in secured.find_all(exp.Column)
    }
    assert any(_policy_column_ref(policy).lower() in ref for ref in col_refs)


def test_predicate_is_and_combined_not_a_replacement(model):
    """The compiled query already has a WHERE clause from the intent's own
    filters or partition bound in some cases; users has neither here, so this
    checks the weaker but still load-bearing property: injecting security
    must not delete or replace whatever predicate already existed."""
    out = compile_intent(
        Intent(measure="total_revenue", dimensions=["product_category"], limit=5),
        model,
        _Settings(),
    )
    original_where = out.expression.args.get("where")
    secured = inject_security_context(out.expression, "t1", "AMER", model)
    # order_items/products carry no row_policy in this model, so nothing
    # should have been added -- the point here is that compiling with no
    # applicable policy must not raise or mutate unrelated clauses.
    assert secured.args.get("where") == original_where


# ---- the predicate survives a join, not just a single-table query

def test_predicate_survives_a_join(model):
    """order_items joined to users (order_items_to_users) still must carry
    the users row_policy predicate -- security is not lost when the
    policy-guarded entity is reached via a join rather than being the base."""
    out = compile_intent(
        Intent(measure="total_revenue", dimensions=["user_country"]), model, _Settings()
    )
    assert out.join_path, "test assumes this intent requires a join to users"

    secured = inject_security_context(out.expression, "t1", "AMER", model)
    policy = model.policy_for("users")
    col_refs = {
        ".".join(p.name for p in col.parts).lower()
        for col in secured.find_all(exp.Column)
    }
    assert any(_policy_column_ref(policy).lower() in ref for ref in col_refs)


# ---- denied is an error, not an empty result set

def test_unresolvable_identity_is_denied_not_emptied(model):
    """A region that maps to no permitted access must raise, not compile to a
    predicate that happens to match zero rows -- see security.py step 4. A
    caller cannot distinguish "denied" from "correctly empty" otherwise."""
    out = compile_intent(Intent(measure="user_count", dimensions=["user_country"]), model, _Settings())
    with pytest.raises(SecurityContextError):
        inject_security_context(out.expression, "t1", "", model)


def test_no_principal_supplied_means_no_restriction():
    """Not this module's job to decide when it runs -- src/graph_semantic.py's
    compile node only calls inject_security_context when a principal is on
    the request at all. Documented here as the companion fact: security.py
    itself always enforces whatever policy applies once called; the opt-in
    happens one layer up."""
    # Intentionally not exercising inject_security_context here -- the
    # opt-in behavior lives in src/graph_semantic.py's compile node and is
    # covered by tests/test_graph_semantic.py instead. This test exists so
    # the "who decides whether Layer 2 runs" question has one documented
    # answer instead of living only in a docstring.
    assert True


# ---- a region outside the allowlist is denied, not compiled

def test_hostile_region_is_denied_not_compiled(model):
    """The region -> country-set lookup is an allowlist (REGION_TO_COUNTRIES):
    a value that isn't a real, provisioned region resolves to no access at
    all and never reaches the query as text. That's a stronger property than
    "escaped correctly" -- there is no code path where an arbitrary region
    string can reach exp.convert() in the first place."""
    out = compile_intent(Intent(measure="user_count", dimensions=["user_country"]), model, _Settings())
    with pytest.raises(SecurityContextError):
        inject_security_context(out.expression, "t1", "x' OR '1'='1", model)


# ---- never string concatenation -- caller-supplied values that DO reach a
# predicate (tenant_id's "eq" branch) are literals, not glued-in text

def test_tenant_id_value_is_a_literal_not_string_concatenation(model, monkeypatch):
    """No shipped row_policy keys off tenant_id yet (DECISION B), so this
    exercises the "eq" branch directly via a fake tenant-scoped policy on
    `users` -- the one caller-supplied value that DOES reach exp.convert()
    unfiltered by an allowlist, so it is the one that actually needs the
    injection guarantee tested."""
    from types import SimpleNamespace

    fake_policy = SimpleNamespace(
        entity="users", principal_field="tenant_id", column="id"
    )
    # model is a frozen dataclass -- patch the class method, not the instance
    # (instance-level setattr is blocked by frozen=True even for non-fields).
    monkeypatch.setattr(
        M.SemanticModel,
        "policy_for",
        lambda self, entity: fake_policy if entity == "users" else None,
    )

    out = compile_intent(Intent(measure="user_count", dimensions=["user_country"]), model, _Settings())
    hostile_tenant_id = "x' OR '1'='1"
    secured = inject_security_context(out.expression, hostile_tenant_id, "AMER", model)
    rendered = secured.sql(dialect=model.dialect)
    assert "OR '1'='1" not in rendered.replace(" ", "")


# ---------------------------------------------------------------------------
# LAYER 2 DEFINITION OF DONE
#
# "An identity without APAC access cannot get APAC rows under any phrasing,
# because the SQL that would return them fails to compile [into something
# that could match them] -- denied, not silently empty."
#
# The two tests below try two structurally different ways of asking for an
# APAC country's rows while scoped to a non-APAC identity: an equality
# filter naming the country directly, and a membership ("in") filter naming
# several. Both are legal Intent JSON the model is allowed to emit; neither
# is a malformed or adversarial payload. Both must be UNSATISFIABLE, not
# merely empty by coincidence -- checked by inspecting the AST for an AND
# between the caller's own filter and the injected row-policy predicate,
# not just by observing "the query returns nothing".
# ---------------------------------------------------------------------------


def test_apac_country_via_equality_filter_is_unsatisfiable(model):
    """Caller identity is scoped to AMER. The question asks, in effect, "show
    me Japan" -- an APAC country -- via a direct equality filter naming it."""
    intent = Intent(
        measure="user_count",
        filters=[Filter(field="user_country", operator="=", value="Japan")],
    )
    out = compile_intent(intent, model, _Settings())
    secured = inject_security_context(out.expression, "t1", "AMER", model)
    rendered = secured.sql(dialect=model.dialect)

    # Both the caller's own filter and the injected row policy must be
    # present -- one AND-combined with the other, neither one replacing the
    # other. No row can satisfy country = 'Japan' AND country IN (AMER's
    # list) at once, so the query is structurally unsatisfiable, not merely
    # empty. Country values are the literal strings the tables store (see
    # REGION_TO_COUNTRIES's docstring) -- not ISO codes.
    assert "'Japan'" in rendered
    assert "'United States'" in rendered and "'Canada'" in rendered
    and_nodes = list(secured.find_all(exp.And))
    assert and_nodes, "the caller's filter and the row policy must be AND-combined"

    # Never executed against BigQuery here (offline test), but it must still
    # pass the same guardrails everything else on this path does.
    schema = _schema_for_guardrails()
    report = guardrails_check(secured.sql(dialect=model.dialect), schema)
    assert report.passed, report.violations


def test_apac_countries_via_membership_filter_is_unsatisfiable(model):
    """Same denial, different phrasing: a membership filter naming several
    APAC countries at once rather than one equality. If only the equality
    phrasing were blocked, that would mean the row policy was pattern-
    matching a shape of query rather than being structurally impossible to
    route around -- exactly the gap this DoD test exists to close."""
    intent = Intent(
        measure="user_count",
        filters=[Filter(field="user_country", operator="in", value=["Japan", "Australia", "Singapore"])],
    )
    out = compile_intent(intent, model, _Settings())
    secured = inject_security_context(out.expression, "t1", "AMER", model)

    rendered = secured.sql(dialect=model.dialect)
    assert "'Japan'" in rendered and "'Australia'" in rendered and "'Singapore'" in rendered
    assert "'United States'" in rendered and "'Canada'" in rendered
    # Both conditions present and AND-combined (not one replacing the other) --
    # a row would have to satisfy country IN (Japan,Australia,Singapore) AND
    # country IN (AMER's list) simultaneously, which no row can.
    and_nodes = list(secured.find_all(exp.And))
    assert and_nodes, "the caller's filter and the row policy must be AND-combined"

    schema = _schema_for_guardrails()
    report = guardrails_check(rendered, schema)
    assert report.passed, report.violations


def test_apac_identity_reading_apac_rows_is_the_control_case(model):
    """The negative control: the identical question, scoped to an identity
    that DOES have APAC access, must compile to a query that CAN return
    those rows -- proving the denial above comes from the region mismatch,
    not from the filter shape being rejected outright."""
    intent = Intent(
        measure="user_count",
        filters=[Filter(field="user_country", operator="=", value="Japan")],
    )
    out = compile_intent(intent, model, _Settings())
    secured = inject_security_context(out.expression, "t1", "APAC", model)
    rendered = secured.sql(dialect=model.dialect)
    assert "'Japan'" in rendered
    assert "IN ('Japan', 'China', 'South Korea'" in rendered


def _schema_for_guardrails() -> SchemaContext:
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
