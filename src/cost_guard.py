"""
src/cost_guard.py

Query cost ceiling: ask BigQuery what a candidate query would cost via a
dry run, and refuse if that exceeds the configured ceiling. Never executes
the query -- src/bq_client.dry_run() guarantees that. Table/column safety
is guardrails.py's job; this module only ever looks at bytes.

Entry point: check_cost(sql, client=None) -> CostVerdict

--------------------------------------------------------------------------
DECISIONS MADE (the four the docstring asks to be explicit about)
--------------------------------------------------------------------------
1. WHERE THE CEILING SITS: settings.max_bytes_billed, fixed at 1 GB for
   now, but that number should not stay a guess. The anchors we have:
     - thelook category count:            361,201 bytes  (measured 2:00)
     - thelook largest table (events):    2.4M rows
     - ga_sessions_* wildcard, no date filter: ALL 366 shards scanned --
       this is exactly the query this guard exists to stop, and it has
       not yet been measured. Before shipping the real ceiling number,
       run `SELECT COUNT(*) FROM ga_sessions_*` through dry_run() and log
       the byte count to NOTES.md; 1 GB should be justified against that
       number (comfortably above a legitimate single-month GA query,
       comfortably below an accidental full-corpus scan) rather than left
       as a round default. Until that measurement exists, 1 GB is a
       placeholder, not a decision.

2. DRY-RUN FAILURE OWNERSHIP: this module RAISES on a BigQuery dry-run
   error (per the docstring's explicit instruction) rather than
   translating it into a CostVerdict. Reasoning: guardrails.py already
   rejects structurally invalid SQL (unparseable, wrong statement type,
   disallowed tables/columns) before a query ever reaches here. If a
   dry run still fails after guardrails.py passed it, that means BigQuery
   itself considers the SQL invalid for a reason static analysis can't
   see -- e.g. a type mismatch, an ambiguous alias sqlglot didn't catch,
   or a schema drift guardrails.py's schema_context doesn't know about
   yet. That is a different failure mode from "too expensive," and
   collapsing the two into one CostVerdict would hide from graph.py
   which kind of retry is worth attempting (rewrite the SQL vs. add a
   filter). graph.py is expected to catch the exception and treat it as
   an execution-error retry, the same bucket as any other BigQuery
   error -- NOT a cost rejection.

3. FIXED CEILING, NOT PER-QUESTION: settings.max_bytes_billed is one
   number for every question. A ceiling that flexes per-question
   ("this is just an exploratory count, allow less") requires classifying
   question intent first, which is itself an unreliable, unauditable
   judgment call -- exactly the kind of thing this project's whole thesis
   argues against (a guardrail you can't explain isn't a guardrail).
   A fixed ceiling is one line in config.py, and defending it in an
   interview is one sentence: "every query gets the same budget, and here
   is the query we measured that budget against."

4. BLOCKED QUERIES ARE RETRYABLE: CostVerdict.retryable is True whenever
   the rejection reason is the byte ceiling (as opposed to a raised
   exception, which is a different failure class entirely -- see #2).
   The verdict also carries a canned retry_hint suggesting a narrowing
   filter, e.g. adding a `_TABLE_SUFFIX` bound on wildcard tables. This
   turns the cost guard into a negotiation rather than a wall: on a
   sharded table, "no date filter" is very often mechanically fixable,
   and spending one of the 2 retries on that fix is worth it. graph.py
   still owns the actual retry-count bookkeeping and the 2-retry cap;
   this module only flags that the rejection is the retryable kind.

--------------------------------------------------------------------------
SHAPE RETURNED
--------------------------------------------------------------------------
CostVerdict, defined below, carries estimated_bytes_scanned on BOTH the
allow and reject paths -- "this query was checked and cost 361 KB" is the
line that makes the demo land, per the docstring. graph.py (or api.py)
is expected to copy verdict.estimated_bytes_scanned into
GuardrailReport.estimated_bytes_scanned; see merge_into_report() below for
a small helper that does exactly that without cost_guard.py needing to
import GuardrailReport's construction logic.

--------------------------------------------------------------------------
NOTE ON DEFENCE IN DEPTH
--------------------------------------------------------------------------
bq_client.execute() also sets maximum_bytes_billed on the job itself, so a
query that somehow bypasses this module (a direct call to execute(),
a future code path that forgets to call check_cost) still cannot run
away. That is deliberate belt-and-braces, not redundancy to remove.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.bq_client import dry_run
from src.config import get_settings


@dataclass
class CostVerdict:
    """What the cost guard decided, and the number the decision was based
    on. `passed=False` never means "invalid SQL" -- that case raises
    instead of returning a verdict; see decision #2 above."""

    passed: bool
    estimated_bytes_scanned: int
    ceiling_bytes: int
    retryable: bool = False
    retry_hint: str | None = None
    violations: list[str] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=lambda: ["cost_ceiling"])


_RETRY_HINT = (
    "Query would scan more than the allowed byte ceiling. If this targets "
    "a date-sharded table (e.g. ga_sessions_*), add a _TABLE_SUFFIX (or "
    "equivalent date) filter to narrow the scan and try again."
)


def check_cost(sql: str, client: object | None = None) -> CostVerdict:
    """See module docstring. OWNER: Ajinkya.

    Raises whatever src.bq_client.dry_run raises on invalid SQL -- this
    function does not catch or translate BigQuery errors. Callers (i.e.
    graph.py) are expected to catch that separately from a returned
    CostVerdict with passed=False.
    """
    if sql is None:
        raise TypeError("check_cost() requires a sql string, got None")

    settings = get_settings()
    ceiling = settings.max_bytes_billed

    # Deliberately not try/except: a BigQuery error here means invalid
    # SQL, not "too expensive," and must propagate to the caller as an
    # exception rather than becoming a CostVerdict. See decision #2.
    estimated_bytes = dry_run(sql, client)

    if estimated_bytes > ceiling:
        return CostVerdict(
            passed=False,
            estimated_bytes_scanned=estimated_bytes,
            ceiling_bytes=ceiling,
            retryable=True,
            retry_hint=_RETRY_HINT,
            violations=[
                f"Query would scan {estimated_bytes:,} bytes, exceeding the "
                f"{ceiling:,}-byte ceiling."
            ],
        )

    return CostVerdict(
        passed=True,
        estimated_bytes_scanned=estimated_bytes,
        ceiling_bytes=ceiling,
        retryable=False,
        retry_hint=None,
        violations=[],
    )


def merge_into_report(report: "GuardrailReport", verdict: CostVerdict) -> "GuardrailReport":  # noqa: F821
    """Convenience helper: fold a CostVerdict into an existing
    GuardrailReport (the shape src/models.py already defines and api.py
    already surfaces), so callers don't have to hand-assemble the merge
    at every call site.

    Does not mutate `report` -- GuardrailReport instances are expected to
    be treated as values, consistent with how guardrails.check() builds
    them.
    """
    from dataclasses import replace

    return replace(
        report,
        passed=report.passed and verdict.passed,
        checks_run=[*report.checks_run, *verdict.checks_run],
        violations=[*report.violations, *verdict.violations],
        estimated_bytes_scanned=verdict.estimated_bytes_scanned,
    )