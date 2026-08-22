"""Query cost ceiling.

OWNER: Ajinkya. Do not let the assistant fill this in.

--------------------------------------------------------------------------
WHAT THIS MODULE MUST DO
--------------------------------------------------------------------------
Before a query executes, ask BigQuery what it would cost, and refuse if
that exceeds the ceiling. The mechanism already exists:

    from src.bq_client import dry_run     # returns bytes, executes nothing
    from src.config import get_settings   # .max_bytes_billed (default 1 GB)

Verified working at 2:00 -- a dry run on a thelook category count returned
361,201 bytes without running the query.

Entry point:

    def check_cost(sql: str) -> CostVerdict

Return a verdict; do not raise on "too expensive." graph.py needs to
decide whether a retry (e.g. asking the model to add a filter) is worth
attempting. DO raise on a BigQuery error -- a dry run that fails means the
SQL is invalid, which is different from expensive, and collapsing those
two into one outcome will confuse the retry loop.

--------------------------------------------------------------------------
DECISIONS THAT ARE YOURS
--------------------------------------------------------------------------
1. WHERE THE CEILING SITS.
   1 GB is the current default and it is a guess. Real anchors from our
   corpus, measured:
     - thelook category count      361 KB
     - thelook is 7 tables, largest is events at 2.4M rows
     - ga_sessions_* is 366 shards; a query without a date filter scans
       ALL of them
   A `SELECT * FROM ga_sessions_*` is the query this guard exists to
   stop. Consider measuring that number and setting the ceiling with it
   in view, rather than picking a round number.

2. WHAT HAPPENS ON A DRY-RUN FAILURE.
   Invalid SQL fails the dry run. Is that this module's problem or
   guardrails.py's? Both will catch it. Decide which owns the message,
   or you will get two different errors for one cause.

3. WHETHER THE CEILING IS FIXED OR PER-QUESTION.
   A fixed ceiling is simple and defensible. A ceiling that scales with
   the question ("this is an exploratory count, allow less") is more
   clever and much harder to justify. Simple is probably right; say why.

4. WHETHER A BLOCKED QUERY IS RETRYABLE.
   If a query is too expensive, telling the model "add a date filter and
   try again" often works on sharded tables. That turns the cost guard
   from a wall into a negotiation -- which is better UX and more retry
   budget spent. Your call, and it interacts with the 2-retry cap.

--------------------------------------------------------------------------
SHAPE TO RETURN
--------------------------------------------------------------------------
Feed GuardrailReport.estimated_bytes_scanned in src/models.py, which is
already surfaced by the API. Include the estimate on the ALLOW path too,
not just on rejection -- "this query was checked and cost 361 KB" is the
line that makes the demo land.

--------------------------------------------------------------------------
NOTE ON DEFENCE IN DEPTH
--------------------------------------------------------------------------
bq_client.execute() also sets maximum_bytes_billed on the job itself, so
a query that somehow bypasses this module still cannot run away. That is
deliberate belt-and-braces, not redundancy to remove -- this module is the
policy, the job setting is the backstop.
"""

from __future__ import annotations


def check_cost(sql: str):
    """See module docstring. OWNER: Ajinkya."""
    raise NotImplementedError("src/cost_guard.py is written by Ajinkya, not the assistant")
