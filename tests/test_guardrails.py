"""Tests for src/guardrails.py (owner: Ajinkya).

These are written against the entry point in the stub spec:

    check(sql: str, schema_context: SchemaContext) -> GuardrailReport

They fail with NotImplementedError until the module is written. That is
intentional -- they are the target, not a regression suite.

Every SQL string below uses REAL column and table names from the snapshot,
so a passing test means the check works on the actual corpus.
"""

import pytest

from src.guardrails import check

TL = "`bigquery-public-data.thelook_ecommerce"
GA = "`bigquery-public-data.google_analytics_sample.ga_sessions_*`"


def _report(sql, schema):
    return check(sql, schema)


# ---------------------------------------------------------------- allowed

def test_plain_select_on_allowed_table_passes(schema):
    sql = f"SELECT category, COUNT(*) AS n FROM {TL}.products` GROUP BY category LIMIT 10"
    r = _report(sql, schema)
    assert r.passed, r.violations


def test_join_across_allowed_tables_passes(schema):
    sql = (
        f"SELECT u.country, SUM(oi.sale_price) AS revenue "
        f"FROM {TL}.order_items` oi "
        f"JOIN {TL}.users` u ON u.id = oi.user_id "
        f"GROUP BY u.country LIMIT 50"
    )
    r = _report(sql, schema)
    assert r.passed, r.violations


def test_ga_nested_path_passes(schema):
    """322 of GA's 338 columns are dotted paths. A column rule written for
    flat tables will reject these."""
    sql = f"SELECT totals.pageviews, trafficSource.source FROM {GA} LIMIT 10"
    r = _report(sql, schema)
    assert r.passed, r.violations


def test_deeply_nested_ga_path_passes(schema):
    """Four levels deep. Real column, verified in the snapshot."""
    sql = (
        f"SELECT trafficSource.adwordsClickInfo.targetingCriteria.boomUserlistId "
        f"FROM {GA} LIMIT 10"
    )
    r = _report(sql, schema)
    assert r.passed, r.violations


# ---------------------------------------------------------------- blocked

def test_stacked_statement_rejected(schema):
    sql = f"SELECT 1; DROP TABLE {TL}.users`"
    r = _report(sql, schema)
    assert not r.passed
    assert any("select" in v.lower() or "statement" in v.lower() for v in r.violations)


def test_dml_buried_in_cte_rejected(schema):
    """Root looks like a SELECT. Walking the tree is the only way to catch this."""
    sql = (
        f"WITH x AS (DELETE FROM {TL}.orders` WHERE order_id = 1 RETURNING order_id) "
        f"SELECT * FROM x"
    )
    r = _report(sql, schema)
    assert not r.passed


def test_table_outside_allowlist_rejected(schema):
    sql = "SELECT name FROM `bigquery-public-data.usa_names.usa_1910_current` LIMIT 5"
    r = _report(sql, schema)
    assert not r.passed
    assert any("allow" in v.lower() for v in r.violations)


def test_hallucinated_column_rejected(schema):
    """profit_margin does not exist anywhere in the corpus."""
    sql = f"SELECT profit_margin FROM {TL}.products` LIMIT 5"
    r = _report(sql, schema)
    assert not r.passed


def test_unparseable_sql_rejected(schema):
    r = _report("SELECT FROM WHERE ((", schema)
    assert not r.passed


def test_information_schema_probe_rejected(schema):
    """Schema introspection through a generated query. Decide deliberately."""
    sql = f"SELECT table_name FROM {TL}.INFORMATION_SCHEMA.TABLES`"
    r = _report(sql, schema)
    assert not r.passed


# ---------------------------------------------------------------- policy

def test_missing_limit_handled(schema):
    """Reject or rewrite -- both defensible. This asserts only that the
    guardrail NOTICES. Tighten to match whichever policy you choose."""
    sql = f"SELECT id FROM {TL}.products`"
    r = _report(sql, schema)
    assert "row_limit" in r.checks_run or not r.passed


def test_report_records_checks_run_even_on_failure(schema):
    """The audit trail is the product. A rejection with an empty
    checks_run tells you nothing."""
    sql = "SELECT name FROM `bigquery-public-data.usa_names.usa_1910_current`"
    r = _report(sql, schema)
    assert r.checks_run
