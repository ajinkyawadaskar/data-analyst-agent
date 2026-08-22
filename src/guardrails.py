"""SQL guardrail layer.

OWNER: Ajinkya. Do not let the assistant fill this in.

This is the module the project exists for. Everything else is plumbing
around it. An interviewer will spend more time here than anywhere else in
the repo, so the reasoning behind each check needs to be yours.

--------------------------------------------------------------------------
WHAT THIS MODULE MUST DO
--------------------------------------------------------------------------
Given a candidate SQL string produced by the LLM, decide whether it is
safe to execute, and return a structured verdict explaining why. It never
executes anything and never talks to BigQuery -- cost checks live in
src/cost_guard.py, execution lives in src/bq_client.py.

Entry point:

    def check(sql: str, schema_context: SchemaContext) -> GuardrailVerdict

Return a verdict, do not raise, on rejection. The caller (src/graph.py)
needs the violation list to decide whether a bounded retry is worthwhile.
Raise only on a malformed call (e.g. sql is None).

--------------------------------------------------------------------------
THE FOUR CHECKS
--------------------------------------------------------------------------
1. SINGLE STATEMENT, SELECT ONLY
   Parse with sqlglot (installed, v30). Reject if:
     - the string parses to more than one statement (stacked-query attack:
       `SELECT 1; DROP TABLE x`)
     - the root expression is not a SELECT (no INSERT/UPDATE/DELETE/MERGE/
       CREATE/DROP/ALTER/TRUNCATE/GRANT/CALL/EXPORT)
     - any DML/DDL node appears anywhere in the tree, including inside a
       CTE or subquery -- walk the AST, do not just inspect the root
     - sqlglot fails to parse it at all. Unparseable means unrunnable.

   Do this on the AST, not with regex. That choice is one of the four
   decisions you have to defend: a prompt instruction lowers the rate of
   bad SQL, a regex catches the spellings you thought of, an AST check
   bounds the whole class.

2. TABLE ALLOWLIST
   Extract every table reference from the AST (sqlglot exp.Table). Resolve
   each to a fully-qualified `project.dataset.table`. Reject any reference
   whose `project.dataset` is not in settings.allowed_datasets.

   Watch for: unqualified names, backtick-quoted BigQuery identifiers
   (`project.dataset.table` as a single token), wildcard tables
   (`ga_sessions_*`), table-valued functions, and INFORMATION_SCHEMA
   probes -- decide deliberately whether schema introspection through a
   generated query is permitted. It probably should not be.

3. COLUMN VALIDATION
   Every column referenced must exist in the schema context handed to the
   model. A column the model invented is a hallucination, and executing it
   just converts a silent error into a BigQuery error message.

   The hard part is unresolved references: `SELECT a FROM x JOIN y` where
   `a` is unqualified and could belong to either table. Decide your policy
   -- strict (reject anything you cannot resolve) or lenient (allow if the
   name exists in any in-scope table) -- and write down why. Note that GA
   nested fields arrive as dotted paths (`totals.pageviews`,
   `hits.product.productSKU`); an ambiguity rule written for flat tables
   will misfire on them.

4. ROW LIMIT
   Enforce settings.max_rows. Decide: reject a query with no LIMIT, or
   rewrite it to add one? Rewriting is friendlier and changes semantics
   for aggregates; rejecting is honest and costs a retry. Either is
   defensible -- an unstated choice is not.

--------------------------------------------------------------------------
SHAPE TO RETURN
--------------------------------------------------------------------------
src/models.py already defines GuardrailReport, which is what the API
surfaces:

    passed: bool
    checks_run: list[str]      # names of checks that actually executed
    violations: list[str]      # human-readable, one per failure
    estimated_bytes_scanned: int | None   # filled by cost_guard, not here

Return that, or a richer internal type that converts to it. Populate
checks_run even on failure -- "which checks ran before the first failure"
is the difference between a demo and an audit trail. Consider whether a
failure should short-circuit or whether all four checks should run so the
retry gets the complete violation list at once.

--------------------------------------------------------------------------
TESTS TO WRITE ALONGSIDE
--------------------------------------------------------------------------
    - plain SELECT on an allowed table                  -> pass
    - `SELECT 1; DROP TABLE users`                      -> reject (stacked)
    - DELETE hidden inside a CTE                        -> reject
    - SELECT against a dataset outside the allowlist    -> reject
    - hallucinated column name                          -> reject
    - GA nested path `totals.pageviews`                 -> pass
    - unparseable garbage                               -> reject
    - missing LIMIT                                     -> your chosen policy

--------------------------------------------------------------------------
AVAILABLE TO YOU
--------------------------------------------------------------------------
    from src.config import get_settings        # allowed_datasets, max_rows
    from src.models import GuardrailReport
    import sqlglot                             # sqlglot.parse, exp.Table
"""

from __future__ import annotations

from src.models import GuardrailReport


def check(sql: str, schema_context: object) -> GuardrailReport:
    """See module docstring. OWNER: Ajinkya."""
    raise NotImplementedError("src/guardrails.py is written by Ajinkya, not the assistant")
