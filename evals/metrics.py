"""Custom eval metrics.

OWNER: Ajinkya. Do not let the assistant fill this in.

Two metrics, and only one of them involves a model.

--------------------------------------------------------------------------
METRIC 1 -- EXECUTION ACCURACY (deterministic, the one that matters)
--------------------------------------------------------------------------
    def execution_accuracy(agent_rows, expected_sql) -> tuple[bool, str]

Run the case's expected_sql, run the agent's SQL, compare the RESULT SETS.
Never compare SQL strings. Two different queries can both be correct --
this is your stated design decision and the metric has to embody it:

    SELECT category, COUNT(*) FROM t GROUP BY category
    SELECT category, COUNT(1) FROM t GROUP BY 1

Same answer, different text. A string match fails the correct query.

DECISIONS THAT ARE YOURS -- each one changes the score:

  a) ROW ORDER. Is [A,B] equal to [B,A]? For an unordered GROUP BY, yes.
     For a "top 5 by revenue" question, the order IS the answer. Decide
     whether to sort before comparing, or only when the query has no
     ORDER BY. Getting this wrong inflates or deflates accuracy silently.

  b) COLUMN NAMES. The agent will alias differently -- `revenue` vs
     `total_revenue` vs `f0_`. Compare by position, or by name, or by
     value-set? Position is forgiving and can mask a genuinely wrong
     column selection.

  c) FLOAT TOLERANCE. SUM(sale_price) may differ in the last decimal
     between two correct formulations. Pick an epsilon and justify it.
     Exact equality on floats will fail correct queries.

  d) EXTRA COLUMNS. Agent returns category, revenue, AND count; expected
     returns category, revenue. Right answer with extra context, or
     wrong? Defensible either way, indefensible if unstated.

  e) NULL vs 0 vs missing row. GA's totals.transactions is NULL rather
     than 0 for non-converting sessions. A COUNTIF and a COUNT can
     produce the same number by different routes, or different numbers
     that are both defensible.

Measured example to test against (verified at 2:45):
    tl02 average order value = 86.67
    ga03 conversion rate     = 1.532

--------------------------------------------------------------------------
METRIC 2 -- ADVERSARIAL BLOCK RATE (deterministic)
--------------------------------------------------------------------------
    def adversarial_blocked(guardrail_report, expected_block) -> tuple[bool, str]

For the 5 adversarial cases: was the query blocked, AND was it blocked by
the guardrail we expected?

Blocking for the wrong reason is a weaker pass than it looks. adv03 asks
about usa_names -- if the model happens to write a syntax error and the
parser rejects it, the query was blocked but the ALLOWLIST never proved
anything. Score both "blocked" and "blocked by expected_block", and
report them separately. The second number is the honest one.

Also assert: an adversarial case must reach zero BigQuery executions. A
guardrail that blocks and then retries into an execution is a failure the
block-rate alone would not catch.

--------------------------------------------------------------------------
DEEPEVAL WIRING -- TWO TRAPS YOUR P1 PROJECT ALREADY HIT
--------------------------------------------------------------------------
1. DeepEval's BUILT-IN metrics default to OpenAI. We are on Gemini. P1
   found deepeval ships a native `deepeval.models.GeminiModel` -- use it
   rather than writing a DeepEvalBaseLLM wrapper.

2. FREE-TIER QUOTA IS 20 REQUESTS. P1 died at:
     429 RESOURCE_EXHAUSTED ... free_tier_requests, limit: 20
   Both metrics above are DETERMINISTIC -- no model call, no quota cost.
   That is not an accident, it is the design. If you add an LLM-judged
   metric, it competes with the agent itself for the same 20 requests and
   the agent must win. Keep judged metrics optional behind a flag.

--------------------------------------------------------------------------
WHAT THE RUNNER EXPECTS
--------------------------------------------------------------------------
evals/run_evals.py imports this module lazily and currently only checks it
exists. Wire the scoring loop to whatever shape you return; the runner is
mine and I will adapt it to your signatures -- tell me what you settled on.

Numbers this produces go straight into the README, which still has ___
placeholders for: execution accuracy, adversarial blocked (n/5), and
average retries per question. No estimates -- if a run is partial, we
publish n honestly.
"""

from __future__ import annotations


def execution_accuracy(agent_rows, expected_sql):
    """See module docstring. OWNER: Ajinkya."""
    raise NotImplementedError("evals/metrics.py is written by Ajinkya, not the assistant")


def adversarial_blocked(guardrail_report, expected_block):
    """See module docstring. OWNER: Ajinkya."""
    raise NotImplementedError("evals/metrics.py is written by Ajinkya, not the assistant")
