"""
src/guardrails.py

Static safety check for LLM-generated SQL before it ever reaches
src/bq_client.py. Never executes anything, never talks to BigQuery.
Cost estimation is cost_guard.py's job -- estimated_bytes_scanned is left
None here and filled in downstream.

Entry point: check(sql, schema_context) -> GuardrailReport

--------------------------------------------------------------------------
ASSUMED COLLABORATOR SHAPES (src/config.py, schema_context)
--------------------------------------------------------------------------
This file only has the docstring-level contract for its neighbors, not
their source, so the following duck-typed shapes are assumed. If the real
objects differ, only the small accessor helpers below (_settings_*,
_schema_*) need to change.

    settings.allowed_datasets  -> iterable[str] of "project.dataset"
    settings.max_rows          -> int

    schema_context exposes the in-scope tables and their columns. Any of
    the following are accepted (first one found wins), tried in order:
      - schema_context.tables: dict[str, Iterable[str]]
            keyed by "project.dataset.table" (preferred) or bare table
            name, value = column names for that table. GA-style nested
            fields are expected to already appear as dotted strings in
            this iterable, e.g. "totals.pageviews", matching the note in
            the module docstring that nested paths arrive pre-dotted.
      - schema_context.get_columns(table: str) -> Iterable[str]
      - schema_context.columns: same shape as .tables

--------------------------------------------------------------------------
DECISIONS MADE (the four the docstring asks to be explicit about)
--------------------------------------------------------------------------
1. AST over regex, as instructed: sqlglot.parse() for statement count,
   then a full tree walk (not just root-level inspection) for DML/DDL
   anywhere, including inside CTEs and subqueries.

2. INFORMATION_SCHEMA probes are treated as a table-allowlist violation.
   A generated query has no legitimate reason to introspect schema
   metadata at runtime -- the model was already given schema_context up
   front. Any table reference whose db/name contains "INFORMATION_SCHEMA"
   is rejected regardless of whether the parent project.dataset is
   otherwise allowed.

3. Column resolution policy: STRICT. An unqualified column must match
   exactly one in-scope table's column set to pass; zero matches is a
   hallucination, more than one is an unresolved ambiguity, and both are
   rejected. Lenient ("allow if it exists anywhere in scope") would let a
   join-order-dependent query through that silently reads the wrong
   column if the schema ever changes. GA nested paths (`totals.pageviews`,
   `hits.product.productSKU`) are matched as a single dotted name against
   the schema's column list, not decomposed into a struct/field pair --
   this matches how the docstring says they arrive in schema_context.

4. Row limit policy: REJECT queries with no LIMIT rather than rewriting
   one in. Rewriting is friendlier but silently changes the semantics of
   an aggregate query (SELECT COUNT(*) with an injected LIMIT 10000 is a
   different, wrong answer, not a safely truncated one). Rejecting costs
   the caller a retry but never changes what the query means.

All four checks always run, even after an earlier one fails, so a single
bounded retry gets the complete violation list instead of playing
whack-a-mole one violation per round trip. checks_run is populated in
every branch, including the ones the caller never reaches because sql
was already unparseable.
"""

from __future__ import annotations

from typing import Iterable

import sqlglot
from sqlglot import exp

from src.config import get_settings
from src.models import GuardrailReport

# Root-level DML/DDL types allowed to reject the whole query outright, and
# also the set walked for anywhere-in-tree (CTE / subquery) detection.
_DML_DDL_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Grant,
    exp.Command,  # catches CALL and other dialect-specific statements
    exp.Export,
)

_CHECK_NAMES = [
    "single_statement_select_only",
    "table_allowlist",
    "column_validation",
    "row_limit",
]


def check(sql: str, schema_context: object) -> GuardrailReport:
    """See module docstring. OWNER: Ajinkya."""
    if sql is None:
        raise TypeError("check() requires a sql string, got None")

    settings = get_settings()
    violations: list[str] = []
    checks_run: list[str] = []

    # ---------------------------------------------------------------
    # Check 1: single statement, SELECT only, no DML/DDL anywhere.
    # ---------------------------------------------------------------
    checks_run.append(_CHECK_NAMES[0])
    tree = _parse_single_select(sql, violations)

    if tree is None:
        # Unparseable, multi-statement, or non-SELECT root/subtree: none
        # of the later checks can run meaningfully against no tree, but
        # they're still recorded as attempted so the report shows the
        # full pipeline was invoked, per "populate checks_run even on
        # failure."
        checks_run.extend(_CHECK_NAMES[1:])
        return GuardrailReport(
            passed=False,
            checks_run=checks_run,
            violations=violations,
            estimated_bytes_scanned=None,
        )

    # ---------------------------------------------------------------
    # Check 2: table allowlist.
    # ---------------------------------------------------------------
    checks_run.append(_CHECK_NAMES[1])
    cte_names = {c.alias.lower() for c in tree.find_all(exp.CTE) if c.alias}
    tables = _resolve_tables(tree, cte_names)
    _check_table_allowlist(tables, settings, violations)

    # ---------------------------------------------------------------
    # Check 3: column validation.
    # ---------------------------------------------------------------
    checks_run.append(_CHECK_NAMES[2])
    _check_columns(tree, tables, schema_context, violations)

    # ---------------------------------------------------------------
    # Check 4: row limit.
    # ---------------------------------------------------------------
    checks_run.append(_CHECK_NAMES[3])
    _check_row_limit(tree, settings, violations)

    return GuardrailReport(
        passed=not violations,
        checks_run=checks_run,
        violations=violations,
        estimated_bytes_scanned=None,
    )


# ==========================================================================
# Check 1
# ==========================================================================
def _parse_single_select(sql: str, violations: list[str]) -> exp.Expression | None:
    try:
        statements = sqlglot.parse(sql, dialect="bigquery")
    except Exception as exc:  # sqlglot raises its own ParseError subclasses
        violations.append(f"SQL did not parse: {exc}")
        return None

    statements = [s for s in statements if s is not None]

    if len(statements) == 0:
        violations.append("SQL did not parse: no statement found")
        return None

    if len(statements) > 1:
        violations.append(
            f"Multiple statements in a single request are not allowed "
            f"({len(statements)} statements found; stacked queries are rejected)."
        )
        return None

    tree = statements[0]

    if not isinstance(tree, exp.Select):
        violations.append(
            f"Only SELECT statements are allowed; got {type(tree).__name__}."
        )
        return None

    # Walk the *whole* tree -- CTEs and subqueries included -- not just
    # the root node, per the docstring's explicit warning.
    offenders = list(tree.find_all(*_DML_DDL_TYPES))
    if offenders:
        kinds = sorted({type(n).__name__ for n in offenders})
        violations.append(
            f"DML/DDL found inside query (including CTEs/subqueries): {', '.join(kinds)}."
        )
        return None

    return tree


# ==========================================================================
# Check 2: table allowlist
# ==========================================================================
def _resolve_tables(tree: exp.Expression, cte_names: set[str]) -> list[exp.Table]:
    """All real table references, excluding names that resolve to a
    locally-defined CTE rather than an actual BigQuery table."""
    tables = []
    for t in tree.find_all(exp.Table):
        # A bare, unqualified name matching a CTE alias is a reference to
        # the CTE, not a table -- it never touches the allowlist.
        if not t.db and not t.catalog and t.name.lower() in cte_names:
            continue
        tables.append(t)
    return tables


def _table_fqn(t: exp.Table) -> str:
    parts = [p for p in (t.catalog, t.db, t.name) if p]
    return ".".join(parts)


def _check_table_allowlist(
    tables: list[exp.Table], settings: object, violations: list[str]
) -> None:
    allowed = {d.lower() for d in getattr(settings, "allowed_datasets", [])}

    for t in tables:
        fqn = _table_fqn(t)

        if not t.catalog or not t.db:
            violations.append(
                f"Table reference '{fqn or t.name}' is not fully qualified "
                f"(need project.dataset.table); unqualified references are rejected."
            )
            continue

        # INFORMATION_SCHEMA probes: rejected outright (decision #2 above),
        # regardless of whether the parent dataset is allowed.
        if "information_schema" in t.db.lower() or "information_schema" in t.name.lower():
            violations.append(
                f"Schema-introspection query against '{fqn}' is not permitted."
            )
            continue

        dataset_key = f"{t.catalog}.{t.db}".lower()
        if dataset_key not in allowed:
            violations.append(
                f"Table '{fqn}' is outside the allowed dataset list ({dataset_key})."
            )


# ==========================================================================
# Check 3: column validation
# ==========================================================================
def _schema_columns_for(schema_context: object, table_key: str) -> set[str] | None:
    """Best-effort lookup of a table's known column names from
    schema_context, trying the shapes documented at the top of this file.
    Returns None if the table isn't known to schema_context at all
    (distinct from an empty-but-known column set)."""
    table_map: dict | None = None
    if hasattr(schema_context, "tables"):
        table_map = getattr(schema_context, "tables")
    elif hasattr(schema_context, "columns"):
        table_map = getattr(schema_context, "columns")

    if table_map is not None:
        if table_key in table_map:
            return {c.lower() for c in table_map[table_key]}
        # fall back to matching by bare table name (last path segment)
        bare = table_key.rsplit(".", 1)[-1]
        if bare in table_map:
            return {c.lower() for c in table_map[bare]}
        return None

    if hasattr(schema_context, "get_columns"):
        try:
            cols = schema_context.get_columns(table_key)
        except (KeyError, LookupError):
            return None
        if cols is None:
            return None
        return {c.lower() for c in cols}

    return None


def _column_parts(col: exp.Column) -> list[str]:
    return [p.name for p in col.parts]


def _check_columns(
    tree: exp.Expression,
    tables: list[exp.Table],
    schema_context: object,
    violations: list[str],
) -> None:
    # Build alias/name -> column-set map for every in-scope real table.
    scope: dict[str, set[str]] = {}
    for t in tables:
        fqn = _table_fqn(t)
        cols = _schema_columns_for(schema_context, fqn)
        if cols is None:
            # Table passed the allowlist but schema_context has no column
            # list for it -- nothing to validate columns against; skip
            # silently rather than blaming individual columns for a gap
            # in schema_context itself.
            continue
        for key in filter(None, (t.alias, t.name, fqn)):
            scope[key.lower()] = cols

    if not scope:
        # Either no tables resolved columns, or the query references no
        # tables at all (e.g. SELECT 1) -- nothing to check.
        return

    seen: set[str] = set()
    for col in tree.find_all(exp.Column):
        parts = _column_parts(col)
        if not parts:
            continue
        ref = ".".join(p.lower() for p in parts)
        if ref in seen:
            continue
        seen.add(ref)

        if len(parts) >= 2 and parts[0].lower() in scope:
            # Qualified reference: table_or_alias.rest.of.path
            table_cols = scope[parts[0].lower()]
            dotted = ".".join(p.lower() for p in parts[1:])
            if dotted not in table_cols and parts[1].lower() not in table_cols:
                violations.append(
                    f"Column '{'.'.join(parts)}' does not exist on "
                    f"'{parts[0]}' per the provided schema."
                )
            continue

        # Unqualified (or qualifier isn't a known table/alias): resolve
        # strictly against every in-scope table's columns.
        full_dotted = ".".join(p.lower() for p in parts)
        matches = [
            name
            for name, cols in scope.items()
            if full_dotted in cols or parts[0].lower() in cols
        ]
        # dedupe tables that share the same alias/name/fqn entry
        distinct_tables = {id(scope[m]) for m in matches}

        if len(distinct_tables) == 0:
            violations.append(
                f"Column '{'.'.join(parts)}' does not exist in any in-scope table."
            )
        elif len(distinct_tables) > 1:
            violations.append(
                f"Column '{'.'.join(parts)}' is ambiguous across joined tables "
                f"and cannot be resolved (strict policy)."
            )
        # exactly one distinct table matched -> passes


# ==========================================================================
# Check 4: row limit
# ==========================================================================
def _check_row_limit(tree: exp.Expression, settings: object, violations: list[str]) -> None:
    max_rows = getattr(settings, "max_rows", None)
    limit_node = tree.args.get("limit")

    if limit_node is None:
        violations.append(
            "Query has no LIMIT clause; queries without an explicit LIMIT are rejected "
            "rather than silently capped (rejecting doesn't change query semantics)."
        )
        return

    if max_rows is None:
        return

    limit_expr = limit_node.expression
    if isinstance(limit_expr, exp.Literal) and limit_expr.is_number:
        try:
            limit_value = int(limit_expr.this)
        except (TypeError, ValueError):
            return
        if limit_value > max_rows:
            violations.append(
                f"LIMIT {limit_value} exceeds the configured maximum of {max_rows} rows."
            )
    # A non-literal LIMIT (parameter/expression) can't be checked
    # statically; leave it to cost_guard's execution-time enforcement.